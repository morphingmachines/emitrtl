# REDEFINE metrics from RTL traces

[rtltrace](../rtltrace/README.md) plugins that compute metrics from a captured RTL simulation log, and help find
*why* a kernel is far from ideal:
- `redefine_metrics.py`: the REDEFINE feature metrics (below), plus sub-windows and a γe waterfall.
  See [Bottleneck analysis](#bottleneck-analysis).
- `ce_phases.py`: where each CE's time goes, by PC-marker region (phase), with IPC, FMA rate and a bound class.
- `dma_metrics.py`: CR-DMA utilisation (requests, throughput, why the engine waits).
- `cache_metrics.py`: LLC and L1-D hits, misses and hit/miss ratios. See [Cache metrics](#cache-metrics).
- `llc_out.py`: what crosses the off-chip boundary (LLC_out), by kernel buffer (A, W, Z, ...), memory section and
  address range: which part of ρ is the kernel's data. See [Off-chip traffic by kernel buffer](#off-chip-traffic-by-kernel-buffer-llc_outpy).
  `llc_out_tally.py` runs it alone and writes per-transaction CSVs.

`redefine_common.py` holds what they share: workload loading, `parameters.md` parsing, the commit-log parse
(retired-instruction counts, markers, counter samples), kernel windows, sub-windows, the CE-phase engine and kernel
sections.

`redefine_report.py` runs everything for one log; `metrics_sweep.py` tabulates several workloads.

| Metric | Formula | Better when |
|---|---|---|
| Compute element utilization efficiency | γe = Ci / Co, where Ci = T_ops / (2·N_CE) | higher |
| Data-reuse index | ρ = D_D / (D_i + D_o) | closer to 1 |
| Data-memory penalty index | φ = D_D / M_i | lower |

## Where each quantity comes from

| Symbol | Source |
|---|---|
| T_ops | `KernelLaunch(..., t_ops=2*M*N*K)` in the workload's `launch.py`. A MAC counts as 2 ops. Override with `--plugin-arg t_ops=`. |
| N_CE | The CEs present in the trace (one `CeState` probe each) × `macs_per_ce` (default 1: one Rocket FPU FMA per CE). This is the hardware's CE count, not the launch grid, so an idle CE lowers γe. |
| Co | The kernel window in cycles: from the first CE leaving `Idle` to the last CE returning to `Idle`. The trace time axis is already in cycles, so γe does not depend on the clock frequency. `freq_mhz` (default 100, the testbench clock) only scales the reported t_i. |
| D_i, D_o | The sizes of the `IN` / `OUT` / `INOUT` buffer args in `launch.py`. `INOUT` counts toward both. |
| D_D | Bytes of TileLink transactions on the device-boundary port that start inside the window. Gets count as data in; Puts count as data out. |
| M_i | LLC size from the RTL config's `parameters.md` × the number of CRs in the trace. |

**Device boundary.** In the 1-CR sim (`sb-cr-sim`) everything behind the LLC (`LLC_out`) stands in for off-chip memory, so the default is `boundary=LLC?out`. For a full-IP sim, point `boundary=` at the port that leads to DDR.

## Usage

### Probes required

| Plugin | Needs (default-on unless noted) |
|---|---|
| `redefine_metrics.py` | `hyperop` (CeState), `LLC` (LLC ports); `llc_cache` (off) for the flush spans in sub-windows |
| `ce_phases.py` | `hyperop`; `ce_phase` (PC markers), or a commit log |
| `dma_metrics.py` | `CR_DMA` for probe mode (else infer mode from `LLC`); `CR_DMA_detail` (off) for the gate reasons |
| `cache_metrics.py` | `llc_cache`, `l1d_cache` (off) for probe mode (else infer mode from `LLC`); `l1d_spm` (off) for the scratchpad table |

The default-on groups are enough for γe/ρ/φ, CE phases and CR-DMA. The full set for a bottleneck analysis
(see [Probe groups and plusargs](#probe-groups-and-plusargs) for log sizes):

```
+rtltrace_llc_cache_on +rtltrace_l1d_cache_on +rtltrace_l1d_spm_on +rtltrace_CR_DMA_detail_on
```

To keep logs small instead, switch off what you do not need, e.g. `+rtltrace_orch.cmd_off`. Do not use
`+rtltrace_all_off` without switching `hyperop`, `LLC`, `ce_phase` and `CR_DMA` back on.

The trace is the simulator's stderr (`@RT` lines); capture stdout and stderr together. In the SDK flow
(`sb-cr-sim/build.py` and the other `sim_build` users), pass runtime plusargs through the `SIM_PLUSARGS` environment
variable:

```sh
cd <sdk>/mm-baremetal-examples/1CR/dmaMatmul_working
SIM_PLUSARGS="+rtltrace_llc_cache_on +rtltrace_l1d_cache_on" ../../../sb-cr-sim/build.py > trace.log 2>&1
```

The simulated RTL must be built with the probes: `trace_points.json` and `kernel_sections.json` next to the RTL
(`generated_sv_dir/<config>/`) list what it has (`python3 -m rtltrace points <...>/trace_points.json`).

### One workload, everything in one go

`redefine_report.py` takes a sim log, plus optionally the ELF and/or `launch.py`, runs the four metric plugins, the
overlap plugin (`overlap_metrics.py`), the off-chip traffic plugin (`llc_out.py`) and the view plugin
(`redefine_view.py`) in one `rtltrace report` and one `rtltrace perfetto` (in parallel), and writes
into `-o <dir>` (default `<log stem>_reports/` next to the log):

| File | Contents |
|---|---|
| `<stem>.summary.md` | **Start here.** The report body as Markdown, in debug-playbook order (below) |
| `<stem>.html` | The same with a table of contents, tooltips (RTL names) and the hardware-detail appendix + RTL name map |
| `csv/` | Every table as CSV; friendly-name columns get a `<column> (RTL)` column with the RTL identifier |
| `<stem>.perfetto.json` | Kernel view for https://ui.perfetto.dev: kernel overview, CE code regions, CR-DMA requests, cache hit rates, correctness checks, CE states (a few MB) |
| `<stem>.perfetto_full.json` | With `--perfetto-full`: every probe, for hardware debug (≈ 340 MB for dmaMatmul with every group on; the kernel view is ≈ 6 MB) |
| `<stem>.findings.json` | Ranked findings (also printed) |
| `<stem>.metrics.json` | γe/ρ/φ, sub-windows, CMU spans, waterfall, D_D by section |
| `<stem>.phases.json` | CE time by PC-marker region |
| `<stem>.dma.json` | CR-DMA windows, per-request records, traffic by section, gate histogram, `issues` (`dma_overflow` / `dma_error` events: correctness findings) |
| `<stem>.cache.json` | LLC / L1-D / scratchpad tables, hit/miss by section, hazard issues |
| `<stem>.overlap.json` | Compute / DMA overlap per CE, prefetch iterations, engine demand vs capacity, buffer safety |
| `<stem>.llc_out.json` | Off-chip (LLC_out) traffic by kernel buffer, by memory section, and every contiguous address range |
| `<stem>.llc_out.csv` | Every LLC_out A-channel transaction: time, opcode, address, bytes, buffer, section, client |

The report is ordered the way a kernel developer debugs: **1 Findings** (correctness first, then the biggest losses
of CE time, each with evidence, a suggestion and the RTL probes it rests on) → **2 Efficiency** (γe, ρ, φ, traffic by
buffer, who drives LLC traffic) → **3 Where the CE time went** (γe waterfall, CE state per CE, setup/steady/tail) →
**4 Code regions** (PC_MARK regions with IPC, flops/cycle and what limits them) → **5 Data movement** (CR-DMA requests,
throughput, why the engine waits) → **6 Memory hierarchy** (LLC by buffer and requester, L1-D, scratchpad sharing) →
**7 Checks and tool warnings** → appendices (glossary, probe groups in this run, hardware detail, RTL name map).
Names come from `redefine_vocab.py` (one place for every label and glossary entry): `CE2 L1-D / scratchpad`
instead of `baseCr.core_2.rockettile.dcache`, `runtime · DMA LLC→SPM call` instead of `rt.DMA_LLC2SPM`, buffer names
from `launch.py` (`W (in, 64 KB)`) instead of `GlobalDataLow[r33]` (addresses from the `[kernel_buffers]` table the
sim prints into the log, else `rtl_sim_results/buffers.txt`, or `--arg buffers=<file>`; see
[Kernel buffer addresses](#kernel-buffer-addresses)). Every friendly name keeps its RTL identifier (tooltip, CSV column, name map, Perfetto args).

Inputs that are not given are discovered:
- `launch.py` and the ELF are found from each other;
- `--config` (the RTL config dir with `parameters.md`, `trace_points.json`, `kernel_sections.json`) is found by
  walking up from the log to `generated_sv_dir/`; with several configs the testbench's top is preferred
  (`sb-cr-sim` → `SimCR`);
- `--regions` defaults to `kernel_sections.json` in the config, else the copy in this directory;
- PC-marker names come from `*pc_markers*.h` next to `launch.py`/the ELF and the SDK runtime header.

```sh
<this dir>/redefine_report.py <sdk>/mm-baremetal-examples/1CR/dmaMatmul_working/trace.log \
    --elf <sdk>/mm-baremetal-examples/1CR/dmaMatmul_working/dmaMatmul.elf \
    [--config <repo>/generated_sv_dir/redefine.cr.sim.SimCR.DemoCRConfig] [-o <dir>]
```

Other options: `--arg key=value` (any plugin arg, e.g. `t_ops=`, `from=`, `markers=`, `buffers=`), `--from/--to`
(cycle range for the timeline and report), `--tl-hops`, `--perfetto-full`, `--no-perfetto`, `--no-report`,
`--title`, `--regions none`, `--rtltrace <dir>`.

### One workload, by hand

```sh
cd <emitrtl>/tools/rtltrace
python3 -m rtltrace report <sim.log> -o report.html \
    --plugin <this dir>/redefine_metrics.py \
    --plugin-arg launch=<sdk>/mm-baremetal-examples/1CR/dmaMatmul_working/launch.py \
    --plugin-arg params=<sdk>/generated_sv_dir/redefine.cr.sim.SimCR.DemoCRConfig/parameters.md \
    --plugin-arg log=<sim.log> \
    --plugin-arg json=metrics.json
```

Add `--plugin <this dir>/ce_phases.py --plugin <this dir>/dma_metrics.py --plugin <this dir>/overlap_metrics.py
--plugin <this dir>/cache_metrics.py --plugin <this dir>/llc_out.py --plugin <this dir>/redefine_view.py` (in this
order: overlap_metrics needs ce_phases and dma_metrics, redefine_view goes last) and `--regions`, `--catalog`, `--elf` for the rest; this is what
`redefine_report.py` does.

`python3 -m rtltrace perfetto ...` with the same plugin args adds these processes to the Perfetto trace:
- **REDEFINE metrics:** a `kernel window` slice carrying every metric, `sub-windows` (setup / steady / tail) and
  `LLC CMU` (flush/clean) tracks, and `D_D cumulative bytes` and `active CEs` counters;
- **CE phases:** one track per CE with nested PC-marker regions, and per-CE `IPC` and `flops/cycle` counters;
- **CR-DMA:** one span per request (`requests`) and a `bytes/cycle` counter;
- **Cache metrics:** hit-% counters per cache, and miss instants with `miss_marks=1`.
- **Off-chip boundary (`llc_out.py`):** one cumulative-bytes counter per kernel buffer and per memory section outside
  them.

### Many workloads

Each run is `<launch.py or workload dir>=<sim log>`:

```sh
<emitrtl>/tools/rtltrace-plugins/metrics_sweep.py -o metrics/ --perfetto \
    --params <sdk>/generated_sv_dir/redefine.cr.sim.SimCR.DemoCRConfig/parameters.md \
    <sdk>/mm-baremetal-examples/1CR/Fib=<sdk>/sb-cr-sim/build/trace_llc_crdma_fib.log \
    <sdk>/mm-baremetal-examples/1CR/dmaMatmul_working=<sdk>/sb-cr-sim/build/trace_llc_crdma_dmaMatmul.log
```

This writes:
- `metrics.csv` and `metrics.md` (one row per kernel): γe, ρ, φ; the cache columns `llc_hit_ratio`, `l1d_hit_ratio`,
  …; and the analysis columns `steady_gamma_e`, `top_loss` / `top_loss_pct` (largest loss in the waterfall), `dma_mode`,
  `dma_steady_Bpc` (CR-DMA read bytes/cycle in the steady sub-window) and `dma_steady_active`;
- for each run, `<workload>.html`, `<workload>.json`, `<workload>.phases.json`, `<workload>.dma.json`,
  `<workload>.cache.json` and, with `--perfetto`, `<workload>.perfetto.json`.

`--no-cache` leaves the cache plugin out. `--regions` defaults to `kernel_sections.json` next to `--params`, else the
copy here. `--elf-from-launch` symbolizes with the `*.elf` next to each `launch.py` (and finds its marker header).
`-j` sets the number of parallel runs.

rtltrace is found through `--rtltrace`, `$RTLTRACE_DIR`, or the `rtltrace` directory next to this one (`emitrtl/tools/rtltrace`).

### Plugin args

| Arg | Meaning |
|---|---|
| `launch` | `launch.py`, or a `launches-*.json` kept by `build.py` |
| `params` | `parameters.md` of the simulated RTL config |
| `log` | Raw sim log. Decoded commit lines (spike-dasm output, `… inst=[..] fmadd.s …`), when present, cross-check T_ops and are the fallback source of PC markers. Raw `DASM(…)` commit lines are not decoded and are ignored (the `ce_marker` counters are used instead). |
| `elf`, `markers` | Where to look for `*pc_markers*.h` / an explicit list of marker headers (`a.h,b.h`) |
| `regions` | `kernel_sections.json` when rtltrace was not given `--regions` |
| `json` | Write the metrics summary here |
| `boundary` / `attrib` | Port globs for D_D (default `LLC?out`) and for the who-drives-the-traffic table (default `LLC?in`) |
| `t_ops`, `d_in`, `d_out`, `n_ce`, `macs_per_ce`, `m_i`, `freq_mhz` | Overrides |
| `from`, `to` | Manual kernel window, in cycles |
| `compute` | Regions counted as compute in the waterfall (`REGION,...`; default: ≥ 0.1 flops/cycle) |
| `tol` | T_ops cross-check tolerance (default 0.1) |

## Reading the report

Besides the metric tables, the report contains:

- **Sub-windows and γe waterfall.** See [Bottleneck analysis](#bottleneck-analysis).
- **D_D by kernel section.** Which kernel sections the off-chip traffic touched (`GlobalDataLow[r33]` in,
  `GroupLocalMemory` out, ...), and how much of D_D is data (GlobalData*, GroupLocal, Local, Scratchpad, Stack) rather
  than code, context or runtime traffic. Without a regions file this is **D_D by address region**, bucketed by the
  boundary port's managers or the top address byte.
- **Traffic on LLC_in by client.** Who caused it: CR-DMA, CE I$/D$ or the orchestrator caches.
- **CE time in the window.** Busy, Suspended and Idle per CE. This shows where γe is lost.
- **T_ops cross-check.** Counts retired `fmadd`-family instructions as 2 ops and `fadd`/`fsub`/`fmul`/`fdiv`/`fsqrt`
  as 1 op, from the commit log; without commit lines, from the `ce_marker` flops counters (up to each CE's last
  marker; fused ops = 2, `fdiv`/`fsqrt` not counted). A warning is raised when the total differs from the declared FP
  T_ops by more than `tol`. This count covers the whole log, not just the kernel window.
- **Warnings.** These cover:
  - ρ < 1: outputs were left in the LLC, or the boundary is wrong.
  - γe > 1
  - PutPartialData counted at full size, because the mask is not traced.
  - Transactions still unfinished at the end of the trace.
  - A CE still busy at the end of the trace.

**Multiple kernels.** With several `LAUNCHES`, the CE activity is split into one window per kernel at the longest all-Idle gaps.

**Any kernel.** The report is generated, not written for a kernel: every run gets the same chapters, filled with
what its trace contains. Missing inputs leave a section empty with a sentence saying why, and the findings name
them: no `t_ops` in `launch.py` (no γe; the waterfall still splits the CE time, with the possibly useful busy time
as one row), no PC markers (no code regions or compute / DMA overlap), a kernel that does not use the CR-DMA
("no CR-DMA requests in this run").

## Cache metrics

rtltrace knows nothing about caches, and a `Trace.tl` tap only sees the traffic crossing a cache's boundary, not the
tag-compare decision inside it. So the decision is exported by RTL probes, and `cache_metrics.py` holds the knowledge
of what to count.

### Probes

All are off by default. Enable them with `+rtltrace_llc_cache_on +rtltrace_l1d_cache_on`.

| Probe | Where | Fires | Fields |
|---|---|---|---|
| `llc_access` | `llc/Mshr.scala` (each MSHR) | once per request, at its **first** lookup | `hit isGet fromRpq tagMatch dirtyVictim fetchModify way source addr` |
| `llc_refill`, `llc_writeback` | `llc/Mshr.scala` | refill Get / dirty-victim writeback issued by an MSHR | – |
| `llc_cmu`, `llc_cmu_done` | `llc/DistributedLlc.scala` | flush/clean start and completion | `opcode oprand` |
| `l1d_access` | `L1Cache/NBDCache.scala` (each CE) | once per accepted cacheable load/store | `hit isWrite secondary addr` |
| `l1d_cmu_wb` | `L1Cache/NBDCache.scala` | writeback caused by an L1-D maintenance command | – |

Why these points:
- **LLC.** A miss is looked up again after its refill, and that second lookup always hits. The `lookedUp` register in
  `Mshr` keeps it out of the trace, so hits and misses are simple counts.
- **L1-D.** Nacked accesses are retried by the core, and replays after a refill go through `s2_replay`, so neither is
  counted twice. Scratchpad accesses (always "hit"), uncached accesses and Orch maintenance commands are excluded.

### What is reported

For each kernel window and for the whole run (`run`):

- **LLC:** accesses, hits, misses, hit and miss %, read and write misses, `merged`, dirty-victim and fetch-modify
  misses, refills and writebacks, and CMU operations. A request that waited in an MSHR's RPQ behind a miss to the same
  line is counted as `merged` (every replay-queue lookup, hit or miss) and also as a hit or a miss.
- **LLC by requester:** the TileLink source is mapped to the `LLC_in` client (Core N ICache/DCache, CR-DMA, the
  orchestrator caches).
- **L1-D per CE:** the same counts, plus secondary misses (merged into an in-flight MSHR) and MPKI. MPKI uses the CE's
  `ce_marker` instret counter over the same window (decoded commit lines, when the log has them, for the `run`
  window).
- **Scratchpad per CE** (`l1d_spm` probes): CE loads and stores, bytes the CR-DMA wrote into / read from the
  scratchpad (`SPM_in`), DMA beat latency, cycles the CE was blocked by a DMA beat and the DMA waited for the CE, and
  `spm_s3_overlap` events.
- **By kernel section** (with a regions file): LLC and L1-D accesses, hits and misses per section.

### Consistency checks

The plugin warns when any of these fail:
- MSHR refills ≠ the refills the traced misses imply. A read miss fetches its line, a store miss fetches only when the
  tag matches, and a fetch-modify victim with another tag is fetched first.
- LLC_out Gets outside CMU periods ≠ MSHR refills.
- Writebacks ≠ dirty-victim misses.

On fib all three hold exactly. For the L1-D, compare its primary read misses with the "L1-D refills at LLC" column.

### Without the probes (`mode=infer`)

Traces captured before these probes existed fall back to `mode=infer`. An LLC_in read counts as a miss when a Get to
the same 32 B line leaves on LLC_out while the read is pending; one fill serves one request.

This is approximate. Store misses issue no Get here, and fetch-modify fills target the old line, so only reads are
classified and the L1-D is not available.

### Plugin args

| Arg | Meaning |
|---|---|
| `launch`, `log` | As for `redefine_metrics.py` (kernel names / retired instructions for MPKI) |
| `cache_json` | Write the cache summary here |
| `from`, `to` | Manual kernel window, in cycles |
| `mode` | `auto` (default), `probe` or `infer` |
| `llc_in`, `llc_out` | Port globs (default `LLC?in` / `LLC?out`) |
| `line` | Cache line size in bytes for infer mode (default 32) |
| `bin` | Cycles per Perfetto hit-% counter sample (default 1000) |
| `miss_marks=1` | Add one Perfetto instant per miss |

## Bottleneck analysis

γe says how far a kernel is from ideal; these sections say where the cycles go and which unit to look at.

### Sub-windows and the γe waterfall (`redefine_metrics.py`)

Each kernel window is split into **setup** (until every CE is active), **steady** and **tail** (after the first CE
has finished for good). LLC CMU commands (flushes) inside the window are listed with their duration. *local γe* is
the FMA rate inside a sub-window (needs instret/flops counters: the `ce_marker` probe or the commit log).

The **waterfall** splits the N_CE × Co CE-cycles into: CEs idle/launching → CEs suspended → busy in each non-compute
region (DMA issue, runtime, untagged) → compute regions below peak → ideal (Ci × N_CE). Each row gives the γe reached
if that loss and the ones above it were removed. A region counts as compute when it retires ≥ 0.1 flops/cycle
(override with `compute=REGION,...`).

### CE phases (`ce_phases.py`)

PC markers are nops (`addi x0, x0, id`) that bracket code regions: `PC_MARK_BEGIN(id)` (even id) ...
`PC_MARK_END(id)` (id|1); regions nest. Two sources, in order of preference:
1. the RTL **`ce_marker`** probe (group `ce_phase`, on by default) in each CE's Rocket writeback stage: exact @RT time
   plus the CE's retired instructions and flops (fused multiply-add = 2) at every marker;
2. the **commit log** (`log=`): markers are the `li zero, N` commit lines, moved onto @RT time per CE by matching where
   the commit stream stops to the CE's Busy→Suspended/Idle edges (approximate, a few cycles).

Marker **names** come from `#define NAME 0x...` lines in `*pc_markers*.h` next to `launch.py`/the ELF and in the SDK
runtime header `CwithHyperOps/include/redefine_pc_markers.h` (`RE_PCM_FOO` is shown as `rt.FOO`), or `markers=a.h,b.h`.

| Id range | Owner |
|---|---|
| 0x400–0x5ff | Runtime: `shared2spm`, `spm2shared`, `local2local`, gather/scatter, channels, `re_suspend`, frame alloc/bind, LLC/CE flushes, `re_sigEndOfKernel`. Every kernel gets these. Compile out with `-DREDEFINE_PC_MARKERS=0`. |
| 0x001–0x3ff, 0x600–0x7ff | Kernels (e.g. `1CR/dmaMatmul_working/pc_markers.h`) |

The stack of open regions survives a suspend/resume and is cleared when the CE goes Idle or starts a new hyperOp.
Busy time outside any region is `untagged`; suspended time outside any region is `suspended`.

Per region: cycles split by CeState, instructions, IPC and flops per busy cycle, and what it is **limited by** (checked
in this order): **waiting** (> 50 % suspended or idle), **blocked in hardware** (busy but IPC < 0.05: a flush, fence
or blocking access), **stall-limited** (IPC < 0.6), **control / data-movement code** (no FP work),
**instruction-limited** (retiring well, < 60 % of peak flops), **compute-bound**. The JSON keeps the older `class`
key (waiting / stall-bound / instruction-bound / compute-bound) next to `limited_by`. Perfetto gets a "CE phases" process (one track per CE) and per-CE IPC and flops/cycle
counters.

| Arg | Meaning |
|---|---|
| `markers`, `elf`, `launch`, `log` | Marker headers / where to find them; kernel names; commit-log fallback |
| `macs_per_ce` | Peak is 2 × this flops/cycle (default 1) |
| `wait`, `idle_ipc`, `stall_ipc`, `compute_frac` | Class thresholds (defaults 0.5, 0.05, 0.6, 0.6). `compute=` is the compute-region list |
| `phases_json` | Write the phase table here |

### CR-DMA (`dma_metrics.py`)

- **probe** mode (CR-DMA probes, group `CR_DMA`, on): `dma_queued` → `dma_start` (type, src, dst, row size, stride,
  rows, sync) → `dma_done` → `dma_retire`, and the engine's TileLink port `CR_DMA_mbus`. Per request: queue wait,
  service time, bytes, the CE whose scratchpad is the source/destination. Engine busy fraction, throughput,
  Get→Get interval. With `+rtltrace_CR_DMA_detail_on`, `dma_gate` says why no request goes out in a cycle.
- **infer** mode (no CR-DMA probes): the CR-DMA's transactions on `LLC_in`. A serial engine shows < 1 transaction in
  flight and a Get→Get interval well above the LLC latency.

Both modes give, per kernel window and sub-window: transactions, Gets, Puts, bytes, bytes/cycle (Gets + Puts) and
read bytes/cycle, average in flight, active fraction, Get→Get interval and latency percentiles. With a regions file,
traffic by kernel section, and requests by source → destination section.

| Arg | Meaning |
|---|---|
| `mode` | `auto` (default: probe when `dma_start` events exist), `probe` or `infer` |
| `mbus`, `orch` | Port globs of the engine's memory and orchestrator links (default `CR?DMA?mbus`, `CR?DMA?orch`) |
| `port`, `client` | Infer mode: port glob (default `LLC?in`) and client substring (default `CR_DMA`) |
| `spm_base`, `spm_stride` | Scratchpad window used to find a request's CE (default `0x30000000`, `0x2000`) |
| `bin` | Cycles per bytes/cycle counter sample (default 1000) |
| `dma_json` | Write the CR-DMA summary here |

### Compute / DMA overlap (`overlap_metrics.py`)

Answers, for any kernel that moves data with the CR-DMA: is the data movement overlapped with compute, is the
overlap enough, and is it safe? No kernel-specific knowledge and no extra probes:

- **Compute** = the PC-marker regions that retire FP work (≥ 0.1 flops per busy cycle, or `compute=<regions>`).
- **Issue time** = the end of the runtime's DMA call (`rt.DMA_LLC2SPM` / `rt.DMA_SPM2LLC` / `rt.DMA_LLC2LLC`
  markers from `redefine_pc_markers.h`), matched in order to the CR-DMA requests whose scratchpad side is that CE
  (the match is validated; otherwise `dma_queued` is used and a warning says so). `dma_queued` alone would miss most
  of the wait: the engine holds one request in its "next" slot; other CEs' requests queue in the Orchestrator.
- **Per CE**: DMA in flight (issue → last write acknowledged), overlapped with compute, DMA hidden %, compute with
  DMA underneath %, exposed DMA (and how much of it the CE spent suspended).
- **Prefetch iterations** (a compute region + the requests issued just before it): data ready before the compute
  ended %, issue → data arrived split into wait for engine (and how many other CEs' requests were ahead) and transfer,
  exposed wait, wake-up latency.
- **Can the shared CR-DMA keep up?** Bytes/cycle the CEs need (prefetched bytes ÷ compute cycles, summed over CEs)
  vs bytes/cycle the engine delivers while transferring. Above 1, no prefetch depth can hide the transfers.
- **Is it safe?** (needs `+rtltrace_l1d_spm_on`): CE loads from a buffer a DMA is still filling, CE stores into a
  buffer a DMA is still sending, and `spm_s3_overlap` events split into "same buffer" (kernel race) and "another
  buffer" (hardware hazard).
- Perfetto: a `CE ↔ DMA overlap` process with each CE's requests as "waiting for engine" + "transfer" spans, to
  line up with the CE code regions.

How each number is computed, a worked dmaMatmul iteration and the cross-check:
[docs/compute_dma_overlap.md](docs/compute_dma_overlap.md). `python3 overlap_xcheck.py <out>/<stem>` recomputes
the overlap from the Perfetto JSON alone (kernel-view tracks, and raw probes with `--perfetto-full`) and compares
it with `<stem>.overlap.json`.

dmaMatmul: transfers run under 98 % of the compute and no buffer is touched while in flight, but each request waits
~1,950 cycles behind two other CEs' requests and transfers in 712, against 2,047 cycles of compute: the data is
ready in time in 1 % of iterations and each iteration waits ~650 cycles. The four CEs need 2.07 B/cycle and the
engine delivers 1.42 (1.46×), so the fix is more work per transferred byte, not deeper prefetch.

### Off-chip traffic by kernel buffer (`llc_out.py`)

Every A-channel transaction on the off-chip boundary port (`LLC?out`, as `redefine_metrics.py`) is attributed to a
kernel buffer by address or, outside the buffers, to its memory section and to contiguous line ranges. Per kernel
window (report chapter **2 Efficiency**, after "Off-chip traffic by buffer / memory section"):

- **kernel buffers vs everything else:** off-chip bytes (same window test and opcode rules as `redefine_metrics.py`,
  so the total equals its D_D) inside the kernel buffers and outside them, and ρ if only the buffers crossed;
- **per kernel buffer and opcode:** transactions, bytes, % of off-chip, unique lines, coverage (unique lines × line /
  buffer size; 1 = every line) and the most times one line crossed (1 = no re-fetch);
- **outside the buffers:** by memory section and opcode; every contiguous address range is in the hardware-detail
  appendix, `<stem>.llc_out.json` and `<stem>.llc_out.csv`.

dmaMatmul (1 CR, 584130f RTL): A, W and Z each cross exactly once (98,304 B = D_i + D_o, so ρ would be 1.0). The
other 141,728 B are group-local-memory write-backs (81,952 B at 0x40080000-0x40093fff, the size of A + W), context
memory (~55 KB) and code fetch (~4.5 KB): ρ = 2.44.

| Arg | Meaning |
|---|---|
| `log` | Sim log holding the `[kernel_buffers]` tables (`redefine_report.py` passes it) |
| `buffers` | Buffer table file(s), comma-separated per kernel, instead of the log |
| `boundary` | Port glob (default `LLC?out`) |
| `line` | Line size for unique lines and ranges (default 32) |
| `from`, `to` | Manual kernel window |
| `llc_out_json`, `llc_out_csv` | Write the summary / every transaction here |

```sh
<this dir>/llc_out_tally.py <sim.log> --config <generated_sv_dir/<config>> [--launch launch.py] \
    [--metrics <stem>.metrics.json] -o <dir>     # prints the tables, checks D_D against the metrics JSON
```

### Kernel buffer addresses

Buffer addresses are chosen by the host driver at run time, so they come from the run, not from `launch.py`. The
sim's host driver (`mm-baremetal-examples/kernel-exec-environment`) prints one block per kernel launch into the sim
log and writes the same table, without the marker, to `<workload>/rtl_sim_results/buffers.txt`:

```
[kernel_buffers]
name                  param       dir     addr_start    addr_end      size_bytes
buf_arg_0.bin         A           in      0x60000000    0x60003fff    16384
buf_arg_1.bin         W           in      0x64000000    0x6400ffff    65536
buf_arg_2.bin         Z           out     0x68000000    0x68003fff    16384
```

The line after the marker is a header (skipped); rows have 6 whitespace-separated fields; `param` is the name from
`buffer(..., name="A")` in `launch.py` (`-` if none); `dir` is `in` / `out` / `inout`; `addr_end` is the last byte,
inclusive; the block ends at the first line that is not a row. The 5-column form without `param` and the older marker
`[load_kernel_env] buffer addresses:` are also accepted. Any other host code that places kernel buffers should print
this block, then every plugin picks the addresses up. The full spec is in
`mm-baremetal-examples/kernel-exec-environment/README.md` ("Buffer address table").

`redefine_common.buffer_tables()` reads, in order: `buffers=<file>[,<file per kernel>]`; the blocks in the log; the
`buffers.txt` the log names; `rtl_sim_results/buffers.txt` next to `launch.py` / the ELF. The two file fallbacks
are reported in the warnings (a later run may have overwritten the file).

### Scratchpad probes (group `l1d_spm`, off by default)

`spm_access` (CE scratchpad loads/stores), `SPM-in` (the CR-DMA's beats into the scratchpad; shown as `SPM_in`),
`spm_ce_blocked`, `spm_dma_waits`. `spm_s3_overlap` (group `l1d_spm_check`, on) should never fire; an event flags a CE
load in stage 2 while a DMA write into the scratchpad is in progress, which NBDCache's stage-3 logic then treats as a
store. The dmaMatmul run shows 459 of them with a numerically correct result; whether any corrupt data is not yet
established.

### Kernel sections

With `--regions kernel_sections.json` (written next to `trace_points.json` by the RTL build, from
`KernelSectionConst`; a copy for older RTL is in this directory and is used by default by `redefine_report.py` and
`metrics_sweep.py`), addresses are labelled with their kernel section (`GlobalDataLow[r32]`, `StackMemory[ce2]`,
`GroupLocalMemory`, ...): D_D by section with a data vs code/runtime split, LLC and L1-D hit/miss by section, CR-DMA
traffic by section, and section names on Perfetto TileLink slices.

### Probe groups and plusargs

| Group | Default | Probes |
|---|---|---|
| `hyperop`, `LLC`, `CR_DMA`, `ce_phase`, `l1d_spm_check` | on | CeState, LLC ports, CR-DMA lifecycle and ports, PC markers, SPM hazard check |
| `orch.cmd`, `error` | on | orchestrator commands, exceptions (not used by these plugins) |
| `llc_cache`, `l1d_cache` | off | cache hit/miss |
| `l1d_spm`, `CR_DMA_detail` | off | scratchpad accesses and DMA beats, DMA gate reason |
| `ce` | off | CeIfc port probes |

For dmaMatmul (≈ 550k cycles) the log is ≈ 180 MB (1.75M lines) with everything above on, the full Perfetto file
(`--perfetto-full`) ≈ 340 MB and the kernel view ≈ 6 MB. The largest single probes are `spm_access` (≈ 290k events, group `l1d_spm`) and `dma_gate` (≈ 100k,
`CR_DMA_detail`); leave those groups off unless you are looking at scratchpad conflicts or CR-DMA stalls.

```sh
SIM_PLUSARGS="+rtltrace_llc_cache_on +rtltrace_l1d_cache_on +rtltrace_l1d_spm_on +rtltrace_CR_DMA_detail_on" \
  <usual sb-cr-sim command> > build/trace.log 2>&1
```

## Tests

```sh
cd <emitrtl>/tools/rtltrace-plugins && python3 -m unittest discover -s tests
```
