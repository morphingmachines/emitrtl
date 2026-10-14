"""Tests for the CE-phase engine (redefine_common), ce_phases.py, the redefine_metrics waterfall and dma_metrics.py.

Run:  cd emitrtl/tools/rtltrace-plugins && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from test_redefine_metrics import RT  # noqa: E402

import redefine_common as rc  # noqa: E402

if RT is not None:
    from rtltrace import open_trace  # noqa: E402

PLUGINS = HERE.parent
CR = "TOP.tb.cr.baseCr"
IDLE, PRE, LAUNCH, BUSY, SUSP = 0, 1, 2, 3, 4
CE_DECL = (f"@RT 15 DECL {CR}.orch.ceIfc_{{i}} 14 change CeState hyperop 3 value:3:e=CeState "
           "CeState=0:Idle/1:PreLaunch/2:Launch/3:Busy/4:Suspended v=1;src=CEIfc.scala:71")
MARK_DECL = (f"@RT 15 DECL {CR}.{{core}}.rockettile.core 9 event ce_marker ce_phase 78 "
             "hart:2:d,imm:12,instret:32,flops:32 - v=1;src=CeRocketCore.scala:60")
TLA = ("opcode:3:e=TLA,param:3,size:3:d,source:9:d,address:32#addr,corrupt:1:b TLA=0:PutFullData/1:PutPartialData/"
       "2:ArithmeticData/3:LogicalData/4:Get/5:Hint/6:AcquireBlock/7:AcquirePerm")
TLD = ("opcode:3:e=TLD,param:2,size:3:d,source:9:d,sink:1:d,denied:1:b,corrupt:1:b TLD=0:AccessAck/1:AccessAckData/"
       "2:HintAck/4:Grant/5:GrantData/6:ReleaseAck")
IN_META = ("tl={ch};port=LLC_in;side=in;clients=Core~200~20DCache@64-67|[CR_DMA]~20MBus~20Client@128-131;"
           "managers=scratchRam@0x60000000/0x1fffffff;beat=32")
DMA_DECLS = [
    f"@RT 15 DECL {CR}.uncore 1 fire LLC_in.a LLC 51 {TLA} v=1;{IN_META.format(ch='a')}",
    f"@RT 15 DECL {CR}.uncore 2 fire LLC_in.d LLC 20 {TLD} v=1;{IN_META.format(ch='d')}",
    f"@RT 15 DECL {CR}.uncore.crDma.descriptors 5 event dma_start CR_DMA 147 type:3:d,src:32#addr,dst:32#addr,"
    "rowBytes:16:d,stride:16:d,rows:16:d,sync:32 - v=1;src=nodeImp.scala:470",
    f"@RT 15 DECL {CR}.uncore.crDma.descriptors 6 event dma_queued CR_DMA 1 busy:1:b - v=1;src=nodeImp.scala:240",
    f"@RT 15 DECL {CR}.uncore.crDma.mbusMaster 7 valid dma_done CR_DMA 0 - - v=1;src=nodeImp.scala:1186",
]


def t(cycle: float) -> int:
    return int(cycle * 10 + 5)


def pack(*fields: tuple[int, int]) -> str:
    v = 0
    for val, width in fields:
        v = v << width | (val & ((1 << width) - 1))
    return f"{v:x}"


def ce(i: int, cycle: int, state: int) -> str:
    return f"@RT {t(cycle)} EVENT {CR}.orch.ceIfc_{i} 14 {state:x}"


def mark(core: str, cycle: int, imm: int, instret: int, flops: int) -> str:
    return f"@RT {t(cycle)} EVENT {CR}.{core}.rockettile.core 9 {pack((0, 2), (imm, 12), (instret, 32), (flops, 32))}"


def M(t, imm, ir=None, fl=None):
    return rc.Mark(t, imm, ir, fl)


NAMES = {0x7e0: "MICRO", 0x7f0: "DMA", 0x720: "SUSPEND"}


class PhaseEngine(unittest.TestCase):
    """Pure functions: no rtltrace needed."""

    def test_nesting_resume_and_reset(self):
        tl = [(0, 10, "Idle"), (10, 12, "PreLaunch"), (12, 14, "Launch"), (14, 40, "Busy"), (40, 60, "Suspended"),
              (60, 62, "Launch"), (62, 80, "Busy"), (80, 100, "Idle")]
        marks = [M(20, 0x7f0), M(25, 0x7f1), M(30, 0x720), M(65, 0x721), M(70, 0x7e0)]  # MICRO left open at Idle
        p = Counter()
        segs = rc.build_phases(tl, marks, NAMES, p)
        got = [(s.t0, s.t1, s.label, s.state) for s in segs]
        self.assertIn((20, 25, "DMA", "Busy"), got)
        self.assertIn((40, 60, "SUSPEND", "Suspended"), got)  # the stack survives Suspended -> Launch -> Busy
        self.assertIn((60, 62, "SUSPEND", "Launch"), got)
        self.assertIn((70, 80, "MICRO", "Busy"), got)
        self.assertIn((80, 100, "idle", "Idle"), got)  # cleared at Idle
        self.assertEqual(p, Counter({"regions left open at the end of a hyperOp": 1}))
        self.assertEqual(sum(s.t1 - s.t0 for s in segs), 100)

    def test_nested_and_bad_ends(self):
        tl = [(0, 100, "Busy")]
        marks = [M(10, 0x7e0), M(20, 0x7f0), M(30, 0x7f1), M(40, 0x7e1), M(50, 0x721),  # END without BEGIN
                 M(60, 0x7e0), M(65, 0x7f0), M(70, 0x7e1)]  # END of the outer region closes the inner one too
        p = Counter()
        segs = rc.build_phases(tl, marks, NAMES, p)
        inner = [s for s in segs if s.label == "DMA"]
        self.assertEqual(inner[0].path, ("MICRO", "DMA"))
        self.assertEqual([(s.t0, s.t1) for s in segs if s.label == "untagged"], [(0, 10), (40, 60), (70, 100)])
        self.assertEqual(p, Counter({"END without a BEGIN": 1,
                                     "END closed an outer region (inner regions not closed)": 1}))

    def test_stats_counters_and_wrap(self):
        tl = [(0, 100, "Busy")]
        marks = [M(10, 0x7e0, 2 ** 32 - 10, 0), M(60, 0x7e1, 40, 40)]  # instret wraps: 50 retired
        segs = rc.build_phases(tl, marks, NAMES, Counter())
        st = rc.phase_stats(segs, [(m.t, m.instret, m.flops) for m in marks], 0, 100)
        self.assertEqual((st["MICRO"].busy, st["MICRO"].instret, st["MICRO"].flops), (50, 50, 40))
        self.assertEqual(rc.classify(st["MICRO"], 2, {}), "instruction-bound")  # IPC 1, 0.8 flops/cycle < 0.6 x 2
        st["MICRO"].flops = 50 * 2
        self.assertEqual(rc.classify(st["MICRO"], 2, {}), "compute-bound")
        st["MICRO"].instret = 10
        self.assertEqual(rc.classify(st["MICRO"], 2, {}), "stall-bound")
        self.assertEqual(rc.counters_in([(0, 0, 0), (100, 100, 200)], 25, 75), (50, 100))

    def test_align_offset(self):
        stops = [100, 300, 700, 900]
        edges = [100 + 11030 + 3, 300 + 11030 + 3, 700 + 11030 + 3, 5000]
        off, q = rc.align_offset(stops, edges)
        self.assertEqual(off, 11033)
        self.assertAlmostEqual(q, 0.75)

    def test_marker_names(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d) / "pc_markers.h"
            h.write_text("#define PC_MARKERS_H\n#define PC_MARKERS_ENABLE 1\n#define PCM_MICRO2X8 0x7e0 /* x */\n"
                         "#define CE_SUSPEND 0x720\n#define RE_PCM_DMA_LLC2SPM 0x400\n#define ODD 0x401\n")
            self.assertEqual(rc.marker_names([h]), {0x7e0: "MICRO2X8", 0x720: "CE_SUSPEND", 0x400: "rt.DMA_LLC2SPM"})

    def test_sub_windows(self):
        tls = {"a": [(0, 10, "Idle"), (10, 100, "Busy"), (100, 120, "Idle")],
               "b": [(0, 30, "Idle"), (30, 80, "Busy"), (80, 120, "Idle")]}
        self.assertEqual(rc.sub_windows(tls, 10, 100), [("setup", 10, 30), ("steady", 30, 80), ("tail", 80, 100)])


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class PhasePlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "pc_markers.h").write_text("#define PCM_MICRO 0x7e0\n#define PCM_DMA 0x7f0\n")
        rc._PHASE_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def trace(self, lines, extra=(), decls=None, plugins=("ce_phases.py", "redefine_metrics.py"), **args):
        log = self.d / "sim.log"
        decls = decls if decls is not None else [CE_DECL.format(i=i) for i in range(2)] + \
            [MARK_DECL.format(core=c) for c in ("core", "core_1")]
        log.write_text("\n".join(list(decls) + sorted(lines, key=lambda s: int(s.split()[1])) + list(extra)) + "\n")
        args = {"markers": str(self.d / "pc_markers.h"), "phases_json": str(self.d / "p.json"),
                "json": str(self.d / "m.json"), "dma_json": str(self.d / "d.json"), "t_ops": "200", **args}
        s = open_trace([str(log)], plugins=[str(PLUGINS / p) for p in plugins], plugin_args=args,
                       warn=lambda *_: None)
        for p in s.plugins.active:
            p.report(s.model)
        return s

    def test_probe_phases_and_waterfall(self):
        # 2 CEs, window 100..300. CE0: DMA 110-130, MICRO 130-230 (100 instr, 200 flops), then Idle at 250.
        # CE1 starts at 150 (setup 100-150), MICRO 160-260 (50 instr, 100 flops), suspended 260-280, Idle at 300.
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 250, IDLE),
                 ce(1, 150, BUSY), ce(1, 260, SUSP), ce(1, 280, BUSY), ce(1, 300, IDLE),
                 mark("core", 110, 0x7f0, 0, 0), mark("core", 130, 0x7f1, 10, 0),
                 mark("core", 131, 0x7e0, 10, 0), mark("core", 231, 0x7e1, 110, 200),
                 mark("core_1", 160, 0x7e0, 0, 0), mark("core_1", 260, 0x7e1, 50, 100)]
        self.trace(lines)
        p = json.loads((self.d / "p.json").read_text())
        self.assertEqual(p["source"], "probe")
        rows = {(r["ce"], r["region"]): r for r in p["rows"]}
        self.assertEqual(rows[("CE0", "MICRO")]["busy"], 100)
        self.assertEqual((rows[("CE0", "MICRO")]["instret"], rows[("CE0", "MICRO")]["flops"]), (100, 200))
        self.assertEqual(rows[("CE0", "MICRO")]["class"], "compute-bound")
        self.assertEqual(rows[("CE1", "MICRO")]["class"], "stall-bound")  # IPC 0.5
        self.assertEqual(rows[("CE0", "DMA")]["busy"], 20)
        m = json.loads((self.d / "m.json").read_text())
        k = m["kernels"][0]
        self.assertEqual((k["window"], k["C_o"]), ([100.5, 300.5], 200))
        self.assertEqual([(s["name"], s["cycles"]) for s in k["sub_windows"]],
                         [("setup", 50), ("steady", 100), ("tail", 50)])
        wf = {w["step"]: w["ce_cycles"] for w in k["waterfall"]}
        self.assertAlmostEqual(sum(wf.values()), 2 * 200)
        self.assertEqual(wf["CEs idle / launching"], 50 + 50)  # CE1 before 150, CE0 after 250
        self.assertEqual(wf["CEs suspended (waiting on DMA, sync, ...)"], 20)
        self.assertEqual(wf["ideal (Ci x N_CE)"], 100)  # T_ops 200 / 2
        self.assertEqual(wf["busy in DMA"], 20)

    def test_commit_log_fallback(self):
        # No ce_marker probe: markers come from commit lines, 1000 cycles earlier than @RT time.
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 1100, BUSY), ce(0, 1200, SUSP), ce(0, 1300, BUSY),
                 ce(0, 1400, IDLE), ce(1, 1100, BUSY), ce(1, 1400, IDLE)]

        def commit(core, cyc, inst, op):
            return f"C{core}: {cyc:10d} [1] pc=[18000000] W[r 0=00000000][0] R[r 0=00000000] inst=[{inst}] {op}"

        nop = "00000013"
        extra = [commit(0, c, nop, "nop") for c in range(100, 110)]
        extra += [commit(0, 110, "7e000013", "li zero, 2016")]
        extra += [commit(0, c, "00b5f543", "fmadd.s fa0, fa1, fa1, fa0") for c in range(111, 199)]
        extra += [commit(0, 199, "7e100013", "li zero, 2017"), commit(0, 300, nop, "nop"),
                  commit(0, 399, nop, "nop")]
        decls = [CE_DECL.format(i=i) for i in range(2)]
        self.trace(lines, extra, decls=decls, plugins=("ce_phases.py",), log=str(self.d / "sim.log"))
        p = json.loads((self.d / "p.json").read_text())
        self.assertEqual(p["source"], "commit log")
        rows = {(r["ce"], r["region"]): r for r in p["rows"]}
        self.assertAlmostEqual(rows[("CE0", "MICRO")]["busy"], 89, delta=1)
        self.assertEqual(rows[("CE0", "MICRO")]["flops"], 176)

    def test_dma_infer_and_probe(self):
        def a(cyc, op, src, addr):
            return f"@RT {t(cyc)} EVENT {CR}.uncore 1 {pack((op, 3), (0, 3), (5, 3), (src, 9), (addr, 32), (0, 1))}"

        def d(cyc, op, src):
            return f"@RT {t(cyc)} EVENT {CR}.uncore 2 {pack((op, 3), (0, 2), (5, 3), (src, 9), (0, 1), (0, 1), (0, 1))}"

        lines = [ce(0, 1, IDLE), ce(0, 100, BUSY), ce(0, 400, IDLE)]
        for i in range(10):  # serial engine: one 32 B Get every 20 cycles, response 10 cycles later
            lines += [a(110 + 20 * i, 4, 128, 0x40000000 + 32 * i), d(120 + 20 * i, 1, 128)]
        lines += [a(150, 4, 64, 0x60000000), d(152, 1, 64)]  # a D$ Get: not the DMA
        decls = [CE_DECL.format(i=0)] + DMA_DECLS
        self.trace(lines, decls=decls, plugins=("dma_metrics.py",), regions=str(PLUGINS / "kernel_sections.json"))
        dm = json.loads((self.d / "d.json").read_text())
        self.assertEqual(dm["mode"], "infer")
        self.assertEqual([(r["opcode"], r["section"], r["bytes"]) for r in dm["by_section"]],
                         [("Get", "GroupLocalMemory", 320)])
        w = dm["windows"][0]
        self.assertEqual((w["gets"], w["bytes"], w["get_interval_p50"], w["latency_p50"]), (10, 320, 20, 11))  # latency counts the response cycle
        self.assertAlmostEqual(w["active"], 110 / 300)
        # probe mode: one LLC2SPM request of 4 rows x 32 B into CE2's scratchpad, queued at 105, 110..190
        lines += [f"@RT {t(105)} EVENT {CR}.uncore.crDma.descriptors 6 0",
                  f"@RT {t(110)} EVENT {CR}.uncore.crDma.descriptors 5 "
                  f"{pack((1, 3), (0x40000000, 32), (0x30004000, 32), (32, 16), (512, 16), (4, 16), (0x140, 32))}",
                  f"@RT {t(190)} EVENT {CR}.uncore.crDma.mbusMaster 7"]
        rc._PHASE_CACHE.clear()
        self.trace(lines, decls=decls, plugins=("dma_metrics.py",), mbus="LLC?in")
        dm = json.loads((self.d / "d.json").read_text())
        self.assertEqual(dm["mode"], "probe")
        (r,) = dm["requests"]
        self.assertEqual((r["type"], r["bytes"], r["ce"], r["queue_wait"], r["service"]),
                         ("LLC2SPM", 128, 2, 5, 80))
        self.assertEqual((r["src_section"], r["dst_section"]), (None, None))  # no regions in this run


if __name__ == "__main__":
    unittest.main()
