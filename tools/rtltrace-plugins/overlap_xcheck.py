#!/usr/bin/env python3
"""Cross-check of overlap_metrics.py: recompute the per-CE compute / DMA overlap from the Perfetto JSON alone.

    python3 overlap_xcheck.py <report dir>/<stem> [--example CE1:10]

Reads <stem>.overlap.json (what the plugin reported), <stem>.perfetto.json (kernel view) and, when present,
<stem>.perfetto_full.json (redefine_report.py --perfetto-full). Nothing from rtltrace is imported. Two sources:

  tracks  the kernel-view tracks a developer looks at in ui.perfetto.dev: Kernel overview (window), CE state
          (CeState), CE code regions (PC_MARK) with the per-CE flops/cycle counter, CR-DMA requests
  raw     the full view's raw probe events: CeState, ce_marker (BEGIN / END, flops), dma_queued / dma_start / dma_done

The arithmetic differs from the plugin on purpose: cycle bitsets (one bit per cycle, Python ints) instead of interval
lists; raw requests paired with dma_done in FIFO order instead of by search; prefetch requests are the ones issued
after the previous compute region with no suspend before this compute region starts (the plugin: at most
prefetch_gap cycles before it). Both sources must agree with each other and with the plugin. Exit status 1 if not.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SPM_BASE, SPM_STRIDE = 0x30000000, 0x2000
CALL = {"LLC2SPM": "rt.DMA_LLC2SPM", "SPM2LLC": "rt.DMA_SPM2LLC", "LLC2LLC": "rt.DMA_LLC2LLC",
        "GRP_SCATTER": "rt.DMA_SCATTER", "GRP_GATHER": "rt.DMA_GATHER"}
COMPUTE_FPC = 0.1  # flops per Busy cycle that makes a region "compute" (as overlap_metrics.py)


# ---------------------------------------------------------------------------------------------- cycle bitsets
def bits(iv) -> int:
    """Bit c set for every cycle c in [floor(a), floor(b)) of each interval (trace times are cycle + 0.5)."""
    x = 0
    for a, b in iv:
        a, b = int(a), int(b)
        if b > a:
            x |= ((1 << (b - a)) - 1) << a
    return x


def runs(x: int) -> list[tuple[int, int]]:
    s = bin(x)[2:][::-1]
    return [(m.start(), m.end()) for m in re.finditer("1+", s)]


def n(x: int) -> int:
    return x.bit_count()


# ---------------------------------------------------------------------------------------------- reading
def load(path: Path):
    d = json.loads(path.read_text())
    pn, tn = {}, {}
    for e in d["traceEvents"]:
        if e.get("ph") == "M" and e["name"] == "process_name":
            pn[e["pid"]] = e["args"]["name"]
        elif e.get("ph") == "M" and e["name"] == "thread_name":
            tn[(e["pid"], e["tid"])] = e["args"]["name"]
    return [(pn.get(e.get("pid")), tn.get((e.get("pid"), e.get("tid"))), e)
            for e in d["traceEvents"] if e.get("ph") != "M"]


def _ce(name: str, args: dict | None = None) -> int:
    """CE index from the RTL path of a probe (…orch.ceIfc_N, …core[_N].…), else from a name such as "CE1 …"."""
    path = (args or {}).get("rtl_path") or ""
    m = re.search(r"\.orch\.ceIfc_(\d+)$", path) or re.search(r"\.core(?:_(\d+))?\.", path)
    if m:
        return int(m.group(1) or 0)
    mt = re.search(r"\bCE(\d+)\b", name)  # "CE1 …", or "CR_Fabric_1.CE1 …" on a multi-CR trace
    if mt is None:
        raise ValueError(f"no CE index in {name!r}")
    return int(mt.group(1))


def _addr(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"\[(0x[0-9a-fA-F]+)\]\s*$", v) or re.fullmatch(r"\s*(0x[0-9a-fA-F]+)\s*", v)
    return int(m.group(1), 16) if m else None


def _spm_ce(r) -> int | None:
    a = r["dst"] if r["type"] == "LLC2SPM" else r["src"] if r["type"] == "SPM2LLC" else None
    if a is None:
        return None
    off = a - SPM_BASE
    return off // SPM_STRIDE if 0 <= off < 64 * SPM_STRIDE else None


class Source:
    """What one source gives: windows, CE states, compute intervals, DMA-call ends, requests."""

    def __init__(self, name):
        self.name = name
        self.windows: list[tuple[str, float, float]] = []
        self.states: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
        self.compute: dict[int, int] = {}  # CE -> bitset of compute cycles (whole trace)
        self.compute_set: set[str] = set()
        self.region_fpc: dict[str, float] = {}
        self.calls: dict[tuple[int, str], list[float]] = defaultdict(list)  # (CE, type) -> call END times
        self.reqs: list[dict] = []
        self.notes: list[str] = []

    def busy(self, ce) -> int:
        return bits((a, b) for a, b, st in self.states[ce] if st == "Busy")

    def suspended(self, ce) -> int:
        return bits((a, b) for a, b, st in self.states[ce] if st == "Suspended")


def from_tracks(evs) -> Source:
    s = Source("tracks")
    regions, fpc, shown = defaultdict(list), defaultdict(list), {}
    for p, t, e in evs:
        ph, a = e.get("ph"), e.get("args") or {}
        if p == "Kernel overview" and t == "kernel window" and ph == "X":
            s.windows.append((a.get("kernel"), e["ts"], e["ts"] + e["dur"]))
        elif a.get("probe") == "CeState" and ph == "X":
            s.states[_ce(p, a)].append((e["ts"], e["ts"] + e["dur"], e["name"]))
        elif p == "CE code regions (PC_MARK)" and ph == "X":
            regions[_ce(t)].append((e["ts"], e["ts"] + e["dur"], a["region"], a["state"],
                                    tuple(a.get("nesting", "").split(" > "))))
            shown[a["region"]] = e["name"].removesuffix(" (suspended)")
        elif p == "CE code regions (PC_MARK)" and ph == "C" and e["name"].endswith(" flops/cycle"):
            fpc[_ce(e["name"])].append((e["ts"], a["value"]))
        elif p == "CR-DMA requests" and t == "requests" and ph == "X" and a.get("service") is not None:
            s.reqs.append({"type": a["type"], "start": e["ts"], "done": e["ts"] + e["dur"], "bytes": a["bytes"],
                           "src": _addr(a.get("src")), "dst": _addr(a.get("dst")), "queued": a.get("queued")})
    if not s.windows or not s.states:
        sys.exit("kernel view without 'Kernel overview' or CeState tracks: generate it with redefine_report.py "
                 "(redefine_metrics.py and redefine_view.py loaded)")
    # compute regions: flops (integral of the flops/cycle counter) per Busy cycle, over all CEs, inside the window
    w0, w1 = s.windows[0][1], s.windows[-1][2]
    fl, bz = defaultdict(float), defaultdict(float)
    for ce, segs in regions.items():
        pts = sorted(fpc.get(ce, []))  # value v_i holds from ts_i to ts_i+1
        ts = [x for x, _ in pts]
        cum = [0.0]
        for (ta, va), (tb, _) in zip(pts, pts[1:]):
            cum.append(cum[-1] + va * (tb - ta))

        def F(t, ts=ts, pts=pts, cum=cum):  # flops retired from the first sample to t
            i = bisect.bisect_right(ts, t) - 1
            return 0.0 if i < 0 else cum[i] + pts[i][1] * (t - ts[i])

        for a, b, lab, st, _ in segs:
            a, b = max(a, w0), min(b, w1)
            if st == "Busy" and b > a:
                fl[lab] += F(b) - F(a)
                bz[lab] += b - a
    s.region_fpc = {lab: fl[lab] / bz[lab] for lab in bz if bz[lab]}
    s.compute_set = {lab for lab, v in s.region_fpc.items() if v >= COMPUTE_FPC}
    for ce, segs in regions.items():
        s.compute[ce] = bits((a, b) for a, b, lab, st, _ in segs if lab in s.compute_set and st == "Busy")
        # a DMA call = contiguous slices with the runtime region open (it nests frame alloc / bind); its END is the
        # issue time
        for typ, reg in CALL.items():
            end = None
            for a, b, lab, st, path in sorted(segs):
                if shown.get(reg) not in path:
                    continue
                if end is not None and a == end:
                    s.calls[(ce, typ)][-1] = b
                else:
                    s.calls[(ce, typ)].append(b)
                end = b
    for r in s.reqs:
        r["ce"] = _spm_ce(r)
    return s


def from_raw(evs, windows) -> Source:
    s = Source("raw")
    s.windows = windows
    marks = defaultdict(list)
    q, st_, dn = [], [], []
    for p, t, e in evs:
        a = e.get("args") or {}
        pr = a.get("probe")
        if pr == "CeState" and e.get("ph") == "X":
            s.states[_ce(p, a)].append((e["ts"], e["ts"] + e["dur"], e["name"]))
        elif pr == "ce_marker":
            m = re.match(r"(BEGIN|END) (\S+)", a["imm"])
            marks[_ce(p, a)].append((e["ts"], m.group(1), m.group(2), int(a["flops"])))
        elif pr == "dma_queued":
            q.append(e["ts"])
        elif pr == "dma_start":
            rows, rb = int(str(a.get("rows", 1)), 0), int(str(a.get("rowBytes", 0)), 0)
            st_.append({"type": a["type"].split()[0], "start": e["ts"], "bytes": rows * rb,
                        "src": _addr(a.get("src")), "dst": _addr(a.get("dst"))})
        elif pr == "dma_done":
            dn.append(e["ts"])
    # kernel window from raw CeState: first CE leaving Idle -> last CE back to Idle
    act = [(a, b) for v in s.states.values() for a, b, x in v if x != "Idle"]
    s.raw_window = (min(a for a, _ in act), max(b for _, b in act))
    # requests: the engine runs one request at a time, so the k-th start ends at the k-th done
    st_.sort(key=lambda r: r["start"])
    dn.sort()
    q.sort()
    bad = 0
    for k, r in enumerate(st_):
        r["done"] = dn[k] if k < len(dn) else None
        nxt = st_[k + 1]["start"] if k + 1 < len(st_) else float("inf")
        bad += r["done"] is None or not r["start"] <= r["done"] <= nxt
        r["ce"] = _spm_ce(r)
    if bad:
        s.notes.append(f"{bad} dma_start / dma_done pairs are not in FIFO order")
    if any(r["src"] is None for r in st_ if r["type"] == "SPM2LLC"):
        s.notes.append("dma_start src address missing in the full view (Perfetto export before chisel_src)")
    s.reqs = [r for r in st_ if r["done"] is not None]
    # regions: a stack per CE; flops between two markers go to the innermost open region
    fl, bz = defaultdict(float), defaultdict(float)
    tops = {}
    for ce, ms in marks.items():
        ms.sort(key=lambda x: x[0])
        busy = s.busy(ce)
        stack, tl = [], []
        for (t, kind, name, f), nxt in zip(ms, ms[1:] + [None]):
            if kind == "BEGIN":
                stack.append(name)
            elif stack and stack[-1] == name:
                stack.pop()
                if name in CALL.values():
                    typ = next(k for k, v in CALL.items() if v == name)
                    s.calls[(ce, typ)].append(t)
            else:
                s.notes.append(f"CE{ce}: unbalanced END {name} at {t}")
            if nxt is not None and stack:
                top = stack[-1]
                tl.append((t, nxt[0], top))
                lo, hi = max(t, s.windows[0][1]), min(nxt[0], s.windows[-1][2])
                if hi > lo:
                    fl[top] += nxt[3] - f
                    bz[top] += n(busy & bits([(lo, hi)]))
        tops[ce] = (tl, busy)
    s.region_fpc = {lab: fl[lab] / bz[lab] for lab in bz if bz[lab]}
    s.compute_set = {lab for lab, v in s.region_fpc.items() if v >= COMPUTE_FPC}
    for ce, (tl, busy) in tops.items():
        s.compute[ce] = bits((a, b) for a, b, top in tl if top in s.compute_set) & busy
    s.queued = q
    return s


# ---------------------------------------------------------------------------------------------- the metrics
def match(s: Source) -> None:
    """Issue time of every request = END of the CE's runtime DMA call: the k-th call of a CE <-> its k-th request
    of that type (LLC2SPM / SPM2LLC); requests without a scratchpad side in time order over all CEs."""
    for typ in CALL:
        rs = sorted((r for r in s.reqs if r["type"] == typ), key=lambda r: r["start"])
        if not rs:
            continue
        if typ in ("LLC2SPM", "SPM2LLC"):
            groups = [(sorted(s.calls.get((ce, typ), [])), [r for r in rs if r["ce"] == ce])
                      for ce in sorted({r["ce"] for r in rs if r["ce"] is not None})]
        else:
            allc = sorted((t, ce) for (ce, ty), ts in s.calls.items() if ty == typ for t in ts)
            for (t, ce), r in zip(allc, rs):
                r["ce"] = ce
            groups = [([t for t, _ in allc], rs)]
        for r in rs:  # unmatched below: the plugin's fallback (dma_queued, else the start)
            r.setdefault("issued", r.get("queued") if r.get("queued") is not None else r["start"])
        for cs, rr in groups:
            if len(cs) != len(rr):
                s.notes.append(f"{typ}: {len(cs)} calls vs {len(rr)} requests")
            for t, r in zip(cs, rr):
                if t > r["start"] + 1:
                    s.notes.append(f"{typ}: call ends at {t} after its request started at {r['start']}")
                r["issued"] = t


def metrics(s: Source, kernel: str, w0: float, w1: float) -> tuple[list[dict], dict, list[dict]]:
    W = bits([(w0, w1)])
    rows, iters_all = [], []
    for ce in sorted(s.compute):
        C, S, B = s.compute[ce] & W, s.suspended(ce) & W, s.busy(ce)
        mine = sorted((r for r in s.reqs if r.get("ce") == ce and r["done"] > w0 and r["issued"] < w1),
                      key=lambda r: r["issued"])
        D = bits((r["issued"], r["done"]) for r in mine) & W
        row = {"kernel": kernel, "ce": f"CE{ce}", "compute": n(C), "inflight": n(D), "overlap": n(C & D),
               "exposed": n(D) - n(C & D), "exposed_suspended": n(D & S), "suspended": n(S),
               "suspended_no_dma": n(S & ~D), "requests": len(mine)}
        row["hidden"] = row["overlap"] / row["inflight"] if row["inflight"] else None
        row["covered"] = row["overlap"] / row["compute"] if row["compute"] else None
        busy_starts = [a for a, _ in runs(B)]
        its, prev = [], int(w0)
        for c0, c1 in runs(C):
            grp = [r for r in mine if prev <= int(r["issued"]) <= c0 and not n(S & bits([(r["issued"], c0)]))]
            prev = c1
            if not grp:
                continue
            for r in grp:
                r["prefetch"] = True
            arrive = max(int(r["done"]) for r in grp)
            last = max(grp, key=lambda r: r["done"])
            first = min(int(r["issued"]) for r in grp)
            wake = next((b - arrive for b in busy_starts if b >= arrive), None) if arrive > c1 else None
            its.append({"ce": f"CE{ce}", "c0": c0, "c1": c1, "compute": c1 - c0, "requests": len(grp),
                        "bytes": sum(r["bytes"] for r in grp), "latency": arrive - first,
                        "engine_wait": last["start"] - last["issued"], "service": last["done"] - last["start"],
                        "overlap": max(0, min(arrive, c1) - max(first, c0)), "ready": arrive <= c1,
                        "exposed_wait": max(0, arrive - c1), "wake": wake,
                        "ahead": sum(1 for x in s.reqs if last["issued"] < x["start"] < last["start"]
                                     and x.get("ce") != ce), "reqs": grp})
        if its:
            med = lambda k: statistics.median([x[k] for x in its if x[k] is not None] or [None])  # noqa: E731
            row.update(iterations=len(its), ready=sum(x["ready"] for x in its) / len(its),
                       iter_compute=med("compute"), iter_latency=med("latency"), iter_engine_wait=med("engine_wait"),
                       iter_service=med("service"), iter_exposed=med("exposed_wait"), iter_wake=med("wake"),
                       iter_ahead=med("ahead"),
                       demand=sum(x["bytes"] for x in its) / sum(x["compute"] for x in its))
        rows.append(row)
        iters_all += its
    pre = [r for r in s.reqs if r.get("prefetch") and w0 <= r["start"] < w1]
    cap = sum(r["bytes"] for r in pre) / sum(r["done"] - r["start"] for r in pre) if pre else None
    dem = sum(r["demand"] for r in rows if r.get("demand")) or None
    eng = {"capacity": cap, "demand": dem, "demand_ratio": dem / cap if dem and cap else None}
    return rows, eng, iters_all


# ---------------------------------------------------------------------------------------------- compare
KEYS = ["compute", "inflight", "overlap", "exposed", "exposed_suspended", "suspended", "suspended_no_dma", "requests",
        "hidden", "covered", "iterations", "ready", "iter_compute", "iter_latency", "iter_engine_wait",
        "iter_service", "iter_ahead", "iter_exposed", "iter_wake", "demand"]
ITER_FIELDS = ("ce", "compute", "requests", "bytes", "latency", "engine_wait", "service", "overlap", "ready",
               "exposed_wait", "wake", "ahead")


def same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def fmt(v) -> str:
    if v is None:
        return "–"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}" if float(2 * v).is_integer() else f"{v:,.4f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stem", help="<report dir>/<stem>, e.g. rep/mm_phase")
    ap.add_argument("--example", default="CE1:10", help="print one prefetch iteration, CE<n>:<index>")
    a = ap.parse_args(argv)
    stem = Path(a.stem)
    plug = json.loads(Path(f"{stem}.overlap.json").read_text())
    srcs = [from_tracks(load(Path(f"{stem}.perfetto.json")))]
    full = Path(f"{stem}.perfetto_full.json")
    if full.exists():
        srcs.append(from_raw(load(full), srcs[0].windows))
    else:
        print(f"({full.name} not found: raw-probe source skipped; run redefine_report.py --perfetto-full)")
    ok = True
    res = {}
    for s in srcs:
        match(s)
        res[s.name] = [metrics(s, k, w0, w1) for k, w0, w1 in s.windows]
        print(f"[{s.name}] compute regions (≥ {COMPUTE_FPC} flops/Busy cycle): {sorted(s.compute_set)}; "
              f"{len(s.reqs)} requests; " + "; ".join(s.notes or ["all DMA calls matched"]))
        ok &= not any("calls vs" in x or "after its request" in x or "FIFO" in x for x in s.notes)
    # source agreement on the inputs
    if len(srcs) == 2:
        t, r = srcs
        print(f"[raw] kernel window from CeState {fmt(r.raw_window[0])}–{fmt(r.raw_window[1])}; "
              f"Kernel overview {fmt(t.windows[0][1])}–{fmt(t.windows[-1][2])}")
        ok &= (r.raw_window[0], r.raw_window[1]) == (t.windows[0][1], t.windows[-1][2])
        key = lambda x: (x["start"], x["done"], x["type"], x["bytes"], x.get("ce"), x.get("issued"))  # noqa: E731
        for what, eq in (("requests (start, done, type, bytes, CE, issue time)",
                          sorted(map(key, t.reqs)) == sorted(map(key, r.reqs))),
                         ("compute cycles per CE", t.compute == r.compute),
                         ("CE states", {k: sorted(v) for k, v in t.states.items()} ==
                          {k: sorted(v) for k, v in r.states.items()}),
                         ("compute region set", t.compute_set == r.compute_set)):
            print(f"  tracks vs raw · {what}: {'same' if eq else 'DIFFERENT'}")
            ok &= eq
    # the plugin's Perfetto lanes carry the issue times it used
    lanes = [(e["ts"], e["ts"] + e["dur"], e["name"]) for p, t_, e in load(Path(f"{stem}.perfetto.json"))
             if p == "CE ↔ DMA overlap" and e.get("ph") == "X"]
    wait = {round(b, 1): a for a, b, nm in lanes if nm.endswith("waiting for engine")}
    bad = sum(1 for r in srcs[0].reqs if r["start"] > r["issued"] and wait.get(round(r["start"], 1)) != r["issued"])
    print(f"  CE ↔ DMA overlap lanes: {len(lanes)} spans; issue times that differ from this script's matching: {bad}")
    ok &= bad == 0
    print()
    for k_i, (kname, w0, w1) in enumerate(srcs[0].windows):
        prow = {r["ce"]: r for r in plug["ces"] if r["kernel"] == kname}
        print(f"{kname}: window {fmt(w0)}–{fmt(w1)} ({fmt(w1 - w0)} cycles)")
        ces = [r["ce"] for r in res[srcs[0].name][k_i][0]]
        head = f"  {'metric':<20}" + "".join(f"{c:>22}" for c in ces) + "  agree (plugin = " + \
            " = ".join(s.name for s in srcs) + ")"
        print(head)
        for key in KEYS:
            vals, agree = [], True
            for ce in ces:
                p = prow.get(ce, {}).get(key)
                xs = [next(r for r in res[s.name][k_i][0] if r["ce"] == ce).get(key) for s in srcs]
                agree &= all(same(p, x) for x in xs)
                vals.append(fmt(xs[0]) + ("" if all(same(p, x) for x in xs) else f" ≠ plugin {fmt(p)}"))
            ok &= agree
            print(f"  {key:<20}" + "".join(f"{v:>22}" for v in vals) + f"  {'yes' if agree else 'NO'}")
        pe = next((e for e in plug["engine"] if e["kernel"] == kname), {})
        for key in ("capacity", "demand", "demand_ratio"):
            xs = [res[s.name][k_i][1][key] for s in srcs]
            agree = all(same(pe.get(key), x) for x in xs)
            ok &= agree
            print(f"  engine {key:<13}{fmt(xs[0]):>22}  plugin {fmt(pe.get(key))}  {'yes' if agree else 'NO'}")
        # not only the medians: the same iterations, field by field
        its_p = sorted(tuple(x[f] for f in ITER_FIELDS) for x in plug["iterations"] if x["kernel"] == kname)
        for s in srcs:
            eq = sorted(tuple(x[f] for f in ITER_FIELDS) for x in res[s.name][k_i][2]) == its_p
            print(f"  prefetch iterations, field by field ({len(its_p)}): plugin vs {s.name} "
                  f"{'identical' if eq else 'DIFFERENT'}")
            ok &= eq
        # a worked example
        ce_s, idx = a.example.split(":")
        its = [x for x in res[srcs[0].name][k_i][2] if x["ce"] == ce_s]
        if int(idx) < len(its):
            x = its[int(idx)]
            print(f"\n  example: {ce_s} prefetch iteration {idx} (cycles; Perfetto ts = cycle + 0.5)")
            print(f"    compute region     {x['c0']:>9,} – {x['c1']:,}  ({x['compute']:,} cycles)")
            for r in sorted(x["reqs"], key=lambda r: r["issued"]):
                q = r.get("queued")
                print(f"    {r['type']:<8} {r['bytes']:>5} B  issued {int(r['issued']):,}  "
                      + (f"queued {int(q):,}  " if q is not None else "")
                      + f"start {int(r['start']):,}  done {int(r['done']):,}")
            print(f"    data arrived {max(int(r['done']) for r in x['reqs']):,}: "
                  f"ready={x['ready']} exposed wait {x['exposed_wait']:,} wake-up {x['wake']} "
                  f"wait for engine {x['engine_wait']:,.0f} transfer {x['service']:,.0f} "
                  f"other CEs' requests ahead {x['ahead']}")
    print("\nRESULT:", "all values agree" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
