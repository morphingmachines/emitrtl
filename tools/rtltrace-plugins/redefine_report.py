#!/usr/bin/env python3
"""One simulation log -> Perfetto timeline, HTML report, REDEFINE metrics (γe, ρ, φ), CE phases, CR-DMA utilisation
and cache hit/miss summary.

    <emitrtl>/tools/rtltrace-plugins/redefine_report.py <sim log> [--elf prog.elf] [--launch launch.py] [-o outdir]

    e.g. (from sb-cr-sim/build)
    redefine_report.py trace_cache_metrics_dmamatmul.log \
        --elf ../../mm-baremetal-examples/1CR/dmaMatmul_working/dmaMatmul.elf

Inputs that are not given are discovered:
  --launch   launch.py next to the ELF (gives D_i/D_o and the declared T_ops; without it γe has no T_ops)
  --elf      the *.elf next to launch.py
  --config   the RTL config dir (parameters.md + trace_points.json): generated_sv_dir/<config>/ found by walking up
             from the log; when several configs exist the one matching the testbench (sb-cr-sim -> SimCR) is used

Writes into outdir (default <log stem>_reports/ next to the log):
  <stem>.perfetto.json   open in https://ui.perfetto.dev
  <stem>.html, csv/      rtltrace report: the generic analyses plus every plugin section (all tables as CSV too)
  <stem>.metrics.json    γe/ρ/φ, sub-windows, waterfall     <stem>.cache.json   cache hit/miss summary
  <stem>.phases.json     CE time by PC-marker region         <stem>.dma.json     CR-DMA utilisation
  <stem>.summary.md      the main tables (metrics, waterfall, phases, CR-DMA, cache, kernel sections), warnings

PC-marker names come from *pc_markers*.h next to launch.py / the ELF and the SDK runtime header (or --arg
markers=a.h,b.h). Without the RTL ce_marker probe, markers are read from the commit lines of the log.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from metrics_sweep import CACHE_PLUGIN, DMA_PLUGIN, PHASES_PLUGIN, PLUGIN, find_rtltrace, fmt  # noqa: E402

TESTBENCH_TOPS = {"sb-cr-sim": "SimCR", "sb-redefineip-sim": "SimRedefineIP", "sb-tinyredefineip-sim": "TinyRedefineIP",
                  "sb-fabric-sim": "Fabric"}
METRIC_COLS = ["kernel", "T_ops", "N_CE", "C_i", "C_o", "t_i_us", "gamma_e", "D_i", "D_o", "D_D_in", "D_D_out", "D_D",
               "rho", "M_i", "phi"]
LLC_COLS = ["cache", "accesses", "hits", "misses", "read_misses", "write_misses", "dirty_victim", "hit_ratio"]
L1D_COLS = ["cache", "accesses", "hits", "misses", "read_misses", "write_misses", "secondary", "hit_ratio", "mpki"]
SPM_COLS = ["cache", "loads", "stores", "dma_write_bytes", "dma_read_bytes", "dma_beat_latency_p50",
            "ce_blocked_cycles", "dma_wait_cycles", "s3_overlap"]
SUB_COLS = ["name", "from", "to", "cycles", "pct", "mean_active_ces", "flops", "local_gamma_e"]
WF_COLS = ["step", "ce_cycles", "pct", "gamma_e_if_removed"]
PHASE_COLS = ["region", "cycles", "pct", "busy", "suspended", "idle", "instret", "ipc", "flops_per_cycle", "class"]
DMA_COLS = ["window", "cycles", "txns", "gets", "puts", "bytes", "bytes_per_cycle", "read_bytes_per_cycle",
            "avg_in_flight", "active",
            "get_interval_p50", "latency_p50"]


def find_config(log: Path) -> Path | None:
    for parent in log.parents:
        configs = sorted(p.parent for p in parent.glob("generated_sv_dir/*/parameters.md"))
        if not configs:
            continue
        if len(configs) == 1:
            return configs[0]
        top = next((t for d, t in TESTBENCH_TOPS.items() if d in log.parts), None)
        match = [c for c in configs if top and f".{top}." in c.name]
        if len(match) == 1:
            return match[0]
        sys.exit("several RTL configs found, pass --config:\n  " + "\n  ".join(map(str, configs)))
    return None


def table(rows: list[dict], cols: list[str]) -> list[str]:
    if not rows:
        return ["(none)"]
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + \
           ["| " + " | ".join(fmt(r.get(c)) for c in cols) + " |" for r in rows]


def summary_md(metrics: dict | None, cache: dict | None, phases: dict | None = None, dma: dict | None = None) -> str:
    out = []
    if metrics:
        out += ["## REDEFINE metrics (per kernel window)", ""] + table(metrics["kernels"], METRIC_COLS) + [""]
        for k in metrics["kernels"]:
            subs = k.get("sub_windows", []) + [{"name": f"CMU {c['op']}", "from": c["from"], "to": c["to"],
                                                "cycles": c["cycles"], "pct": 100 * c["cycles"] / k["C_o"]}
                                               for c in k.get("cmu", [])]
            out += [f"## Sub-windows — `{k['kernel']}`", ""] + table(subs, SUB_COLS) + [""]
            if k.get("waterfall"):
                out += [f"## γe waterfall — `{k['kernel']}` (CE-cycles = N_CE x Co)", ""] + \
                       table(k["waterfall"], WF_COLS) + [""]
            if k.get("regions") and k.get("D_D_data") is not None:
                out += [f"## D_D by kernel section — `{k['kernel']}` (data sections: {fmt(k['D_D_data'])} of "
                        f"{fmt(k['D_D'])} B)", ""] + table(k["regions"], ["region", "dir", "bytes"]) + [""]
    if phases and phases.get("source"):
        rows = sorted((r for r in phases["rows"] if r["ce"] == "all"), key=lambda r: (r["kernel"], -r["cycles"]))
        out += [f"## CE phases — all CEs (source: {phases['source']})", ""] + table(rows, ["kernel"] + PHASE_COLS) + [""]
    if dma:
        out += [f"## CR-DMA ({dma['mode']})", ""] + table(dma["windows"], ["kernel"] + DMA_COLS) + [""]
        if dma.get("by_section"):
            out += ["CR-DMA traffic by kernel section:", ""] + \
                   table(dma["by_section"], ["kernel", "opcode", "section", "txns", "bytes"]) + [""]
    if cache:
        for w in cache["windows"]:
            span = f"cycles {w['from']}..{w['to']}" if w.get("from") is not None else ""
            out += [f"## Cache — window `{w['window']}` {span} (mode: {cache['mode']})", "", "LLC:", ""]
            out += table(w["llc"], LLC_COLS) + ["", "L1-D:", ""] + table(w["l1d"], L1D_COLS) + [""]
            if w.get("spm"):
                out += ["Scratchpad:", ""] + table(w["spm"], SPM_COLS) + [""]
            for key, name in (("llc_by_section", "LLC"), ("l1d_by_section", "L1-D")):
                if w.get(key):
                    out += [f"{name} by kernel section:", ""] + \
                           table(w[key], ["section", "accesses", "hits", "misses", "miss_ratio"]) + [""]
    warnings = list(dict.fromkeys(w for x in (metrics, cache, phases, dma) for w in (x or {}).get("warnings", [])))
    if warnings:
        out += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="captured simulation log (stdout+stderr with the @RT lines)")
    ap.add_argument("--elf", help="program ELF for symbolization (default: *.elf next to --launch)")
    ap.add_argument("--launch", help="workload launch.py or its directory (default: launch.py next to --elf)")
    ap.add_argument("--config", help="RTL config dir holding parameters.md and trace_points.json")
    ap.add_argument("--regions", help="kernel_sections.json (default: in --config, else the copy next to this "
                                      "script); 'none' to disable")
    ap.add_argument("-o", "--out", help="output directory (default <log stem>_reports next to the log)")
    ap.add_argument("--rtltrace", help="emitrtl tools/rtltrace directory")
    ap.add_argument("--arg", action="append", default=[], help="extra key=value plugin arg (e.g. t_ops=..., from=...)")
    ap.add_argument("--from", dest="t_from", help="first cycle to keep (perfetto/report)")
    ap.add_argument("--to", dest="t_to", help="last cycle to keep (perfetto/report)")
    ap.add_argument("--tl-hops", action="store_true", help="link TileLink transactions across ports in Perfetto")
    ap.add_argument("--no-perfetto", action="store_true", help="skip the Perfetto timeline")
    ap.add_argument("--no-report", action="store_true", help="skip the HTML report (metrics JSON is still written)")
    a = ap.parse_args()

    log = Path(a.log).resolve()
    if not log.is_file():
        sys.exit(f"log not found: {log}")
    elf = Path(a.elf).resolve() if a.elf else None
    launch = Path(a.launch).resolve() if a.launch else None
    if launch and launch.is_dir():
        launch = launch / "launch.py"
    if launch is None and elf and (elf.parent / "launch.py").is_file():
        launch = elf.parent / "launch.py"
    if elf is None and launch:
        elf = next(iter(sorted(launch.parent.glob("*.elf"))), None)
    for what, p in (("ELF", elf), ("launch file", launch)):
        if p and not p.is_file():
            sys.exit(f"{what} not found: {p}")
    config = Path(a.config).resolve() if a.config else find_config(log)

    stem = log.stem
    out = Path(a.out).resolve() if a.out else log.parent / f"{stem}_reports"
    out.mkdir(parents=True, exist_ok=True)
    metrics_json, cache_json = out / f"{stem}.metrics.json", out / f"{stem}.cache.json"
    phases_json, dma_json = out / f"{stem}.phases.json", out / f"{stem}.dma.json"

    print(f"log     {log}\nelf     {elf or '-'}\nlaunch  {launch or '- (no D_i/D_o, no declared T_ops)'}\n"
          f"config  {config or '- (no parameters.md / trace_points.json)'}\nout     {out}")

    pargs = {"log": log, "json": metrics_json, "cache_json": cache_json, "phases_json": phases_json,
             "dma_json": dma_json}
    if launch:
        pargs["launch"] = launch
    if elf:
        pargs["elf"] = elf
    if config and (config / "parameters.md").is_file():
        pargs["params"] = config / "parameters.md"
    pargs.update(kv.split("=", 1) for kv in a.arg)
    common = [str(log)] + [x for p in (PLUGIN, PHASES_PLUGIN, DMA_PLUGIN, CACHE_PLUGIN) for x in ("--plugin", str(p))]
    common += [x for k, v in pargs.items() for x in ("--plugin-arg", f"{k}={v}")]
    if elf:
        common += ["--elf", str(elf)]
    if config and (config / "trace_points.json").is_file():
        common += ["--catalog", str(config / "trace_points.json")]
    regions = a.regions or next((str(p) for p in ((config / "kernel_sections.json") if config else None,
                                                  HERE / "kernel_sections.json") if p and p.is_file()), "none")
    common += ["--regions", regions]
    for flag, v in (("--from", a.t_from), ("--to", a.t_to)):
        if v:
            common += [flag, v]

    cmds = {}
    if not a.no_report:
        cmds["report"] = ["report"] + common + ["-o", str(out / f"{stem}.html"), "--csv", str(out / "csv")]
    if not a.no_perfetto:
        cmds["perfetto"] = ["perfetto"] + common + (["--tl-hops"] if a.tl_hops else []) + \
                           ["-o", str(out / f"{stem}.perfetto.json")]
    if not cmds:  # metrics only: a report run is the cheapest way to drive the plugins
        cmds["report"] = ["report"] + common + ["-o", str(out / f"{stem}.html")]

    print(f"regions {regions}\n")
    rt = find_rtltrace(a.rtltrace)
    run = lambda kv: (kv[0], subprocess.run([sys.executable, "-m", "rtltrace"] + kv[1], cwd=rt,
                                             capture_output=True, text=True))
    with ThreadPoolExecutor(max_workers=len(cmds)) as ex:
        results = list(ex.map(run, cmds.items()))
    failed = False
    for name, p in results:
        if p.returncode:
            failed = True
            print(f"[{name}] FAILED (exit {p.returncode})\n{p.stdout}{p.stderr}", file=sys.stderr)
    if failed:
        return 1

    metrics, cache, phases, dma = (json.loads(f.read_text()) if f.is_file() else None
                                   for f in (metrics_json, cache_json, phases_json, dma_json))
    md = summary_md(metrics, cache, phases, dma)
    (out / f"{stem}.summary.md").write_text(md)
    print(md)
    print("written:")
    for f in sorted(out.iterdir()):
        print(f"  {f}")
    if not a.no_perfetto:
        print("\nopen the .perfetto.json in https://ui.perfetto.dev (Open trace file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
