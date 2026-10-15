"""Tests for llc_out.py on synthetic @RT traces (probe layouts as in test_cache_metrics.py).

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
from test_cache_metrics import CR, DECLS, ev, tl  # noqa: E402
from test_redefine_metrics import RT  # noqa: E402

if RT is not None:
    from rtltrace import open_trace  # noqa: E402

PLUGIN = HERE.parent / "llc_out.py"
METRICS = HERE.parent / "redefine_metrics.py"
CE = f"{CR}.orch.ceIfc_0"
TABLE = ["[kernel_buffers]",
         "name                  param       dir     addr_start    addr_end      size_bytes",
         "buf_arg_0.bin         A           in      0x60000000    0x6000007f    128",
         "buf_arg_1.bin         -           out     0x64000000    0x6400003f    64"]


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class LlcOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        out = [ev(1, CE, 14, "0"), ev(100, CE, 14, "3"), ev(400, CE, 14, "0")]  # kernel window 100..400
        out += tl(50, "out", 4, 0x60000000, 0)  # before the window
        out += tl(110, "out", 4, 0x60000000, 0) + tl(120, "out", 4, 0x60000020, 0)  # A: 2 lines ...
        out += tl(130, "out", 4, 0x60000000, 1)  # ... and line 0 again
        out += tl(200, "out", 0, 0x64000000, 1)  # second buffer (no param name): one Put
        out += tl(210, "out", 0, 0x40080000, 2) + tl(220, "out", 0, 0x40080020, 2)  # outside: one 2-line range
        out += tl(230, "out", 0, 0x40090000, 3)  # outside: a second range
        return out

    def run_plugins(self, lines, extra=(), **args):
        log = self.d / "sim.log"
        rt = sorted(lines, key=lambda s: int(s.split()[1]))
        log.write_text("\n".join(list(DECLS) + rt + list(extra)) + "\n")
        args = {"log": str(log), "llc_out_json": str(self.d / "o.json"), "json": str(self.d / "m.json"), **args}
        s = open_trace([str(log)], plugins=[str(METRICS), str(PLUGIN)], plugin_args=args, warn=lambda *_: None)
        for p in s.plugins.active:
            p.report(s.model)
        return json.loads((self.d / "o.json").read_text()), json.loads((self.d / "m.json").read_text())

    def test_buffers_from_log(self):
        out, metrics = self.run_plugins(self.lines(), TABLE)
        self.assertEqual(out["buffer_source"], "sim log")
        (k,) = out["kernels"]
        (mk,) = metrics["kernels"]
        for key in ("D_D_in", "D_D_out", "D_D"):  # same accounting as redefine_metrics.py
            self.assertEqual(k[key], mk[key], key)
        self.assertEqual((k["D_D_in"], k["D_D_out"], k["txns_before"], k["txns"]), (96, 128, 1, 7))
        self.assertEqual((k["D_i"], k["D_o"], k["D_D_buffers"], k["D_D_other"]), (128, 64, 128, 96))
        by = {(b["buffer"], b["opcode"]): b for b in k["by_buffer"]}
        a = by[("A", "Get")]
        self.assertEqual((a["txns"], a["unique_lines"], a["max_per_line"], a["coverage"]), (3, 2, 2, 0.5))
        self.assertEqual(by[("arg1", "PutFullData")]["txns"], 1)  # unnamed buffer (param "-") -> arg<i>
        self.assertEqual(by[("other", "PutFullData")]["txns"], 3)
        rng = [(x["from"], x["to"], x["lines"]) for x in k["other_ranges"]]
        self.assertEqual(rng, [("0x40080000", "0x4008003f", 2), ("0x40090000", "0x4009001f", 1)])

    def test_legacy_marker(self):
        legacy = ["[load_kernel_env] buffer addresses:"] + TABLE[1:]
        out, _ = self.run_plugins(self.lines(), legacy)
        self.assertEqual(out["buffer_source"], "sim log")
        self.assertIn("A", {b["buffer"] for b in out["kernels"][0]["by_buffer"]})

    def test_buffers_file_fallback(self):
        bt = self.d / "buffers.txt"
        bt.write_text("name                  dir     addr_start    addr_end      size_bytes\n"
                      "buf_arg_0.bin         in      0x60000000    0x6000007f    128\n")
        out, _ = self.run_plugins(self.lines(), [f"[load_kernel_env] buffer manifest -> {bt}"])
        self.assertEqual(out["buffer_source"], str(bt))
        self.assertTrue(any("has no [kernel_buffers] table" in w for w in out["warnings"]), out["warnings"])
        by = {(b["buffer"], b["opcode"]) for b in out["kernels"][0]["by_buffer"]}
        self.assertIn(("arg0", "Get"), by)

    def test_no_buffers(self):
        out, _ = self.run_plugins(self.lines())
        self.assertTrue(any("no buffer addresses" in w for w in out["warnings"]), out["warnings"])
        self.assertEqual({b["buffer"] for b in out["kernels"][0]["by_buffer"]}, {"other"})


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class BufferTables(unittest.TestCase):
    """redefine_common's parser, shared by llc_out.py and the buffer names in the other reports."""

    def test_six_column_file_keeps_names_and_sections(self):
        import redefine_common as rc
        from rtltrace.regions import load
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "rtl_sim_results").mkdir()
            (d / "rtl_sim_results" / "buffers.txt").write_text("\n".join(TABLE[1:]) + "\n")
            bufs = rc.kernel_buffers({"elf": str(d / "k.elf")}, load(HERE.parent / "kernel_sections.json"), [])
        self.assertEqual([(b.name, b.arg, b.dir, b.start, b.size) for b in bufs],
                         [("A", 0, "in", 0x60000000, 128), ("arg1", 1, "out", 0x64000000, 64)])
        self.assertEqual({b.section for b in bufs}, {"GlobalDataLow[r32]", "GlobalDataLow[r33]"})

    def test_blocks_per_kernel(self):
        import redefine_common as rc
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "sim.log"
            log.write_text("\n".join(TABLE + ["@RT 5 EVENT x 1 0"] + TABLE[:3] + ["done"]) + "\n")
            tables, src = rc.buffer_tables({"log": str(log)})
        self.assertEqual(src, "sim log")
        self.assertEqual([[b.name for b in t] for t in tables], [["A", "arg1"], ["A"]])


if __name__ == "__main__":
    unittest.main()
