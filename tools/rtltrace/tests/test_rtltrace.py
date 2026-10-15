"""rtltrace tests. Fixtures in tests/data come from the emitrtl self-test (emitrtl/src/test/scala/trace):
  probe.stderr      ProbeTop run with +rtltrace_all_on (two Stage instances, a slow clock, a stuck port, SB line)
  tl.stderr         TLTop run (fuzzer -> tapIn -> xbar -> buffer -> adapter(adp) -> xbar -> tap0/tap1 -> RAMs)
  chiselsim.stderr  emitrtl.VerilatorSimulator run without a reset pulse (no DECL lines, %m with .unnamedblk1)

Run:  cd emitrtl/tools/rtltrace && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import collections
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from rtltrace import open_trace  # noqa: E402
from rtltrace.__main__ import main  # noqa: E402
from rtltrace.analyze import Table, analyze  # noqa: E402
from rtltrace.elf import Elf  # noqa: E402
from rtltrace.perfetto import Writer  # noqa: E402
from rtltrace.plugin import HW_GROUP, Plugin, RtlName, Section  # noqa: E402
from rtltrace.reader import parse_clients, parse_meta, unescape  # noqa: E402
from rtltrace.regions import RegionMap  # noqa: E402
from rtltrace.report import layout, write_csv, write_html, write_md  # noqa: E402
from rtltrace.text import render  # noqa: E402

DATA = HERE / "data"
PROBE, TL, CSIM = DATA / "probe.stderr", DATA / "tl.stderr", DATA / "chiselsim.stderr"


def nesting_errors(doc: dict) -> int:
    xs = collections.defaultdict(list)
    for e in doc["traceEvents"]:
        if e["ph"] == "X":
            xs[(e["pid"], e["tid"])].append((e["ts"], e["ts"] + e["dur"]))
    bad = 0
    for lst in xs.values():
        lst.sort(key=lambda x: (x[0], -x[1]))
        stack = []
        for a, b in lst:
            while stack and stack[-1] <= a + 1e-9:
                stack.pop()
            if stack and b > stack[-1] + 1e-9:
                bad += 1
            stack.append(b)
    return bad


class ProbeTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = open_trace([PROBE])
        cls.m = cls.s.model

    def test_scoreboard(self):
        sb = next(l for l in PROBE.read_text().splitlines() if l.startswith("SB "))
        fires, stalls, wait = (int(x.split("=")[1]) for x in sb.split()[1:])
        a_in = next(p for p in self.m.trace.probes.values() if p.name == "in" and p.inst.endswith("dut.a"))
        after = max(a_in.decl_times) / self.m.trace.base_period
        hs = [h for h in self.m.handshakes if h.probe is a_in and h.t1 >= after]
        self.assertEqual(len(hs), fires)
        self.assertEqual(sum(h.wait for h in hs), wait)
        self.assertEqual(sum(1 for h in hs if h.wait), stalls)

    def test_decode_round_trip(self):
        rt = next(p for p in self.m.trace.probes.values() if p.name == "rt")
        evs = [e for e in self.m.trace.events if e.probe is rt]
        self.assertGreater(len(evs), 20)
        for e in evs:
            u = e.vals["u"]
            self.assertEqual(e.vals["s"], u - 256 if u >= 128 else u)
            self.assertEqual(rt.enum_name("n.e", e.vals["n.e"]), "Drain" if u & 2 else "Idle")

    def test_clock_periods(self):
        slow = next(p for p in self.m.trace.probes.values() if p.name == "slow")
        self.assertEqual(self.m.trace.base_period, 10000)
        self.assertEqual(slow.period, 30000)

    def test_pending_and_instances(self):
        pend = [mk for mk in self.m.markers if mk.kind == "pending"]
        self.assertEqual([mk.probe.name for mk in pend], ["stuck"])
        insts = {p.inst.split(".")[-1] for p in self.m.trace.probes.values() if p.name == "in"}
        self.assertEqual(insts, {"a", "b"})

    def test_directions(self):
        d = {p.name: p.direction for p in self.m.trace.probes.values() if p.inst.endswith("pd")}
        self.assertEqual(d, {"req": "in", "resp": "out", "lanes[0]": "out", "lanes[1]": "out"})

    def test_user_links_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "links.json"
            spec.write_text(json.dumps([{"name": "a2b", "from": {"probe": "in", "key": "id"},
                                         "to": {"probe": "in", "key": "id"}, "pairing": "nearest", "window": 50}]))
            s = open_trace([PROBE], links=str(spec))
            self.assertTrue(any(l.kind == "a2b" for l in s.model.links))

    def test_key_links_and_glued_lines(self):
        # a DECL glued to stdout text without a newline; `key_frame` is how emitrtl before sanitizeTag wrote key=frame
        trace = "\n".join([
            "Kernel ELF loading: x.elf ...@RT 15 DECL top.a 1 fire Enq g 8 id:8#key_frame - v=1;dir=out",
            "@RT 15 DECL top.b 2 fire Launch g 8 id:8#key=frame - v=1;dir=in",
            "@RT 100 EVENT top.a 1 40",
            "@RT 110 EVENT top.a 1 41",
            "@RT 120 EVENT top.b 2 40",
            "@RT 130 EVENT top.b 2 41",
        ]) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "glued.stderr"
            f.write_text(trace)
            s = open_trace([f], period=10)
            self.assertEqual(sorted(p.name for p in s.model.trace.probes.values()), ["Enq", "Launch"])
            links = [(l.src.vals["id"], l.dst.vals["id"]) for l in s.model.links if l.kind == "key:frame"]
            self.assertEqual(sorted(links), [(0x40, 0x40), (0x41, 0x41)])

    def test_rtl_args_keep_fields(self):
        # a probe field named like an RTL arg (dma_start has `src`) must not be overwritten by the Chisel location
        trace = "@RT 15 DECL top.a 1 pulse Go g 8 src:8 - v=1;src=X.scala:7\n@RT 100 EVENT top.a 1 42\n"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "src.stderr"
            f.write_text(trace)
            s = open_trace([f], period=10)
            doc = Writer(s.model, s.labeler, s.emitted).build()
            args = next(e["args"] for e in doc["traceEvents"] if e.get("ph") == "X")
            self.assertEqual((args["src"], args["chisel_src"], args["probe"]), ("0x42", "X.scala:7", "Go"))

    def test_views(self):
        doc = Writer(self.m, self.s.labeler, self.s.emitted).build()
        self.assertEqual(nesting_errors(doc), 0)
        buf = io.StringIO()
        render(self.m, self.s.labeler, buf)
        self.assertIn("(waited", buf.getvalue())
        self.assertIn("PENDING AT END", buf.getvalue())
        tables = analyze(self.m)
        titles = [t.title for t in tables]
        self.assertIn("Modules: input-side vs output-side stall", titles)
        with tempfile.TemporaryDirectory() as tmp:
            write_html(f"{tmp}/r.html", tables, [], {"traces": "probe"}, [])
            write_csv(f"{tmp}/csv", tables)
            self.assertIn("Interfaces (ready/valid)", Path(f"{tmp}/r.html").read_text())
            self.assertTrue(list(Path(f"{tmp}/csv").glob("*.csv")))


class Named(Plugin):
    """A plugin that names modules and orders the report (chapters, legend)."""
    name = "named"
    perfetto_keep = ["state"]

    def display(self, kind, name):
        return "Stage A" if kind == "module" and name.endswith("dut.a") else None

    def report(self, model):
        return [Section("Headline", ["module", "x"], [[RtlName("Stage zero", "top.s0"), 1]], "note",
                        legend={"x": "an x"}, group="1 · Summary", order=10, key="headline")]


class ReportLayout(unittest.TestCase):
    def test_empty_text_and_trace_details(self):
        secs = [Section("Findings", ["finding"], [], group="1 · Summary", order=1, empty="Nothing to report.")]
        with tempfile.TemporaryDirectory() as tmp:
            write_html(f"{tmp}/r.html", [], secs, {"trace": "a.log"}, [], "T", details={"trace file": "/x/a.log"})
            write_md(f"{tmp}/r.md", [], secs, {"trace": "a.log"}, [], "T")
            h, md = Path(f"{tmp}/r.html").read_text(), Path(f"{tmp}/r.md").read_text()
            self.assertIn("Nothing to report.", h)
            self.assertIn("Trace details", h)
            self.assertIn("/x/a.log", h)
            self.assertIn("Nothing to report.", md)
            self.assertNotIn("/x/a.log", md)  # file paths stay out of the Markdown report

    def test_chapters_names_and_rtl(self):
        s = open_trace([PROBE], plugins=[Named()])
        tables = analyze(s.model, 10, None, s.labeler)
        mods = {str(r[0]): r[0].rtl for t in tables for r in t.rows if isinstance(r[0], RtlName)}
        self.assertEqual(mods.get("Stage A"), "TOP.tb.dut.a")
        items = layout(tables, s.plugins.report(s.model))
        self.assertEqual(items[0].title, "Headline")
        self.assertTrue(all(x.group == HW_GROUP and x.level == "hw" for x in items[1:]))
        with tempfile.TemporaryDirectory() as tmp:
            write_html(f"{tmp}/r.html", tables, s.plugins.report(s.model), {"k": "v"}, [], "T")
            h = Path(f"{tmp}/r.html").read_text()
            self.assertIn('class="toc"', h)
            self.assertIn('<div class="rtl">', h)  # hardware-detail tables show the RTL path inline
            self.assertIn('title="RTL: top.s0"', h)  # the body uses a tooltip
            self.assertIn("<dt>x</dt><dd>an x</dd>", h)
            write_md(f"{tmp}/r.md", tables, s.plugins.report(s.model), {"k": "v"}, [], "T")
            self.assertNotIn("Interfaces (ready/valid)", Path(f"{tmp}/r.md").read_text())
            write_csv(f"{tmp}/csv", [Table("Headline", ["module", "x"], [[RtlName("Stage zero", "top.s0"), 1]],
                                           key="headline")])
            self.assertEqual(Path(f"{tmp}/csv/headline.csv").read_text().splitlines(),
                             ["module,module (RTL),x", "Stage zero,top.s0,1"])
        kern = Writer(s.model, s.labeler, s.emitted, "kernel", s.plugins.keep()).build()
        threads = {e["args"]["name"] for e in kern["traceEvents"] if e.get("name") == "thread_name"}
        self.assertEqual(threads, {"state"})
        full = Writer(s.model, s.labeler, s.emitted).build()
        procs = [e["args"]["name"] for e in full["traceEvents"] if e.get("name") == "process_name"]
        self.assertIn("Stage A  [dut.a]", procs)


class TileLinkTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = open_trace([TL], tl_hops=True)
        cls.m = cls.s.model

    def test_pairing_and_conservation(self):
        n = {p.name: len(p.txns) for p in self.m.ports.values()}
        self.assertEqual(n["tapIn"], 200)
        self.assertEqual(n["adp.in0"], 200)
        self.assertEqual(n["adp.out0"], 200)
        self.assertEqual(n["tap0"] + n["tap1"], 200)
        for p in self.m.ports.values():
            self.assertTrue(all(t.done for t in p.txns), p.name)
            self.assertEqual(p.orphans, [], p.name)
        multi = [t for t in self.m.txns("tapIn") if t.resp_beats > 1 or t.req_beats > 1]
        self.assertTrue(multi, "fixture must contain bursts")

    def test_port_meta(self):
        p = next(p for p in self.m.ports.values() if p.name == "tap0")
        self.assertEqual(p.clients, [("Fuzzer", 0, 3)])
        self.assertEqual(p.managers, [("ram0", [(0x0, 0xfff)])])
        self.assertEqual(p.manager_of(0x10), "ram0")
        self.assertEqual(p.client_of(2), "Fuzzer")

    def test_hops(self):
        c = collections.Counter((l.src.port.name, l.dst.port.name) for l in self.m.links if l.kind == "tl-hop")
        self.assertEqual(c[("tapIn", "adp.in0")], 200)
        self.assertEqual(c[("adp.in0", "adp.out0")], 200)
        self.assertEqual(c[("adp.out0", "tap0")] + c[("adp.out0", "tap1")], 200)
        self.assertEqual(sum(c.values()), 600, c)

    def test_perfetto(self):
        doc = Writer(self.m, self.s.labeler, self.s.emitted).build()
        self.assertEqual(nesting_errors(doc), 0)
        flows = [e for e in doc["traceEvents"] if e["ph"] == "s"]
        self.assertEqual(len(flows), 600)

    def test_plugin_example(self):
        s = open_trace([TL], plugins=[str(HERE.parent / "examples" / "llc_miss.py")],
                       plugin_args={"in": "adp.in0", "out": "adp.out0"})
        sec = s.plugins.report(s.model)
        self.assertEqual(sec[0].rows[0][:2], [200, 200])  # a pass-through: every input request goes out
        doc = Writer(s.model, s.labeler, s.emitted).build()
        names = {e["args"]["name"] for e in doc["traceEvents"] if e.get("name") == "process_name"}
        self.assertIn("LLC · hit/miss", names)

    def test_failing_plugin_is_isolated(self):
        class Boom(Plugin):
            name = "boom"

            def derive(self, m, out):
                raise RuntimeError("boom")

        msgs = []
        s = open_trace([TL], plugins=[Boom()], warn=msgs.append)
        self.assertTrue(any("boom" in m for m in msgs))
        self.assertEqual(s.plugins.active, [])
        self.assertEqual(sum(len(p.txns) for p in s.model.ports.values()), 800)


class ChiselSimTrace(unittest.TestCase):
    def test_catalog_fallback_and_paths(self):
        s = open_trace([CSIM], catalog=DATA / "chiselsim_trace_points.json")
        probes = list(s.model.trace.probes.values())
        self.assertTrue(all(p.from_catalog for p in probes))
        self.assertTrue(all(not p.inst.endswith("unnamedblk1") for p in probes))
        e = next(e for e in s.model.trace.events if e.probe.name == "in")
        self.assertEqual(e.vals["id"], 7)

    def test_without_catalog_warns(self):
        s = open_trace([CSIM])
        self.assertTrue(any("no DECL" in w for w in s.warnings))


class Regions(unittest.TestCase):
    DOC = {"addr_width": 13, "kernel_id_width": 2, "cr_id_width": 1, "sections": [
        {"name": "Low", "value": "0x0", "care": "0x1000", "offset_mask": "0xfff",
         "fields": [{"name": "word", "hi": 4, "lo": 2, "add": 0}], "scope": "LOCAL"},
        {"name": "High", "value": "0x1000", "care": "0x1000", "offset_mask": "0xfff",
         "fields": [{"name": "region", "hi": 11, "lo": 8, "add": 32}], "caching": "CACHED"}]}

    def test_lookup_and_fields(self):
        r = RegionMap(self.DOC)
        x = r.lookup(0x14)
        self.assertEqual((x.name, x.offset, x.fields, x.attrs), ("Low", 0x14, {"word": 5}, {"scope": "LOCAL"}))
        self.assertEqual(r.label(0x1340), "High[r35]+0x340")
        ext = r.lookup((1 << 15 | 3 << 13) | 0x1340)  # cr 1, kernel 3 above the 13-bit logical address
        self.assertEqual(ext.fields, {"region": 35, "kernel_id": 3, "cr_id": 1})
        self.assertIsNone(RegionMap({"sections": [{"name": "A", "value": "0x10", "care": "0x10"}]}).lookup(0))

    def test_trace_views(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "kernel_sections.json"
            f.write_text(json.dumps(self.DOC))
            s = open_trace([TL], regions=str(f))
            self.assertEqual(s.labeler.addr(0x14), ("Low[word5]+0x14", "Low[word5]+0x14 [0x14]"))
            doc = Writer(s.model, s.labeler, s.emitted).build()
            tl = [e for e in doc["traceEvents"] if e["ph"] == "X" and e.get("args", {}).get("port") == "tapIn"]
            self.assertTrue(tl and all(e["args"]["region"] in ("Low", "High") for e in tl))
            self.assertTrue(any(e["name"].split()[-1].startswith(("Low", "High")) for e in tl))
            tabs = {t.title: t for t in analyze(s.model, 10, s.regions)}
            rows = [r for r in tabs["TileLink traffic by address region"].rows if r[1] == "tapIn"]
            self.assertEqual(sum(r[4] for r in rows), 200)
            self.assertIn("region", tabs["Slowest 10 TileLink transactions"].columns)
            self.assertIsNone(open_trace([TL], regions="auto").regions)  # nothing next to the fixture


class Pieces(unittest.TestCase):
    def test_escaping(self):
        self.assertEqual(unescape("Core~200~20DCache"), "Core 0 DCache")
        meta = parse_meta("clients=Core~200~20DCache@0-3|a~40b@4-7;x=a~3Bb")
        self.assertEqual(meta["x"], "a;b")
        self.assertEqual(parse_clients(meta["clients"]), [("Core 0 DCache", 0, 3), ("a@b", 4, 7)])

    def test_elf(self):
        syms = [("result", 0x40000000, 4, 0x11), ("partial", 0x40000004, 32, 0x11), ("A", 0x40000024, 2048, 0x11),
                ("main", 0x10000118, 64, 0x12)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.elf"
            path.write_bytes(_mini_elf(syms))
            e = Elf(path)
            self.assertEqual(e.symbolize(0x40000000), "result")
            self.assertEqual(e.symbolize(0x40000028), "A+0x4")
            self.assertEqual(e.covers(0x40000000, 64), ["result", "partial", "A"])
            self.assertIsNone(e.symbolize(0x50000000))

    def test_cli(self):
        buf = io.StringIO()
        old, sys.stdout = sys.stdout, buf
        try:
            main(["args", "--on", "llc,ce", "--off", "crdma"])
            main(["points", str(DATA / "probe_trace_points.json"), "--grep", "stuck"])
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertIn("+rtltrace_llc_on +rtltrace_ce_on +rtltrace_crdma_off", out)
        self.assertIn("stuck", out)


def _mini_elf(syms) -> bytes:
    """A minimal little-endian ELF32 with .symtab/.strtab, enough for the symbol reader."""
    shstr = b"\0.text\0.symtab\0.strtab\0.shstrtab\0"
    strtab = b"\0" + b"".join(n.encode() + b"\0" for n, *_ in syms)
    symtab = b"\0" * 16
    off = 1
    for n, addr, size, info in syms:
        symtab += struct.pack("<IIIBBH", off, addr, size, info, 0, 1)
        off += len(n) + 1
    body = shstr + strtab + symtab
    o_shstr, o_str, o_sym = 52, 52 + len(shstr), 52 + len(shstr) + len(strtab)
    shoff = 52 + len(body)
    sh = struct.pack("<10I", *[0] * 10)
    sh += struct.pack("<10I", 1, 1, 6, 0x10000000, 0, 0, 0, 0, 4, 0)  # .text (no content)
    sh += struct.pack("<10I", 7, 2, 0, 0, o_sym, len(symtab), 3, 1, 4, 16)  # .symtab -> link .strtab
    sh += struct.pack("<10I", 15, 3, 0, 0, o_str, len(strtab), 0, 0, 1, 0)  # .strtab
    sh += struct.pack("<10I", 23, 3, 0, 0, o_shstr, len(shstr), 0, 0, 1, 0)  # .shstrtab
    hdr = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 9
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 0xF3, 1, 0, 0, shoff, 0, 52, 0, 0, 40, 5, 4)
    return hdr + body + sh


if __name__ == "__main__":
    unittest.main()


class ReaderRobustness(unittest.TestCase):
    """Review fixes: file order, malformed lines, --from/--to window, ELF labels, non-finite values."""

    def test_files_out_of_order(self):
        lines = PROBE.read_text().splitlines(keepends=True)
        half = len(lines) // 2
        ref = open_trace([PROBE]).model.trace
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.log", Path(tmp) / "b.log"
            a.write_text("".join(lines[half:]))
            b.write_text("".join(lines[:half]))
            tr = open_trace([a, b]).model.trace
        self.assertEqual(len(tr.events), len(ref.events))
        self.assertEqual(sorted((m.kind, m.t0, m.cycles) for m in tr.markers),
                         sorted((m.kind, m.t0, m.cycles) for m in ref.markers))

    def test_malformed_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.log"
            p.write_text(PROBE.read_text() + "@RT 4500x EVENT TOP.tb.dut 9 00\n@RT 1 EVENT TOP.tb.dut nine 0\n")
            tr = open_trace([p]).model.trace
        self.assertTrue(any("2 malformed" in w for w in tr.warnings))
        self.assertEqual(len(tr.events), len(open_trace([PROBE]).model.trace.events))

    def test_window_clips_end_and_keeps_last_cycle(self):
        full = open_trace([PROBE]).model.trace
        cyc = sorted({e.cycle for e in full.events})
        lo, hi = cyc[len(cyc) // 4], cyc[len(cyc) // 2]
        tr = open_trace([PROBE], t_from=lo, t_to=hi).model.trace
        self.assertEqual(tr.end_ts, hi + 1)
        self.assertEqual(tr.start_ts, lo)
        got = {e.cycle for e in tr.events}
        self.assertIn(hi, got)
        self.assertIn(lo, got)
        self.assertTrue(all(lo <= c <= hi for c in got))

    def test_elf_prefers_object_over_label(self):
        syms = [("A", 0x40000024, 2048, 0x11), ("_sec_start", 0x40000024, 0, 0x10), ("_end_lbl", 0x40001000, 0, 0x10)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.elf"
            path.write_bytes(_mini_elf(syms))
            e = Elf(path)
            self.assertEqual(e.symbolize(0x40000024), "A")
            self.assertEqual(e.symbolize(0x40000028), "A+0x4")
            self.assertEqual(e.symbolize(0x40001000), "_end_lbl")
            self.assertIsNone(e.symbolize(0x40001004))  # past a bare label: nothing

    def test_non_finite_values(self):
        from rtltrace.report import fmt_num
        self.assertEqual(fmt_num(float("nan")), "nan")
        self.assertEqual(fmt_num(float("inf")), "inf")
        from rtltrace import perfetto
        self.assertEqual(json.loads(json.dumps(perfetto._finite({"a": [float("nan"), 1.5]}))), {"a": [None, 1.5]})


class WriterRobustness(unittest.TestCase):
    def test_md_text_csv_and_anchors(self):
        from rtltrace.report import _md_cell, write_text
        self.assertEqual(_md_cell("busy (< 1 %) a|b"), "busy (< 1 %) a\\|b")
        self.assertEqual(_md_cell("<emitrtl>/x"), "&lt;emitrtl>/x")
        secs = [Section("Notes", [], [], "line one\nline two"), Section("T", ["a", "b"], [[1], [2, 3]])]
        buf = io.StringIO()
        write_text(buf, [], secs)  # a short row and a text-only section
        self.assertIn("line two", buf.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            write_csv(tmp, [Table("Same name", ["x"], [[1]]), Table("Same name!", ["x"], [[2]])])
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["same_name.csv", "same_name_2.csv"])
            out = Path(tmp) / "r.html"
            write_html(str(out), [], [Section("Findings", ["a"], [[1]], group="Findings"),
                                      Section("Findings", ["a"], [[2]], group="Other")], {}, [])
            ids = __import__("re").findall(r'id="([^"]+)"', out.read_text())
            self.assertEqual(len(ids), len(set(ids)), ids)
