"""Regression tests for the review fixes (merged / duplicate kernels, missing boundary, multi-CR labels, counter
wrap, findings evidence, CR-DMA correctness events).

Run:  cd emitrtl/tools/rtltrace-plugins && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import redefine_common as rc  # noqa: E402
import redefine_metrics  # noqa: E402
import test_redefine_metrics as trm  # noqa: E402
import test_view  # noqa: E402
from redefine_findings import CRIT, findings  # noqa: E402
from test_redefine_metrics import BUSY, IDLE, RT, ce, get, launches  # noqa: E402

F = test_view.Findings  # fixtures only; not bound to a TestCase name here, so its tests are not collected twice
METRICS, PHASES, CACHE = F.METRICS, F.PHASES, F.CACHE
del F


@unittest.skipIf(RT is None, "rtltrace not found")
class MetricsFixes(unittest.TestCase):
    run_plugin = trm.Metrics.run_plugin
    setUp = trm.Metrics.setUp
    tearDown = trm.Metrics.tearDown

    def test_merged_kernels_combine(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 300, IDLE)]  # one activity interval
        lines += get(150, 0x60000000, 1)
        js = launches(Path(self.tmp.name) / "launches.json", [
            {"elf": "a.elf", "entry": "a", "grid": [1, 1, 2], "t_ops": 100,
             "args": [{"kind": "buffer", "size": 32, "dir": "in"}]},
            {"elf": "b.elf", "entry": "b", "grid": [1, 1, 2], "t_ops": 300,
             "args": [{"kind": "buffer", "size": 64, "dir": "out"}]}])
        out, _ = self.run_plugin(lines, launch=js, m_i="1024")
        (k,) = out["kernels"]
        self.assertEqual((k["kernel"], k["T_ops"], k["D_i"], k["D_o"]), ("a+b", 400, 32, 64))

    def test_duplicate_kernel_names(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 200, IDLE), ce(0, 1000, BUSY),
                 ce(0, 1100, IDLE)]
        js = launches(Path(self.tmp.name) / "launches.json", [
            {"elf": "a.elf", "entry": "mm", "grid": [1, 1, 2], "args": []},
            {"elf": "a.elf", "entry": "mm", "grid": [1, 1, 2], "args": []}])
        out, _ = self.run_plugin(lines, launch=js, m_i="1024")
        self.assertEqual([k["kernel"] for k in out["kernels"]], ["mm", "mm#2"])

    def test_no_boundary_port(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 300, IDLE)]
        js = launches(Path(self.tmp.name) / "launches.json", [
            {"elf": "a.elf", "entry": "a", "grid": [1, 1, 2], "args": [{"kind": "buffer", "size": 32, "dir": "in"}]}])
        out, _ = self.run_plugin(lines, launch=js, m_i="1024", boundary="NoSuchPort")
        k = out["kernels"][0]
        self.assertEqual((k["D_D"], k["rho"], k["phi"]), (None, None, None))
        self.assertFalse(any("ρ=" in w for w in out["warnings"]), out["warnings"])


class CommonFixes(unittest.TestCase):
    def test_multi_cr_labels(self):
        a = rc.ce_key("TOP.cr_fabric.CR_Fabric_0.cr.baseCr.orch.ceIfc_2")
        b = rc.ce_key("TOP.cr_fabric.CR_Fabric_1.cr.baseCr.core_2.rockettile.core")
        self.assertEqual((rc.ce_label(a, True), rc.ce_label(b, True)), ("CR_Fabric_0.CE2", "CR_Fabric_1.CE2"))
        self.assertEqual(rc.ce_label(a), "CE2")

    def test_counters_in_across_wrap(self):
        pts = [(0, 2 ** 32 - 50, 2 ** 32 - 10), (100, 50, 90)]  # both counters wrap: 100 retired, 100 flops
        self.assertEqual(rc.counters_in(pts, 0, 100), (100, 100))
        self.assertEqual(rc.counters_in(pts, 0, 50), (50, 50))

    def test_phase_stats_pro_rata_past_window(self):
        marks = [rc.Mark(10, 0x7e0, 0, 0), rc.Mark(110, 0x7e1, 100, 200)]
        segs = rc.build_phases([(0, 120, "Busy")], marks, {0x7e0: "MICRO"}, rc.Counter())
        st = rc.phase_stats(segs, [(m.t, m.instret, m.flops) for m in marks], 0, 60)
        self.assertEqual((st["MICRO"].instret, st["MICRO"].flops), (50, 100))


class FindingsFixes(unittest.TestCase):
    def test_below_peak_without_counters(self):
        f = findings(METRICS, None)
        below = next(x for x in f if x["title"].startswith("Compute runs below peak"))
        self.assertIn("No per-region flop counters", below["evidence"])
        self.assertNotIn("near peak", below["action"])

    def test_dma_issues_and_zero_hit_ratio(self):
        dma = {"issues": [{"id": "dma_overflow", "count": 2, "first_cycle": 10, "rtl_path": "x.regNode",
                           "probe": "dma_overflow", "src": None},
                          {"id": "dma_error", "count": 1, "first_cycle": 20, "rtl_path": "x.mbusMaster",
                           "probe": "dma_error", "src": "nodeImp.scala:1248", "source": 1, "denied": True,
                           "corrupt": False}]}
        cache = copy.deepcopy(CACHE)
        cache["issues"] = []
        cache["windows"][0]["l1d"] = [{"core": 0, "accesses": 5000, "hit_ratio": 0.0}]
        f = findings(METRICS, PHASES, dma, cache)
        crit = [x["title"] for x in f if x["severity"] == CRIT]
        self.assertTrue(any("descriptor(s) overwritten" in t for t in crit), crit)
        self.assertTrue(any("TileLink error" in t for t in crit), crit)
        self.assertTrue(any("Low L1-D hit rate on CE0" in x["title"] for x in f))


class InstructionMix(unittest.TestCase):
    def test_inst_class(self):
        c = {n: i for i, n in enumerate(rc.MIX_CLASSES)}
        cases = {0x509ef543: "fma",  # fmadd.s
                 0x5a8f7f4b: "fma",  # fnmsub.s
                 0x0007a287: "load",  # flw
                 0x0006a703: "load",  # lw
                 0x00062227: "store",  # fsw
                 0x00a62023: "store",  # sw
                 0x4208: "load",  # c.lw
                 0xe208: "store",  # c.fsw
                 0x0691: "other",  # c.addi
                 0x20f78553: "other",  # fmv.s (fsgnj.s)
                 0x0007f553: "fp_other",  # fadd.s
                 0x48000013: "other"}  # a PC marker nop
        for w, name in cases.items():
            self.assertEqual(rc.inst_class(w), c[name], hex(w))

    def test_mix_index_ranges(self):
        lg = rc.MixLog(cls=bytearray([1, 3, 3, 0, 1, 4]))
        ix = rc.MixIndex(lg)
        self.assertEqual(ix.delta(0, 6), {"other": 1, "fma": 2, "fp_other": 0, "load": 2, "store": 1})
        self.assertEqual(ix.delta(1, 4), {"other": 1, "fma": 0, "fp_other": 0, "load": 2, "store": 0})
        self.assertEqual(ix.delta(4, 99)["store"], 1)  # clamped to the stream

    def test_phase_stats_mix_and_stall_cycles(self):
        ix = rc.MixIndex(rc.MixLog(cls=bytearray([1, 1, 3, 0] * 5)))
        segs = [rc.PhaseSeg(0, 100, "R", "Busy", ("R",))]
        st = rc.phase_stats(segs, [(0, 0, 0), (50, 20, 20)], 0, 100, ix)["R"]
        self.assertEqual(dict(st.mix), {"other": 5, "fma": 10, "fp_other": 0, "load": 5, "store": 0})
        self.assertEqual(sum(st.mix.values()), st.instret)
        mc = rc.mix_cycles(st)
        self.assertEqual(mc["fma"], 10)
        self.assertEqual(mc["stall"], 100 - 20)  # busy 100, 20 retired

    def test_breakup_adds_up_to_the_loss(self):
        mix = {"fma": 500.0, "fp_other": 0.0, "load": 250.0, "store": 20.0, "other": 100.0, "stall": 130.0}
        parts = redefine_metrics.RedefineMetrics._breakup(mix, 500.0, 2000.0)
        self.assertEqual([p["part"] for p in parts], ["load", "store", "other", "stall"])
        self.assertAlmostEqual(sum(p["ce_cycles"] for p in parts), 500)  # busy (1000) - ideal (500)
        self.assertAlmostEqual(parts[-1]["gamma_e_if_removed"], 1.0 * 500 / (2000 - 500))
        # T_ops that does not match the retired FMAs shows up as its own part, keeping the sum exact
        parts = redefine_metrics.RedefineMetrics._breakup({**mix, "fma": 600.0}, 500.0, 2000.0)
        self.assertEqual(parts[-1]["part"], "fma_vs_t_ops")
        self.assertAlmostEqual(parts[-1]["ce_cycles"], 100)


if __name__ == "__main__":
    unittest.main()
