"""Tests for redefine_metrics.py on synthetic @RT traces with hand-computed metrics.

Run:  cd emitrtl/tools/rtltrace-plugins && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent / "redefine_metrics.py"


def _rtltrace_dir() -> Path | None:
    if os.environ.get("RTLTRACE_DIR"):
        return Path(os.environ["RTLTRACE_DIR"])
    for parent in HERE.parents:
        for cand in (parent / "rtltrace",  # emitrtl/tools/rtltrace, next to this directory
                     parent / "dependencies/emitrtl/tools/rtltrace",
                     parent / "playground/dependencies/emitrtl/tools/rtltrace"):
            if (cand / "rtltrace/__init__.py").is_file():
                return cand
    return None


RT = _rtltrace_dir()
if RT is not None:
    sys.path.insert(0, str(RT))
    from rtltrace import open_trace  # noqa: E402

UNCORE = "TOP.tb.cr.baseCr.uncore"
MGR = "managers=scratchRam@0x60000000/0x1fffffff|scratchRam@0x40000000/0xfffff"
DECLS = [
    f"@RT 15 DECL {UNCORE} 3 fire LLC_out.a LLC 46 opcode:3:e=TLA,param:3,size:3:d,source:4:d,address:32#addr,"
    "corrupt:1:b TLA=0:PutFullData/1:PutPartialData/2:ArithmeticData/3:LogicalData/4:Get/5:Hint/6:AcquireBlock/"
    f"7:AcquirePerm v=1;src=Uncore.scala:150;tl=a;port=LLC_out;side=in;clients=Distributes~20LLC@0-15;{MGR};beat=32",
    f"@RT 15 DECL {UNCORE} 4 fire LLC_out.d LLC 15 opcode:3:e=TLD,param:2,size:3:d,source:4:d,sink:1:d,denied:1:b,"
    "corrupt:1:b TLD=0:AccessAck/1:AccessAckData/2:HintAck/4:Grant/5:GrantData/6:ReleaseAck v=1;src=Uncore.scala:150;"
    f"tl=d;port=LLC_out;side=in;clients=Distributes~20LLC@0-15;{MGR};beat=32",
] + [
    f"@RT 15 DECL TOP.tb.cr.baseCr.orch.ceIfc_{i} 14 change CeState hyperop 3 value:3:e=CeState "
    "CeState=0:Idle/1:PreLaunch/2:Launch/3:Busy/4:Suspended v=1;src=CEIfc.scala:71" for i in range(2)
]
IDLE, PRE, BUSY = 0, 1, 3


def t(cycle: int) -> int:
    return cycle * 10 + 5


def ce(i: int, cycle: int, state: int) -> str:
    return f"@RT {t(cycle)} EVENT TOP.tb.cr.baseCr.orch.ceIfc_{i} 14 {state:x}"


def tl_a(cycle: int, opcode: int, address: int, source: int, size: int = 5) -> str:
    v = (((opcode << 3 | 0) << 3 | size) << 4 | source) << 32 | address
    return f"@RT {t(cycle)} EVENT {UNCORE} 3 {v << 1:x}"


def tl_d(cycle: int, opcode: int, source: int, size: int = 5) -> str:
    v = (((opcode << 2) << 3 | size) << 4 | source) << 3
    return f"@RT {t(cycle)} EVENT {UNCORE} 4 {v:x}"


def get(cycle: int, address: int, source: int) -> list[str]:
    return [tl_a(cycle, 4, address, source), tl_d(cycle + 2, 1, source)]


def put(cycle: int, address: int, source: int) -> list[str]:
    return [tl_a(cycle, 0, address, source), tl_d(cycle + 2, 0, source)]


def fmadd(n: int) -> list[str]:
    return [f"C0:  {1000 + i} [1] pc=[00010040] W[r 0=00000000][0] R[r 0=00000000] R[r 0=00000000] "
            "inst=[00b5f543] fmadd.s fa0, fa1, fa1, fa0" for i in range(n)]


def launches(path: Path, kernels: list[dict]) -> str:
    path.write_text(json.dumps({"kernels": kernels}))
    return str(path)


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class Metrics(unittest.TestCase):
    def run_plugin(self, lines: list[str], **args):
        d = Path(self.tmp.name)
        log = d / "sim.log"
        log.write_text("\n".join(DECLS + sorted(lines, key=lambda s: int(s.split()[1]) if s.startswith("@RT") else 0))
                       + "\n")
        args = {"json": str(d / "m.json"), **args}
        s = open_trace([str(log)], plugins=[str(PLUGIN)], plugin_args=args, warn=lambda *_: None)
        p = s.plugins.active[0]
        p.report(s.model)
        return json.loads((d / "m.json").read_text()), s

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_kernel(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE),
                 ce(0, 100, PRE), ce(0, 110, BUSY), ce(0, 300, IDLE),  # window = [100, 300] -> Co = 200
                 ce(1, 150, BUSY), ce(1, 250, IDLE)]
        lines += get(50, 0x40000000, 1)  # before the window: not in D_D
        lines += get(150, 0x60000000, 1) + get(160, 0x60000020, 2)  # 64 B in
        lines += put(200, 0x40000040, 3)  # 32 B out
        lines += put(400, 0x60000040, 1)  # after the window: not in D_D
        js = launches(Path(self.tmp.name) / "launches.json", [{
            "elf": "k.elf", "entry": "k", "grid": [1, 1, 2], "t_ops": 400,
            "args": [{"kind": "buffer", "size": 32, "dir": "in"}, {"kind": "buffer", "size": 16, "dir": "out"},
                     {"kind": "scalar", "type": "i32", "value": 3}]}])
        log_lines = lines + fmadd(200)
        out, _ = self.run_plugin(log_lines, launch=js, m_i="192")
        # the commit lines live in the same log
        self.assertEqual(out["retired"], None)  # no log= given: no cross-check
        r = out["kernels"][0]
        self.assertEqual(r["window"], [100.5, 300.5])
        self.assertEqual(r["C_o"], 200)
        self.assertEqual(r["N_CE"], 2)
        self.assertEqual(r["C_i"], 100)  # 400 / (2 * 2)
        self.assertAlmostEqual(r["gamma_e"], 0.5)
        self.assertEqual((r["D_i"], r["D_o"]), (32, 16))
        self.assertEqual((r["D_D_in"], r["D_D_out"], r["D_D"]), (64, 32, 96))
        self.assertEqual((r["D_D_before_window"], r["D_D_after_window"]), (32, 32))
        self.assertAlmostEqual(r["rho"], 2.0)  # 96 / 48
        self.assertAlmostEqual(r["phi"], 0.5)  # 96 / 192
        self.assertEqual({(g["region"], g["dir"]): g["bytes"] for g in r["regions"]},
                         {("scratchRam@0x60000000", "in"): 64, ("scratchRam@0x40000000", "out"): 32})
        self.assertEqual(r["unfinished"], 0)
        self.assertEqual(out["warnings"], [])

    def test_kernel_sections(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 300, IDLE)]
        lines += get(150, 0x60000000, 1) + get(160, 0x18000040, 2)  # GlobalDataLow r32 data, KernelText code
        lines += put(200, 0x40000040, 3) + put(210, 0x21000000, 4)  # GroupLocalMemory data, ContextMemory runtime
        out, _ = self.run_plugin(lines, regions=str(HERE.parent / "kernel_sections.json"))
        r = out["kernels"][0]
        self.assertEqual({(g["region"], g["dir"]): g["bytes"] for g in r["regions"]},
                         {("GlobalDataLow[r32]", "in"): 32, ("KernelText", "in"): 32,
                          ("GroupLocalMemory", "out"): 32, ("ContextMemory", "out"): 32})
        self.assertEqual((r["D_D"], r["D_D_data"]), (128, 64))

    def test_retired_cross_check(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE), ce(0, 100, BUSY), ce(0, 200, IDLE)] + get(150, 0x60000000, 1)
        d = Path(self.tmp.name)
        js = launches(d / "launches.json", [{"elf": "k.elf", "entry": "k", "grid": [1, 1, 2], "t_ops": 400,
                                             "args": [{"kind": "buffer", "size": 32, "dir": "in"}]}])
        log = d / "sim.log"
        out, _ = self.run_plugin(lines + fmadd(200), launch=js, log=str(log), m_i="1024")
        self.assertEqual(out["retired"]["fp_ops"], 400)
        self.assertEqual(out["retired"]["diff"], 0)
        self.assertEqual(out["warnings"], [])
        out, _ = self.run_plugin(lines + fmadd(100), launch=js, log=str(log), m_i="1024")
        self.assertTrue(any("retired 200 FP ops" in w for w in out["warnings"]), out["warnings"])

    def test_two_kernels_split_at_longest_gap(self):
        lines = [ce(0, 1, IDLE), ce(1, 1, IDLE),
                 ce(0, 100, BUSY), ce(0, 200, IDLE), ce(1, 210, BUSY), ce(1, 250, IDLE),  # kernel a (short gap)
                 ce(0, 1000, BUSY), ce(0, 1100, IDLE)]  # kernel b
        lines += get(220, 0x60000000, 1) + get(1050, 0x60000000, 1) + get(1060, 0x60000020, 1)
        js = launches(Path(self.tmp.name) / "launches.json", [
            {"elf": "a.elf", "entry": "a", "grid": [1, 1, 2], "args": [{"kind": "buffer", "size": 32, "dir": "in"}]},
            {"elf": "b.elf", "entry": "b", "grid": [1, 1, 2], "args": [{"kind": "buffer", "size": 64, "dir": "inout"}]},
        ])
        out, _ = self.run_plugin(lines, launch=js, m_i="1024")
        a, b = out["kernels"]
        self.assertEqual((a["kernel"], a["window"], a["D_D"]), ("a", [100.5, 250.5], 32))
        self.assertEqual((b["kernel"], b["window"], b["D_D"]), ("b", [1000.5, 1100.5], 64))
        self.assertEqual((b["D_i"], b["D_o"]), (64, 64))
        self.assertAlmostEqual(b["rho"], 0.5)
        self.assertTrue(any("b: ρ=0.5 < 1" in w for w in out["warnings"]), out["warnings"])

    def test_launch_py(self):
        d = Path(self.tmp.name)
        kl = d / "kernel-launch-descriptor"
        kl.mkdir()
        kl.joinpath("kernel_launch.py").write_text(
            "from dataclasses import dataclass\n"
            "from typing import Optional\n"
            "@dataclass\nclass BufferArg:\n    size: int\n    dir: str\n"
            "@dataclass\nclass KernelLaunch:\n    elf: str\n    entry: str\n    grid: tuple\n    args: list\n"
            "    t_ops: Optional[int] = None\n    op_kind: str = 'fp'\n"
            "def buffer(size, dir):\n    return BufferArg(size, dir)\n")
        w = d / "1CR" / "W"
        w.mkdir(parents=True)
        w.joinpath("launch.py").write_text(
            "from kernel_launch import KernelLaunch, buffer\n"
            "LAUNCHES = [KernelLaunch(elf='w.elf', entry='w', grid=(1, 1, 2), t_ops=1000, op_kind='int',\n"
            "                         args=[buffer(64, 'in'), buffer(32, 'out')])]\n")
        sys.path.insert(0, str(HERE.parent))
        try:
            from redefine_metrics import load_workload
        finally:
            sys.path.remove(str(HERE.parent))
        (k,) = load_workload(str(w / "launch.py"))
        self.assertEqual((k.name, k.t_ops, k.op_kind, k.d_in, k.d_out), ("w", 1000, "int", 64, 32))


if __name__ == "__main__":
    unittest.main()
