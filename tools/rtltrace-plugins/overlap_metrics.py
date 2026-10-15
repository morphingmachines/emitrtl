"""rtltrace plugin: does data movement (CR-DMA) overlap compute, is it enough, and is it safe?

A double-buffered kernel issues the DMA for the next chunk, computes on the current one, then waits for the DMA.
This plugin measures that pattern from existing probes; the kernel needs nothing beyond PC_MARK around its compute:

  compute     per CE, the PC-marker regions that retire FP work (≥ 0.1 flops/busy cycle, or compute=<regions>)
  DMA issue   per CE, the end of the runtime's DMA call (rt.DMA_LLC2SPM / rt.DMA_SPM2LLC / rt.DMA_LLC2LLC markers,
              added by redefine_pc_markers.h in shared2spm / spm2shared / local2local), matched in order to the
              CR-DMA requests whose scratchpad side is that CE (LLC2LLC: in time order over all CEs). When the
              counts do not match, dma_queued is used as the issue time (later than the real issue).
  DMA life    dma_queued / dma_start / dma_done (CR_DMA group, on by default), from dma_metrics.py
  waiting     CeState Suspended, and the first Busy after a request's dma_done (wake-up)
  safety      spm_access (group l1d_spm, +rtltrace_l1d_spm_on): CE loads from a scratchpad range a DMA is still
              filling, CE stores into a range a DMA is still sending; spm_s3_overlap events split into "same
              buffer as the in-flight DMA" (a kernel race) and "another buffer" (the hardware hazard)

Reported per CE and per kernel window:
  DMA in flight, overlapped with compute, DMA hidden %, compute with DMA underneath %, exposed DMA (and how much of
  it the CE spent suspended);
  prefetch iterations (a compute region + the requests issued since the previous one): data ready before compute
  ended %, median compute / issue→done latency split into wait-for-engine and transfer / exposed wait / wake-up;
  requests of other CEs ahead in the shared engine; DMA demand vs capacity (can any overlap hide the transfers?).

Load after dma_metrics.py and ce_phases.py (it reads their published summaries).
Plugin args: compute=<region,...>, prefetch_gap=<cycles> (max issue → compute start distance, default 512),
overlap_json=<path>, spm_base / spm_stride (as dma_metrics).
"""

from __future__ import annotations

import bisect
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from rtltrace.plugin import Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (_r, ce_key, ce_label, ce_timelines, compute_arg, kernel_windows,  # noqa: E402
                             load_workload, phase_analysis, phase_stats, probe_marks, publish, published, window_names)

CALL_MARKER = {"LLC2SPM": "rt.DMA_LLC2SPM", "SPM2LLC": "rt.DMA_SPM2LLC", "LLC2LLC": "rt.DMA_LLC2LLC",
               "GRP_SCATTER": "rt.DMA_SCATTER", "GRP_GATHER": "rt.DMA_GATHER"}


def _union(iv) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _inter(A, B) -> list[list[float]]:
    i = j = 0
    out = []
    while i < len(A) and j < len(B):
        a, b = max(A[i][0], B[j][0]), min(A[i][1], B[j][1])
        if b > a:
            out.append([a, b])
        if A[i][1] < B[j][1]:
            i += 1
        else:
            j += 1
    return out


def _clip(iv, w0, w1):
    return [[max(a, w0), min(b, w1)] for a, b in iv if b > w0 and a < w1]


def _len(iv) -> float:
    return sum(b - a for a, b in iv)


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


class OverlapMetrics(Plugin):
    name = "dma-overlap"
    requires = ["CeState"]

    def setup(self, ctx):
        a = ctx.args
        self.args = a
        self.warnings: list[str] = []
        self.kernels = [k.name for k in load_workload(a["launch"])] if a.get("launch") else []
        self.gap = float(a.get("prefetch_gap", 512))
        self.rows: list[dict] = []  # per kernel x CE
        self.iters: list[dict] = []
        self.safety: list[dict] = []
        self.engine: list[dict] = []
        self.issue_source = None

    # ------------------------------------------------------------------ matching
    def _issue_times(self, m, pa, reqs) -> str:
        """Set r['issued'] (CE-side issue: end of the runtime DMA call) on every request; returns the source."""
        rev = {n: i for i, n in pa["names"].items()}
        marks = pa.get("marks") or probe_marks(m)  # the commit-log marks when there is no ce_marker probe
        ok = True
        for typ, mname in CALL_MARKER.items():
            mid = rev.get(mname)
            rs = [r for r in reqs if r["type"] == typ]
            if not rs:
                continue
            if mid is None:
                ok = False
                continue
            calls = {k: [] for k in marks}
            for k, ms in marks.items():
                open_ = None
                for x in ms:
                    if x.imm == mid:
                        open_ = x.t
                    elif x.imm == mid | 1 and open_ is not None:
                        calls[k].append((x.t, k))
                        open_ = None
            if typ in ("LLC2SPM", "SPM2LLC"):
                groups = [([c for c, _ in calls.get(k, [])], [r for r in rs if r["ce"] == k[1]]) for k in calls]
            else:  # no scratchpad side: time order over all CEs
                allc = sorted(c for v in calls.values() for c in v)
                groups = [([t for t, _ in allc], sorted(rs, key=lambda r: r["start"]))]
                for (t, k), r in zip(allc, groups[0][1]):
                    r["issuer"] = k[1]
            for cs, rr in groups:
                if len(cs) != len(rr) or any(t > r["start"] + 1 for t, r in zip(cs, rr)):
                    ok = False
                    continue
                for t, r in zip(cs, rr):
                    r["issued"] = t
        for r in reqs:
            if r.get("issued") is None:
                r["issued"] = r.get("queued") if r.get("queued") is not None else r["start"]
                r["approx"] = True
                ok = False  # e.g. a CE with no marks at all: its requests were not matched either
        if not ok:
            self.warnings.append("some DMA requests could not be matched to the CE's runtime DMA-call markers "
                                 "(redefine_pc_markers.h); their issue time is dma_queued, which is later than the "
                                 "real issue (wait-for-engine is under-counted)")
        return "runtime DMA-call markers" if ok else "markers where they match, else dma_queued"

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        dma = published(m, "dma")
        if dma is None:
            self.warnings.append("dma_metrics.py is not loaded before this plugin: overlap not measured")
            return self._publish(m)
        if not dma.get("requests"):
            if dma.get("mode") == "infer" and any(w.get("bytes") for w in dma.get("windows", [])):
                self.warnings.append("CR-DMA probes are not in this trace (+rtltrace_CR_DMA_on): compute / DMA "
                                     "overlap not measured")
            return self._publish(m)  # otherwise the kernel uses no CR-DMA: nothing to overlap
        tls = ce_timelines(m)
        pa = phase_analysis(m, self.args, tls, self.warnings)
        if not pa["source"]:
            self.warnings.append("no PC markers in this trace: the compute regions are unknown, so compute / DMA "
                                 "overlap is not measured (put PC_MARK_BEGIN/END around the compute)")
            return self._publish(m)
        reqs = [dict(r) for r in dma["requests"] if r.get("done") is not None]
        self.issue_source = self._issue_times(m, pa, reqs)
        windows = kernel_windows(m, tls, max(1, len(self.kernels)), self.args, self.warnings)
        names = window_names(self.kernels, len(windows))
        keys = {ce_key(inst): tl for inst, tl in tls.items() if ce_key(inst)}
        want = compute_arg(self.args)
        known = {seg.label for segs in pa["phases"].values() for seg in segs}
        if want and not want & known:
            self.warnings.append(f"compute={','.join(sorted(want))} matches no PC region "
                                 f"(regions: {', '.join(sorted(known)) or 'none'}); using the regions that retire flops")
            want = set()
        spm = self._spm_events(m)
        for kname, (w0, w1) in zip(names, windows):
            compute = want or self._compute_regions(pa, w0, w1)
            for k in sorted(pa["phases"]):
                self._ce(kname, k, keys.get(k, []), pa["phases"][k], compute, reqs, w0, w1, spm.get(k))
            self._engine(kname, reqs, w0, w1)
        self._s3(m, reqs)
        self._perfetto(out, reqs)
        self._publish(m)

    def _publish(self, m) -> None:
        publish(m, "overlap", self.summary())
        if self.args.get("overlap_json"):
            Path(self.args["overlap_json"]).write_text(json.dumps(self.summary(), indent=2, default=str))

    @staticmethod
    def _compute_regions(pa, w0, w1) -> set[str]:
        fl, bz = Counter(), Counter()
        for k, segs in pa["phases"].items():
            for lab, st in phase_stats(segs, pa["points"].get(k, []), w0, w1).items():
                if st.counted:
                    fl[lab] += st.flops
                    bz[lab] += st.busy
        return {lab for lab in bz if bz[lab] and fl[lab] / bz[lab] >= 0.1}

    def _spm_events(self, m) -> dict:
        out = defaultdict(list)
        for e in m.events("spm_access"):
            k = ce_key(e.probe.inst)
            if k:
                out[k].append((e.ts, e.vals.get("isWrite"), e.vals.get("addr")))
        for v in out.values():
            v.sort()
        return out

    def _ce(self, kname, k, tl, segs, compute, reqs, w0, w1, spm):
        ce = k[1]
        C = _clip(_union((s.t0, s.t1) for s in segs if s.label in compute and s.state == "Busy"), w0, w1)
        S = _clip(_union((a, b) for a, b, st in tl if st == "Suspended"), w0, w1)
        mine = sorted((r for r in reqs if (r["ce"] if r["ce"] is not None else r.get("issuer")) == ce
                       and r["done"] > w0 and r["issued"] < w1), key=lambda r: r["issued"])
        D = _clip(_union((r["issued"], r["done"]) for r in mine), w0, w1)
        ov = _inter(D, C)
        row = {"kernel": kname, "ce": ce_label(k), "window": w1 - w0, "compute": _len(C), "inflight": _len(D),
               "overlap": _len(ov), "exposed": _len(D) - _len(ov), "exposed_suspended": _len(_inter(D, S)),
               "suspended": _len(S), "suspended_no_dma": _len(S) - _len(_inter(S, D)), "requests": len(mine)}
        row["hidden"] = row["overlap"] / row["inflight"] if row["inflight"] else None
        row["covered"] = row["overlap"] / row["compute"] if row["compute"] else None
        # prefetch iterations: a compute region and the requests issued after the previous compute region ended
        # and at most prefetch_gap cycles before this one starts
        busy_starts = sorted(a for a, b, st in tl if st == "Busy")
        prev_end, it = w0, []
        for c0, c1 in C:
            grp = [r for r in mine if prev_end <= r["issued"] <= c0 and c0 - r["issued"] <= self.gap]
            prev_end = c1
            if not grp:
                continue
            for r in grp:
                r["prefetch"] = True
            arrive = max(r["done"] for r in grp)
            last = max(grp, key=lambda r: r["done"])
            i = bisect.bisect_left(busy_starts, arrive)
            wake = busy_starts[i] - arrive if i < len(busy_starts) and arrive > c1 else None
            it.append({"kernel": kname, "ce": row["ce"], "compute": c1 - c0, "requests": len(grp),
                       "bytes": sum(r["bytes"] for r in grp),
                       "latency": arrive - min(r["issued"] for r in grp),
                       "engine_wait": last["start"] - last["issued"], "service": last["done"] - last["start"],
                       "overlap": max(0.0, min(arrive, c1) - max(min(r["issued"] for r in grp), c0)),
                       "ready": arrive <= c1, "exposed_wait": max(0.0, arrive - c1), "wake": wake,
                       "ahead": sum(1 for x in reqs if last["issued"] < x["start"] < last["start"]
                                    and (x["ce"] if x["ce"] is not None else x.get("issuer")) != ce)})
        self.iters += it
        if it:
            row.update(iterations=len(it), ready=sum(x["ready"] for x in it) / len(it),
                       iter_compute=_med(x["compute"] for x in it), iter_latency=_med(x["latency"] for x in it),
                       iter_engine_wait=_med(x["engine_wait"] for x in it),
                       iter_service=_med(x["service"] for x in it),
                       iter_exposed=_med(x["exposed_wait"] for x in it), iter_wake=_med(x["wake"] for x in it),
                       iter_ahead=_med(x["ahead"] for x in it),
                       demand=sum(x["bytes"] for x in it) / sum(x["compute"] for x in it))
        self.rows.append(row)
        if spm:
            self._safety(kname, row["ce"], spm, mine)

    def _safety(self, kname, ce, spm, mine):
        ts = [a[0] for a in spm]
        rd = wr = 0
        first = None
        for r in mine:
            if r["type"] not in ("LLC2SPM", "SPM2LLC"):
                continue
            lo = r["dst"] if r["type"] == "LLC2SPM" else r["src"]
            hi = lo + r["bytes"]
            for t, w, a in spm[bisect.bisect_left(ts, r["issued"]):bisect.bisect_right(ts, r["done"])]:
                if lo <= a < hi and ((r["type"] == "LLC2SPM" and not w) or (r["type"] == "SPM2LLC" and w)):
                    rd += r["type"] == "LLC2SPM"
                    wr += r["type"] == "SPM2LLC"
                    first = first or {"cycle": round(t), "addr": hex(a), "type": r["type"]}
        self.safety.append({"kernel": kname, "ce": ce, "loads_in_flight_dst": rd, "stores_in_flight_src": wr,
                            "first": first, "spm_accesses": len(spm)})

    def _s3(self, m, reqs):
        same = other = 0
        for e in m.events("spm_s3_overlap"):
            k = ce_key(e.probe.inst)
            a = e.vals.get("addr")
            hit = any(r["ce"] == k[1] and r["type"] == "LLC2SPM" and r["start"] <= e.ts <= r["done"]
                      and r["dst"] <= a < r["dst"] + r["bytes"] for r in reqs) if k and a is not None else False
            same += hit
            other += not hit
        self.s3 = {"same_buffer": same, "other_buffer": other}

    def _engine(self, kname, reqs, w0, w1):
        rs = [r for r in reqs if w0 <= r["start"] < w1 and r.get("service") and r.get("prefetch")]
        if not rs:
            return
        cap = sum(r["bytes"] for r in rs) / sum(r["service"] for r in rs)
        rows = [r for r in self.rows if r["kernel"] == kname and r.get("demand")]
        demand = sum(r["demand"] for r in rows) if rows else None
        self.engine.append({"kernel": kname, "capacity": cap, "demand": demand,
                            "demand_ratio": demand / cap if demand and cap else None, "ces": len(rows)})

    # ------------------------------------------------------------------ perfetto
    def _perfetto(self, out, reqs):
        proc = out.process("CE ↔ DMA overlap", sort=2)
        lanes: dict[int, list[tuple[float, int]]] = defaultdict(list)
        tracks = {}
        for r in sorted(reqs, key=lambda r: r["issued"]):
            ce = r["ce"] if r["ce"] is not None else r.get("issuer")
            if ce is None:
                continue
            ln = lanes[ce]
            j = next((i for i, (end, _) in enumerate(ln) if end <= r["issued"]), None)
            if j is None:
                ln.append((0, len(ln)))
                j = len(ln) - 1
            ln[j] = (r["done"], ln[j][1])
            key = (ce, ln[j][1])
            if key not in tracks:
                tracks[key] = out.track(proc, f"CE{ce} DMA" + (f" ({key[1] + 1})" if key[1] else ""),
                                        sort=ce * 10 + key[1])
            tr = tracks[key]
            args = {"type": r["type"], "bytes": r["bytes"], "probe": "rt.DMA_* marker → dma_start → dma_done"}
            if r["start"] > r["issued"]:
                out.span(tr, r["issued"], r["start"], f"{r['type']} waiting for engine", args)
            out.span(tr, r["start"], r["done"], f"{r['type']} transfer {r['bytes']} B", args)

    # ------------------------------------------------------------------ outputs
    def summary(self) -> dict:
        return {"issue_source": self.issue_source, "ces": self.rows, "iterations": self.iters,
                "engine": self.engine, "safety": self.safety, "s3": getattr(self, "s3", None),
                "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        if not self.rows:
            if self.warnings:
                G, b = V.G_CHECK
                return [Section("Tool warnings · overlap", ["warning"], [[w] for w in self.warnings], group=G,
                                order=b + 15, key="Overlap warnings")]
            return []
        G, b = V.G_DMA
        pc = lambda x: None if x is None else _r(100 * x, 3)  # noqa: E731
        out = [Section("Compute / DMA overlap per CE",
                       ["kernel", "CE", "compute", "DMA in flight", "overlapped", "DMA hidden %",
                        "compute with DMA underneath %", "exposed DMA", "exposed while suspended",
                        "suspended, no DMA in flight"],
                       [[r["kernel"], r["ce"], round(r["compute"]), round(r["inflight"]), round(r["overlap"]),
                         pc(r["hidden"]), pc(r["covered"]), round(r["exposed"]), round(r["exposed_suspended"]),
                         round(r["suspended_no_dma"])] for r in self.rows],
                       "Cycles inside the kernel window. DMA in flight = from the CE issuing a request (end of its "
                       f"runtime DMA call; source: {self.issue_source}) to the last write acknowledged. 'suspended, "
                       "no DMA in flight' = waiting for something else (sync, other CEs).",
                       legend=V.legend("DMA in flight (CE)", "overlapped with compute", "DMA hidden %",
                                       "compute with DMA underneath %", "exposed DMA"),
                       group=G, order=b, key="Overlap · per CE")]
        rows = [[r["kernel"], r["ce"], r.get("iterations"), pc(r.get("ready")), r.get("iter_compute"),
                 r.get("iter_latency"), r.get("iter_engine_wait"), r.get("iter_service"), r.get("iter_ahead"),
                 r.get("iter_exposed"), r.get("iter_wake")] for r in self.rows if r.get("iterations")]
        if rows:
            out.append(Section("Prefetch iterations (double buffering)",
                               ["kernel", "CE", "iterations", "ready before compute ended %", "compute p50",
                                "issue → data arrived p50", "wait for engine p50", "transfer p50",
                                "other CEs' requests ahead p50", "exposed wait p50", "wake-up p50"], rows,
                               "An iteration = a compute region plus the DMA requests issued just before it (the "
                               "next chunk). Hidden when the data arrives before the compute ends; otherwise the CE "
                               "waits 'exposed wait' plus the wake-up. issue → arrived = wait for engine + transfer.",
                               legend=V.legend("prefetch iteration", "ready before compute ended %",
                                               "wait for engine", "transfer", "exposed wait per iteration",
                                               "wake-up latency"),
                               group=G, order=b + 1, key="Overlap · prefetch iterations"))
        if self.engine:
            out.append(Section("Can the shared CR-DMA keep up?",
                               ["kernel", "CEs prefetching", "needed bytes/cycle", "engine bytes/cycle while "
                                                                                  "transferring", "demand ÷ capacity"],
                               [[e["kernel"], e["ces"], _r(e["demand"], 3), _r(e["capacity"], 3),
                                 _r(e["demand_ratio"], 3)] for e in self.engine],
                               "Needed = bytes each CE prefetches per iteration ÷ its compute cycles, summed over the "
                               "CEs sharing the engine; delivered = bytes ÷ transfer cycles of those prefetch "
                               "requests. Above 1, transfers cannot be hidden however well they overlap: move less "
                               "data per flop or speed up the engine.",
                               legend=V.legend("DMA demand vs capacity"), group=G, order=b + 2,
                               key="Overlap · engine demand"))
        if self.safety or getattr(self, "s3", None):
            s3 = getattr(self, "s3", None) or {}
            rows = [[x["kernel"], x["ce"], x["loads_in_flight_dst"], x["stores_in_flight_src"],
                     f"cycle {x['first']['cycle']} {x['first']['type']} {x['first']['addr']}" if x["first"] else "–"]
                    for x in self.safety]
            out.append(Section("Is the overlap safe? (scratchpad buffers in flight)",
                               ["kernel", "CE", "loads from a buffer still being filled",
                                "stores into a buffer still being sent", "first"], rows,
                               "Needs +rtltrace_l1d_spm_on (CE scratchpad accesses). A load inside the destination "
                               "of an LLC→SPM request between its issue and its completion reads stale or partial "
                               "data; a store inside the source of an SPM→LLC request corrupts what is sent. "
                               f"Hazard events (spm_s3_overlap): {s3.get('same_buffer', 0)} on the buffer being "
                               f"filled (kernel race), {s3.get('other_buffer', 0)} on another buffer (hardware "
                               "hazard of concurrent CE / DMA access).",
                               group=G, order=b + 3, key="Overlap · safety"))
        if self.warnings:
            G, b = V.G_CHECK
            out.append(Section("Tool warnings · overlap", ["warning"], [[w] for w in self.warnings], group=G,
                               order=b + 15, key="Overlap warnings"))
        return out
