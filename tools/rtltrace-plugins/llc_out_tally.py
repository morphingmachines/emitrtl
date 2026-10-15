#!/usr/bin/env python3
"""Stand-alone run of the llc_out.py plugin: LLC_out (D_D boundary) traffic by launch buffer and address.

redefine_report.py already runs the plugin (tables in the HTML / summary.md, <stem>.llc_out.json / .csv). This script
runs just that plugin on a log, prints the tables, writes the per-transaction files, and optionally checks the
in-window bytes against D_D in a redefine_report.py metrics JSON.

    llc_out_tally.py <sim log> --config <generated_sv_dir/<config>> [--launch launch.py] \\
        [--buffers buffers.txt] [--metrics <stem>.metrics.json] -o <out dir>

Writes into the out dir:
  llc_out_transactions.csv   every LLC_out A-channel transaction (window before / in / after)
  llc_out_other.csv          in-window transactions outside every launch buffer
  llc_out_other_ranges.txt   those, grouped by section/opcode into contiguous line ranges
  llc_out.json               the plugin summary
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from metrics_sweep import find_rtltrace, fmt  # noqa: E402

PLUGIN = HERE / "llc_out.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="captured simulation log (with the @RT lines)")
    ap.add_argument("--config", required=True, help="RTL config dir holding trace_points.json")
    ap.add_argument("--regions", help="kernel_sections.json (default: in --config)")
    ap.add_argument("--launch", help="launch.py (kernel names; buffers.txt fallback next to it)")
    ap.add_argument("--buffers", help="buffers.txt to use when the log has no buffer table (comma list per kernel)")
    ap.add_argument("--metrics", help="<stem>.metrics.json from redefine_report.py: check D_D against it")
    ap.add_argument("--boundary", default="LLC?out", help="TileLink port pattern (default LLC?out)")
    ap.add_argument("--arg", action="append", default=[], help="extra key=value plugin arg (line=, from=, to=)")
    ap.add_argument("--rtltrace", help="emitrtl tools/rtltrace directory")
    ap.add_argument("-o", "--out", required=True, help="output directory")
    a = ap.parse_args()

    sys.path.insert(0, str(find_rtltrace(a.rtltrace)))
    from rtltrace import open_trace

    cfg = Path(a.config)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pargs = {"log": a.log, "boundary": a.boundary, "llc_out_json": str(out / "llc_out.json"),
             "llc_out_csv": str(out / "llc_out_transactions.csv")}
    if a.launch:
        pargs["launch"] = a.launch
    if a.buffers:
        pargs["buffers"] = a.buffers
    pargs.update(kv.split("=", 1) for kv in a.arg)
    s = open_trace(a.log, catalog=str(cfg / "trace_points.json"), regions=a.regions or str(cfg / "kernel_sections.json"),
                   plugins=[str(PLUGIN)], plugin_args=pargs, warn=lambda m: print(m, file=sys.stderr))
    if not s.plugins.active:
        sys.exit("llc_out plugin did not run (see the messages above)")
    p = s.plugins.active[0]
    ref = json.loads(Path(a.metrics).read_text())["kernels"] if a.metrics else []

    for i, r in enumerate(p.results):
        print(f"kernel {r['kernel']}  window {r['window'][0]} .. {r['window'][1]}  {r['boundary']} A-txns: "
              f"{r['txns_before']} before, {r['txns']} in, {r['txns_after']} after   (buffers: {p.buffer_source})")
        if i < len(ref):
            for key in ("D_D_in", "D_D_out", "D_D"):
                print(f"  {key:<8}{r[key]:>9}  metrics.json {ref[i][key]:>9}  "
                      f"{'OK' if r[key] == ref[i][key] else 'MISMATCH'}")
        print(f"  D_D {r['D_D']} = buffers {r['D_D_buffers']} + outside {r['D_D_other']};  D_i+D_o "
              f"{r['D_i'] + r['D_o']};  rho {fmt(r['rho'])}  (buffers only {fmt(r['rho_buffers_only'])})")
        print(f"\n  {'buffer':<8}{'range':<24}{'opcode':<14}{'txns':>7}{'bytes':>9}{'%D_D':>8}{'uniq':>7}"
              f"{'cover':>7}{'max/ln':>7}")
        for b in r["by_buffer"]:
            print(f"  {b['buffer']:<8}{b['range']:<24}{b['opcode']:<14}{b['txns']:>7}{b['bytes']:>9}"
                  f"{fmt(b['pct_bytes']):>8}{b['unique_lines']:>7}{fmt(b['coverage']):>7}{b['max_per_line']:>7}")
        if r["other_sections"]:
            print("\n  outside the launch buffers, by kernel section:")
            for o in r["other_sections"]:
                print(f"    {o['section']:<28}{o['opcode']:<14}{o['txns']:>6} txns {o['bytes']:>8} B  "
                      f"{o['ranges']:>4} range(s)  {o['span']}")
        print()

    other = [t for t in p.txn_rows if t["window"] == "in" and t["buffer"] == "other"]
    if other:
        with open(out / "llc_out_other.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(other[0]))
            w.writeheader()
            w.writerows(other)
    lines = []
    for r in p.results:
        lines.append(f"# {r['kernel']}")
        cur = None
        for x in r["other_ranges"]:
            if (x["section"], x["opcode"]) != cur:
                cur = (x["section"], x["opcode"])
                s_ = next(o for o in r["other_sections"] if (o["section"], o["opcode"]) == cur)
                lines.append(f"\n{cur[0]}  {cur[1]}: {s_['txns']} txns, {s_['unique_lines']} unique lines, "
                             f"{s_['ranges']} contiguous range(s), span {s_['span']}")
            lines.append(f"    {x['from']}-{x['to']}  {x['lines']:5d} lines  {x['txns']:5d} txns")
        lines.append("")
    (out / "llc_out_other_ranges.txt").write_text("\n".join(lines))
    for w in p.warnings:
        print(f"warning: {w}")
    print(f"wrote {out}/llc_out_transactions.csv, llc_out_other.csv, llc_out_other_ranges.txt, llc_out.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
