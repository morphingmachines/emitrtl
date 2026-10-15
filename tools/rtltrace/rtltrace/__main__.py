"""rtltrace command line.

    python3 -m rtltrace perfetto sim.stderr -o trace.json [--elf prog.elf] [--tl-hops] [--plugin p.py] [--view kernel]
    python3 -m rtltrace report   sim.stderr -o report.html [--csv dir] [--md report.md] [--title "..."]
    python3 -m rtltrace text     sim.stderr [--groups llc,ce]
    python3 -m rtltrace points   trace_points.json [--grep llc]
    python3 -m rtltrace args     --on llc,ce --off crdma
    python3 -m rtltrace elf      prog.elf [--lookup 0x40000024]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import open_trace


def _common(p: argparse.ArgumentParser):
    p.add_argument("trace", nargs="+", help="trace file(s): captured simulation stderr")
    p.add_argument("--elf", help="program ELF for address symbolization")
    p.add_argument("--catalog", help="trace_points.json (decodes probes whose DECL line never printed)")
    p.add_argument("--regions", default="auto",
                   help="address-regions JSON (kernel_sections.json); default: found next to --catalog or the "
                        "trace; 'none' to disable")
    p.add_argument("--period", type=int, help="simulation time units per clock cycle (default: inferred)")
    p.add_argument("--stall", type=int, default=64, help="stall-alarm threshold in cycles (default 64)")
    p.add_argument("--only", help="keep only module instances whose path contains this text")
    p.add_argument("--from", dest="t_from", type=float, help="first cycle to keep")
    p.add_argument("--to", dest="t_to", type=float, help="last cycle to keep")
    p.add_argument("--links", help="rtltrace_links.json with user-declared relationships")
    p.add_argument("--tl-hops", action="store_true", help="link TileLink transactions across ports (heuristic)")
    p.add_argument("--line", type=int, default=64, help="cache line size for --tl-hops (default 64)")
    p.add_argument("--plugin", action="append", default=[], help="plugin file.py[:Class] (repeatable)")
    p.add_argument("--plugin-arg", action="append", default=[], help="key=value passed to plugins (repeatable)")
    p.add_argument("-o", "--out", help="output file")


def _session(a):
    bad = [kv for kv in a.plugin_arg if "=" not in kv]
    if bad:
        raise SystemExit(f"rtltrace: --plugin-arg expects key=value, got {bad[0]!r}")
    args = dict(kv.split("=", 1) for kv in a.plugin_arg)
    return open_trace(a.trace, elf=a.elf, catalog=a.catalog, period=a.period, stall=a.stall, only=a.only,
                      t_from=a.t_from, t_to=a.t_to, plugins=a.plugin, plugin_args=args, links=a.links,
                      tl_hops=a.tl_hops, line=a.line, warn=lambda s: print(s, file=sys.stderr),
                      regions=None if a.regions == "none" else a.regions)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rtltrace", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("perfetto", help="Perfetto timeline JSON (open in ui.perfetto.dev)")
    _common(pf)
    pf.add_argument("--view", choices=["full", "kernel"], default="full",
                    help="full: every probe (default); kernel: plugin tracks plus the probes plugins keep")
    r = sub.add_parser("report", help="HTML report (+ optional CSV) of the generic analyses and plugin sections")
    _common(r)
    r.add_argument("--csv", help="also write each table (generic and plugin) as CSV into this directory")
    r.add_argument("--md", help="also write the report body (no hardware-detail appendix) as Markdown")
    r.add_argument("--title", default="rtltrace report", help="report title")
    r.add_argument("--top", type=int, default=10)
    t = sub.add_parser("text", help="decoded chronological log")
    _common(t)
    t.add_argument("--groups", help="comma-separated groups to show")
    pt = sub.add_parser("points", help="browse a trace_points.json catalog")
    pt.add_argument("catalog")
    pt.add_argument("--grep", default="")
    ag = sub.add_parser("args", help="print the plusargs that switch groups on or off")
    ag.add_argument("--on", default="")
    ag.add_argument("--off", default="")
    ag.add_argument("--all", choices=["on", "off"])
    el = sub.add_parser("elf", help="list ELF symbols or look up addresses")
    el.add_argument("elf")
    el.add_argument("--lookup", action="append", default=[])
    a = ap.parse_args(argv)

    if a.cmd == "points":
        with open(a.catalog) as f:
            doc = json.load(f)
        print(f"{'pid':>4}  {'kind':<6} {'name':<24} {'groups':<14} {'on':<3} {'width':>5}  module / src")
        for p in doc["points"]:
            line = f"{p['pid']:>4}  {p['kind']:<6} {p['name']:<24} {','.join(p['groups']):<14} " \
                   f"{'on' if p['on'] else '-':<3} {p['width']:>5}  {p['module']}  ({p['src']})"
            if a.grep in line or a.grep in p.get("meta", ""):
                print(line)
        return 0
    if a.cmd == "args":
        out = []
        if a.all:
            out.append(f"+rtltrace_all_{a.all}")
        out += [f"+rtltrace_{g}_on" for g in filter(None, a.on.split(","))]
        out += [f"+rtltrace_{g}_off" for g in filter(None, a.off.split(","))]
        print(" ".join(out))
        return 0
    if a.cmd == "elf":
        from .elf import Elf
        e = Elf(a.elf)
        if a.lookup:
            for x in a.lookup:
                v = int(x, 0)
                print(f"0x{v:08x}  {e.symbolize(v) or '?'}")
        else:
            for s in e.symbols:
                print(f"0x{s.addr:08x} {s.size:>7} {s.kind:<7} {s.name}")
        return 0

    s = _session(a)
    for w in s.warnings:
        print(f"[rtltrace] warning: {w}", file=sys.stderr)
    m = s.model
    if a.cmd == "text":
        from .text import render
        out = open(a.out, "w") if a.out else sys.stdout
        render(m, s.labeler, out, set(a.groups.split(",")) if a.groups else None)
    elif a.cmd == "perfetto":
        from .perfetto import export
        path = a.out or "rtltrace.json"
        doc = export(m, s.labeler, path, s.emitted, a.view, s.plugins.keep())
        n = sum(1 for e in doc["traceEvents"] if e["ph"] == "X")
        print(f"wrote {path} ({a.view} view): {len(m.trace.probes)} probes, {n} slices, "
              f"{sum(len(p.txns) for p in m.ports.values())} TileLink transactions, {len(m.links)} links, "
              f"{len(m.markers)} markers")
    elif a.cmd == "report":
        from .analyze import Table, analyze
        from .report import write_csv, write_html, write_md, write_text
        tables = analyze(m, a.top, s.regions, s.labeler)
        sections = s.plugins.report(m)
        summary = dict(s.plugins.card())
        summary["trace"] = (f"{', '.join(os.path.basename(t) for t in a.trace)} · {len(m.trace.probes)} probes · "
                            f"{int(m.trace.end_ts):,} cycles")
        details = {"trace file": ", ".join(os.path.abspath(t) for t in a.trace),
                   "time base": f"1 cycle = {m.trace.base_period:,} simulation time units (the fastest traced clock)"}
        if s.regions is not None:
            details["address regions"] = s.regions.source
        if a.out:
            write_html(a.out, tables, sections, summary, s.warnings, a.title, details=details)
            print(f"wrote {a.out}")
        else:
            write_text(sys.stdout, tables, sections)
        if a.md:
            write_md(a.md, tables, sections, summary, s.warnings, a.title,
                     html_name=os.path.basename(a.out) if a.out else "")
            print(f"wrote {a.md}")
        if a.csv:
            write_csv(a.csv, tables + [Table(x.title, x.columns, x.rows, x.text, x.key) for x in sections
                                       if x.columns])
    return 0


if __name__ == "__main__":
    sys.exit(main())
