"""rtltrace plugin: where each CE's time goes, by PC-marker region (phase).

A PC marker is a nop `addi x0, x0, id` placed in kernel or runtime code (PC_MARK_BEGIN(id) / PC_MARK_END(id), END =
id|1); regions nest. Markers are read from the RTL `ce_marker` probe (exact @RT time, instret and flops counters) or,
when the trace has no such probe, from the Rocket commit lines of the sim log (`log=`), aligned to @RT time per CE.

For every kernel window and CE the plugin reports, per region: cycles split by CeState (busy / suspended / idle),
instructions, IPC over busy cycles, flops per busy cycle, and a class:
  waiting            mostly suspended/idle, or busy but not retiring (IPC < idle_ipc)
  stall-bound        retiring slowly (IPC < stall_ipc): load-use, FPU dependencies, I$ misses, ...
  instruction-bound  retiring well but too few flops per cycle (< compute x peak): loads, address math, loop overhead
  compute-bound      near peak FMA rate
Busy time outside any region is `untagged`, suspended time outside any region `suspended`.

    python3 -m rtltrace report sim.log -o report.html --plugin ce_phases.py \
        --plugin-arg log=sim.log --plugin-arg launch=<workload>/launch.py [--plugin-arg markers=a.h,b.h]

Plugin args:
  markers=<h[,h...]>   C headers naming the marker ids (#define PCM_FOO 0x7e0). Default: *pc_markers*.h next to
                       launch=/elf= and the SDK runtime's CwithHyperOps/include/redefine_pc_markers.h
  log=<sim log>        commit-log fallback for traces without the ce_marker probe
  launch=<launch.py>   number of kernels (windows); from=/to= override the window
  macs_per_ce=<n>      FMAs per CE per cycle (default 1; peak flops/cycle = 2 x n)
  wait= idle_ipc= stall_ipc= compute=   class thresholds (defaults 0.5, 0.05, 0.6, 0.6)
  phases_json=<path>   write the tables as JSON
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from rtltrace.plugin import Plugin, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redefine_common import (IDLE_STATES, PhaseStats, _r, ce_label, ce_timelines, classify,  # noqa: E402
                             kernel_windows, load_workload, phase_analysis, phase_stats)


class CePhases(Plugin):
    name = "ce-phases"
    requires = ["CeState"]

    def setup(self, ctx):
        self.args = ctx.args
        self.warnings: list[str] = []
        self.kernels = [k.name for k in load_workload(ctx.args["launch"])] if ctx.args.get("launch") else []
        self.peak = 2 * int(ctx.args.get("macs_per_ce", 1))
        self.th = {k: float(ctx.args[k]) for k in ("wait", "idle_ipc", "stall_ipc", "compute") if k in ctx.args}
        self.rows: list[dict] = []
        self.source = None

    def derive(self, m, out):
        tls = ce_timelines(m)
        windows = kernel_windows(m, tls, max(1, len(self.kernels)), self.args, self.warnings)
        pa = phase_analysis(m, self.args, tls, self.warnings)
        self.source, self.names = pa["source"], pa["names"]
        if not self.source:
            self.warnings.append("no PC markers (no ce_marker probe and no marker commit lines in log=): "
                                 "only CeState time is shown")
        names = self.kernels + [f"kernel{i}" for i in range(len(self.kernels), len(windows))]
        for kname, (w0, w1) in zip(names, windows):
            total: dict[str, PhaseStats] = {}
            multi = len({k[0] for k in pa["phases"]}) > 1
            for k, segs in sorted(pa["phases"].items()):
                stats = phase_stats(segs, pa["points"].get(k, []), w0, w1)
                for lab, st in stats.items():
                    self.rows.append(self._row(kname, ce_label(k, multi), lab, st, w1 - w0))
                    t = total.setdefault(lab, PhaseStats())
                    t.cycles.update(st.cycles)
                    t.instret += st.instret
                    t.flops += st.flops
                    t.counted |= st.counted
            n = len(pa["phases"]) or 1
            for lab, st in total.items():
                self.rows.append(self._row(kname, "all", lab, st, n * (w1 - w0)))
        self._perfetto(m, out, pa)
        if self.args.get("phases_json"):
            Path(self.args["phases_json"]).write_text(json.dumps(self.summary(), indent=2))

    def _row(self, kernel, ce, label, st: PhaseStats, denom) -> dict:
        busy = st.busy
        return {"kernel": kernel, "ce": ce, "region": label, "cycles": st.total,
                "pct": 100 * st.total / denom if denom else None, "busy": busy,
                "suspended": st.cycles.get("Suspended", 0),
                "idle": sum(st.cycles.get(s, 0) for s in IDLE_STATES),
                "instret": st.instret if st.counted else None,
                "ipc": st.instret / busy if st.counted and busy else None,
                "flops": st.flops if st.counted else None,
                "flops_per_cycle": st.flops / busy if st.counted and busy else None,
                "class": classify(st, self.peak, self.th)}

    def _perfetto(self, m, out, pa):
        if not pa["source"]:
            return
        proc = out.process("CE phases", sort=4)
        multi = len({k[0] for k in pa["phases"]}) > 1
        for k, segs in sorted(pa["phases"].items()):
            lab = ce_label(k, multi)
            tr = out.track(proc, lab, sort=k[1])
            for s in segs:
                if s.label == "idle":
                    continue
                name = s.label + (" (suspended)" if s.state == "Suspended" and s.label != "suspended" else "")
                out.span(tr, s.t0, s.t1, name, {"state": s.state, "regions": " > ".join(s.path) or "–"})
            pts = pa["points"].get(k, [])
            ipc, fpc = [], []
            for (ta, ia, fa), (tb, ib, fb) in zip(pts, pts[1:]):
                if tb > ta:
                    ipc.append((ta, round(((ib - ia) % (1 << 32)) / (tb - ta), 3)))
                    fpc.append((ta, round((((fb or 0) - (fa or 0)) % (1 << 32)) / (tb - ta), 3)))
            if ipc:
                out.counter(proc, f"{lab} IPC", ipc)
                out.counter(proc, f"{lab} flops/cycle", fpc)

    def summary(self) -> dict:
        return {"source": self.source, "markers": {f"0x{k:03x}": v for k, v in sorted(self.names.items())},
                "rows": self.rows, "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        cols = ["kernel", "CE", "region", "cycles", "% of window", "busy", "suspended", "idle/launch", "instructions",
                "IPC", "flops/cycle", "class"]

        def fmt(r):
            return [r["kernel"], r["ce"], r["region"], round(r["cycles"]), _r(r["pct"], 3), round(r["busy"]),
                    round(r["suspended"]), round(r["idle"]), r["instret"], _r(r["ipc"], 3),
                    _r(r["flops_per_cycle"], 3), r["class"]]

        src = {"probe": "ce_marker probe", "commit log": "commit-log markers (approximate)"}.get(self.source,
                                                                                               "no markers")
        allr = sorted((r for r in self.rows if r["ce"] == "all"), key=lambda r: (r["kernel"], -r["cycles"]))
        per = sorted((r for r in self.rows if r["ce"] != "all"), key=lambda r: (r["kernel"], r["ce"], -r["cycles"]))
        out = [Section("CE phases · all CEs (CE-cycles)", cols, [fmt(r) for r in allr],
                       f"Source: {src}. IPC and flops/cycle are over busy cycles; peak flops/cycle = {self.peak}. "
                       "% is of N_CE x window."),
               Section("CE phases · per CE", cols, [fmt(r) for r in per])]
        if self.warnings:
            out.append(Section("CE phases · warnings", ["warning"], [[w] for w in self.warnings]))
        return out
