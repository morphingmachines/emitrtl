"""Tests for overlap_metrics.py: compute / CR-DMA overlap, prefetch iterations and the buffer-safety check.

Run:  cd emitrtl/tools/rtltrace-plugins && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from test_phases import BUSY, CE_DECL, CR, DMA_DECLS, IDLE, MARK_DECL, SUSP, ce, mark, pack, t  # noqa: E402
from test_redefine_metrics import RT  # noqa: E402

import redefine_common as rc  # noqa: E402
from redefine_findings import findings  # noqa: E402

if RT is not None:
    from rtltrace import open_trace  # noqa: E402

PLUGINS = HERE.parent
SPM_DECL = (f"@RT 15 DECL {CR}.core.rockettile.dcache 20 event spm_access l1d_spm 33 isWrite:1:b,addr:32#addr - "
            "v=1;src=NBDCache.scala:949")
DST1, DST2 = 0x30000840, 0x300008c0  # two 128 B buffers in CE0's scratchpad


def start(cyc, dst):  # LLC2SPM, 4 rows x 32 B
    return (f"@RT {t(cyc)} EVENT {CR}.uncore.crDma.descriptors 5 "
            f"{pack((1, 3), (0x40000000, 32), (dst, 32), (32, 16), (512, 16), (4, 16), (0x140, 32))}")


def spm(cyc, write, addr):
    return f"@RT {t(cyc)} EVENT {CR}.core.rockettile.dcache 20 {pack((write, 1), (addr, 32))}"


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class Overlap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "pc_markers.h").write_text("#define PCM_MICRO 0x7e0\n#define RE_PCM_DMA_LLC2SPM 0x400\n")
        rc._PHASE_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self) -> Path:
        # Iteration 1: issue at 120 (end of the runtime call), DMA 130-230, compute 130-200 -> data late by 30, the CE
        # sleeps 205-240. Iteration 2: issue 260, DMA 265-300, compute 270-370 -> ready. A load at 280 reads the
        # buffer DMA 2 is still filling (race); a load at 285 reads the other buffer (fine).
        lines = [ce(0, 1, IDLE), ce(0, 100, BUSY), ce(0, 205, SUSP), ce(0, 240, BUSY), ce(0, 400, IDLE),
                 mark("core", 110, 0x400, 0, 0), mark("core", 120, 0x401, 5, 0),
                 mark("core", 130, 0x7e0, 6, 0), mark("core", 200, 0x7e1, 76, 140),
                 mark("core", 250, 0x400, 77, 140), mark("core", 260, 0x401, 82, 140),
                 mark("core", 270, 0x7e0, 83, 140), mark("core", 370, 0x7e1, 183, 340),
                 f"@RT {t(125)} EVENT {CR}.uncore.crDma.descriptors 6 0", start(130, DST1),
                 f"@RT {t(230)} EVENT {CR}.uncore.crDma.mbusMaster 7",
                 f"@RT {t(262)} EVENT {CR}.uncore.crDma.descriptors 6 0", start(265, DST2),
                 f"@RT {t(300)} EVENT {CR}.uncore.crDma.mbusMaster 7",
                 spm(280, 0, DST2 + 4), spm(285, 0, DST1 + 4)]
        decls = [CE_DECL.format(i=0), MARK_DECL.format(core="core"), SPM_DECL] + DMA_DECLS
        log = self.d / "sim.log"
        log.write_text("\n".join(decls + sorted(lines, key=lambda s: int(s.split()[1]))) + "\n")
        return log

    def test_two_iterations(self):
        log = self._log()
        s = open_trace([str(log)], plugins=[str(PLUGINS / p) for p in ("ce_phases.py", "dma_metrics.py",
                                                                        "overlap_metrics.py")],
                       plugin_args={"markers": str(self.d / "pc_markers.h"), "overlap_json": str(self.d / "o.json"),
                                    "compute": "MICRO"},  # the region list must not break ce_phases' thresholds
                       warn=lambda *_: None)
        self.assertIn("ce-phases", [p.name for p in s.plugins.active])
        o = json.loads((self.d / "o.json").read_text())
        self.assertEqual(o["issue_source"], "runtime DMA-call markers")
        (r,) = o["ces"]
        self.assertEqual((r["inflight"], r["compute"], r["overlap"]), (150, 170, 100))  # [120,230]+[260,300]
        self.assertAlmostEqual(r["hidden"], 100 / 150)
        self.assertEqual(r["exposed_suspended"], 25)  # suspended 205-230 while DMA 1 was still in flight
        it = o["iterations"]
        self.assertEqual([(x["ready"], x["exposed_wait"], x["engine_wait"], x["service"]) for x in it],
                         [(False, 30, 10, 100), (True, 0, 5, 35)])
        self.assertEqual(it[0]["wake"], 10)  # data at 230, CE running again at 240
        (sf,) = o["safety"]
        self.assertEqual((sf["loads_in_flight_dst"], sf["stores_in_flight_src"]), (1, 0))
        self.assertEqual(sf["first"]["addr"], hex(DST2 + 4))
        titles = [x.title for x in s.plugins.report(s.model) if x.group]
        self.assertIn("Prefetch iterations (double buffering)", titles)

    def test_perfetto_cross_check(self):
        # overlap_xcheck.py recomputes everything from the Perfetto JSON alone (kernel-view tracks and raw probes)
        import contextlib
        import io

        import overlap_xcheck
        from rtltrace.perfetto import Writer
        stem = self.d / "x"
        s = open_trace([str(self._log())], plugins=[str(PLUGINS / p) for p in (
            "redefine_metrics.py", "ce_phases.py", "dma_metrics.py", "overlap_metrics.py", "redefine_view.py")],
            plugin_args={"markers": str(self.d / "pc_markers.h"), "overlap_json": f"{stem}.overlap.json"},
            warn=lambda *_: None)
        for view, suffix in (("kernel", ".perfetto.json"), ("full", ".perfetto_full.json")):
            doc = Writer(s.model, s.labeler, s.emitted, view, s.plugins.keep()).build()
            Path(f"{stem}{suffix}").write_text(json.dumps(doc))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = overlap_xcheck.main([str(stem), "--example", "CE0:0"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("prefetch iterations, field by field (2): plugin vs raw identical", out.getvalue())


class OverlapFindings(unittest.TestCase):
    def test_diagnosis(self):
        from test_view import Findings
        ov = {"s3": {"same_buffer": 0, "other_buffer": 3},
              "ces": [{"kernel": "k", "ce": "CE0", "compute": 900, "inflight": 1000, "overlap": 800,
                       "iterations": 2, "ready": 0.0}],
              "iterations": [{"kernel": "k", "compute": 450, "latency": 600, "engine_wait": 400, "service": 200,
                              "exposed_wait": 150, "ahead": 2, "ready": False}] * 2,
              "engine": [{"kernel": "k", "demand": 2.0, "capacity": 1.4, "demand_ratio": 2.0 / 1.4}],
              "safety": [{"kernel": "k", "ce": "CE0", "loads_in_flight_dst": 0, "stores_in_flight_src": 0,
                          "spm_accesses": 10}]}
        fs = findings(Findings.METRICS, Findings.PHASES, Findings.DMA, Findings.CACHE, {}, ov)
        hz = fs[0]
        self.assertIn("hardware, not a kernel race", hz["title"])
        susp = next(f for f in fs if "suspended" in f["title"])
        self.assertIn("shared CR-DMA cannot keep up", susp["title"])
        self.assertIn("1.43×", susp["action"])
        self.assertTrue(any(f["title"] == "Double buffering is in place and race-free" for f in fs))


if __name__ == "__main__":
    unittest.main()
