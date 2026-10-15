"""Tests for the kernel-developer view: vocabulary, buffer names, findings, report order and RTL traceability
(redefine_vocab.py, redefine_common buffers, redefine_findings.py, redefine_view.py).

Run:  cd emitrtl/tools/rtltrace-plugins && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from test_phases import BUSY, CE_DECL, CR, IDLE, MARK_DECL, SUSP, ce, mark, pack, t  # noqa: E402
from test_redefine_metrics import RT  # noqa: E402

import redefine_common as rc  # noqa: E402
import redefine_vocab as V  # noqa: E402
from redefine_findings import CRIT, INFO, MAJOR, findings  # noqa: E402

if RT is not None:
    from rtltrace import open_trace  # noqa: E402
    from rtltrace.analyze import analyze  # noqa: E402
    from rtltrace.perfetto import Writer  # noqa: E402
    from rtltrace.report import layout, write_csv, write_html, write_md  # noqa: E402
    from rtltrace.analyze import Table  # noqa: E402

PLUGINS = HERE.parent
SECTIONS = PLUGINS / "kernel_sections.json"


class Vocabulary(unittest.TestCase):
    def test_names(self):
        self.assertEqual(V.module_name("TOP.tb.cr.baseCr.core_3.rockettile.dcache"), "CE3 L1-D / scratchpad")
        self.assertEqual(V.module_name("TOP.tb.cr.baseCr.core.rockettile.core"), "CE0 core")
        self.assertEqual(V.module_name("TOP.tb.cr.baseCr.orch.ceIfc_2"), "CE2 ↔ Orchestrator interface")
        self.assertEqual(V.module_name("TOP.tb.cr.baseCr.uncore.cache.mshrs.mshrs_11"), "LLC MSHRs (miss handlers)")
        self.assertIsNone(V.module_name("TOP.tb.something.else"))
        self.assertEqual(V.client_name("Core 1 DCache"), "CE1 L1-D")
        self.assertEqual(V.client_name("[CR_DMA] MBus Client"), "CR-DMA (data)")
        self.assertEqual(V.section_name("ScratchpadMemory[ce2]"), "CE2 scratchpad")
        self.assertEqual(V.section_name("GlobalDataLow[r33]"), "global data region 33")
        self.assertEqual(V.section_name("GlobalDataLow[r33]", {"GlobalDataLow[r33]": "W (in, 64 KB)"}),
                         "W (in, 64 KB)")
        self.assertEqual(V.region_name("rt.DMA_LLC2SPM"), "runtime · DMA LLC→SPM call")
        self.assertEqual(V.region_name("untagged"), "busy, outside any marker")
        self.assertEqual(V.region_name("MICRO2X8"), "MICRO2X8")
        for term in V.GLOSSARY:
            self.assertEqual(len(V.GLOSSARY[term]), 3, term)

    def test_limited_by(self):
        st = rc.PhaseStats()
        st.cycles["Busy"], st.instret, st.counted = 100, 2, True
        self.assertEqual(rc.limited_by(st, 2, {}), "blocked")  # busy, IPC 0.02: a flush, not a wake-up wait
        st.instret = 80
        self.assertEqual(rc.limited_by(st, 2, {}), "control")  # no FP
        st.flops = 100
        self.assertEqual(rc.limited_by(st, 2, {}), "instruction")  # 1 flop/cycle < 0.6 x 2
        st.cycles["Suspended"] = 500
        self.assertEqual(rc.limited_by(st, 2, {}), "waiting")


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class Buffers(unittest.TestCase):
    def test_manifest_and_launch_names(self):
        from rtltrace.regions import load
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "rtl_sim_results").mkdir()
            (d / "rtl_sim_results" / "buffers.txt").write_text(
                "name  dir  addr_start  addr_end  size_bytes\n"
                "buf_arg_0.bin  in   0x60000000  0x60003fff  16384\n"
                "buf_arg_1.bin  in   0x64000000  0x6400ffff  65536\n"
                "buf_arg_2.bin  out  0x68000000  0x68003fff  16384\n")
            (d / "launches.json").write_text(json.dumps({"kernels": [{"entry": "k", "elf": "k.elf", "args": [
                {"kind": "buffer", "size": 16384, "dir": "in", "name": "A"},
                {"kind": "buffer", "size": 65536, "dir": "in", "name": "W"},
                {"kind": "buffer", "size": 16384, "dir": "out", "name": "Z"}]}]}))
            w = []
            bufs = rc.kernel_buffers({"launch": str(d / "launches.json")}, load(SECTIONS), w)
            self.assertEqual(rc.buffer_sections(bufs), {"GlobalDataLow[r32]": "A (in, 16 KB)",
                                                        "GlobalDataLow[r33]": "W (in, 64 KB)",
                                                        "GlobalDataLow[r34]": "Z (out, 16 KB)"})
            self.assertEqual(w, [])


class Findings(unittest.TestCase):
    METRICS = {"config": {"macs_per_ce": 1}, "warnings": [], "kernels": [{
        "kernel": "k", "C_o": 1000, "gamma_e": 0.25,
        "ce_time": {f"{CR}.orch.ceIfc_0": {"Busy": 900, "Idle": 100}, f"{CR}.orch.ceIfc_1": {"Busy": 500, "Idle": 500}},
        "sub_windows": [{"name": "setup", "pct": 30.0, "mean_active_ces": 1.0}],
        "cmu": [{"op": "FLUSH_ALL", "cycles": 100}],
        "waterfall": [
            {"step": "CEs idle / launching", "ce_cycles": 600, "pct": 30.0, "gamma_e_if_removed": 0.36},
            {"step": "CEs suspended (waiting on DMA, sync, ...)", "ce_cycles": 400, "pct": 20.0,
             "gamma_e_if_removed": 0.5},
            {"step": "busy in rt.LLC_FLUSH", "ce_cycles": 100, "pct": 5.0, "gamma_e_if_removed": 0.55},
            {"step": "busy in rt.FRAME_ALLOC", "ce_cycles": 10, "pct": 0.5, "gamma_e_if_removed": 0.56},
            {"step": "compute below peak (MICRO)", "ce_cycles": 390, "pct": 19.5, "gamma_e_if_removed": 1},
            {"step": "ideal (Ci x N_CE)", "ce_cycles": 500, "pct": 25.0, "gamma_e_if_removed": None}]}]}
    PHASES = {"source": "probe", "warnings": ["PC markers: 2 × END without a BEGIN"], "rows": [
        {"kernel": "k", "ce": "all", "region": "MICRO", "busy": 890, "suspended": 0, "ipc": 0.9,
         "flops_per_cycle": 1.0, "limited_by": "instruction"},
        {"kernel": "k", "ce": "all", "region": "rt.SUSPEND", "busy": 0, "suspended": 400, "ipc": None,
         "flops_per_cycle": None, "limited_by": "waiting"},
        {"kernel": "k", "ce": "all", "region": "rt.LLC_FLUSH", "busy": 100, "suspended": 0, "ipc": 0.0,
         "flops_per_cycle": 0, "limited_by": "blocked"}]}
    DMA = {"gate": {"idle": 100, "waitGetD": 700, "issue": 200},
           "windows": [{"kernel": "k", "window": "steady", "engine_busy": 0.95, "bytes_per_cycle": 2.0,
                        "avg_in_flight": 1.05, "latency_p50": 18}],
           "requests": [{"type": "LLC2SPM", "queue_wait": 500, "service": 700}]}
    CACHE = {"issues": [{"id": "spm_s3_overlap", "count": 3, "per_ce": {"CE1": 3}, "regions": {"MICRO": 3},
                         "first_cycle": 42, "first_ce": "CE1", "first_addr": "CE1 scratchpad+0x10",
                         "rtl_path": f"{CR}.core_1.rockettile.dcache", "probe": "spm_s3_overlap",
                         "src": "NBDCache.scala:962"}],
             "windows": [{"window": "k", "from": 0, "to": 1000, "l1d": [], "spm": [],
                          "llc_by_section": [{"section": "GlobalDataLow[r33]", "name": "W (in, 64 KB)",
                                              "accesses": 2048, "miss_ratio": 1.0}]}]}

    def test_rules_and_order(self):
        fs = findings(self.METRICS, self.PHASES, self.DMA, self.CACHE, {"spm_s3_overlap": "NBDCache.scala:962"})
        self.assertEqual(fs[0]["severity"], CRIT)
        self.assertIn("NBDCache.scala:962", fs[0]["rtl"])
        self.assertIn(f"{CR}.core_1.rockettile.dcache", fs[0]["rtl"])
        titles = [f["title"] for f in fs]
        perf = [f for f in fs if f["severity"] == MAJOR]
        self.assertEqual([round(f["pct"]) for f in perf], [30, 20, 20])  # biggest loss first
        self.assertIn("serial setup", perf[0]["title"])
        self.assertIn("DMA latency-bound", perf[1]["title"])
        self.assertIn("50 % of peak", next(f["evidence"] for f in fs if "below peak" in f["title"]))
        self.assertTrue(any("LLC flush" in f["action"] for f in fs))
        self.assertFalse(any("frame alloc" in x for x in titles))  # 0.5 % < MIN_PCT
        self.assertTrue(any("Unbalanced PC markers" == x for x in titles))
        self.assertEqual(fs[-1]["severity"], INFO)
        self.assertIn("W (in, 64 KB)", fs[-1]["evidence"])

    def test_missing_inputs(self):
        # no T_ops and no PC markers: the CE-time losses are still found, and the missing inputs are named
        k = {"kernel": "k", "C_o": 1000, "gamma_e": None, "T_ops": None,
             "ce_time": {f"{CR}.orch.ceIfc_0": {"Busy": 300, "Idle": 700}},
             "waterfall": [{"step": "CEs idle / launching", "ce_cycles": 700, "pct": 70.0, "gamma_e_if_removed": None},
                           {"step": "busy, not split", "ce_cycles": 300, "pct": 30.0, "gamma_e_if_removed": None}]}
        fs = findings({"config": {}, "warnings": [], "kernels": [k]}, {"source": None, "rows": [], "warnings": []})
        self.assertEqual([(f["severity"], f["title"]) for f in fs],
                         [(MAJOR, "CEs are idle 70.0 % of the CE-cycles"),
                          (INFO, "γe is not computed: the kernel declares no T_ops"),
                          (INFO, "No PC markers: CE time is not split by code region")])


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class ViewPlugin(unittest.TestCase):
    ALL = ("redefine_metrics.py", "ce_phases.py", "dma_metrics.py", "cache_metrics.py", "redefine_view.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "pc_markers.h").write_text("#define PCM_MICRO 0x7e0\n#define RE_PCM_SUSPEND 0x470\n")
        rc._PHASE_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        hz = (f"@RT 15 DECL {CR}.core_1.rockettile.dcache 30 event spm_s3_overlap l1d_spm_check 37 "
              "addr:32#addr,cmd:5 - v=1;src=NBDCache.scala:962")
        decls = [CE_DECL.format(i=i) for i in range(2)] + [MARK_DECL.format(core=c) for c in ("core", "core_1")] + [hz]
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 250, IDLE),
                 ce(1, 150, BUSY), ce(1, 200, SUSP), ce(1, 280, BUSY), ce(1, 300, IDLE),
                 mark("core", 110, 0x7e0, 0, 0), mark("core", 230, 0x7e1, 100, 120),
                 mark("core_1", 160, 0x7e0, 0, 0), mark("core_1", 190, 0x7e1, 25, 30),
                 mark("core_1", 195, 0x470, 26, 30), mark("core_1", 285, 0x471, 28, 30),
                 f"@RT {t(170)} EVENT {CR}.core_1.rockettile.dcache 30 {pack((0x30002010, 32), (0, 5))}"]
        log = self.d / "sim.log"
        log.write_text("\n".join(decls + sorted(lines, key=lambda s: int(s.split()[1]))) + "\n")
        args = {"markers": str(self.d / "pc_markers.h"), "t_ops": "200", "findings_json": str(self.d / "f.json")}
        return open_trace([str(log)], plugins=[str(PLUGINS / p) for p in self.ALL], plugin_args=args,
                          warn=lambda *_: None, regions=str(SECTIONS))

    def test_report_order_names_and_rtl(self):
        s = self.session()
        m = s.model
        sections = s.plugins.report(m)
        tables = analyze(m, 10, s.regions, s.labeler)
        items = layout(tables, sections)
        groups = list(dict.fromkeys(x.group for x in items))
        self.assertEqual(groups[0], V.G_FIND[0])
        self.assertLess(groups.index(V.G_EFF[0]), groups.index(V.G_REGION[0]))
        self.assertEqual(items[0].title, "What to look at first")
        self.assertEqual(items[0].rows[0][1], CRIT)  # the scratchpad hazard ranks first
        self.assertIn("NBDCache.scala:962", items[0].rows[0][-1].rtl)
        hw = [x for x in items if x.level == "hw"]
        self.assertTrue(any(x.title == "Modules: input-side vs output-side stall" for x in hw))
        # generic tables: friendly module names that keep the RTL path
        mod = next(x for x in tables if x.title == "Time in each value (change probes)").rows[0][0]
        self.assertEqual((str(mod), mod.rtl), ("CE0 ↔ Orchestrator interface", f"{CR}.orch.ceIfc_0"))
        # name map: every probe with RTL path and Chisel source
        nm = next(x for x in items if x.title == "RTL name map")
        srcs = {r[-1] for r in nm.rows}
        self.assertTrue({"CEIfc.scala:71", "CeRocketCore.scala:60", "NBDCache.scala:962"} <= srcs)
        self.assertEqual(len(nm.rows), len(m.trace.probes))
        # code regions use friendly names, keep the marker label
        reg = next(x for x in items if x.title == "Code regions · all CEs")
        names = {str(r[2]): r[2].rtl for r in reg.rows}
        self.assertEqual(names.get("runtime · suspend (wait for wake-up)"), "rt.SUSPEND")
        f = json.loads((self.d / "f.json").read_text())
        self.assertEqual(f[0]["severity"], CRIT)
        with tempfile.TemporaryDirectory() as tmp:
            card = s.plugins.card()
            self.assertIn("kernel", card)
            write_html(f"{tmp}/r.html", tables, sections, card, [], "Kernel performance report")
            html = Path(f"{tmp}/r.html").read_text()
            self.assertIn('title="RTL: ', html)
            self.assertLess(html.index("What to look at first"), html.index("Modules: input-side vs output-side"))
            write_md(f"{tmp}/r.md", tables, sections, card, [], "Kernel performance report", html_name="r.html")
            md = Path(f"{tmp}/r.md").read_text()
            self.assertIn("## 1 · Findings", md)
            self.assertNotIn("Modules: input-side vs output-side stall", md)  # hardware detail stays in the HTML
            write_csv(f"{tmp}/csv", tables + [Table(x.title, x.columns, x.rows, x.text, x.key) for x in sections
                                              if x.columns])
            with open(f"{tmp}/csv/ce_phases___all_ces__ce_cycles.csv") as fh:  # CSV name kept from before
                head = next(csv.reader(fh))
            self.assertIn("region (RTL)", head)

    def test_perfetto_views(self):
        s = self.session()
        full = Writer(s.model, s.labeler, s.emitted, "full", s.plugins.keep()).build()
        kern = Writer(s.model, s.labeler, s.emitted, "kernel", s.plugins.keep()).build()
        procs = lambda d: [e["args"]["name"] for e in d["traceEvents"] if e.get("name") == "process_name"]  # noqa
        self.assertIn(f"CE0 core  [{s.model.trace.module_short(CR + '.core.rockettile.core')}]", procs(full))
        self.assertNotIn("CE0 core", " ".join(procs(kern)))  # raw probe tracks are not in the kernel view
        self.assertIn("CE code regions (PC_MARK)", procs(kern))
        self.assertIn("Correctness checks", procs(kern))
        self.assertTrue(any(p.startswith("CE0 ↔ Orchestrator interface") for p in procs(kern)))  # CeState kept
        marks = [e for e in full["traceEvents"] if e.get("ph") == "X" and e["args"].get("probe") == "ce_marker"]
        self.assertTrue(marks and all(e["args"]["chisel_src"] == "CeRocketCore.scala:60" for e in marks))
        self.assertIn("imm=BEGIN MICRO", marks[0]["name"])
        self.assertLess(len(json.dumps(kern)), len(json.dumps(full)))


if __name__ == "__main__":
    unittest.main()
