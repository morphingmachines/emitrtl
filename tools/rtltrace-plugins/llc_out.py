"""rtltrace plugin: what crosses the D_D boundary (LLC_out), address by address.

Every A-channel transaction on the boundary port is attributed to the kernel's launch buffers (A, W, Z, ... with
their host addresses) or, outside them, to its kernel section and to contiguous address ranges. Per kernel window
the in-window bytes are reconciled with D_D the way redefine_metrics.py counts it (same port, same window test, same
opcode rules), so the tables say which part of ρ = D_D / (D_i + D_o) is the algorithm's data and which is not.

    python3 -m rtltrace report sim.log -o report.html --plugin llc_out.py --plugin-arg log=sim.log \\
        [--plugin-arg launch=<launch.py>] [--regions kernel_sections.json]

Buffer addresses: redefine_common.buffer_tables() -- the `[kernel_buffers]` blocks the sim's host driver prints into
the log (one per kernel launch; format in kernel-exec-environment/README.md, "Buffer address table"), else buffers=,
else a buffers.txt (with a warning: it may belong to a later run).

Plugin args:
  log=<sim log>            where the buffer table is read from (redefine_report.py passes it)
  buffers=<file>[,...]     buffer table file(s), one per kernel, instead of the log
  boundary=<port glob>     default LLC?out (as redefine_metrics.py)
  line=<bytes>             line size for unique lines / contiguous ranges (default 32)
  from= / to=              manual kernel window (as redefine_metrics.py)
  llc_out_json=<path>      write the summary as JSON
  llc_out_csv=<path>       write every boundary transaction as CSV (time, opcode, address, bytes, buffer, section, ...)
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from fnmatch import fnmatch
from pathlib import Path

from rtltrace.plugin import HW_GROUP, Plugin, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (_r, buffer_tables, ce_timelines, kernel_windows, load_workload,  # noqa: E402
                             plugin_regions, section_of)
from redefine_metrics import BOTH_OPS, OUT_OPS  # noqa: E402

OTHER = "other"


def buffer_of(bufs, addr: int) -> str:
    for b in bufs:
        if b.start <= addr <= b.end:
            return b.name
    return OTHER


def line_ranges(addrs, line: int) -> list[dict]:
    """Contiguous runs of line addresses: [{from, to, lines, txns}], `to` inclusive (last byte)."""
    cnt = Counter(a - a % line for a in addrs)
    u = sorted(cnt)
    out = []
    if not u:
        return out
    s = p = u[0]
    for a in u[1:] + [None]:
        if a is not None and a == p + line:
            p = a
            continue
        out.append({"from": s, "to": p + line - 1, "lines": (p - s) // line + 1,
                    "txns": sum(cnt[x] for x in range(s, p + 1, line))})
        if a is not None:
            s = p = a
    return out


def _dir(opcode: str) -> str:
    return "out" if opcode in OUT_OPS else "in+out" if opcode in BOTH_OPS else "in"


def _dd_bytes(t) -> int:
    """Bytes the transaction adds to D_D (atomics count in both directions)."""
    return 2 * t.nbytes if t.opcode in BOTH_OPS else t.nbytes


class LlcOut(Plugin):
    name = "llc-out"
    requires = ["CeState"]

    def setup(self, ctx):
        a = ctx.args
        self.args = a
        self.boundary = a.get("boundary", "LLC?out")
        self.port_label = self.boundary
        self.line = int(a.get("line", 32))
        self.kernels = [k.name for k in load_workload(a["launch"])] if a.get("launch") else []
        self.regions = plugin_regions(ctx)
        self.warnings: list[str] = []
        self.results: list[dict] = []
        self.txn_rows: list[dict] = []
        self.buffer_sets, self.buffer_source = buffer_tables(a, self.warnings)
        if not self.buffer_sets:
            self.warnings.append("no buffer addresses (no [kernel_buffers] table in the log, no buffers= / "
                                 "buffers.txt): every transaction is attributed to a kernel section only")

    def _bufs(self, k: int):
        return self.buffer_sets[min(k, len(self.buffer_sets) - 1)] if self.buffer_sets else []

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        ports = [p for p in m.ports.values() if fnmatch(p.name, self.boundary) or
                 fnmatch(f"{p.inst}/{p.name}", self.boundary)]
        if not ports:
            self.warnings.append(f"no TileLink port matches boundary '{self.boundary}'")
        self.port_label = ", ".join(sorted({p.name for p in ports})) or self.boundary
        txns = sorted((t for t in m.txns(self.boundary) if t.kind == "A"), key=lambda t: t.start)
        windows = kernel_windows(m, ce_timelines(m), max(1, len(self.kernels)), self.args, self.warnings)
        names = self.kernels + [f"kernel{i}" for i in range(len(self.kernels), len(windows))]
        if len(self.buffer_sets) > 1 and len(self.buffer_sets) != len(windows):
            self.warnings.append(f"{len(self.buffer_sets)} buffer tables for {len(windows)} kernel windows")
        if not windows and txns:
            self.warnings.append("no kernel window (no CE left Idle): nothing attributed")

        for k, (kname, (w0, w1)) in enumerate(zip(names, windows)):
            bufs = self._bufs(k)
            self.results.append(self._measure(kname, w0, w1, txns, bufs))
            for t in txns:
                if (k == 0 and t.start < w0) or w0 <= t.start <= w1 or (k == len(windows) - 1 and t.start > w1):
                    self.txn_rows.append({
                        "kernel": kname, "window": "before" if t.start < w0 else "after" if t.start > w1 else "in",
                        "start": t.start, "opcode": t.opcode, "address": f"0x{t.address:08x}", "bytes": t.nbytes,
                        "dir": _dir(t.opcode), "buffer": buffer_of(bufs, t.address),
                        "section": section_of(self.regions, t.address), "client": t.client, "done": t.done})
        self._perfetto(out, txns)
        if self.args.get("llc_out_json"):
            Path(self.args["llc_out_json"]).write_text(json.dumps(self.summary(), indent=2))
        if self.args.get("llc_out_csv") and self.txn_rows:
            with open(self.args["llc_out_csv"], "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(self.txn_rows[0]))
                w.writeheader()
                w.writerows(self.txn_rows)

    def _measure(self, kname, w0, w1, txns, bufs) -> dict:
        inw = [t for t in txns if w0 <= t.start <= w1]
        dd_in = sum(t.nbytes for t in inw if _dir(t.opcode) != "out")
        dd_out = sum(t.nbytes for t in inw if _dir(t.opcode) != "in")
        dd = dd_in + dd_out
        d_i = sum(b.size for b in bufs if b.dir in ("in", "inout"))
        d_o = sum(b.size for b in bufs if b.dir in ("out", "inout"))

        agg = defaultdict(lambda: {"txns": 0, "bytes": 0, "lines": Counter()})
        for t in inw:
            g = agg[(buffer_of(bufs, t.address), t.opcode)]
            g["txns"] += 1
            g["bytes"] += _dd_bytes(t)
            g["lines"][t.address - t.address % self.line] += 1
        by_buffer = []
        for b in list(bufs) + [None]:
            bn = b.name if b else OTHER
            for (name, op), g in sorted(agg.items()):
                if name != bn:
                    continue
                uniq = len(g["lines"])
                by_buffer.append({
                    "buffer": bn, "range": f"0x{b.start:08x}-0x{b.end:08x}" if b else "–",
                    "buffer_bytes": b.size if b else None, "opcode": op, "txns": g["txns"], "bytes": g["bytes"],
                    "pct_txns": 100 * g["txns"] / len(inw) if inw else None,
                    "pct_bytes": 100 * g["bytes"] / dd if dd else None, "unique_lines": uniq,
                    "coverage": uniq * self.line / b.size if b and b.size else None,
                    "max_per_line": max(g["lines"].values())})
        buf_bytes = sum(_dd_bytes(t) for t in inw if buffer_of(bufs, t.address) != OTHER)

        oth = defaultdict(list)
        for t in inw:
            if buffer_of(bufs, t.address) == OTHER:
                oth[(section_of(self.regions, t.address) or "–", t.opcode)].append(t)
        other_sections, other_ranges = [], []
        for (sec, op), ts in sorted(oth.items(), key=lambda kv: min(t.address for t in kv[1])):
            rr = line_ranges([t.address for t in ts], self.line)
            other_sections.append({"section": sec, "opcode": op, "txns": len(ts),
                                   "bytes": sum(_dd_bytes(t) for t in ts),
                                   "unique_lines": len({t.address - t.address % self.line for t in ts}),
                                   "ranges": len(rr), "span": f"0x{rr[0]['from']:08x}-0x{rr[-1]['to']:08x}"})
            other_ranges += [{"section": sec, "opcode": op, "from": f"0x{x['from']:08x}", "to": f"0x{x['to']:08x}",
                              "lines": x["lines"], "txns": x["txns"]} for x in rr]
        other_sections.sort(key=lambda r: -r["bytes"])
        return {"kernel": kname, "window": [w0, w1], "boundary": self.port_label, "txns": len(inw),
                "bytes": sum(t.nbytes for t in inw),
                "txns_before": sum(t.start < w0 for t in txns), "txns_after": sum(t.start > w1 for t in txns),
                "D_D_before_window": sum(t.nbytes for t in txns if t.start < w0),
                "D_D_after_window": sum(t.nbytes for t in txns if t.start > w1),
                "D_D_in": dd_in, "D_D_out": dd_out, "D_D": dd, "D_i": d_i, "D_o": d_o,
                "rho": dd / (d_i + d_o) if d_i + d_o else None,
                "D_D_buffers": buf_bytes, "D_D_other": dd - buf_bytes,
                "rho_buffers_only": buf_bytes / (d_i + d_o) if d_i + d_o else None,
                "buffers": [{"name": b.name, "arg": b.arg, "dir": b.dir, "start": f"0x{b.start:08x}",
                             "end": f"0x{b.end:08x}", "size": b.size} for b in bufs],
                "by_buffer": by_buffer, "other_sections": other_sections, "other_ranges": other_ranges}

    def _perfetto(self, out, txns):
        if not txns or not self.results:
            return
        proc = out.process(f"{self.port_label} (off-chip boundary, D_D)", sort=6)
        bufs = self._bufs(0)
        cum = Counter()
        pts = defaultdict(list)
        for t in txns:
            k = buffer_of(bufs, t.address)
            if k == OTHER and self.regions is not None:
                k = f"other: {(section_of(self.regions, t.address) or '–').split('[')[0]}"
            cum[k] += _dd_bytes(t)
            pts[k].append((t.start, cum[k]))
        for k in sorted(pts):
            out.counter(proc, f"cumulative bytes · {k}", pts[k])

    # ------------------------------------------------------------------ outputs
    def summary(self) -> dict:
        return {"boundary": self.port_label, "line": self.line, "buffer_source": self.buffer_source,
                "kernels": self.results, "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        G, b = V.G_EFF  # after redefine_metrics' "Off-chip traffic by buffer / memory section" (b + 2)
        src = self.buffer_source or "–"
        out = [
            Section("Off-chip traffic: kernel buffers vs everything else",
                    ["kernel", "off-chip total", "in kernel buffers", "outside buffers", "D_i + D_o", "ρ",
                     "ρ if only the buffers crossed", "transactions", "before window", "after window"],
                    [[r["kernel"], r["D_D"], r["D_D_buffers"], r["D_D_other"], r["D_i"] + r["D_o"], _r(r["rho"]),
                      _r(r["rho_buffers_only"]), r["txns"], r["txns_before"], r["txns_after"]]
                     for r in self.results],
                    f"Bytes of A-channel transactions on '{self.port_label}' that start inside the kernel window, "
                    f"counted as for ρ. Buffer addresses from {src}; D_i / D_o = sizes of the in / out buffers.",
                    legend={"ρ if only the buffers crossed": "(bytes inside the kernel buffers) / (D_i + D_o): 1.0 "
                                                             "means every buffer byte crossed once; the gap to ρ is "
                                                             "traffic that is not the kernel's data"},
                    group=G, order=b + 2.1, key="LLC_out · D_D by buffer"),
            Section("Off-chip transactions per kernel buffer",
                    ["kernel", "buffer", "address range", "size", "opcode", "transactions", "bytes", "% of off-chip",
                     "unique lines", "coverage", "max per line"],
                    [[r["kernel"], x["buffer"], x["range"], x["buffer_bytes"], x["opcode"], x["txns"], x["bytes"],
                      _r(x["pct_bytes"], 3), x["unique_lines"], _r(x["coverage"], 3), x["max_per_line"]]
                     for r in self.results for x in r["by_buffer"]],
                    "Every off-chip transaction whose address falls in a kernel buffer, and the rest as 'other'.",
                    legend={"coverage": f"unique {self.line}-B lines touched × line size / buffer size (1 = every "
                                        "line of the buffer)",
                            "max per line": "most times one line crossed the boundary (1 = no re-fetch)"},
                    group=G, order=b + 2.2, key="LLC_out · transactions per buffer"),
        ]
        rows = [[r["kernel"], s["section"], s["opcode"], s["txns"], s["bytes"], s["unique_lines"], s["ranges"],
                 s["span"]] for r in self.results for s in r["other_sections"]]
        if rows:
            out.append(Section("Off-chip traffic outside the kernel buffers, by memory section",
                               ["kernel", "section", "opcode", "transactions", "bytes", "unique lines",
                                "address ranges", "span"], rows,
                               "Traffic that is not the kernel's input or output data: runtime / context state, code "
                               "fetch, staging copies in group-local memory, ... Every range is listed in the "
                               "hardware-detail appendix and in <stem>.llc_out.json / .csv.",
                               group=G, order=b + 2.3, key="LLC_out · outside the launch buffers by section"))
            out.append(Section("LLC_out address ranges outside the kernel buffers",
                               ["kernel", "section", "opcode", "from", "to", "lines", "transactions"],
                               [[r["kernel"], x["section"], x["opcode"], x["from"], x["to"], x["lines"], x["txns"]]
                                for r in self.results for x in r["other_ranges"]],
                               "Contiguous runs of line addresses (to = last byte).",
                               group=HW_GROUP, order=930, level="hw",
                               key="LLC_out · address ranges outside the launch buffers"))
        if self.warnings:
            G, b = V.G_CHECK
            out.append(Section("Off-chip traffic attribution: warnings", ["warning"], [[w] for w in self.warnings],
                               group=G, order=b + 20, key="LLC_out · warnings"))
        return out
