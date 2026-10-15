#!/usr/bin/env python3
"""One simulation log -> kernel performance report (HTML + Markdown + CSV), Perfetto timeline and the JSON summaries
(REDEFINE metrics γe/ρ/φ, CE code regions, CR-DMA utilisation, cache hit/miss, findings).

    <emitrtl>/tools/rtltrace-plugins/redefine_report.py <sim log> [--elf prog.elf] [--launch launch.py] [-o outdir]

    e.g. (from sb-cr-sim/build)
    redefine_report.py trace_cache_metrics_dmamatmul.log \\
        --elf ../../mm-baremetal-examples/1CR/dmaMatmul_working/dmaMatmul.elf

Inputs that are not given are discovered:
  --launch   launch.py next to the ELF (gives D_i/D_o, buffer names and the declared T_ops; without it γe has no T_ops)
  --elf      the *.elf next to launch.py
  --config   the RTL config dir (parameters.md + trace_points.json): generated_sv_dir/<config>/ found by walking up
             from the log; when several configs exist the one matching the testbench (sb-cr-sim -> SimCR) is used
  buffers    rtl_sim_results/buffers.txt next to launch.py / the ELF (buffer addresses -> buffer names in the report)

Writes into outdir (default <log stem>_reports/ next to the log):
  <stem>.summary.md        the report body as Markdown (findings, efficiency, CE time, code regions, DMA, caches,
                           glossary) -- start here
  <stem>.html, csv/        the same plus the hardware-detail appendix and the RTL name map (every table as CSV too;
                           friendly-name columns get a '<column> (RTL)' column with the RTL identifier)
  <stem>.perfetto.json     kernel view for https://ui.perfetto.dev: CE code regions, CR-DMA requests, kernel overview,
                           counters, correctness checks, CE states (--perfetto-full adds <stem>.perfetto_full.json
                           with every probe)
  <stem>.findings.json     ranked findings          <stem>.metrics.json   γe/ρ/φ, sub-windows, waterfall
  <stem>.phases.json       CE time by PC region     <stem>.dma.json       CR-DMA utilisation
  <stem>.cache.json        cache hit/miss, hazards  <stem>.overlap.json   compute / DMA overlap

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
from metrics_sweep import CACHE_PLUGIN, DMA_PLUGIN, PHASES_PLUGIN, PLUGIN, find_rtltrace  # noqa: E402

TESTBENCH_TOPS = {"sb-cr-sim": "SimCR", "sb-redefineip-sim": "SimRedefineIP", "sb-tinyredefineip-sim": "TinyRedefineIP",
                  "sb-fabric-sim": "Fabric"}
VIEW_PLUGIN = HERE / "redefine_view.py"
OVERLAP_PLUGIN = HERE / "overlap_metrics.py"


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
    ap.add_argument("--perfetto-full", action="store_true",
                    help="also write <stem>.perfetto_full.json with every probe (large; for hardware debug)")
    ap.add_argument("--title", help="report title (default: 'Kernel performance report — <kernels>')")
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
    findings_json = out / f"{stem}.findings.json"
    overlap_json = out / f"{stem}.overlap.json"

    print(f"log     {log}\nelf     {elf or '-'}\nlaunch  {launch or '- (no D_i/D_o, no declared T_ops)'}\n"
          f"config  {config or '- (no parameters.md / trace_points.json)'}\nout     {out}")

    pargs = {"log": log}
    # the JSON summaries are written by one run only: the runs go in parallel and would write the same files
    jargs = {"json": metrics_json, "cache_json": cache_json, "phases_json": phases_json, "dma_json": dma_json,
             "findings_json": findings_json, "overlap_json": overlap_json}
    if launch:
        pargs["launch"] = launch
    if elf:
        pargs["elf"] = elf
    if config and (config / "parameters.md").is_file():
        pargs["params"] = config / "parameters.md"
    bad = [kv for kv in a.arg if "=" not in kv]
    if bad:
        sys.exit(f"--arg expects key=value, got {bad[0]!r}")
    pargs.update(kv.split("=", 1) for kv in a.arg)
    common = [str(log)] + [x for p in (PLUGIN, PHASES_PLUGIN, DMA_PLUGIN, OVERLAP_PLUGIN, CACHE_PLUGIN, VIEW_PLUGIN)
                           for x in ("--plugin", str(p))]
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

    title = a.title
    if not title:
        try:
            from redefine_common import load_workload
            names = [k.name for k in load_workload(str(launch))] if launch else []
        except Exception:  # noqa: BLE001 - the title is cosmetic
            names = []
        title = "Kernel performance report" + (f" — {', '.join(names)}" if names else f" — {stem}")
    cmds = {}
    if not a.no_report:
        cmds["report"] = ["report"] + common + ["-o", str(out / f"{stem}.html"), "--csv", str(out / "csv"),
                                                "--md", str(out / f"{stem}.summary.md"), "--title", title]
    if not a.no_perfetto:
        hops = ["--tl-hops"] if a.tl_hops else []
        cmds["perfetto"] = ["perfetto"] + common + hops + ["--view", "kernel", "-o", str(out / f"{stem}.perfetto.json")]
        if a.perfetto_full:
            cmds["perfetto-full"] = ["perfetto"] + common + hops + ["--view", "full", "-o",
                                                                    str(out / f"{stem}.perfetto_full.json")]
    if "report" not in cmds:
        # the findings JSON is written by the report step (redefine_view.report), so a JSON-writing run is always a
        # report run, even with --no-report (its HTML goes to a scratch name and is removed afterwards)
        cmds["report"] = ["report"] + common + ["-o", str(out / f".{stem}.metrics_only.html"), "--title", title]
    cmds["report"] += [x for k, v in jargs.items() for x in ("--plugin-arg", f"{k}={v}")]
    for f in jargs.values():  # never list or print a summary left over from an earlier run
        f.unlink(missing_ok=True)

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
        elif "[rtltrace] plugin '" in p.stderr:
            # rtltrace disables a failing plugin and carries on: say so, the report is missing that part
            print(f"[{name}] a plugin failed or was skipped; its tables and JSON are missing:\n{p.stderr}",
                  file=sys.stderr)
    scratch = out / f".{stem}.metrics_only.html"
    scratch.unlink(missing_ok=True)
    if failed:
        return 1

    found = json.loads(findings_json.read_text()) if findings_json.is_file() else []
    if found:
        print("findings (most important first):")
        for i, f in enumerate(found, 1):
            print(f"  {i}. [{f['severity']}] {f['title']}")
        print()
    print("written:")
    for f in sorted(out.iterdir()):
        print(f"  {f}")
    if not a.no_report:
        print(f"\nstart with {out / f'{stem}.summary.md'} (or the .html: same content plus hardware detail)")
    if not a.no_perfetto:
        print("open the .perfetto.json in https://ui.perfetto.dev (Open trace file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
