#!/usr/bin/env python3
"""Compute the REDEFINE feature metrics (γe, ρ, φ) for several workloads and tabulate them.

Each RUN pairs a workload's launch.py with the captured simulation log of that workload:

    <emitrtl>/tools/rtltrace-plugins/metrics_sweep.py -o metrics/ \
        --params <sdk>/generated_sv_dir/redefine.cr.sim.SimCR.DemoCRConfig/parameters.md \
        <sdk>/mm-baremetal-examples/1CR/Fib/launch.py=<sdk>/sb-cr-sim/build/trace_llc_crdma_fib.log \
        <sdk>/mm-baremetal-examples/1CR/dmaMatmul_working=<sdk>/sb-cr-sim/build/trace_llc_crdma_dmaMatmul.log

For every run it writes <name>.html (rtltrace report with the metrics sections), <name>.json (γe/ρ/φ summary,
sub-windows, waterfall), <name>.phases.json (CE phases), <name>.dma.json (CR-DMA utilisation) and <name>.cache.json
(cache hit/miss summary, unless --no-cache), plus <name>.perfetto.json with --perfetto. All runs are aggregated into
metrics.csv and metrics.md (cache columns are LLC and L1-D totals over the kernel window; steady_gamma_e, top_loss and
dma_steady_* come from the steady sub-window).
rtltrace is found via --rtltrace, $RTLTRACE_DIR or the emitrtl/tools/rtltrace directory next to this one.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE / "redefine_metrics.py"
CACHE_PLUGIN = HERE / "cache_metrics.py"
PHASES_PLUGIN = HERE / "ce_phases.py"
DMA_PLUGIN = HERE / "dma_metrics.py"
COLUMNS = ["workload", "kernel", "T_ops", "N_CE", "C_i", "C_o", "gamma_e", "D_i", "D_o", "D_D_in", "D_D_out", "D_D",
           "rho", "M_i", "phi", "D_D_before_window", "D_D_after_window"]
CACHE_COLUMNS = ["cache_mode", "llc_accesses", "llc_misses", "llc_hit_ratio", "l1d_accesses", "l1d_misses",
                 "l1d_hit_ratio"]
ANALYSIS_COLUMNS = ["steady_gamma_e", "top_loss", "top_loss_pct", "dma_mode", "dma_steady_Bpc", "dma_steady_active"]


def find_rtltrace(given: str | None) -> Path:
    if given or os.environ.get("RTLTRACE_DIR"):
        return Path(given or os.environ["RTLTRACE_DIR"]).resolve()
    for parent in HERE.parents:
        for cand in (parent / "rtltrace",  # emitrtl/tools/rtltrace, next to this directory
                     parent / "dependencies/emitrtl/tools/rtltrace",
                     parent / "playground/dependencies/emitrtl/tools/rtltrace"):
            if (cand / "rtltrace/__init__.py").is_file():
                return cand
    sys.exit("rtltrace not found: pass --rtltrace <emitrtl>/tools/rtltrace or set RTLTRACE_DIR")


def parse_run(spec: str) -> tuple[str, Path, Path]:
    launch, sep, log = spec.partition("=")
    if not sep:
        sys.exit(f"run '{spec}' must be <launch.py or workload dir>=<sim log>")
    launch = Path(launch)
    if launch.is_dir():
        launch = launch / "launch.py"
    if not launch.is_file():
        sys.exit(f"launch file not found: {launch}")
    if not Path(log).is_file():
        sys.exit(f"sim log not found: {log}")
    return launch.parent.name, launch.resolve(), Path(log).resolve()


def run_one(rt: Path, name: str, launch: Path, log: Path, a) -> tuple[str, dict | None, str]:
    out = Path(a.out).resolve()
    pargs = {"launch": launch, "log": log, "json": out / f"{name}.json", **dict(kv.split("=", 1) for kv in a.arg)}
    if a.params:
        pargs["params"] = Path(a.params).resolve()
    plugins = ["--plugin", str(PLUGIN), "--plugin", str(PHASES_PLUGIN), "--plugin", str(DMA_PLUGIN)]
    pargs["phases_json"] = out / f"{name}.phases.json"
    pargs["dma_json"] = out / f"{name}.dma.json"
    if not a.no_cache:
        pargs["cache_json"] = out / f"{name}.cache.json"
        plugins += ["--plugin", str(CACHE_PLUGIN)]
    common = [str(log)] + plugins + [x for k, v in pargs.items() for x in ("--plugin-arg", f"{k}={v}")]
    regions = a.regions or next((str(p) for p in (Path(a.params).resolve().parent / "kernel_sections.json"
                                                  if a.params else None, HERE / "kernel_sections.json")
                                 if p and p.is_file()), "none")
    common += ["--regions", regions]
    if a.elf_from_launch:
        elfs = sorted(launch.parent.glob("*.elf"))
        if elfs:
            common += ["--elf", str(elfs[0]), "--plugin-arg", f"elf={elfs[0]}"]
    cmds = [[sys.executable, "-m", "rtltrace", "report"] + common + ["-o", str(out / f"{name}.html")]]
    if a.perfetto:
        cmds.append([sys.executable, "-m", "rtltrace", "perfetto"] + common + ["-o", str(out / f"{name}.perfetto.json")])
    msgs = []
    for cmd in cmds:
        p = subprocess.run(cmd, cwd=rt, capture_output=True, text=True)
        msgs.append(p.stdout + p.stderr)
        if p.returncode:
            return name, None, "".join(msgs)
    summary = json.loads((out / f"{name}.json").read_text())
    for key, f in (("cache", out / f"{name}.cache.json"), ("phases", out / f"{name}.phases.json"),
                   ("dma", out / f"{name}.dma.json")):
        if f.is_file():
            summary[key] = json.loads(f.read_text())
    return name, summary, "".join(msgs)


def analysis_columns(summary: dict, k: dict) -> dict:
    """Steady-state γe, the largest waterfall loss and the CR-DMA steady-state throughput of one kernel."""
    row = {}
    subs = {sw["name"]: sw for sw in k.get("sub_windows", [])}
    steady = subs.get("steady") or subs.get("whole")
    if steady:
        row["steady_gamma_e"] = steady.get("local_gamma_e")
    losses = [w for w in k.get("waterfall", []) if not w["step"].startswith("ideal")]
    if losses:
        top = max(losses, key=lambda w: w["ce_cycles"])
        row.update(top_loss=top["step"], top_loss_pct=top["pct"])
    dma = summary.get("dma")
    if dma:
        row["dma_mode"] = dma["mode"]
        ws = {w["window"]: w for w in dma["windows"] if w["kernel"] == k["kernel"]}
        w = ws.get("steady") or ws.get("whole")
        if w:
            row.update(dma_steady_Bpc=w.get("read_bytes_per_cycle", w["bytes_per_cycle"]),
                       dma_steady_active=w["active"])
    return row


def cache_columns(cache: dict | None, kernel: str) -> dict:
    """LLC and L1-D totals (all LLCs, all CEs) for one kernel window of a cache_metrics summary."""
    if not cache:
        return {}
    w = next((w for w in cache["windows"] if w["window"] == kernel), None)
    if w is None:
        return {"cache_mode": cache["mode"]}
    row = {"cache_mode": cache["mode"]}
    for key, rows in (("llc", w["llc"]), ("l1d", w["l1d"])):
        if rows:
            n, hits = sum(r["accesses"] for r in rows), sum(r["hits"] for r in rows)
            row.update({f"{key}_accesses": n, f"{key}_misses": sum(r["misses"] for r in rows),
                        f"{key}_hit_ratio": hits / n if n else None})
    return row


def fmt(v):
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.4g}"
    return "–" if v is None else str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="<launch.py or workload dir>=<sim log>")
    ap.add_argument("-o", "--out", default="metrics", help="output directory (default ./metrics)")
    ap.add_argument("--params", help="parameters.md of the simulated RTL config (LLC size, nCEs)")
    ap.add_argument("--rtltrace", help="emitrtl tools/rtltrace directory")
    ap.add_argument("--regions", help="kernel_sections.json (default: next to --params, else the copy next to this "
                                      "script); 'none' to disable")
    ap.add_argument("--arg", action="append", default=[], help="extra key=value plugin arg for every run")
    ap.add_argument("--perfetto", action="store_true", help="also write a Perfetto trace per run")
    ap.add_argument("--no-cache", action="store_true", help="skip the cache hit/miss plugin (cache_metrics.py)")
    ap.add_argument("--elf-from-launch", action="store_true", help="symbolize with the *.elf next to launch.py")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()

    rt = find_rtltrace(a.rtltrace)
    runs = [parse_run(s) for s in a.runs]
    names = [n for n, _, _ in runs]
    runs = [(f"{n}_{i}" if names.count(n) > 1 else n, l, g) for i, (n, l, g) in enumerate(runs)]
    Path(a.out).mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(lambda r: run_one(rt, *r, a), runs))

    rows, failed = [], 0
    for name, summary, msg in results:
        if summary is None:
            failed += 1
            print(f"[{name}] FAILED\n{msg}", file=sys.stderr)
            continue
        warnings = list(dict.fromkeys(summary["warnings"] + [w for key in ("cache", "phases", "dma")
                                                              for w in (summary.get(key) or {}).get("warnings", [])]))
        for k in summary["kernels"]:
            rows.append({**{c: k.get(c) for c in COLUMNS}, "workload": name,
                         **cache_columns(summary.get("cache"), k["kernel"]), **analysis_columns(summary, k),
                         "warnings": len(warnings)})
        for w in warnings:
            print(f"[{name}] warning: {w}", file=sys.stderr)

    out = Path(a.out)
    columns = COLUMNS + ([] if a.no_cache else CACHE_COLUMNS) + ANALYSIS_COLUMNS + ["warnings"]
    with open(out / "metrics.csv", "w", newline="") as f:
        w = csv.DictWriter(f, columns, restval="")
        w.writeheader()
        w.writerows(rows)
    md = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    md += ["| " + " | ".join(fmt(r.get(c)) for c in columns) + " |" for r in rows]
    (out / "metrics.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nwrote {out / 'metrics.csv'}, {out / 'metrics.md'} and per-run reports in {out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
