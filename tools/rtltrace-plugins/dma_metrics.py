"""rtltrace plugin: CR-DMA utilisation (is the DMA engine the bottleneck, and why).

Two modes (mode=auto picks probe when the trace has dma_start events):

  probe   CR-DMA lifecycle probes: dma_queued (descriptor written) -> dma_start (next promoted to exec, with type,
          src, dst, rowBytes, stride, rows) -> dma_done (last write acked) -> dma_retire (completion sync acked), the
          engine's TileLink port `CR-DMA-mbus` (per-row Gets/Puts), and optionally the `dma_gate` state (why no
          request goes out in a cycle; +rtltrace_CR_DMA_detail_on).
          Per request: queue wait, service time, bytes, requesting CE; engine busy fraction and throughput.
  infer   no CR-DMA probes: the CR-DMA's transactions on the LLC input port (client contains "CR_DMA"). Gives bytes,
          throughput, transactions in flight, how much of the time the engine has a transaction outstanding, and the
          issue interval, per sub-window (setup / steady / tail) of each kernel window.

    python3 -m rtltrace report sim.log -o report.html --plugin dma_metrics.py [--plugin-arg launch=<launch.py>]

Plugin args:
  mode=auto|probe|infer   port=<LLC input port glob> (infer; default LLC?in)   client=<substring> (default CR_DMA)
  mbus=<port glob> (default CR?DMA?mbus)   orch=<port glob> (default CR?DMA?orch)
  spm_base=0x30000000 spm_stride=0x2000    SPM window of CE i = base + i x stride (requesting CE of a request)
  bin=<cycles>            throughput counter bin in Perfetto (default 1000)
  dma_json=<path>         write the summary as JSON
"""

from __future__ import annotations

import bisect
import json
import statistics
import sys
from collections import Counter, defaultdict
from fnmatch import fnmatch
from pathlib import Path

from rtltrace.plugin import Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (Names, _r, ce_timelines, kernel_windows, load_workload, plugin_regions,  # noqa: E402
                             publish, section_of, sub_windows, window_names)

DMA_TYPES = V.DMA_TYPES


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _union(iv, w0, w1) -> float:
    """Length of the union of intervals clipped to [w0, w1]."""
    tot, end = 0.0, w0
    for a, b in sorted(iv):
        a, b = max(a, end), min(b, w1)
        if b > a:
            tot += b - a
            end = b
    return tot


class DmaMetrics(Plugin):
    name = "dma-metrics"
    requires = ["CeState"]
    fields = {"dma_start/type": "dma_type"}
    tags = {"dma_type"}

    def annotate(self, f, value, ev):
        t = DMA_TYPES.get(value)
        return (t, f"{t} ({value})") if t else None

    def setup(self, ctx):
        a = ctx.args
        self.args = a
        self.mode = a.get("mode", "auto")
        self.port = a.get("port", "LLC?in")
        self.client = a.get("client", "CR_DMA")
        self.mbus = a.get("mbus", "CR?DMA?mbus")
        self.orch = a.get("orch", "CR?DMA?orch")
        self.spm_base = int(a.get("spm_base", "0x30000000"), 0)
        self.spm_stride = int(a.get("spm_stride", "0x2000"), 0)
        self.bin = float(a.get("bin", 1000))
        self.kernels = [k.name for k in load_workload(a["launch"])] if a.get("launch") else []
        self.warnings: list[str] = []
        self.issues: list[dict] = []
        self.txns_seen = True
        self.declared = False
        self.windows: list[dict] = []
        self.requests: list[dict] = []
        self.gate: Counter = Counter()
        self.regions = plugin_regions(ctx)
        self.by_section: list[dict] = []
        self.names = Names(a, self.regions, self.warnings, ctx.trace)

    # ------------------------------------------------------------------ helpers
    def _ce_of(self, addr) -> int | None:
        if addr is None:
            return None
        off = addr - self.spm_base
        return off // self.spm_stride if 0 <= off < 64 * self.spm_stride else None

    def _txns(self, m, glob, client=None):
        return sorted((t for t in m.txns(glob) if t.kind == "A" and (client is None or client in str(t.client))),
                      key=lambda t: t.start)

    def _engine(self, txns, a, b) -> dict:
        w = [t for t in txns if a <= t.start < b]
        cyc = b - a
        end = lambda t: b if t.end is None else min(t.end, b)  # noqa: E731 - unanswered at trace end: open
        lat = [t.end - t.start for t in w if t.done]
        gets = [t for t in w if t.opcode == "Get"]
        gap = [y.start - x.start for x, y in zip(gets, gets[1:])]
        by = sum(t.nbytes for t in w)
        rd = sum(t.nbytes for t in gets)
        return {"txns": len(w), "gets": len(gets), "puts": len(w) - len(gets), "bytes": by,
                "bytes_per_cycle": by / cyc if cyc else None, "read_bytes_per_cycle": rd / cyc if cyc else None,
                "avg_in_flight": sum(end(t) - t.start for t in w) / cyc if cyc else None,
                "active": _union([(t.start, end(t)) for t in w], a, b) / cyc if cyc else None,
                "get_interval_p50": _pct(gap, 0.5), "get_interval_mean": statistics.mean(gap) if gap else None,
                "latency_p50": _pct(lat, 0.5), "latency_p95": _pct(lat, 0.95)}

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        starts = sorted(m.events("dma_start"), key=lambda e: e.ts)
        # a probe's DECL line prints only when its group is on: declared without events = no request was made
        declared = self.declared = any(p.name == "dma_start" for p in m.trace.probes.values())
        if self.mode == "auto":
            self.mode = "probe" if starts or (declared and not self._txns(m, self.port, self.client)) else "infer"
        tls = ce_timelines(m)
        windows = kernel_windows(m, tls, max(1, len(self.kernels)), self.args, self.warnings)
        names = window_names(self.kernels, len(windows))
        if self.mode == "probe":
            txns = self._txns(m, self.mbus)
            if not txns and starts:
                self.warnings.append(f"no transactions on '{self.mbus}': per-row Get/Put timing not available")
            self._requests(m, starts)
            for e in m.states("dma_gate"):
                self.gate[e.probe.enum_name("value", e.event.vals.get("value", 0)) or e.event.vals.get("value")] += \
                    e.t1 - e.t0
        else:
            txns = self._txns(m, self.port, self.client)
            if txns and declared:
                self.warnings.append("the CR-DMA probes recorded no request, but there is CR-DMA traffic on "
                                     f"'{self.port}': throughput is inferred from that traffic")
            elif txns:
                self.warnings.append("CR-DMA probes are not in this trace (group CR_DMA off): DMA requests, their "
                                     f"CEs and waits are not available; throughput is inferred from '{self.client}' "
                                     f"traffic on '{self.port}' (turn the probes on with +rtltrace_CR_DMA_on)")
            self.txns_seen = bool(txns)
        for kname, (w0, w1) in zip(names, windows):
            for sub, a, b in [("whole", w0, w1)] + [s for s in sub_windows(tls, w0, w1) if s[0] != "whole"]:
                row = {"kernel": kname, "window": sub, "from": a, "to": b, "cycles": b - a,
                       **self._engine(txns, a, b)}
                if self.mode == "probe":
                    reqs = [r for r in self.requests if a <= r["start"] < b]
                    row["requests"] = len(reqs)
                    row["engine_busy"] = (_union([(r["start"], r["done"] or b) for r in reqs], a, b) / (b - a)
                                          if b > a else None)
                self.windows.append(row)
            if self.regions is not None:
                per = defaultdict(lambda: [0, 0])
                for t in txns:
                    if w0 <= t.start < w1:
                        c = per[(t.opcode, section_of(self.regions, t.address))]
                        c[0] += 1
                        c[1] += t.nbytes
                self.by_section += [{"kernel": kname, "opcode": op, "section": sec, "txns": n, "bytes": b}
                                    for (op, sec), (n, b) in sorted(per.items(), key=lambda x: -x[1][1])]
        self._issues(m)
        self._perfetto(out, txns)
        publish(m, "dma", self.summary())
        if self.args.get("dma_json"):
            Path(self.args["dma_json"]).write_text(json.dumps(self.summary(), indent=2))

    def _requests(self, m, starts):
        queued = sorted(e.ts for e in m.events("dma_queued"))
        dones = sorted(e.ts for e in m.events("dma_done"))
        # completion sync: dma_retire (exec slot freed after the orchestrator acked the sync), else the CR-DMA's
        # writes on its orchestrator port
        syncs = sorted(e.ts for e in m.events("dma_retire")) or sorted(t.start for t in self._txns(m, self.orch))
        if starts and not dones:
            self.warnings.append("no dma_done events: service times not available")
        for e in starts:
            v = e.vals
            i = bisect.bisect_left(dones, e.ts)
            done = dones[i] if i < len(dones) else None
            q = bisect.bisect_right(queued, e.ts) - 1
            j = bisect.bisect_left(syncs, done) if done is not None else len(syncs)
            typ = DMA_TYPES.get(v.get("type"), v.get("type"))
            spm = v.get("dst") if typ == "LLC2SPM" else v.get("src") if typ == "SPM2LLC" else None
            rows, rb = v.get("rows", 1) or 1, v.get("rowBytes", 0) or 0
            self.requests.append({
                "start": e.ts, "done": done, "type": typ, "src": v.get("src"), "dst": v.get("dst"),
                "rows": rows, "row_bytes": rb, "bytes": rows * rb, "ce": self._ce_of(spm),
                "src_section": section_of(self.regions, v.get("src")),
                "dst_section": section_of(self.regions, v.get("dst")),
                "queued": queued[q] if q >= 0 else None,
                "queue_wait": e.ts - queued[q] if q >= 0 else None,
                "service": done - e.ts if done is not None else None,
                "done_to_sync": syncs[j] - done if done is not None and j < len(syncs) else None})

    def _perfetto(self, out, txns):
        proc = out.process("CR-DMA requests", sort=3)
        if self.requests:
            tr = out.track(proc, "requests")
            for r in self.requests:
                src, dst = self._sec(r["src_section"]), self._sec(r["dst_section"])
                where = f" {src}→{dst}" if src and dst else ""
                out.span(tr, r["start"], r["done"] or r["start"] + 1, f"{r['type']}{where} {_bytes(r['bytes'])}",
                         {**{k: (hex(v) if k in ("src", "dst") and isinstance(v, int) else v) for k, v in r.items()
                             if k not in ("start", "done")}, "probe": "dma_start → dma_done"})
        if txns:
            bins = defaultdict(int)
            for t in txns:
                bins[int(t.start // self.bin)] += t.nbytes
            pts = [(k * self.bin, round(bins.get(k, 0) / self.bin, 3))
                   for k in range(min(bins), max(bins) + 2)]
            out.counter(proc, f"DMA bytes/cycle ({self.bin:.0f}-cycle bins)", pts)

    def _issues(self, m):
        """Correctness events of the CR-DMA: dma_overflow (a descriptor was written while the next slot was still
        full: the queued request was overwritten) and dma_error (a D beat with denied/corrupt set; info =
        {source, denied, corrupt})."""
        for name, what in (("dma_overflow", "descriptor overwritten: a request was written while the next slot "
                                            "was still full, so a queued request was lost"),
                           ("dma_error", "TileLink error response (denied / corrupt) on the CR-DMA memory port")):
            evs = sorted(m.events(name), key=lambda e: e.ts)
            if not evs:
                continue
            e = evs[0]
            row = {"id": name, "count": len(evs), "first_cycle": e.cycle, "rtl_path": e.probe.inst, "probe": name,
                   "src": e.probe.meta.get("src"), "what": what}
            if name == "dma_error":
                info = e.vals.get("info", 0)
                row.update(source=info >> 2, denied=bool(info & 2), corrupt=bool(info & 1))
            self.issues.append(row)
            self.warnings.append(f"{name} fired {len(evs)} time(s), first at cycle {e.cycle:,}: {what} "
                                 f"[{e.probe.inst} · {name}" + (f" · {row['src']}]" if row["src"] else "]"))

    # ------------------------------------------------------------------ outputs
    def summary(self) -> dict:
        return {"mode": self.mode, "windows": self.windows, "requests": self.requests, "by_section": self.by_section,
                "gate": dict(self.gate), "issues": self.issues, "warnings": self.warnings}

    def _sec(self, label) -> str | None:
        """Short buffer / section name for slice titles: 'W', 'CE2 scratchpad'."""
        n = self.names.section(label)
        return str(n).split(" (")[0] if n else None

    def report(self, m) -> list[Section]:
        G, b = V.G_DMA
        out = []
        if self.mode == "probe" and not self.requests:
            return [Section("DMA requests by type and CE", ["type", "CE", "requests", "bytes"], [],
                            empty="No CR-DMA requests in this run: the kernel does not use the CR-DMA (its probes "
                                  "are on, so a request would have been seen)." if self.declared else
                                  "No CR-DMA requests: the dma_start probe is not in this trace (group CR_DMA off; "
                                  "+rtltrace_CR_DMA_on).",
                            group=G, order=b + 10, key="CR-DMA · requests by type and CE")] + self._warn_section()
        if self.mode == "infer" and not self.txns_seen:
            return [Section("DMA engine throughput", ["kernel", "phase", "bytes"], [],
                            empty=f"No CR-DMA traffic on '{self.port}'. The CR-DMA probes are not in this trace, so "
                                  "this is inferred from LLC traffic (turn them on with +rtltrace_CR_DMA_on).",
                            group=G, order=b + 11, key="CR-DMA · engine (infer)")] + self._warn_section()
        if self.mode == "probe":
            by = defaultdict(list)
            for r in self.requests:
                by[(r["type"], r["ce"])].append(r)
            rows = []
            for (typ, ce), rs in sorted(by.items(), key=lambda x: (str(x[0][0]), x[0][1] if x[0][1] is not None
                                                                   else -1)):
                svc = [r["service"] for r in rs if r["service"] is not None]
                qw = [r["queue_wait"] for r in rs if r["queue_wait"] is not None]
                ds = [r["done_to_sync"] for r in rs if r["done_to_sync"] is not None]
                byt = sum(r["bytes"] for r in rs)
                byt_done = sum(r["bytes"] for r in rs if r["service"] is not None)  # same requests as svc
                rows.append([typ, "–" if ce is None else f"CE{ce}", len(rs), byt,
                             _r(statistics.mean(r["rows"] for r in rs), 3), _pct(qw, 0.5), _pct(svc, 0.5),
                             _pct(svc, 0.95), max(svc) if svc else None,
                             _r(byt_done / sum(svc), 3) if svc and sum(svc) else None, _pct(ds, 0.5)])
            out.append(Section("DMA requests by type and CE",
                               ["type", "CE", "requests", "bytes", "rows per request", "slot wait p50",
                                "service p50", "service p95", "service max", "B/cycle while serving", "done → sync p50"],
                               rows, "One row per DMA call type and CE (the CE whose scratchpad is the source or "
                               "destination). Times in cycles. The time a request waits for the shared engine from "
                               "the CE's call is in 'Prefetch iterations' (wait for engine).",
                               legend=V.legend("DMA requests", "slot wait", "service time", "B/cycle while serving",
                                               "done → sync"),
                               group=G, order=b + 10, key="CR-DMA · requests by type and CE"))
        cols = ["kernel", "phase", "cycles", "row Gets", "row Puts", "bytes", "bytes/cycle", "read bytes/cycle",
                "avg in flight", "active %", "Get → Get gap p50", "Get → Get gap mean", "latency p50", "latency p95"]
        rows = [[w["kernel"], w["window"], int(w["cycles"]), w["gets"], w["puts"], w["bytes"],
                 _r(w["bytes_per_cycle"], 3), _r(w["read_bytes_per_cycle"], 3), _r(w["avg_in_flight"], 3),
                 _r(100 * (w["active"] or 0), 3), w["get_interval_p50"], _r(w["get_interval_mean"], 3),
                 w["latency_p50"], w["latency_p95"]] for w in self.windows]
        note = (f"Transactions of requesters matching '{self.client}' on '{self.port}' (no CR-DMA probes in this "
                "trace)." if self.mode == "infer" else
                f"The engine's TileLink requests on '{self.mbus}': one Get per row chunk read, one Put per chunk "
                "written.") + \
            " A latency-bound engine shows avg in flight near 1 and a Get → Get gap close to the latency."
        out.append(Section("DMA engine throughput", cols, rows, note,
                           legend=V.legend("row Gets / Puts", "bytes/cycle", "avg in flight", "active %",
                                           "Get → Get gap", "latency p50 / p95"),
                           group=G, order=b + 11, key=f"CR-DMA · engine ({self.mode})"))
        if self.mode == "probe":
            out.append(Section("DMA engine busy per phase",
                               ["kernel", "phase", "requests", "engine busy %"],
                               [[w["kernel"], w["window"], w.get("requests"),
                                 _r(100 * (w.get("engine_busy") or 0), 3)] for w in self.windows],
                               legend=V.legend("engine busy %"), group=G, order=b + 12,
                               key="CR-DMA · requests per window"))
            if self.gate:
                tot = sum(self.gate.values()) or 1
                out.append(Section("Why the DMA engine waits",
                                   ["reason", "cycles", "% of cycles"],
                                   [[RtlName(V.DMA_GATE.get(k, str(k)), f"dma_gate = {k}"), round(v),
                                     _r(100 * v / tot, 3)] for k, v in self.gate.most_common()],
                                   "Per cycle of the whole trace, the reason the engine issued no request "
                                   "(probe dma_gate, group CR_DMA_detail). 'waiting for read data from the LLC' "
                                   "dominating = the engine is latency-bound: more requests in flight or larger "
                                   "transfers would help.",
                                   legend=V.legend("DMA wait reason"), group=G, order=b + 13,
                                   key="CR-DMA · why the next Get waits (dma_gate)"))
        if self.by_section:
            out.append(Section("DMA traffic by buffer / memory section",
                               ["kernel", "request", "buffer / section", "row requests", "bytes"],
                               [[r["kernel"], "read (Get)" if r["opcode"] == "Get" else f"write ({r['opcode']})",
                                 self.names.section(r["section"]), r["txns"], r["bytes"]] for r in self.by_section],
                               "A buffer read many times its size means the kernel re-fetches that data.",
                               group=G, order=b + 14, key="CR-DMA · traffic by kernel section"))
        if self.regions is not None and self.requests:
            by = defaultdict(lambda: [0, 0])
            for r in self.requests:
                c = by[(r["type"], r["src_section"], r["dst_section"])]
                c[0] += 1
                c[1] += r["bytes"]
            out.append(Section("DMA requests: source → destination",
                               ["type", "source", "destination", "requests", "bytes"],
                               [[t, self.names.section(s), self.names.section(d), n, b_]
                                for (t, s, d), (n, b_) in sorted(by.items(), key=lambda x: -x[1][1])],
                               group=G, order=b + 15, key="CR-DMA · requests by source → destination section"))
        return out + self._warn_section()

    def _warn_section(self) -> list[Section]:
        if not self.warnings:
            return []
        G, b = V.G_CHECK
        return [Section("Tool warnings · CR-DMA", ["warning"], [[w] for w in self.warnings], group=G, order=b + 12,
                        key="CR-DMA · warnings")]


def _bytes(n) -> str:
    return f"{n // 1024} KB" if n and n % 1024 == 0 else f"{n} B"
