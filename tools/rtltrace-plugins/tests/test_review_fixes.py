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


if __name__ == "__main__":
    unittest.main()
