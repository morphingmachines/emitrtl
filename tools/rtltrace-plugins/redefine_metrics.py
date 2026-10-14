"""rtltrace plugin: REDEFINE feature metrics from an RTL simulation trace.

Implements the two metrics of "REDEFINE Feature Metrics" (doc dated 01.07.2026):

  Compute element utilization efficiency   γe = Ci / Co     Ci = T_ops / (2 · N_CE)    Co = t_i · F_i
  Data-reuse index                          ρ  = D_D / (D_i + D_o)
  Data-memory penalty index                 φ  = D_D / M_i

Where each quantity comes from:
  T_ops     KernelLaunch(t_ops=...) in the workload's launch.py (or --plugin-arg t_ops=)
  D_i, D_o  sizes of the IN / OUT / INOUT buffer args in launch.py (INOUT counts toward both)
  N_CE      CEs present in the trace (one CeState probe each) x macs_per_ce (Rocket FPU: one FMA per CE)
  Co        kernel window in cycles: first CE leaving Idle -> last CE back to Idle (CeState probes).
            The trace time axis is already in cycles, so F_i only scales the reported t_i.
  D_D       bytes of TileLink A-channel transactions on the device-boundary port(s) that start inside the window.
            Default boundary LLC_out: in the 1-CR sim everything behind the LLC stands in for off-chip memory.
  M_i       LLC size (parameters.md "CR L2 Cache Configuration: Size is N KB") x CRs in the trace

    python3 -m rtltrace report sim.log -o report.html --plugin redefine_metrics.py \
        --plugin-arg launch=<workload>/launch.py \
        --plugin-arg params=<sdk>/generated_sv_dir/<config>/parameters.md \
        [--plugin-arg log=sim.log] [--plugin-arg json=metrics.json]

Other plugin args:
  boundary=<port glob>   device <-> external memory port(s) (default LLC?out)
  attrib=<port glob>     port whose clients are shown as the cause of the traffic (default LLC?in)
  t_ops=<n[,n...]>       per-kernel T_ops override        d_in=<bytes> d_out=<bytes>  (when no launch file)
  n_ce=<n>  macs_per_ce=<n>  m_i=<bytes>  freq_mhz=<f> (default 100, the testbench clock)
  from=<cycle> to=<cycle>   manual kernel window          tol=<fraction> (cross-check tolerance, default 0.1)
  markers=<h[,h...]>     PC-marker name headers for the waterfall (see ce_phases.py; default: found next to launch=)
  compute=<region,...>   regions counted as compute in the waterfall (default: regions retiring >= 0.1 flops/cycle)

Besides γe/ρ/φ the plugin splits each kernel window into sub-windows (setup until every CE is active, steady, tail
after the first CE has finished), lists the LLC CMU commands (flushes) inside it, and builds a waterfall of the
N_CE x Co CE-cycles: idle -> suspended -> busy outside compute regions -> compute below peak -> ideal (Ci x N_CE),
with the γe reachable if each loss (and the ones above it) were removed. With PC markers (ce_marker probe, or the
commit log in log=) busy time is split by region; without them it is one "busy" step.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from fnmatch import fnmatch
from pathlib import Path

from rtltrace.plugin import Plugin, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redefine_common import (IDLE_STATES, Kernel, _overlap, _r, _ratio, _region, ce_timelines,  # noqa: E402
                             cmu_spans, count_retired, counters_in, is_data_section, kernel_windows, load_workload,
                             parse_params, phase_analysis, phase_stats, plugin_regions, section_of, sub_windows)

CMU_OPS = {0: "FLUSH line", 1: "CLEAN line", 2: "FLUSH_ALL", 3: "CLEAN_ALL", 4: "FLUSH range", 5: "CLEAN range"}

OUT_OPS = {"PutFullData", "PutPartialData"}  # device -> external memory
BOTH_OPS = {"ArithmeticData", "LogicalData"}  # atomics carry data both ways; everything else reads


# ---------------------------------------------------------------------------------------------- plugin

class RedefineMetrics(Plugin):
    name = "redefine-metrics"
    requires = ["CeState"]

    def setup(self, ctx):
        a = ctx.args
        self.args = a
        self.warnings: list[str] = []
        self.kernels = load_workload(a["launch"]) if a.get("launch") else []
        if not self.kernels:
            args = [(None, int(a.get("d_in", 0)), "in"), (None, int(a.get("d_out", 0)), "out")]
            self.kernels = [Kernel("kernel", None, a.get("op_kind", "fp"),
                                   args if ("d_in" in a or "d_out" in a) else [])]
        if a.get("t_ops"):
            for k, v in zip(self.kernels, a["t_ops"].split(",")):
                k.t_ops = int(float(v))
        self.hw = parse_params(a["params"]) if a.get("params") else {}
        self.regions = plugin_regions(ctx)
        self.boundary = a.get("boundary", "LLC?out")
        self.attrib = a.get("attrib", "LLC?in")
        self.freq_mhz = float(a.get("freq_mhz", 100))
        self.macs_per_ce = int(a.get("macs_per_ce", 1))
        self.tol = float(a.get("tol", 0.1))
        rt = count_retired(a["log"]) if a.get("log") else None
        self.retired = dict(rt) if rt else None
        self.results: list[dict] = []
        self.config: dict = {}
        self.pa = None

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        ces = ce_timelines(m)
        windows = kernel_windows(m, ces, len(self.kernels), self.args, self.warnings)
        for inst, segs in ces.items():
            if segs and segs[-1][2] != "Idle" and segs[-1][1] >= m.end:
                self.warnings.append(f"{m.trace.module_short(inst)} is still {segs[-1][2]} at the end of the trace; "
                                     "the kernel window is cut short")
        if not windows:
            self.warnings.append("no CE ever left Idle: no kernel window")
        if len(self.kernels) != len(windows):
            self.kernels = (self.kernels + [Kernel(f"kernel{i}", None, "fp") for i in range(len(windows))])[
                :len(windows)]

        ports = [p for p in m.ports.values() if fnmatch(p.name, self.boundary) or fnmatch(f"{p.inst}/{p.name}",
                                                                                          self.boundary)]
        if not ports:
            self.warnings.append(f"no TileLink port matches boundary '{self.boundary}': D_D is not measured")
        bt = sorted((t for t in m.txns(self.boundary) if t.kind == "A"), key=lambda t: t.start)
        at = [t for t in m.txns(self.attrib) if t.kind == "A"]

        n_ces_seen = len(ces)
        n_cr = max(1, round(n_ces_seen / self.hw["n_ces"])) if self.hw.get("n_ces") else 1
        if self.hw.get("n_ces") and n_ces_seen % self.hw["n_ces"]:
            self.warnings.append(f"{n_ces_seen} CEs in the trace is not a multiple of nCEs={self.hw['n_ces']}")
        n_ce = int(self.args["n_ce"]) if self.args.get("n_ce") else n_ces_seen * self.macs_per_ce
        if self.args.get("m_i"):
            m_i = int(float(self.args["m_i"]))
        elif self.hw.get("llc_bytes"):
            m_i = self.hw["llc_bytes"] * n_cr
        else:
            m_i = None
            self.warnings.append("M_i unknown: pass params=<parameters.md> or m_i=<bytes>")
        self.config = {"boundary": self.boundary, "boundary_ports": [f"{p.inst}/{p.name}" for p in ports],
                       "n_ce_in_trace": n_ces_seen, "n_cr": n_cr, "macs_per_ce": self.macs_per_ce, "N_CE": n_ce,
                       "M_i": m_i, "F_MHz": self.freq_mhz, "hw": self.hw, "launch": self.args.get("launch")}

        proc = out.process("REDEFINE metrics", sort=5)
        wtrack = out.track(proc, "kernel window")
        strack = out.track(proc, "sub-windows", sort=1)
        ctrack = out.track(proc, "LLC CMU", sort=2)
        self.pa = phase_analysis(m, self.args, ces, self.warnings)
        cmus = cmu_spans(m)
        for k, (w0, w1) in zip(self.kernels, windows):
            r = self._measure(k, w0, w1, bt, at, ces, n_ce, m_i)
            r["sub_windows"] = self._sub_windows(ces, w0, w1)
            r["cmu"] = [{"op": CMU_OPS.get(op, op), "from": a, "to": b, "cycles": b - a}
                        for a, b, op in cmus if b > w0 and a < w1]
            r["waterfall"] = self._waterfall(r, ces, w0, w1)
            for sw in r["sub_windows"]:
                out.span(strack, sw["from"], sw["to"], sw["name"],
                         {key: v for key, v in sw.items() if key not in ("from", "to")})
            for c in r["cmu"]:
                out.span(ctrack, c["from"], c["to"], f"CMU {c['op']}", {"cycles": c["cycles"]})
            self.results.append(r)
            g = f"{r['gamma_e']:.3g}" if r["gamma_e"] is not None else "–"
            rho = f"{r['rho']:.3g}" if r["rho"] is not None else "–"
            phi = f"{r['phi']:.3g}" if r["phi"] is not None else "–"
            out.span(wtrack, w0, w1, f"{k.name}: γe={g} ρ={rho} φ={phi}",
                     {key: v for key, v in r.items() if not isinstance(v, (list, dict))})
        self._counters(m, out, proc, bt, ces)
        self._check_retired()
        for r in self.results:
            self._sanity(r)
        if self.args.get("json"):
            Path(self.args["json"]).write_text(json.dumps(self.summary(), indent=2))

    def _rg(self, t) -> str:
        """Kernel section of a boundary transaction (with --regions), else the port's manager address set."""
        return section_of(self.regions, t.address) if self.regions is not None else _region(t)

    def _measure(self, k: Kernel, w0, w1, bt, at, ces, n_ce, m_i) -> dict:
        d_in = d_out = before = after = partial = unfinished = 0
        regions = Counter()
        for t in bt:
            if t.start < w0:
                before += t.nbytes
                continue
            if t.start > w1:
                after += t.nbytes
                continue
            n = t.nbytes
            rg = self._rg(t)
            if t.opcode in OUT_OPS:
                d_out += n
                regions[(rg, "out")] += n
            else:
                d_in += n
                regions[(rg, "in")] += n
                if t.opcode in BOTH_OPS:
                    d_out += n
                    regions[(rg, "out")] += n
            partial += t.opcode == "PutPartialData"
            unfinished += not t.done
        attrib = defaultdict(lambda: [0, 0])
        for t in at:
            if w0 <= t.start <= w1:
                attrib[t.client][1 if t.opcode in OUT_OPS else 0] += t.nbytes
        ce_time = {}
        for inst, segs in ces.items():
            c = Counter()
            for t0, t1, st in segs:
                c[st] += _overlap(t0, t1, w0, w1)
            ce_time[inst] = dict(c)
        co = w1 - w0
        ci = k.t_ops / (2 * n_ce) if k.t_ops is not None and n_ce else None
        dd = d_in + d_out
        return {
            "kernel": k.name, "window": [w0, w1], "T_ops": k.t_ops, "op_kind": k.op_kind, "N_CE": n_ce,
            "C_i": ci, "C_o": co, "F_MHz": self.freq_mhz, "t_i_us": co / self.freq_mhz, "gamma_e": _ratio(ci, co),
            "D_i": k.d_in, "D_o": k.d_out, "D_D_in": d_in, "D_D_out": d_out, "D_D": dd,
            "rho": _ratio(dd, k.d_in + k.d_out), "M_i": m_i, "phi": _ratio(dd, m_i),
            "D_D_before_window": before, "D_D_after_window": after, "partial_puts": partial,
            "unfinished": unfinished,
            "regions": [{"region": rg, "dir": d, "bytes": b} for (rg, d), b in regions.most_common()],
            "D_D_data": sum(b for (rg, _), b in regions.items() if is_data_section(rg))
            if self.regions is not None else None,
            "attribution": [{"client": c, "in": v[0], "out": v[1]}
                            for c, v in sorted(attrib.items(), key=lambda x: -sum(x[1]))],
            "ce_time": ce_time,
        }

    def _points(self) -> dict:
        return self.pa["points"] if self.pa else {}

    def _sub_windows(self, ces, w0, w1) -> list[dict]:
        out = []
        n = len(ces) or 1
        for name, a, b in sub_windows(ces, w0, w1):
            act = sum(_overlap(t0, t1, a, b) for segs in ces.values() for t0, t1, st in segs if st != "Idle")
            row = {"name": name, "from": a, "to": b, "cycles": b - a, "pct": 100 * (b - a) / (w1 - w0),
                   "mean_active_ces": act / (b - a), "flops": None, "local_gamma_e": None}
            fl = [counters_in(p, a, b) for p in self._points().values() if p]
            if fl and all(fl):
                row["flops"] = sum(f for _, f in fl)
                row["local_gamma_e"] = row["flops"] / (2 * self.macs_per_ce * n * (b - a))
            out.append(row)
        return out

    def _waterfall(self, r, ces, w0, w1) -> list[dict]:
        """N_CE x Co CE-cycles split into losses and the ideal Ci x N_CE; cumulative γe if the losses were removed."""
        n = len(ces)
        total = n * (w1 - w0)
        by_label: dict[str, Counter] = {}
        flops_label: dict[str, tuple[int, float]] = {}
        phases = self.pa["phases"] if self.pa and self.pa["source"] else {}
        idle = susp = 0.0
        if phases:
            for key, segs in phases.items():
                for lab, st in phase_stats(segs, self._points().get(key, []), w0, w1).items():
                    idle += sum(st.cycles.get(x, 0) for x in IDLE_STATES)
                    susp += st.cycles.get("Suspended", 0)
                    if st.busy:
                        by_label.setdefault(lab, Counter())["busy"] += st.busy
                        f, b = flops_label.get(lab, (0, 0.0))
                        flops_label[lab] = (f + (st.flops if st.counted else 0), b + st.busy)
        else:
            busy = 0.0
            for segs in ces.values():
                for t0, t1, st in segs:
                    c = _overlap(t0, t1, w0, w1)
                    if st in IDLE_STATES:
                        idle += c
                    elif st == "Suspended":
                        susp += c
                    else:
                        busy += c
            by_label["busy"] = Counter(busy=busy)
        fl = [counters_in(p, w0, w1) for p in self._points().values() if p]
        flops = sum(f for _, f in fl) if fl and all(fl) else None
        if r["C_i"] is not None:
            ideal = r["C_i"] * r["N_CE"] / self.macs_per_ce
        elif flops is not None:
            ideal = flops / (2 * self.macs_per_ce)
        else:
            return []
        want = set(filter(None, self.args.get("compute", "").split(",")))
        compute = {lab for lab, (f, b) in flops_label.items() if (lab in want if want else b and f / b >= 0.1)}
        if not phases or not compute:
            compute = set(by_label)  # no regions: all busy time is "compute below peak" once the rest is removed
        steps = [("CEs idle / launching", idle), ("CEs suspended (waiting on DMA, sync, ...)", susp)]
        steps += sorted(((f"busy in {lab}", c["busy"]) for lab, c in by_label.items() if lab not in compute),
                        key=lambda x: -x[1])
        comp = sum(by_label[lab]["busy"] for lab in compute if lab in by_label)
        steps.append((f"compute below peak ({', '.join(sorted(compute))})", comp - ideal))
        rows, left = [], total
        for name, c in steps:
            left -= c
            rows.append({"step": name, "ce_cycles": c, "pct": 100 * c / total if total else None,
                         "gamma_e_if_removed": ideal / left if left > 0 else None})
        rows.append({"step": "ideal (Ci x N_CE)", "ce_cycles": ideal, "pct": 100 * ideal / total if total else None,
                     "gamma_e_if_removed": None})
        return rows

    def _counters(self, m, out, proc, bt, ces):
        acc, pts = 0, [(0.0, 0)]
        for t in bt:
            acc += t.nbytes
            pts.append((t.start, acc))
        if bt:
            out.counter(proc, "D_D cumulative bytes", pts)
        steps = sorted([(t0, 1) for segs in ces.values() for t0, t1, st in segs if st != "Idle"] +
                       [(t1, -1) for segs in ces.values() for t0, t1, st in segs if st != "Idle"])
        n, pts = 0, [(0.0, 0)]
        for t, d in steps:
            n += d
            pts.append((t, n))
        out.counter(proc, "active CEs", pts)

    def _check_retired(self):
        if self.retired is None and self.pa and self.pa["source"] == "probe":
            # No commit log: the ce_marker probe's flops counters (cumulative per CE) give the FP work instead.
            last = [max(pts, key=lambda p: p[0]) for pts in self.pa["points"].values() if pts]
            if last:
                fl = {i: p[2] or 0 for i, p in enumerate(last)}
                self.retired = {"retired": sum(p[1] or 0 for p in last), "fma": None, "fp_other": None,
                                "fp_ops": sum(fl.values()), "int_mul": None, "fp_ops_per_core": fl,
                                "source": "ce_marker counters (up to each CE's last marker)"}
        if self.retired is None:
            if self.args.get("log"):
                self.warnings.append("log has no retired-instruction (C<n>:) lines: T_ops cross-check skipped")
            return
        declared = [r["T_ops"] for r in self.results if r["op_kind"] == "fp" and r["T_ops"] is not None]
        if not declared:
            return
        want, got = sum(declared), self.retired["fp_ops"]
        self.retired["declared_fp_ops"] = want
        self.retired["diff"] = _ratio(got - want, want)
        if want and abs(got - want) > self.tol * want:
            self.warnings.append(f"declared T_ops={want} but the CEs retired {got} FP ops "
                                 f"({100 * (got - want) / want:+.1f}%): check t_ops in launch.py")

    def _sanity(self, r):
        k = r["kernel"]
        if r["T_ops"] is None:
            self.warnings.append(f"{k}: T_ops not declared (KernelLaunch(t_ops=...) or t_ops=): γe not computed")
        if r["gamma_e"] is not None and r["gamma_e"] > 1:
            self.warnings.append(f"{k}: γe={r['gamma_e']:.3g} > 1: T_ops or N_CE is wrong")
        if r["D_i"] + r["D_o"] == 0:
            self.warnings.append(f"{k}: no buffer args (D_i + D_o = 0): ρ not computed")
        elif r["rho"] is not None and r["rho"] < 1:
            self.warnings.append(f"{k}: ρ={r['rho']:.3g} < 1: less traffic crossed '{self.boundary}' than the "
                                 "workload's inputs+outputs (data preloaded on chip, outputs left in the LLC, "
                                 "or wrong window/boundary)")
        if r["partial_puts"]:
            self.warnings.append(f"{k}: {r['partial_puts']} PutPartialData counted at full size (mask not traced)")
        if r["unfinished"]:
            self.warnings.append(f"{k}: {r['unfinished']} boundary transactions had not completed at trace end")

    # ------------------------------------------------------------------ outputs
    def summary(self) -> dict:
        return {"config": self.config, "kernels": self.results, "retired": self.retired,
                "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        c = self.config
        notes = (f"N_CE = {c.get('n_ce_in_trace')} CEs in trace x {c.get('macs_per_ce')} MAC; "
                 f"M_i = LLC x {c.get('n_cr')} CR = {c.get('M_i')} B; boundary '{c.get('boundary')}' = "
                 f"{', '.join(c.get('boundary_ports') or ['none'])}; F_i = {c.get('F_MHz')} MHz.")
        out = [Section("REDEFINE metrics · compute utilization (γe = Ci / Co)",
                       ["kernel", "T_ops", "N_CE", "Ci (cycles)", "window from", "window to", "Co (cycles)",
                        "t_i (µs)", "γe"],
                       [[r["kernel"], r["T_ops"], r["N_CE"], None if r["C_i"] is None else round(r["C_i"]),
                         int(r["window"][0]), int(r["window"][1]),
                         int(r["C_o"]), _r(r["t_i_us"]), _r(r["gamma_e"])] for r in self.results], notes),
               Section("REDEFINE metrics · data movement (ρ = D_D / (D_i + D_o), φ = D_D / M_i)",
                       ["kernel", "D_i", "D_o", "D_D in", "D_D out", "D_D", "ρ", "M_i", "φ", "before window",
                        "after window"],
                       [[r["kernel"], r["D_i"], r["D_o"], r["D_D_in"], r["D_D_out"], r["D_D"], _r(r["rho"]),
                         r["M_i"], _r(r["phi"]), r["D_D_before_window"], r["D_D_after_window"]]
                        for r in self.results],
                       "Bytes. D_D counts boundary transactions that start inside the kernel window; "
                       "'before'/'after' are boundary bytes outside it (loading, host read-back).")]
        rows = [[r["kernel"], sw["name"], int(sw["from"]), int(sw["to"]), int(sw["cycles"]), _r(sw["pct"], 3),
                 _r(sw["mean_active_ces"], 3), sw["flops"], _r(sw["local_gamma_e"])]
                for r in self.results for sw in r["sub_windows"]]
        rows += [[r["kernel"], f"CMU {c['op']}", int(c["from"]), int(c["to"]), int(c["cycles"]),
                  _r(100 * c["cycles"] / r["C_o"], 3), None, None, None] for r in self.results for c in r["cmu"]]
        out.append(Section("REDEFINE metrics · sub-windows",
                           ["kernel", "sub-window", "from", "to", "cycles", "% of Co", "mean active CEs", "flops",
                            "local γe"], rows,
                           "setup = until every CE is active; steady; tail = after the first CE has finished. "
                           "local γe = flops / (2 x N_CE x cycles) inside the sub-window (needs instret/flops "
                           "counters: ce_marker probe or commit log). CMU rows are LLC flush/clean commands."))
        rows = [[r["kernel"], w["step"], round(w["ce_cycles"]), _r(w["pct"], 3), _r(w["gamma_e_if_removed"])]
                for r in self.results for w in r["waterfall"]]
        src = (self.pa or {}).get("source")
        out.append(Section("REDEFINE metrics · γe waterfall (CE-cycles = N_CE x Co)",
                           ["kernel", "step", "CE-cycles", "%", "γe if this and the steps above were removed"], rows,
                           f"Busy time split by PC-marker region ({src})." if src else
                           "No PC markers: busy time is one step (add markers, or pass log= with marker commit "
                           "lines, to split it by region)."))
        rows = []
        for r in self.results:
            tot = r["D_D"] or 1
            rows += [[r["kernel"], g["region"], g["dir"], g["bytes"], _r(100 * g["bytes"] / tot, 3)]
                     for g in r["regions"]]
        if self.regions is not None:
            for r in self.results:
                rows.append([r["kernel"], "data sections (GlobalData*, GroupLocal, Local, Stack)", "in+out",
                             r["D_D_data"], _r(100 * r["D_D_data"] / (r["D_D"] or 1), 3)])
                rows.append([r["kernel"], "code / runtime sections (Text, Context, *Runtime*, StaticMetadata)",
                             "in+out", r["D_D"] - r["D_D_data"],
                             _r(100 * (r["D_D"] - r["D_D_data"]) / (r["D_D"] or 1), 3)])
            out.append(Section("REDEFINE metrics · D_D by kernel section", ["kernel", "section", "dir", "bytes", "%"],
                               rows, f"Sections from {self.regions.source} (KernelSectionConst). Code/runtime "
                               "traffic is part of D_D but not of the algorithm's data movement."))
        else:
            out.append(Section("REDEFINE metrics · D_D by address region", ["kernel", "region", "dir", "bytes", "%"],
                               rows, "Region = the boundary port's manager address set the transaction falls in "
                               "(pass --regions kernel_sections.json for kernel sections)."))
        rows = [[r["kernel"], a["client"], a["in"], a["out"]] for r in self.results for a in r["attribution"]]
        out.append(Section(f"REDEFINE metrics · traffic on '{self.attrib}' by client (who drives D_D)",
                           ["kernel", "client", "read bytes", "write bytes"], rows))
        rows = []
        for r in self.results:
            co = r["C_o"] or 1
            for inst, st in r["ce_time"].items():
                rows.append([r["kernel"], m.trace.module_short(inst)] +
                            [_r(100 * st.get(s, 0) / co, 3) for s in ("Busy", "Suspended", "Idle")] +
                            [_r(100 * (st.get("PreLaunch", 0) + st.get("Launch", 0)) / co, 3)])
        out.append(Section("REDEFINE metrics · CE time in the kernel window (%)",
                           ["kernel", "CE", "Busy", "Suspended", "Idle", "PreLaunch+Launch"], rows))
        if self.retired:
            rt = self.retired
            out.append(Section("REDEFINE metrics · T_ops cross-check (retired instructions, whole log)",
                               ["declared FP T_ops", "retired FP ops", "diff", "fma", "other FP", "int mul",
                                "retired instructions"],
                               [[rt.get("declared_fp_ops"), rt["fp_ops"],
                                 None if rt.get("diff") is None else f"{100 * rt['diff']:+.1f}%", rt["fma"],
                                 rt["fp_other"], rt["int_mul"], rt["retired"]]],
                               "fma = 2 ops; fadd/fsub/fmul/fdiv/fsqrt = 1 op. Source: "
                               + rt.get("source", "commit log") + "."))
        if self.warnings:
            out.append(Section("REDEFINE metrics · warnings", ["warning"], [[w] for w in self.warnings]))
        return out
