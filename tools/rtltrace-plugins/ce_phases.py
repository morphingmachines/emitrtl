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
  wait= idle_ipc= stall_ipc= compute_frac=   class thresholds (defaults 0.5, 0.05, 0.6, 0.6); compute= is the
                       compute-region list of redefine_metrics / overlap_metrics (a number there is still read as
                       compute_frac)
  phases_json=<path>   write the tables as JSON
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from rtltrace.plugin import Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (IDLE_STATES, PhaseStats, _r, ce_label, ce_timelines, classify,  # noqa: E402
                             kernel_windows, limited_by, load_workload, mix_cycles, phase_analysis, phase_stats,
                             publish, window_names)


class CePhases(Plugin):
    name = "ce-phases"
    requires = ["CeState"]
    # ce_marker fields shown as marker names and decimal counters (Perfetto slices, text view)
    fields = {"ce_marker/imm": "pc_mark", "ce_marker/instret": "count", "ce_marker/flops": "count",
              "ce_marker/hart": "count"}
    tags = {"pc_mark", "count"}

    def setup(self, ctx):
        self.args = ctx.args
        self.warnings: list[str] = []
        self.kernels = [k.name for k in load_workload(ctx.args["launch"])] if ctx.args.get("launch") else []
        self.peak = 2 * int(ctx.args.get("macs_per_ce", 1))
        a = ctx.args
        self.th = {k: float(a[arg]) for k, arg in (("wait", "wait"), ("idle_ipc", "idle_ipc"), ("stall_ipc", "stall_ipc"),
                                                    ("compute", "compute_frac")) if arg in a}
        if "compute_frac" not in a and a.get("compute", "").replace(".", "", 1).isdigit():
            self.th["compute"] = float(a["compute"])  # before compute_frac existed; compute=<regions> is not a number
        self.rows: list[dict] = []
        self.source = None
        self.names: dict[int, str] = {}
        self.headers: list[str] = []

    def annotate(self, f, value, ev):
        if f.name == "imm":
            n = self.names.get(value & ~1)
            what = "END" if value & 1 else "BEGIN"
            return (f"{what} {V.region_name(n)}" if n else f"{what} 0x{value & ~1:03x}",
                    f"{what} {n or 'unnamed marker'} (id 0x{value:03x})")
        return str(value), str(value)

    def derive(self, m, out):
        tls = ce_timelines(m)
        windows = kernel_windows(m, tls, max(1, len(self.kernels)), self.args, self.warnings)
        pa = phase_analysis(m, self.args, tls, self.warnings)
        self.source, self.names, self.headers = pa["source"], pa["names"], pa["headers"]
        if not self.source:
            self.warnings.append("no PC markers (no ce_marker probe and no marker commit lines in log=): "
                                 "only CeState time is shown")
        names = window_names(self.kernels, len(windows))
        for kname, (w0, w1) in zip(names, windows):
            total: dict[str, PhaseStats] = {}
            multi = len({k[0] for k in pa["phases"]}) > 1
            for k, segs in sorted(pa["phases"].items()):
                stats = phase_stats(segs, pa["points"].get(k, []), w0, w1, pa["mix"].get(k))
                for lab, st in stats.items():
                    self.rows.append(self._row(kname, ce_label(k, multi), lab, st, w1 - w0))
                    t = total.setdefault(lab, PhaseStats())
                    t.cycles.update(st.cycles)
                    t.instret += st.instret
                    t.flops += st.flops
                    t.mix.update(st.mix)
                    t.counted |= st.counted
            n = len(pa["phases"]) or 1
            for lab, st in total.items():
                self.rows.append(self._row(kname, "all", lab, st, n * (w1 - w0)))
        self._perfetto(m, out, pa)
        publish(m, "phases", self.summary())
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
                "class": classify(st, self.peak, self.th), "limited_by": limited_by(st, self.peak, self.th),
                "mix": mix_cycles(st)}

    def _perfetto(self, m, out, pa):
        if not pa["source"]:
            return
        proc = out.process("CE code regions (PC_MARK)", sort=2)
        multi = len({k[0] for k in pa["phases"]}) > 1
        for k, segs in sorted(pa["phases"].items()):
            lab = ce_label(k, multi)
            tr = out.track(proc, lab, sort=k[1])
            for s in segs:
                if s.label == "idle":
                    continue
                name = V.region_name(s.label) + (" (suspended)" if s.state == "Suspended" and s.label != "suspended"
                                                 else "")
                out.span(tr, s.t0, s.t1, name, {"state": s.state, "region": s.label,
                                                "nesting": " > ".join(V.region_name(x) for x in s.path) or "–",
                                                "probe": "ce_marker + CeState"})
            pts = pa["points"].get(k, [])
            ipc, fpc = [], []
            for (ta, ia, fa), (tb, ib, fb) in zip(pts, pts[1:]):
                if tb > ta:
                    ipc.append((ta, round(((ib - ia) % (1 << 32)) / (tb - ta), 3)))
                    fpc.append((ta, round((((fb or 0) - (fa or 0)) % (1 << 32)) / (tb - ta), 3)))
            if ipc:
                ipc.append((pts[-1][0], 0))  # unknown after the last marker
                fpc.append((pts[-1][0], 0))
                out.counter(proc, f"{lab} IPC", ipc)
                out.counter(proc, f"{lab} flops/cycle", fpc)

    def summary(self) -> dict:
        return {"source": self.source, "markers": {f"0x{k:03x}": v for k, v in sorted(self.names.items())},
                "headers": self.headers, "rows": self.rows, "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        cols = ["kernel", "CE", "region", "cycles", "% of window", "busy", "suspended", "idle / launch",
                "instructions", "IPC", "flops/cycle", "limited by"]

        def fmt(r):
            return [r["kernel"], r["ce"], RtlName(V.region_name(r["region"]), r["region"]), round(r["cycles"]),
                    _r(r["pct"], 3), round(r["busy"]), round(r["suspended"]), round(r["idle"]), r["instret"],
                    _r(r["ipc"], 3), _r(r["flops_per_cycle"], 3),
                    RtlName(V.LIMITED_BY[r["limited_by"]], r["class"]) if r["limited_by"] else "–"]

        src = {"probe": "ce_marker probe", "commit log": "commit-log markers (approximate)"}.get(self.source,
                                                                                               "no markers")
        th = {"wait": 0.5, "idle_ipc": 0.05, "stall_ipc": 0.6, "compute": 0.6, **self.th}
        lg = V.legend("region (PC_MARK)", "busy / suspended / idle", "instructions", "IPC", "flops/cycle")
        lg["limited by"] = (f"waiting: > {th['wait']:.0%} of the region suspended or idle · blocked in hardware: "
                            f"busy with IPC < {th['idle_ipc']} (e.g. a flush or fence) · stall-limited: IPC < "
                            f"{th['stall_ipc']} · control code: no FP work · instruction-limited: flops/cycle < "
                            f"{th['compute']:.0%} of peak ({self.peak}) · compute-bound: otherwise")
        allr = sorted((r for r in self.rows if r["ce"] == "all"), key=lambda r: (r["kernel"], -r["cycles"]))
        per = sorted((r for r in self.rows if r["ce"] != "all"), key=lambda r: (r["kernel"], r["ce"], -r["cycles"]))
        G, b = V.G_REGION
        out = [Section("Code regions · all CEs", cols, [fmt(r) for r in allr],
                       f"Source: {src}. Cycles are CE-cycles (all CEs added; % is of N_CE × kernel window). IPC and "
                       f"flops/cycle are over busy cycles; peak flops/cycle = {self.peak}. Time goes to the innermost "
                       "open region.", legend=lg, group=G, order=b, key="CE phases · all CEs (CE-cycles)"),
               Section("Code regions · per CE", cols, [fmt(r) for r in per], "% is of the kernel window.",
                       group=G, order=b + 1, key="CE phases · per CE")]
        mixr = [r for r in allr if r["mix"] and r["busy"]]
        if mixr:
            mc = ("fma", "fp_other", "load", "store", "other", "stall")
            out.append(Section(
                "Busy cycles by instruction type · all CEs",
                ["kernel", "region", "busy cycles", "FMA", "other FP arithmetic", "loads", "stores",
                 "other instructions", "no instruction retired", "FMA % of busy"],
                [[r["kernel"], RtlName(V.region_name(r["region"]), r["region"]), round(r["busy"])] +
                 [round(r["mix"][k]) for k in mc] + [_r(100 * r["mix"]["fma"] / r["busy"], 3)] for r in mixr],
                "Busy CE-cycles of each region split by what the CE retired in them: one cycle per retired "
                "instruction (single-issue core), the classes from the instruction words of the commit lines in "
                "the sim log, and the cycles in which nothing retired. The columns add up to 'busy cycles'.",
                legend=V.legend("FMA instructions", "no instruction retired"), group=G, order=b + 0.5,
                key="CE phases · busy cycles by instruction type"))
        if self.warnings:
            G, b = V.G_CHECK
            out.append(Section("Tool warnings · code regions", ["warning"], [[w] for w in self.warnings],
                               group=G, order=b + 11, key="CE phases · warnings"))
        return out
