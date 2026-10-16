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

from rtltrace.plugin import Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (IDLE_STATES, Kernel, Names, PhaseStats, _overlap, _r, _ratio, _region,  # noqa: E402
                             ce_label, ce_timelines, cmu_spans, compute_arg, count_retired, counters_in,
                             is_data_section, kernel_windows, limited_by, load_workload, mix_cycles, parse_params,
                             phase_analysis, phase_stats, plugin_regions, publish, section_of, sub_windows, window_kernels)

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
            self.kernels = [Kernel("kernel0", None, a.get("op_kind", "fp"),
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
        self.names = Names(a, self.regions, self.warnings, ctx.trace)

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
        self.kernels = window_kernels(self.kernels, len(windows))

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

        proc = out.process("Kernel overview", sort=1)
        wtrack = out.track(proc, "kernel window")
        strack = out.track(proc, "phases (setup / steady / tail)", sort=1)
        ctrack = out.track(proc, "LLC flush / clean", sort=2)
        self.pa = phase_analysis(m, self.args, ces, self.warnings)
        cmus = cmu_spans(m)
        for k, (w0, w1) in zip(self.kernels, windows):
            r = self._measure(k, w0, w1, bt, at, ces, n_ce, m_i, bool(ports))
            r["sub_windows"] = self._sub_windows(ces, w0, w1)
            r["cmu"] = [{"op": CMU_OPS.get(op, op), "from": a, "to": b, "cycles": b - a}
                        for a, b, op in cmus if b > w0 and a < w1]
            r["waterfall"] = self._waterfall(r, ces, w0, w1)
            for sw in r["sub_windows"]:
                out.span(strack, sw["from"], sw["to"], sw["name"],
                         {key: v for key, v in sw.items() if key not in ("from", "to")})
            for c in r["cmu"]:
                out.span(ctrack, c["from"], c["to"], f"LLC {c['op']}", {"cycles": c["cycles"],
                                                                         "probe": "llc_cmu / llc_cmu_done"})
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
        publish(m, "metrics", self.summary())
        if self.args.get("json"):
            Path(self.args["json"]).write_text(json.dumps(self.summary(), indent=2))

    def _rg(self, t) -> str:
        """Kernel section of a boundary transaction (with --regions), else the port's manager address set."""
        return section_of(self.regions, t.address) if self.regions is not None else _region(t)

    def _measure(self, k: Kernel, w0, w1, bt, at, ces, n_ce, m_i, has_boundary: bool = True) -> dict:
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
        dd = d_in + d_out if has_boundary else None  # no boundary port traced: D_D unknown, not 0
        return {
            "kernel": k.name, "window": [w0, w1], "T_ops": k.t_ops, "op_kind": k.op_kind, "N_CE": n_ce,
            "C_i": ci, "C_o": co, "F_MHz": self.freq_mhz, "t_i_us": co / self.freq_mhz, "gamma_e": _ratio(ci, co),
            "D_i": k.d_in, "D_o": k.d_out, "D_D_in": d_in if has_boundary else None,
            "D_D_out": d_out if has_boundary else None, "D_D": dd,
            "rho": _ratio(dd, k.d_in + k.d_out), "M_i": m_i, "phi": _ratio(dd, m_i),
            "D_D_before_window": before, "D_D_after_window": after, "partial_puts": partial,
            "unfinished": unfinished,
            "regions": [{"region": rg, "name": self.names.section(rg) if self.regions is not None else rg, "dir": d,
                         "bytes": b} for (rg, d), b in regions.most_common()],
            "D_D_data": sum(b for (rg, _), b in regions.items() if is_data_section(rg))
            if self.regions is not None else None,
            "attribution": [{"client": c, "name": str(self.names.client(c)), "in": v[0], "out": v[1]}
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
            row = {"name": name, "from": a, "to": b, "cycles": b - a, "pct": 100 * (b - a) / (w1 - w0) if w1 > w0 else None,
                   "mean_active_ces": act / (b - a) if b > a else None, "flops": None, "local_gamma_e": None}
            fl = [counters_in(p, a, b) for p in self._points().values() if p]
            if fl and all(fl):
                row["flops"] = sum(f for _, f in fl)
                row["local_gamma_e"] = row["flops"] / (2 * self.macs_per_ce * n * (b - a)) if b > a else None
            out.append(row)
        return out

    def _waterfall(self, r, ces, w0, w1) -> list[dict]:
        """N_CE x Co CE-cycles split into losses and the ideal Ci x N_CE; cumulative γe if the losses were removed.

        Without T_ops and flop counters the ideal is unknown: the rows still split the CE time (idle, suspended,
        busy per region), the busy time that may be useful work is one row "... not split" and γe stays empty."""
        n = len(ces)
        total = n * (w1 - w0)
        by_label: dict[str, Counter] = {}
        agg: dict[str, PhaseStats] = {}  # per region over all CEs: the "limited by" of the below-peak row
        flops_label: dict[str, tuple[int, float]] = {}
        phases = self.pa["phases"] if self.pa and self.pa["source"] else {}
        idle = susp = 0.0
        if phases:
            for key, segs in phases.items():
                for lab, st in phase_stats(segs, self._points().get(key, []), w0, w1, self.pa["mix"].get(key)).items():
                    idle += sum(st.cycles.get(x, 0) for x in IDLE_STATES)
                    susp += st.cycles.get("Suspended", 0)
                    if st.busy:
                        a = agg.setdefault(lab, PhaseStats(counted=True))
                        a.cycles["Busy"] += st.busy
                        a.instret += st.instret
                        a.flops += st.flops
                        a.mix.update(st.mix)
                        a.counted = a.counted and st.counted
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
        elif flops:
            ideal = flops / (2 * self.macs_per_ce)
        else:
            ideal = None  # no T_ops, and no counters or a kernel without FP work: nothing to compare with
        want = compute_arg(self.args)
        if want and phases and not want & set(flops_label) and not getattr(self, "_warned_compute", False):
            self._warned_compute = True
            self.warnings.append(f"compute={','.join(sorted(want))} matches no PC region "
                                 f"(regions: {', '.join(sorted(flops_label)) or 'none'})")
        compute = {lab for lab, (f, b) in flops_label.items() if (lab in want if want else b and f / b >= 0.1)}
        if ideal is None:
            steps = [("CEs idle / launching", idle), ("CEs suspended (waiting on DMA, sync, ...)", susp)]
            if phases:
                steps += sorted(((f"busy in {lab}", c["busy"]) for lab, c in by_label.items() if lab not in compute),
                                key=lambda x: -x[1])
                if compute:
                    steps.append((f"compute, not split ({', '.join(sorted(compute))})",
                                  sum(by_label[lab]["busy"] for lab in compute if lab in by_label)))
            else:
                steps.append(("busy, not split", by_label["busy"]["busy"]))
            return [{"step": name, "ce_cycles": c, "pct": 100 * c / total if total else None,
                     "gamma_e_if_removed": None} for name, c in steps]
        if not phases or not compute:
            compute = set(by_label)  # no regions: all busy time is "compute below peak" once the rest is removed
        steps = [("CEs idle / launching", idle), ("CEs suspended (waiting on DMA, sync, ...)", susp)]
        steps += sorted(((f"busy in {lab}", c["busy"]) for lab, c in by_label.items() if lab not in compute),
                        key=lambda x: -x[1])
        comp = sum(by_label[lab]["busy"] for lab in compute if lab in by_label)
        # never negative: an ideal above the measured compute time means T_ops or N_CE is off (γe > 1 warns)
        steps.append((f"compute below peak ({', '.join(sorted(compute))})", max(0.0, comp - ideal)))
        tot = PhaseStats(counted=all(agg[lab].counted for lab in compute if lab in agg))
        for lab in compute:
            if lab in agg:
                tot.cycles["Busy"] += agg[lab].busy
                tot.instret += agg[lab].instret
                tot.flops += agg[lab].flops
                tot.mix.update(agg[lab].mix)
        mixc = mix_cycles(tot) if tot.counted and tot.instret and sum(tot.mix.values()) else None
        why = limited_by(tot, 2.0 * self.macs_per_ce, {"wait": 0.5, "idle_ipc": 0.05, "stall_ipc": 0.6,
                                                        "compute": 0.6}) if tot.busy else None
        rows, left = [], total
        for name, c in steps:
            left -= c
            rows.append({"step": name, "ce_cycles": c, "pct": 100 * c / total if total else None,
                         "gamma_e_if_removed": ideal / left if left > 0 else None})
            if name.startswith("compute below peak"):
                rows[-1]["limited_by"] = why  # what the compute regions are limited by (ce_phases classes)
                if mixc and c > 0:
                    rows[-1]["mix"] = {"regions": sorted(compute), "busy": tot.busy, "instret": tot.instret, **mixc}
                    rows[-1]["breakup"] = self._breakup(mixc, ideal, left + c)
        rows.append({"step": "ideal (Ci x N_CE)", "ce_cycles": ideal, "pct": 100 * ideal / total if total else None,
                     "gamma_e_if_removed": None})
        return rows

    @staticmethod
    def _breakup(mix: dict, ideal: float, left: float) -> list[dict]:
        """The below-peak loss (busy cycles of the compute regions − ideal; `left` = CE-cycles not yet removed
        before it) by instruction type: loads, stores, other
        instructions, other FP arithmetic, cycles with no retirement, and the retired FMAs minus the ideal (0 when
        the declared T_ops matches the FMAs the CE retired). The parts add up to the loss."""
        parts = [("load", mix["load"]), ("store", mix["store"]), ("other", mix["other"]),
                 ("fp_other", mix["fp_other"]), ("stall", mix["stall"]), ("fma_vs_t_ops", mix["fma"] - ideal)]
        out = []
        for key, c in parts:
            if abs(c) < 0.5:
                continue
            left -= c
            out.append({"part": key, "ce_cycles": c, "gamma_e_if_removed": ideal / left if left > 0 else None})
        return out

    def _counters(self, m, out, proc, bt, ces):
        acc, pts = 0, [(0.0, 0)]
        for t in bt:
            acc += t.nbytes
            pts.append((t.start, acc))
        if bt:
            out.counter(proc, "off-chip bytes, cumulative (D_D)", pts)
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
                multi = len({k[0] for k in self.pa["points"]}) > 1
                fl = {ce_label(k, multi): max(pts, key=lambda p: p[0])[2] or 0
                      for k, pts in self.pa["points"].items() if pts}
                self.retired = {"retired": sum(p[1] or 0 for p in last), "fma": None, "fp_other": None,
                                "fp_ops": sum(fl.values()), "int_mul": None, "fp_ops_per_core": fl,
                                "source": "ce_marker counters (up to each CE's last marker)"}
        if self.retired is None:
            if self.args.get("log"):
                self.warnings.append("log has no retired-instruction (C<n>:) lines: T_ops cross-check skipped")
            return
        fp = [r for r in self.results if r["op_kind"] == "fp"]
        declared = [r["T_ops"] for r in fp if r["T_ops"] is not None]
        if not declared:
            return
        if len(declared) < len(fp):
            # the retired count covers every kernel; comparing it with a partial declaration would flag a mismatch
            self.warnings.append("T_ops is declared for only some kernels: T_ops cross-check skipped")
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
        G, b = V.G_EFF
        notes = (f"N_CE = {c.get('n_ce_in_trace')} CEs × {c.get('macs_per_ce')} FMA/cycle; M_i = LLC × {c.get('n_cr')} "
                 f"CR = {c.get('M_i')} B; off-chip boundary = {', '.join(c.get('boundary_ports') or ['none'])} "
                 f"(port '{c.get('boundary')}'); clock {c.get('F_MHz')} MHz.")
        out = [Section("Compute efficiency (γe)",
                       ["kernel", "γe", "kernel time (µs)", "Co (cycles)", "Ci (cycles)", "T_ops", "N_CE",
                        "window from", "window to"],
                       [[r["kernel"], _r(r["gamma_e"]), _r(r["t_i_us"]), int(r["C_o"]),
                         None if r["C_i"] is None else round(r["C_i"]), r["T_ops"], r["N_CE"],
                         int(r["window"][0]), int(r["window"][1])] for r in self.results], notes,
                       legend=V.legend("γe (compute efficiency)", "Co (measured cycles)", "Ci (ideal cycles)",
                                       "T_ops", "N_CE"),
                       group=G, order=b, key="REDEFINE metrics · compute utilization (γe = Ci / Co)"),
               Section("Off-chip data movement (ρ, φ)",
                       ["kernel", "D_i (bytes)", "D_o (bytes)", "off-chip in", "off-chip out", "off-chip total",
                        "ρ", "M_i (bytes)", "φ", "before window", "after window"],
                       [[r["kernel"], r["D_i"], r["D_o"], r["D_D_in"], r["D_D_out"], r["D_D"], _r(r["rho"]),
                         r["M_i"], _r(r["phi"]), r["D_D_before_window"], r["D_D_after_window"]]
                        for r in self.results],
                       "Bytes. Off-chip = LLC ↔ memory traffic that starts inside the kernel window; before / after "
                       "are bytes outside it (loading inputs, reading results back).",
                       legend=V.legend("D_i / D_o (bytes)", "off-chip bytes (D_D)", "ρ (data reuse index)", "M_i",
                                       "φ (memory penalty index)"),
                       group=G, order=b + 1,
                       key="REDEFINE metrics · data movement (ρ = D_D / (D_i + D_o), φ = D_D / M_i)")]
        rows = []
        for r in self.results:
            tot = r["D_D"] or 1
            rows += [[r["kernel"], g["name"] if isinstance(g.get("name"), RtlName) else
                      self.names.section(g["region"]) if self.regions is not None else g["region"],
                      g["dir"], g["bytes"], _r(100 * g["bytes"] / tot, 3)] for g in r["regions"]]
        if self.regions is not None:
            for r in (r for r in self.results if r["D_D"] is not None):
                rows.append([r["kernel"], "all data sections (buffers, group-local, stack, scratchpad)", "in+out",
                             r["D_D_data"], _r(100 * r["D_D_data"] / (r["D_D"] or 1), 3)])
                rows.append([r["kernel"], "all code / runtime sections (kernel code, context memory, runtime data)",
                             "in+out", r["D_D"] - r["D_D_data"],
                             _r(100 * (r["D_D"] - r["D_D_data"]) / (r["D_D"] or 1), 3)])
            out.append(Section("Off-chip traffic by buffer / memory section",
                               ["kernel", "buffer / section", "direction", "bytes", "% of off-chip"], rows,
                               "Kernel buffers are named from launch.py (addresses from rtl_sim_results/buffers.txt). "
                               "Code / runtime traffic counts in ρ but is not the algorithm's data. "
                               f"Sections: {Path(str(self.regions.source)).name}.",
                               legend={"direction": "in = read from memory into the LLC; out = written back"},
                               group=G, order=b + 2, key="REDEFINE metrics · D_D by kernel section"))
        else:
            out.append(Section("Off-chip traffic by address region", ["kernel", "region", "direction", "bytes",
                                                                     "% of off-chip"],
                               rows, "Region = the boundary port's manager address set (pass --regions "
                               "kernel_sections.json for kernel sections and buffer names).",
                               group=G, order=b + 2, key="REDEFINE metrics · D_D by address region"))
        rows = [[r["kernel"], self.names.client(a["client"]), a["in"], a["out"]]
                for r in self.results for a in r["attribution"]]
        out.append(Section("Who drives the LLC traffic", ["kernel", "requester", "read bytes", "write bytes"], rows,
                           f"TileLink traffic into the LLC (port '{self.attrib}') by requester, inside the kernel "
                           "window.", group=G, order=b + 3,
                           key=f"REDEFINE metrics · traffic on '{self.attrib}' by client (who drives D_D)"))

        G, b = V.G_TIME
        src = (self.pa or {}).get("source")
        rows, useful, mixrows = [], [], []
        for r in self.results:
            wf, small = [], []
            ideal = next((w for w in r["waterfall"] if w["step"].startswith("ideal")), None)
            for w in r["waterfall"]:
                if w is ideal:
                    continue  # the ideal is useful work, not a loss: shown below the table
                if w["step"].startswith("busy in ") and w["pct"] is not None and w["pct"] < 1:
                    small.append(w)
                    continue
                if small:
                    wf.append(self._fold(small))
                    small = []
                wf.append(w)
            if small:  # small rows at the very end (the below-peak row is last when T_ops is known)
                wf.append(self._fold(small))
            # the rows share the CE-cycles that are not ideal useful work, so they add up to 100 %
            lost = sum(w["ce_cycles"] for w in wf)
            total_ce = lost + (ideal["ce_cycles"] if ideal else 0)
            for w in wf:
                # the below-peak row is shown as its parts when the instruction mix is known
                parts = [(self._step_name(w["step"], w.get("limited_by")), w["ce_cycles"], w["gamma_e_if_removed"])]
                if w.get("breakup"):
                    regs = ", ".join(V.region_name(x) for x in w["mix"]["regions"])
                    parts = [(RtlName(f"{regs}: {V.MIX_PARTS[b['part']]}", b["part"]), b["ce_cycles"],
                              b["gamma_e_if_removed"]) for b in w["breakup"]]
                rows += [[r["kernel"], nm, round(c), _r(100 * c / lost, 3) if lost else None,
                          _r(100 * c / total_ce, 3) if total_ce else None, _r(g, 3)] for nm, c, g in parts]
            bw = next((w for w in r["waterfall"] if w.get("mix")), None)
            if bw:
                mx = bw["mix"]
                regs = ", ".join(V.region_name(x) for x in mx["regions"])
                mixrows += [[r["kernel"], regs, V.MIX_PARTS_ALL[k], round(mx[k]), _r(100 * mx[k] / mx["busy"], 3)]
                            for k in ("fma", "fp_other", "load", "store", "other", "stall") if mx[k] >= 0.5]
                mixrows.append([r["kernel"], regs, "Busy cycles in the compute regions", round(mx["busy"]), 100.0])
            if lost:
                rows.append([r["kernel"], "Total lost CE-cycles" if ideal else "Total CE-cycles", round(lost), 100.0,
                             _r(sum(w["pct"] or 0 for w in wf), 3), None])
            if ideal and r["gamma_e"] is not None:
                total = lost + ideal["ce_cycles"]
                useful += [[r["kernel"], "Useful work at peak: ideal Ci × N_CE = γe", round(ideal["ce_cycles"]),
                            _r(ideal["pct"], 3), _r(r["gamma_e"])],
                           [r["kernel"], "Lost (total of the table above)", round(lost),
                            _r(100 * lost / total, 3), None],
                           [r["kernel"], "All CE-cycles: N_CE × Co", round(total), 100.0, None]]
        no_ideal = any(r["waterfall"] and not any(w["step"].startswith("ideal") for w in r["waterfall"])
                       for r in self.results)
        out.append(Section("Where the CE-cycles went (γe waterfall)",
                           ["kernel", "loss", "CE-cycles", "% of lost CE-cycles", "% of all CE-cycles",
                            "γe if removed"], rows,
                           "Read top-down: each row is a loss of CE-cycles; the '% of lost CE-cycles' column adds up "
                           "to 100 %, without the ideal (useful) cycles, which are shown separately below. "
                           "'γe if removed' is the efficiency reached if that loss and every loss above it were "
                           "eliminated. "
                           + (f"Busy time is split by PC-marker region (source: {src}); regions under 1 % are "
                              "folded into one row." if src else
                              "No PC markers: busy time is one row (add PC_MARK regions to split it).")
                           + (" No T_ops declared (launch.py KernelLaunch(t_ops=...)) and no flop counters: the "
                              "useful work and γe are unknown, so the busy time that may be useful is not split."
                              if no_ideal else ""),
                           legend=V.legend("CE-cycles", "% of lost CE-cycles", "γe if removed"), group=G, order=b,
                           key="REDEFINE metrics · γe waterfall (CE-cycles = N_CE x Co)"))
        if mixrows:
            out.append(Section("Compute regions: busy cycles by instruction type",
                               ["kernel", "compute regions", "part", "CE-cycles", "% of busy cycles"], mixrows,
                               "The busy CE-cycles of the compute regions split by what the CE retired: FMA "
                               "instructions are the useful work, the other rows are what shares the single issue "
                               "slot with them (loads, stores, loop control and address math) and the cycles in "
                               "which nothing retired. One cycle per retired instruction; classes from the "
                               "instruction words in the sim log's commit lines.",
                               legend=V.legend("FMA instructions", "no instruction retired"), group=G,
                               order=b + 0.6, key="REDEFINE metrics · compute-region cycles by instruction type"))
        if useful:
            out.append(Section("γe: useful work vs lost CE-cycles",
                               ["kernel", "part", "CE-cycles", "% of all CE-cycles", "γe"], useful,
                               "The CE-cycles the kernel's flops would need at peak (Ci × N_CE) as a share of all "
                               "CE-cycles is the efficiency γe = Ci / Co. The rest is the lost cycles of the "
                               "waterfall.", legend=V.legend("γe (compute efficiency)", "CE-cycles"), group=G, order=b + 0.5,
                               key="REDEFINE metrics · γe = ideal CE-cycles / all CE-cycles"))
        rows = []
        for r in self.results:
            co = r["C_o"] or 1
            for inst, st in r["ce_time"].items():
                rows.append([r["kernel"], self.names.ce(inst)] +
                            [_r(100 * st.get(s, 0) / co, 3) for s in ("Busy", "Suspended", "Idle")] +
                            [_r(100 * (st.get("PreLaunch", 0) + st.get("Launch", 0)) / co, 3)])
        out.append(Section("CE state per CE (% of the kernel window)",
                           ["kernel", "CE", "busy %", "suspended %", "idle %", "launching %"], rows,
                           "From the Orchestrator's CeState of each CE. Uneven busy % across CEs = load imbalance.",
                           legend=V.legend("busy / suspended / idle"), group=G, order=b + 1,
                           key="REDEFINE metrics · CE time in the kernel window (%)"))
        rows = [[r["kernel"], sw["name"], int(sw["from"]), int(sw["to"]), int(sw["cycles"]), _r(sw["pct"], 3),
                 _r(sw["mean_active_ces"], 3), sw["flops"], _r(sw["local_gamma_e"])]
                for r in self.results for sw in r["sub_windows"]]
        rows += [[r["kernel"], f"LLC {c['op']} (flush/clean)", int(c["from"]), int(c["to"]), int(c["cycles"]),
                  _r(100 * c["cycles"] / r["C_o"], 3), None, None, None] for r in self.results for c in r["cmu"]]
        out.append(Section("Timeline phases (setup / steady / tail)",
                           ["kernel", "phase", "from cycle", "to cycle", "cycles", "% of kernel", "mean active CEs",
                            "flops", "local γe"], rows,
                           "LLC flush/clean rows overlap the phases: they show when cache maintenance ran.",
                           legend=V.legend("setup / steady / tail", "mean active CEs", "local γe"), group=G,
                           order=b + 2, key="REDEFINE metrics · sub-windows"))
        G, b = V.G_CHECK
        if self.retired:
            rt = self.retired
            out.append(Section("Declared vs retired FP work (T_ops check)",
                               ["declared FP ops (T_ops)", "retired FP ops", "difference", "FMAs", "other FP ops",
                                "integer multiplies", "retired instructions"],
                               [[rt.get("declared_fp_ops"), rt["fp_ops"],
                                 None if rt.get("diff") is None else f"{100 * rt['diff']:+.1f}%", rt["fma"],
                                 rt["fp_other"], rt["int_mul"], rt["retired"]]],
                               "FMA = 2 ops; fadd/fsub/fmul/fdiv/fsqrt = 1 op. A large difference means t_ops in "
                               "launch.py is wrong, so γe is wrong. Source: " + rt.get("source", "commit log") + ".",
                               group=G, order=b,
                               key="REDEFINE metrics · T_ops cross-check (retired instructions, whole log)"))
        if self.warnings:
            out.append(Section("Tool warnings · efficiency metrics", ["warning"], [[w] for w in self.warnings],
                               group=G, order=b + 10, key="REDEFINE metrics · warnings"))
        return out

    @staticmethod
    def _fold(rows: list[dict]) -> dict:
        c = sum(w["ce_cycles"] for w in rows)
        return {"step": f"busy in {len(rows)} other regions (< 1 % each)", "ce_cycles": c,
                "pct": sum(w["pct"] or 0 for w in rows), "gamma_e_if_removed": rows[-1]["gamma_e_if_removed"]}

    @staticmethod
    def _step_name(step: str, limited: str | None = None) -> RtlName:
        if step.startswith("busy in ") and "other regions" not in step:
            lab = step[8:]
            return RtlName(f"busy in {V.region_name(lab)}", lab)
        if step.startswith("compute below peak ("):
            labs = step[len("compute below peak ("):-1]
            regs = ", ".join(V.region_name(x) for x in labs.split(", "))
            # regions that retire an instruction nearly every cycle but few FMAs: the loss is the loads, stores and
            # loop control that share the single issue slot with the FMAs
            return RtlName(f"load, store and loop operations ({regs})" if limited == "instruction"
                           else f"compute below peak ({regs})", labs)
        if step.startswith("ideal"):
            return RtlName("ideal: Ci × N_CE (useful work)", step)
        if step == "busy, not split":
            return RtlName("busy (useful work and losses: add PC_MARK regions and T_ops to split it)", step)
        if step.startswith("compute, not split ("):
            labs = step[len("compute, not split ("):-1]
            return RtlName(f"busy in compute regions ({', '.join(V.region_name(x) for x in labs.split(', '))}): "
                           "useful work and below-peak loss (declare T_ops to split them)", labs)
        if step.startswith("CEs suspended"):
            return RtlName("CEs suspended (waiting for DMA, sync, ...)", step)
        return RtlName(step, step)
