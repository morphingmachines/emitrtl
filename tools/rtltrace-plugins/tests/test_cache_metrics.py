"""Tests for cache_metrics.py on synthetic @RT traces (probe layouts copied from a real SimCR run).

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
from test_redefine_metrics import RT  # noqa: E402

if RT is not None:
    from rtltrace import open_trace  # noqa: E402

PLUGIN = HERE.parent / "cache_metrics.py"
CR = "TOP.tb.cr.baseCr"
UNC = f"{CR}.uncore"
LLC = f"{UNC}.cache"
TLA = ("opcode:3:e=TLA,param:3,size:3:d,source:{sw}:d,address:32#addr,corrupt:1:b TLA=0:PutFullData/1:PutPartialData/"
       "2:ArithmeticData/3:LogicalData/4:Get/5:Hint/6:AcquireBlock/7:AcquirePerm")
TLD = ("opcode:3:e=TLD,param:2,size:3:d,source:{sw}:d,sink:1:d,denied:1:b,corrupt:1:b TLD=0:AccessAck/1:AccessAckData/"
       "2:HintAck/4:Grant/5:GrantData/6:ReleaseAck")
MGR = "managers=scratchRam@0x60000000/0x1fffffff"
IN_META = f"tl={{ch}};port=LLC_in;side=in;clients=Core~200~20DCache@64-67|Core~200~20ICache@72-72;{MGR};beat=32"
OUT_META = f"tl={{ch}};port=LLC_out;side=in;clients=Distributes~20LLC@0-15;{MGR};beat=32"
DECLS = [
    f"@RT 15 DECL {UNC} 1 fire LLC_in.a LLC 54 {TLA.format(sw=9)} v=1;{IN_META.format(ch='a')}",
    f"@RT 15 DECL {UNC} 2 fire LLC_in.d LLC 20 {TLD.format(sw=9)} v=1;{IN_META.format(ch='d')}",
    f"@RT 15 DECL {UNC} 3 fire LLC_out.a LLC 46 {TLA.format(sw=4)} v=1;{OUT_META.format(ch='a')}",
    f"@RT 15 DECL {UNC} 4 fire LLC_out.d LLC 15 {TLD.format(sw=4)} v=1;{OUT_META.format(ch='d')}",
    f"@RT 15 DECL {CR}.orch.ceIfc_0 14 change CeState hyperop 3 value:3:e=CeState "
    "CeState=0:Idle/1:PreLaunch/2:Launch/3:Busy/4:Suspended v=1;src=CEIfc.scala:71",
    f"@RT 15 DECL {LLC} 6 fire llc_cmu llc_cache 32 opcode:4,oprand:28 - v=1;src=DistributedLlc.scala:207",
    f"@RT 15 DECL {LLC} 7 valid llc_cmu_done llc_cache 0 - - v=1;src=DistributedLlc.scala:208",
] + [
    line for i in range(2) for line in (
        f"@RT 15 DECL {LLC}.mshrs.mshrs_{i} 3 event llc_access llc_cache 50 hit:1:b,isGet:1:b,fromRpq:1:b,"
        "tagMatch:1:b,dirtyVictim:1:b,fetchModify:1:b,way:3,source:9,addr:32#addr - v=1;src=Mshr.scala:371",
        f"@RT 15 DECL {LLC}.mshrs.mshrs_{i} 4 valid llc_refill llc_cache 0 - - v=1;src=Mshr.scala:388",
        f"@RT 15 DECL {LLC}.mshrs.mshrs_{i} 5 valid llc_writeback llc_cache 0 - - v=1;src=Mshr.scala:389")
] + [
    line for c in ("core", "core_1") for line in (
        f"@RT 15 DECL {CR}.{c}.rockettile.dcache 1 event l1d_access l1d_cache 35 hit:1:b,isWrite:1:b,secondary:1:b,"
        "addr:32#addr - v=1;src=NBDCache.scala:931",
        f"@RT 15 DECL {CR}.{c}.rockettile.dcache 2 valid l1d_cmu_wb l1d_cache 0 - - v=1;src=NBDCache.scala:943")
]


def t(cycle: int) -> int:
    return cycle * 10 + 5


def pack(*fields: tuple[int, int]) -> str:
    v = 0
    for val, width in fields:
        v = v << width | (val & ((1 << width) - 1))
    return f"{v:x}"


def ev(cycle: int, inst: str, pid: int, payload: str | None = None) -> str:
    return f"@RT {t(cycle)} EVENT {inst} {pid}" + (f" {payload}" if payload is not None else "")


def tl(cycle: int, port: str, opcode: int, address: int, source: int) -> list[str]:
    a, d, sw = (1, 2, 9) if port == "in" else (3, 4, 4)
    resp = 1 if opcode == 4 else 0
    return [ev(cycle, UNC, a, pack((opcode, 3), (0, 3), (5, 3), (source, sw), (address, 32), (0, 1))),
            ev(cycle + 2, UNC, d, pack((resp, 3), (0, 2), (5, 3), (source, sw), (0, 1), (0, 1), (0, 1)))]


def llc(cycle: int, mshr: int, hit=0, get=1, rpq=0, tm=0, dv=0, fm=0, source=64, addr=0x60000000) -> str:
    return ev(cycle, f"{LLC}.mshrs.mshrs_{mshr}", 3,
              pack((hit, 1), (get, 1), (rpq, 1), (tm, 1), (dv, 1), (fm, 1), (0, 3), (source, 9), (addr, 32)))


def l1d(cycle: int, core: str, hit=0, write=0, sec=0, addr=0x60000000) -> str:
    return ev(cycle, f"{CR}.{core}.rockettile.dcache", 1, pack((hit, 1), (write, 1), (sec, 1), (addr, 32)))


@unittest.skipIf(RT is None, "rtltrace not found: set RTLTRACE_DIR=<emitrtl>/tools/rtltrace")
class CacheMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_plugin(self, lines: list[str], extra: list[str] = (), decls=DECLS, **args):
        log = self.d / "sim.log"
        rt = sorted(lines, key=lambda s: int(s.split()[1]))
        log.write_text("\n".join(list(decls) + rt + list(extra)) + "\n")
        args = {"cache_json": str(self.d / "c.json"), **args}
        s = open_trace([str(log)], plugins=[str(PLUGIN)], plugin_args=args, warn=lambda *_: None)
        s.plugins.active[0].report(s.model)
        out = json.loads((self.d / "c.json").read_text())
        return out, {w["window"]: w for w in out["windows"]}

    def test_probe_mode(self):
        lines = [ev(1, f"{CR}.orch.ceIfc_0", 14, "0"), ev(100, f"{CR}.orch.ceIfc_0", 14, "3"),
                 ev(400, f"{CR}.orch.ceIfc_0", 14, "0")]
        lines += [llc(50, 0, hit=1)]  # before the kernel window
        lines += tl(105, "in", 4, 0x60000000, 64)  # a D$ refill Get at the LLC (also creates the LLC_in port)
        # 1: read miss -> 1 refill
        lines += [llc(110, 0), ev(112, f"{LLC}.mshrs.mshrs_0", 4)] + tl(112, "out", 4, 0x60000000, 0)
        # 2: read that waited in the RPQ behind 1 -> hit
        lines += [llc(150, 0, hit=1, rpq=1, source=72)]
        # 3: store miss, dirty victim with another tag -> writeback, no refill (write-allocate without fetch)
        lines += [llc(200, 1, get=0, dv=1), ev(202, f"{LLC}.mshrs.mshrs_1", 5)] + tl(202, "out", 0, 0x60000100, 1)
        # 4: store miss to a matching tag (partial line) -> 1 refill
        lines += [llc(250, 1, get=0, tm=1), ev(252, f"{LLC}.mshrs.mshrs_1", 4)] + tl(252, "out", 4, 0x60000200, 1)
        # 5: read miss, fetch-modify victim with another tag -> refill old line, writeback, refill new line
        lines += [llc(300, 0, dv=1, fm=1), ev(302, f"{LLC}.mshrs.mshrs_0", 4), ev(310, f"{LLC}.mshrs.mshrs_0", 5),
                  ev(320, f"{LLC}.mshrs.mshrs_0", 4)]
        lines += tl(302, "out", 4, 0x60000300, 0) + tl(310, "out", 0, 0x60000300, 0) + tl(320, "out", 4, 0x60000400, 0)
        # a flush: its Get on LLC_out is not an MSHR refill
        lines += [ev(350, LLC, 6, pack((0, 4), (0, 28))), ev(380, LLC, 7)] + tl(360, "out", 4, 0x60000500, 1)
        # L1-D: CE0 hit, primary read miss, secondary read miss, write miss; CE1 one hit
        lines += [l1d(120, "core", hit=1), l1d(130, "core"), l1d(131, "core", sec=1), l1d(140, "core", write=1),
                  l1d(160, "core_1", hit=1), ev(330, f"{CR}.core.rockettile.dcache", 2)]
        commits = [f"C0:  {i} [1] pc=[00010040] W[r 0=00000000][0] R[r 0=00000000] R[r 0=00000000] "
                   "inst=[00000013] addi zero, zero, 0" for i in range(100)]
        log = str(self.d / "sim.log")
        out, w = self.run_plugin(lines, commits, log=log)
        self.assertEqual(out["mode"], "probe")
        self.assertEqual(out["warnings"], [])
        self.assertEqual(set(w), {"run", "kernel0"})
        self.assertEqual((w["kernel0"]["from"], w["kernel0"]["to"]), (100.5, 400.5))
        (s,) = w["kernel0"]["llc"]
        self.assertEqual({k: s[k] for k in ("accesses", "hits", "misses", "read_misses", "write_misses", "merged",
                                            "dirty_victim", "fetch_modify", "refills", "expected_refills",
                                            "writebacks", "llc_out_gets", "llc_out_gets_in_cmu")},
                         {"accesses": 5, "hits": 1, "misses": 4, "read_misses": 2, "write_misses": 2, "merged": 1,
                          "dirty_victim": 2, "fetch_modify": 1, "refills": 4, "expected_refills": 4,
                          "writebacks": 2, "llc_out_gets": 5, "llc_out_gets_in_cmu": 1})
        self.assertAlmostEqual(s["hit_ratio"], 0.2)
        self.assertEqual(w["kernel0"]["cmu_ops"], 1)
        self.assertEqual(w["run"]["llc"][0]["accesses"], 6)
        clients = {c["client"]: (c["accesses"], c["hits"]) for c in w["kernel0"]["llc_by_client"]}
        self.assertEqual(clients, {"Core 0 DCache": (4, 0), "Core 0 ICache": (1, 1)})
        l1 = {r["core"]: r for r in w["kernel0"]["l1d"]}
        self.assertEqual({k: l1[0][k] for k in ("accesses", "hits", "misses", "read_misses", "write_misses",
                                                "secondary", "cmu_writebacks", "llc_in_dcache_gets")},
                         {"accesses": 4, "hits": 1, "misses": 3, "read_misses": 2, "write_misses": 1,
                          "secondary": 1, "cmu_writebacks": 1, "llc_in_dcache_gets": 1})
        self.assertEqual((l1[1]["accesses"], l1[1]["hit_ratio"]), (1, 1.0))
        self.assertIsNone(l1[0]["mpki"])  # only for the whole run
        run_l1 = {r["core"]: r for r in w["run"]["l1d"]}
        self.assertAlmostEqual(run_l1[0]["mpki"], 30.0)  # 3 misses / 100 retired instructions

    def test_inconsistent_refills_are_flagged(self):
        lines = [llc(110, 0), llc(200, 0)] + tl(112, "out", 4, 0x60000000, 0)  # 2 read misses, no refill pulse
        out, _ = self.run_plugin(lines)
        self.assertTrue(any("0 MSHR refills but the traced misses imply 2" in x for x in out["warnings"]),
                        out["warnings"])
        self.assertTrue(any("1 LLC_out Gets outside maintenance but 0 MSHR refills" in x for x in out["warnings"]),
                        out["warnings"])

    def test_infer_mode(self):
        decls = DECLS[:4]
        lines = tl(10, "in", 4, 0x60000000, 64) + tl(12, "out", 4, 0x60000000, 0)  # miss: fill while pending
        lines += tl(50, "in", 4, 0x60000004, 64)  # same line later: hit
        lines += tl(60, "in", 4, 0x60000040, 64)[:1] + tl(61, "in", 4, 0x60000040, 72)[:1]
        lines += tl(63, "out", 4, 0x60000040, 0) + [ev(66, UNC, 2, pack((1, 3), (0, 2), (5, 3), (64, 9), (0, 3))),
                                                    ev(67, UNC, 2, pack((1, 3), (0, 2), (5, 3), (72, 9), (0, 3)))]
        out, w = self.run_plugin(lines, decls=decls)
        self.assertEqual(out["mode"], "infer")
        (s,) = w["run"]["llc"]
        # one fill serves one request: the second read of line 0x40 waited for the same fill, counted as a hit
        self.assertEqual((s["accesses"], s["hits"], s["misses"]), (4, 2, 2))
        self.assertEqual(w["run"]["l1d"], [])
        self.assertTrue(any("mode=infer" in x for x in out["warnings"]))


if __name__ == "__main__":
    unittest.main()
