"""rtltrace plugin: the kernel developer's view of a REDEFINE trace. Load it after the other REDEFINE plugins.

It adds no new measurement. It names things and puts the report in debug-playbook order:

  names       friendly names for RTL instance paths, TileLink clients / ports, probes and kernel sections (launch.py
              buffer names), used by the generic tables and the Perfetto view (display hook)
  run card    kernel, configuration, efficiency headline, the inputs that were found (report header)
  findings    ranked correctness issues and bottlenecks with evidence and a suggestion (redefine_findings.py)
  glossary    every metric and term used in the report
  name map    every probe in the trace: friendly name -> RTL instance path, probe, kind, group, Chisel source line,
              so a hardware engineer can go from any number in the report back to the RTL
  probe groups which groups were on in this run and what is missing when one is off
  Perfetto    a "Correctness checks" process with one instant per hazard event; kernel view keeps CeState

    python3 -m rtltrace report sim.log -o report.html --plugin redefine_metrics.py ... --plugin redefine_view.py \
        [--plugin-arg findings_json=findings.json]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from rtltrace.plugin import Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (Names, _seg_at, buffer_tables, ce_key, ce_label, phase_analysis,  # noqa: E402
                             ce_timelines, plugin_regions, published)
from redefine_findings import MIN_PCT, findings  # noqa: E402


class RedefineView(Plugin):
    name = "redefine-view"
    fields = {"CeFreeBitMap/*": "ce_bitmap"}
    tags = {"ce_bitmap"}
    perfetto_keep = ["CeState"]

    def setup(self, ctx):
        self.args = ctx.args
        self.trace = ctx.trace
        self.regions = plugin_regions(ctx)
        self.warnings: list[str] = []
        self.names = Names(ctx.args, self.regions, self.warnings, ctx.trace)
        self.srcs = {p.name: p.meta.get("src") for p in ctx.trace.probes.values() if p.meta.get("src")}
        self.elf = ctx.args.get("elf")
        self.m = None
        self.found: list[dict] = []

    # ------------------------------------------------------------------ names
    def display(self, kind: str, name: str) -> str | None:
        if kind == "module":
            n = self.names.module(name)
            return str(n) if n != name else None
        if kind == "client":
            return V.client_name(name)
        if kind == "port":
            return V.port_name(name)
        if kind == "probe":
            return V.probe_name(name)
        if kind == "region":
            n = V.section_name(name, self.names.buffers)
            return n.split(" (")[0] if n else None
        return None

    def annotate(self, f, value, ev):
        ces = [f"CE{i}" for i in range(value.bit_length()) if value >> i & 1]
        return (f"free: {', '.join(ces)}" if ces else "no CE free",
                f"free CEs: {', '.join(ces) or 'none'} (bitmap 0x{value:x})")

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        self.m = m
        evs = sorted(m.events("spm_s3_overlap"), key=lambda e: e.ts)
        if not evs:
            return
        tls = ce_timelines(m)
        pa = phase_analysis(m, self.args, tls, []) if tls else None
        segs = {k: (v, [s.t0 for s in v]) for k, v in (pa or {}).get("phases", {}).items()}
        proc = out.process("Correctness checks", sort=0)
        tracks = {}
        for e in evs:
            k = ce_key(e.probe.inst)
            lab = ce_label(k) if k else e.probe.inst
            if lab not in tracks:
                tracks[lab] = out.track(proc, f"{lab}: scratchpad read during DMA write", sort=k[1] if k else 0)
            sg = _seg_at(segs[k][0], segs[k][1], e.ts) if k in segs else None
            addr = e.vals.get("addr")
            r = self.regions.lookup(addr) if self.regions is not None and addr is not None else None
            out.instant(tracks[lab], e.ts, "⚠ scratchpad read during DMA write",
                        {"region": V.region_name(sg.label) if sg else "–",
                         "address": (f"{self.names.section(r.label().rsplit('+', 1)[0])}+0x{r.offset:x}" if r
                                     else hex(addr) if addr is not None else "–"),
                         "rtl_path": e.probe.inst, "probe": e.probe.name, "chisel_src": self.srcs.get(e.probe.name)})

    # ------------------------------------------------------------------ report header
    def card(self) -> dict[str, str]:
        m = self.m
        met = published(m, "metrics") if m is not None else None
        out: dict[str, str] = {}
        if met:
            ks = met.get("kernels", [])
            c = met.get("config", {})
            hw = c.get("hw", {})
            out["kernel"] = ", ".join(k["kernel"] for k in ks) or "–"
            out["configuration"] = (f"{c.get('n_ce_in_trace')} CEs × {c.get('n_cr')} CR, LLC "
                                    f"{(hw.get('llc_bytes') or 0) // 1024} KB, L1-D {(hw.get('l1d_bytes') or 0) // 1024}"
                                    f" KB, clock {c.get('F_MHz')} MHz")
            for k in ks:
                g = k.get("gamma_e")
                label = f"result · {k['kernel']}" if len(ks) > 1 else "result"
                out[label] = (f"{k['C_o']:,.0f} cycles = {k['t_i_us']:,.1f} µs; γe = "
                              f"{'–' if g is None else f'{g:.3f}'}; ρ = "
                              f"{'–' if k.get('rho') is None else format(k['rho'], '.3g')}")
        ov = published(m, "overlap") if m is not None else None
        for e in (ov or {}).get("engine", []):
            rows = [r for r in ov["ces"] if r["kernel"] == e["kernel"] and r.get("iterations")]
            if rows:
                hid = sum(r["overlap"] for r in rows) / (sum(r["inflight"] for r in rows) or 1)
                rdy = sum(r["ready"] * r["iterations"] for r in rows) / sum(r["iterations"] for r in rows)
                key = "compute / DMA overlap" + (f" · {e['kernel']}" if len(ov.get("engine", [])) > 1 else "")
                out[key] = (f"{100 * hid:.0f} % of DMA time hidden behind compute; data ready "
                                                f"before compute ended in {100 * rdy:.0f} % of iterations; DMA "
                                                f"demand ÷ capacity {e['demand_ratio']:.2f}"
                                                if e.get("demand_ratio") else f"{100 * hid:.0f} % hidden")
        if self.found:
            n = Counter(f["severity"] for f in self.found)
            out["findings"] = ", ".join(f"{v} {k}" for k, v in n.items())
        ph = published(m, "phases") if m is not None else None
        hdrs = (ph or {}).get("headers") or []
        out["inputs"] = " · ".join(x for x in (
            f"ELF {Path(self.elf).name}" if self.elf else "no ELF (addresses not symbolized)",
            f"launch {Path(self.args['launch']).name}" if self.args.get("launch") else "no launch.py (no T_ops / "
                                                                                       "buffer names)",
            f"PC-marker names from {', '.join(Path(h).name for h in hdrs)}" if hdrs else "no PC-marker header",
            f"buffer addresses from {self._manifest()}" if self._manifest() else "no buffer address table "
                                                                                 "(buffers shown by section)",
        ) if x)
        return out

    def _manifest(self) -> str | None:
        src = buffer_tables(self.args)[1]
        return src if src in (None, "sim log") else _short(src)

    # ------------------------------------------------------------------ report
    def report(self, m) -> list[Section]:
        self.m = m
        met, ph, dma, cache, ov = (published(m, n) for n in ("metrics", "phases", "dma", "cache", "overlap"))
        if ph is not None:
            ph = dict(ph, headers=(ph.get("headers") or []))
        self.found = findings(met, ph, dma, cache, self.srcs, ov)
        if self.args.get("findings_json"):
            Path(self.args["findings_json"]).write_text(json.dumps(self.found, indent=2, default=str))
        out = []
        G, b = V.G_FIND
        rows = [[i + 1, f["severity"], f["title"], f["evidence"], f["action"],
                 None if f["pct"] is None else round(f["pct"], 1),
                 None if f["gamma_if_fixed"] is None else round(f["gamma_if_fixed"], 3), f["see"],
                 RtlName("probes", f["rtl"]) if f["rtl"] else None] for i, f in enumerate(self.found)]
        out.append(Section("What to look at first", ["#", "severity", "finding", "evidence", "what to try",
                                                     "% of CE-cycles", "γe if fixed", "details in", "RTL"], rows,
                           "Ranked: correctness first, then the biggest losses of CE time. Each finding names the "
                           "report section with the details and (hover 'probes') the RTL probes it is based on.",
                           legend={"% of CE-cycles": "Share of N_CE × kernel cycles this loss costs.",
                                   "γe if fixed": "Compute efficiency if only this loss were removed (γe ÷ (1 − "
                                                  "share)); fixing several adds up as in the γe waterfall."},
                           group=G, order=b, key="Findings",
                           empty=f"Nothing to report: no correctness issue and no loss of {MIN_PCT:g} % or more of "
                                 "the CE-cycles."))
        G, b = V.G_GLOSS
        out.append(Section("Glossary", ["term", "meaning", "how to read it", "JSON key / probe"],
                           [[t, mn, how or "", internal] for t, (mn, how, internal) in V.GLOSSARY.items()],
                           group=G, order=b, key="Glossary"))
        rows = []
        present = defaultdict(list)
        for p in self.trace.probes.values():
            for g in p.groups:
                present[g].append(p.name)
        for g in sorted(set(V.PROBE_GROUPS) | set(present), key=lambda g: (g not in present, g)):
            on, gives, missing = V.PROBE_GROUPS.get(g, (None, "", ""))
            rows.append([g, "on" if g in present else "off", "on" if on else "off" if on is not None else "?",
                         gives, "" if g in present else missing, f"+rtltrace_{g}_{'off' if g in present else 'on'}",
                         len(set(present.get(g, [])))])
        out.append(Section("Probe groups in this trace",
                           ["group", "in this run", "default", "what it gives", "if it is off", "plusarg to toggle",
                            "probes"], rows,
                           "Groups are switched at simulation time with SIM_PLUSARGS (no RTL rebuild). A group is "
                           "'on in this run' when its probes appear in the trace.",
                           group=G, order=b + 1, key="Probe groups"))
        G, b = V.G_MAP
        rows = []
        tr = self.trace
        for p in sorted(tr.probes.values(), key=lambda p: (p.inst, p.port or "", p.name)):
            if p.is_tl:
                if p.channel != "a":
                    continue
                what = RtlName(V.port_name(p.port) or p.port, p.port)
                rtl, kind = f"{p.port} (TileLink port, channels a–e)", "tl"
            else:
                what = RtlName(V.probe_name(p.name) or p.name, p.name)
                rtl, kind = p.name, p.kind
            rows.append([self.names.module(p.inst, tr.module_short(p.inst)), what, kind, ", ".join(p.groups),
                         tr.module_short(p.inst), rtl, p.meta.get("src") or p.meta.get("at") or "–"])
        out.append(Section("RTL name map", ["module", "probe / port", "kind", "group", "RTL instance",
                                            "RTL probe / port", "Chisel source"], rows,
                           "Every probe in this trace: the friendly names used in this report and in Perfetto, and "
                           "where they are in the RTL. Instance paths are relative to the testbench top.",
                           group=G, order=b, level="hw", key="RTL name map"))
        rows = sorted({(RtlName(V.client_name(c) or c, c), c) for pt in m.ports.values()
                       for c, _, _ in pt.clients}, key=lambda x: str(x[0]))
        out.append(Section("TileLink requesters", ["requester", "TileLink client name"], [list(r) for r in rows],
                           "Client names as declared in Chisel (TLMasterParameters name).", group=G, order=b + 1,
                           level="hw", key="RTL name map · TileLink clients"))
        if self.warnings:
            G, b = V.G_CHECK
            out.append(Section("Tool warnings · names", ["warning"], [[w] for w in self.warnings], group=G,
                               order=b + 14, key="View warnings"))
        return out


def _short(p) -> str:
    p = Path(p)
    return "/".join(p.parts[-3:])
