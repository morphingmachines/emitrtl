# Kernel report metrics: how every number is measured

This reference covers every value in `<stem>.summary.md`, in report order. For each value it gives:

- a short **summary** of what is measured and the formula
- the **detail**: probes, RTL signals and edge cases
- an **example** from the dmaMatmul trace

The compute / DMA overlap tables have their own document, [compute_dma_overlap.md](compute_dma_overlap.md).

| | |
|---|---|
| Code | [`redefine_metrics.py`](../redefine_metrics.py) (chapters 2, 3, 7), [`ce_phases.py`](../ce_phases.py) (4), [`dma_metrics.py`](../dma_metrics.py) and [`overlap_metrics.py`](../overlap_metrics.py) (5), [`cache_metrics.py`](../cache_metrics.py) (6), [`redefine_findings.py`](../redefine_findings.py) and [`redefine_view.py`](../redefine_view.py) (run card, findings, appendix), [`redefine_common.py`](../redefine_common.py) (shared: window, phases, counters) |
| Example run | dmaMatmul (`1CR/dmaMatmul_working`): Z(32×128) = A(32×128) · W(128×128), 1 CR × 4 CEs, trace `mm_phase.log`, report `redefine_report.py mm_phase.log --elf dmaMatmul.elf --perfetto-full` |
| Verified | Every example value was recomputed from the raw probe events in `mm_phase.perfetto_full.json` by an independent script that does not use rtltrace. All values agreed (§13). |

**Contents:**

1. [Conventions](#1-conventions)
2. [Probes and the RTL signals behind them](#2-probes-and-the-rtl-signals-behind-them)
3. [Run card](#3-run-card)
4. [Findings](#4-findings-what-to-look-at-first)
5. [Efficiency](#5-efficiency-chapter-2)
6. [Where the CE time went](#6-where-the-ce-time-went-chapter-3)
7. [Code regions (PC_MARK)](#7-code-regions-chapter-4)
8. [Data movement (CR-DMA)](#8-data-movement-cr-dma-chapter-5)
9. [Memory hierarchy](#9-memory-hierarchy-chapter-6)
10. [Checks and tool warnings](#10-checks-and-tool-warnings-chapter-7)
11. [Appendix tables](#11-appendix-tables)
12. [Values the summary does not show](#12-values-the-summary-does-not-show)
13. [How the examples were verified](#13-how-the-examples-were-verified)
14. [Assumptions and decisions to review](#14-assumptions-and-decisions-to-review)

---

## 1. Conventions

**Time.** Every probe prints `@RT <sim time> …` lines. rtltrace divides the simulation time by the period of the
fastest traced clock. Every time in the report is therefore a **cycle of the CR clock**: 1 cycle = 10,000,000
simulation time units, which is 10 ns at the 100 MHz testbench clock. Events are stamped at cycle + 0.5, so the
dmaMatmul window starts at 13,712.5. The report prints whole cycles; differences are exact integers.

**Kernel window W = [w0, w1].** w0 is when the first CE leaves Idle; w1 is when the last CE returns to Idle.
Both come from the Orchestrator's `CeState` of each CE. "Inside the window" means the event's time t satisfies
w0 ≤ t ≤ w1. For a transaction, t is its start (see below).
- *Several kernels:* when `launch.py` has several kernels, the CE activity is split at the longest all-Idle gaps.
- *Manual window:* `--arg from=<cycle> --arg to=<cycle>` (plugin args) replace it. `--from` / `--to` only clip the
  trace: the window is then found inside the clipped span, starting at the first `CeState` change after `--from`
  (the state already open at `--from` is not seen).
- *dmaMatmul:* W = [13,712.5, 565,878.5], so Co = 552,166 cycles.

**TileLink transactions.** On each traced port (`Trace.tl`), the A-channel beats are paired with the D-channel
response by `source` ID. A multi-beat burst is one transaction.
- **start** = the first A beat minus its request wait, i.e. the cycle `valid` rose.
- **end** = the last D beat + 1 cycle.
- **latency** = first D beat − last A beat.
- **total** = end − start.
- **bytes** = 2^`size`. A PutPartialData counts at full size, because the mask is not traced.
- **client:** the TileLink master that issued the transaction, looked up from `source` and the port's
  client-ID ranges (`TLMasterParameters` names, e.g. `[CR_DMA] MBus Client`).

**CE-cycles.** For efficiency, time is counted per CE: (CEs in the trace) × Co CE-cycles in the window. This equals
N_CE × Co at the default `macs_per_ce` = 1 (it differs only with `macs_per_ce=` or `n_ce=`). dmaMatmul has
4 × 552,166 = 2,208,664 CE-cycles.

**Percentiles.** p50 / p95 are taken from the sorted list as `xs[min(n−1, ⌊q·n⌋)]`, so with an even count p50 is
the upper median. One exception: the "Prefetch iterations" medians (chapter 5) use the true median, which can
end in .5.

**CE states** (`CeState`, Orchestrator view of a CE): Idle, PreLaunch, Launch, Busy, Suspended.
- "Busy" = executing a HyperOp.
- "Suspended" = a resumable HyperOp parked by `re_suspend()` until its wake-up count reaches 0 (DMA completion,
  syncs).
- "idle / launching" = Idle + PreLaunch + Launch.

**Names.** The report shows friendly names. The RTL instance path stays attached:
- in the HTML: tooltips, and inline in the hardware appendix
- in the CSVs: `<column> (RTL)` columns
- in Perfetto: the `rtl_path` / `probe` / `chisel_src` args

RTL paths below are relative to `TOP.testbench.SimCR_i.cr.cr`.

---

## 2. Probes and the RTL signals behind them

Every number in the report comes from the probes below. Each is one `emitrtl` `Trace.*` call in the Chisel
source, which elaborates to a `printf` under `ifndef SYNTHESIS`, gated by `~reset` and by its group's plusarg.

**How each kind prints:**

| Kind | Prints |
|---|---|
| `Trace.event(name, cond, fields)` | one line in every cycle `cond` is high, with the fields |
| `Trace.pulse(name, b)` (declared as kind *valid*) | one line in every cycle `b` is high |
| `Trace.change(name, sig)` | one line in the first cycle after reset and whenever `sig` differs from its previous-cycle value: a state register's line appears **one cycle after** the cycle in which its transition condition was true |
| `Trace.fire(name, rv)` | one line per transfer (`valid && ready`), and a STALL line in the first cycle of `valid && !ready` |
| `Trace.tl(name, bundle, edge)` | a *fire* probe per channel. These links are TL-UL: A fields opcode, param, size, source, address, corrupt; D fields opcode, param, size, source, sink, denied, corrupt. **Data and mask are not traced.** |

**Common rules:**
- **Sampling:** a printf samples the values before the clock edge, so a register in a payload shows its value
  *before* that cycle's update.
- **Clock:** every probe here is on the single CR clock. CE tiles are synchronous and `clockGate = false`.
- **Default-off groups**, enabled with `+rtltrace_<group>_on`: `ce`, `l1d_cache`, `l1d_spm`, `llc_cache`,
  `CR_DMA_detail`. Everything else is on by default.
- **Cache sizes** in this build (`parameters.md`): L1-D 8 KB (64 sets × 4 ways × 32 B, 4 MSHRs); LLC 512 KB
  (2,048 sets × 8 ways × 32 B lines, replay queue depth 2). The LLC has 16 MSHRs: that number is not in
  `parameters.md`, it is the count of `uncore.cache.mshrs_N` probe instances in the trace.

### 2.1 CE core and Orchestrator

**`CeState`** (change, `hyperop`)
- **Where:** `orch.ceIfc_N` · `orchestrator/…/ceIfc/CEIfc.scala:71` · `ceStateReg` (enum).
- **Reset value:** Idle.
- **Transitions:**
  - Idle → PreLaunch: the launcher acked the CE's free request (`ceFreeMutex.io.deq.fire`).
  - PreLaunch → Launch: the frame was delivered (`launchReqBuf.io.enq.fire`).
  - Suspended → Launch: a continuation frame arrived (`launchReqBuf.io.deq.valid`).
  - Launch → Busy: the frame-store serializer accepted the whole frame (`launchReqBuf.io.deq.fire`).
  - Busy → Idle (END with `op1 == 0`) or Suspended / Launch (END with `op1 != 0`, i.e. `re_suspend`).
  - PreLaunch → Idle if the CE left redefine-exe mode in the launch-ack cycle.
- **Caveats:** states are shifted by one cycle (change probe). Durations are exact.

**`ce_marker`** (event, `ce_phase`)
- **Where:** `core[_N].rockettile.core` · `ceTile/…/CeRocketCore.scala:69`.
- **Fires when:** `wb_valid && wb_reg_inst` is `addi x0, x0, imm` with imm ≠ 0, i.e. when a PC marker *retires*.
  The compressed `c.addi x0` hint also matches.
- **Fields:**
  - `hart = io.hartid`
  - `imm = wb_reg_inst(31,20)`
  - `instret`: a 32-bit counter, +1 on every `wb_valid` (the same condition as `minstret`)
  - `flops`: a 32-bit counter, +2 for a retired fused FMA (`fma && ren3`), +1 for fadd / fsub / fmul (`fma`)
- **Caveats:**
  - **Sampled before the update:** a marker's `instret` excludes the marker itself. Δinstret(BEGIN→END) counts the
    BEGIN marker and everything up to, but not including, END.
  - **What counts as flops:** fdiv / fsqrt, min / max, compares, conversions and FP loads / stores count 0. Flops
    are counted at retirement in WB.
  - **Free-running counters:** they run from reset across all code, boot included, and wrap at 2³².
  - **`hart`** is the CE index inside the CR; it is not unique across CRs.

**`Launch`** (event, `hyperop`)
- **Where:** `orch.ceIfc_N` · `CEIfc.scala:310`.
- **Fires when:** a **fresh** (non-continuation) HyperOp launch, in the Launch→Busy cycle. Resumes emit none.
- **Fields:** `instId`, `codePtr`.

**`CeFreeBitMap`** (change, `hyperop`)
- **Where:** `orch` · `orchestrator/…/Orchestrator.scala:173` · `schedQ.io.ceFree`.
- **Meaning:** bit i = CE i is Idle, in redefine-exe mode and holds a free token. A CE that is Idle but not in
  redefine mode shows 0.

### 2.2 CR-DMA (`crDMA/src/main/scala/nodeImp.scala`)

The engine has two descriptor banks:
- **next:** software writes the registers, then the doorbell `0x18` sets `nextValid`.
- **exec:** when exec is free, next is promoted to exec (`execValid`) the following cycle. Exec is freed when the
  completion sync is acknowledged.

| Probe | Kind · group | Where · line | Fires when | Fields / caveats |
|---|---|---|---|---|
| `dma_queued` | event · CR_DMA | `uncore.crDma.regNode` · 572 | rising edge of `nextValid`, one cycle after the doorbell write | `busy = execValid` (1: this request waits behind the one executing). A doorbell while `nextValid` is already 1 overwrites *next* and gives no new `dma_queued`; see `dma_overflow` |
| `dma_overflow` | pulse · CR_DMA | regNode · 573 | rising edge of `overflow` (a doorbell while *next* was occupied) | a descriptor was lost. A correctness finding and a CR-DMA tool warning (§4) |
| `dma_start` | event · CR_DMA | regNode · 574 | rising edge of `execValid`, the first exec cycle (the memory engine sees it one cycle later) | `type` (1 LLC2SPM, 2 SPM2LLC, 3 LLC2LLC, 4/5 group scatter / gather, 6 CR2P, 7 P2CR), `src`, `dst`, `rowBytes`, `stride`, `rows`, `dstRowBytes`, `dstStride`, `dstRows`, `dstCrX/Y`, `sync`. **For SPM2LLC / P2CR the source side is one contiguous row** (`rows = 1`, `rowBytes = dstRowBytes × dstRows`); for LLC2SPM / LLC2LLC / CR2P the destination side is |
| `dma_done` | pulse · CR_DMA | `uncore.crDma.mbusMaster` · 1247 | the D-channel AccessAck of the **last write of the last row**, counted by acks (`lastBurstInAccessTx2ndAck && lastStrideTx2ndAck && d.fire`). Also pulses on any denied / corrupt D beat (`tlError`), which ends the transfer | CR2P completes elsewhere and gives no `dma_done` |
| `dma_retire` | pulse · CR_DMA | regNode · 595 | the Orchestrator's AccessAck of the completion-sync PutFullData (`wrDoneSyncAddr && execValid`); exec is freed at the next edge | a denied ack does not retire |
| `dma_error` | event · CR_DMA | mbusMaster · 1248 | denied or corrupt D beat | `info = {source, denied, corrupt}`. A correctness finding and a CR-DMA tool warning (§4) |
| `dma_gate` | change · CR_DMA_detail | mbusMaster · 1262 | the engine's per-cycle state, priority-encoded (first match wins), see below | `io.execValid` lags `dma_start` by one cycle |

**`dma_gate` values**, in priority order:

| Value | Condition | Meaning |
|---|---|---|
| `idle` | `!execValid` or the mode is not 1–5 | no request (CR2P / P2CR transfers also report idle) |
| `issue` | `sourceAQueue.io.enq.fire` | a Get or Put entered the A queue |
| `aQueueFull` | `enq.valid && !enq.ready` | the 1-entry TileLink A queue is occupied |
| `waitPut` | `fifoDeq.valid` | a line is ready but no Put can go (the previous Put is not acked yet) |
| `waitGetD` | `readBurstInProgress` | a Get is outstanding, waiting for its data |
| `waitLineDrain` | `readLineCount ≥ 1` | a read line is not yet out of the data FIFO |
| `fifoFull` | `numFifoLinesToEnque > fifoVirtualFreeCount` | the FIFO has no room |
| `other` | anything else | mostly the end of a request: last write ack, completion sync |

**Engine configuration:**
- one source ID per direction: source 0 = Get, 2 = Put
- every Get / Put is one 32 B beat
- at most one read and one write outstanding

### 2.3 Caches and scratchpad

**`llc_access`** (event, `llc_cache`)
- **Where:** `uncore.cache.mshrs.mshrs_N` · `llc/…/Mshr.scala:371`.
- **Fires when:** `state === s_lookup_result && !lookedUp`, i.e. once per request, at its **first** lookup. Every
  LLC request goes through an MSHR, hit or miss. The lookup after a refill is suppressed. Corrupt or invalid
  requests never fire.
- **Fields:**
  - `hit`: for a Get, a tag match with the line valid or all words dirty; for a Put, a tag match alone (writes
    allocate without fetching, per-word dirty bits)
  - `isGet`
  - `fromRpq`: this lookup is a request that waited in the MSHR's replay queue behind one to the same line
  - `tagMatch`
  - `dirtyVictim = !hit && !tagMatch && isDirty`
  - `fetchModify = !hit && isDirty && !valid`: the MSHR first Gets the partial line and merges it
  - `way`
  - `source` (the `LLC_in` source ID)
  - `addr`

**`llc_refill`** (pulse, `llc_cache`)
- **Where:** MSHR · `Mshr.scala:388`.
- **Fires when:** the MSHR's refill Get wins the arbiters (`io.sourceA.fire`), in the same cycle as the `LLC_out`
  Get. It covers misses and fetch-modify. CMU refills are not counted.

**`llc_writeback`** (pulse, `llc_cache`)
- **Where:** MSHR · `Mshr.scala:389`.
- **Fires when:** a dirty victim is handed to the writeback unit (`io.wbReq.fire`). The `LLC_out` PutFullData
  follows ≥ 2 cycles later. CMU writebacks are not counted.

**`llc_cmu`** (fire) / **`llc_cmu_done`** (pulse), `llc_cache`
- **Where:** `uncore.cache` · `llc/…/DistributedLlc.scala:207-208`.
- **What:** a cache-maintenance command is accepted (opcode 0 FLUSH line, 1 CLEAN line, 2 FLUSH_ALL, 3 CLEAN_ALL,
  4/5 FLUSH / CLEAN range), and its one-cycle response.
- **Caveats:**
  - `LLC_in.a` is held off for the whole command.
  - `LLC_out` traffic in that window is maintenance (CMU Get source 1, writeback source 0), not misses.

**`l1d_access`** (event, `l1d_cache`)
- **Where:** `core[_N].rockettile.dcache` · `L1Cache/…/NBDCache.scala:931`.
- **Fires when:** `s2_valid_masked && s2_is_cacheable_req && !s2IsOrchCmd && (read or write)`, i.e. once per
  **accepted** cacheable load / store.
- **Excluded:** nacked attempts (the retry counts once accepted), MSHR replays, scratchpad, uncached, AMO / LR / SC,
  maintenance commands.
- **Fields:**
  - `hit = s2_hit_effective`. This includes store-to-load forwarding, and a full-word store to a tag-matched
    invalid line.
  - `isWrite`
  - `secondary`: a miss merged into an outstanding MSHR for the same line
  - `addr`

**`l1d_cmu_wb`** (pulse, `l1d_cache`)
- **Where:** `NBDCache.scala:943`.
- **Fires when:** one dirty line written back by an L1-D maintenance command (CMU `wb_req`). Eviction writebacks
  are not counted.

**`spm_access`** (event, `l1d_spm`)
- **Where:** `NBDCache.scala:949`.
- **Fires when:** an accepted CE load / store to the scratchpad, including the Orchestrator's continuation-frame
  stores through RoCC.
- **Excluded:** DMA beats (`M_SPMTLREQ`).
- **Fields:** `isWrite`, `addr`.

**`SPM_in`** (`Trace.tl`, `l1d_spm`)
- **Where:** `NBDCache.scala:956`.
- **What:** the scratchpad's TileLink manager port. The CR-DMA memory engine is its only client.
  - Every request is one 32 B beat: LLC2SPM arrives as PutFullData, SPM2LLC as Get.
  - CE n's window is `0x3000_0000 + n × 0x2000`, mask 0x1fff.

**`spm_ce_blocked`** (pulse, `l1d_spm`)
- **Where:** `NBDCache.scala:957`.
- **Fires when:** `io.cpu.req.valid && !io.cpu.req.ready && s1_tlSpmInProgress`: a CE L1-D request (**any**
  address) is refused because a DMA beat is in progress.
- **Caveat:** it counts refused-request cycles. The core replays rather than holding valid, so this is a **lower
  bound** on the stall.

**`spm_dma_waits`** (pulse, `l1d_spm`)
- **Where:** `NBDCache.scala:958`.
- **Fires when:** `tl_spmIn.a.valid && !a.ready && cpuWins`: a DMA beat waits because a CE request had priority.
  It is a subset of the `SPM_in.a` stall cycles.

**`spm_s3_overlap`** (event, `l1d_spm_check`, on by default)
- **Where:** `NBDCache.scala:962`.
- **Fires when:** a CE load that hits (cached line or scratchpad) while `s1_tlSpmWrInProgress` (a DMA write burst
  into this CE's scratchpad).
- **Why it matters:** `s3_valid` includes `s1_tlSpmWrInProgress`, so the load can take the store path and return
  the DMA's data. It should never fire.
- **Fields:** `addr`, `cmd`.

### 2.4 TileLink ports in the uncore (`cr/src/main/scala/Uncore.scala`)

| Port | Line | Link | Clients |
|---|---|---|---|
| `CR_DMA_mbus` | 149 | `crDma.mbusMaster` → `crDmaXbar` → each CE's scratchpad (`SPM_in`), or the ARU → LLC / remote CR | `[CR_DMA] MBus Client` (source 0 Get, 2 Put), one 32 B beat each |
| `LLC_in` | 153 | `llcXbar` → LLC. Carries everything on the memory bus: CE I-caches and L1-Ds, Orchestrator caches, runtime frame manager, CR-DMA via the ARU, and remote-CR requests | the `clients=` list on the probe's DECL line maps each `source` to its requester |
| `LLC_out` | 154 | LLC → `memoryNode` → device memory (off-chip in the 1-CR sim) | `Distributes LLC`: MSHR refills / writebacks (source = MSHR id), CMU (Get 1, Put 0) |

---

## 3. Run card

The header lines of `summary.md`.

| Line | Summary | Example |
|---|---|---|
| **kernel** | Kernel names from `launch.py` (`LAUNCHES[i].entry`, else the ELF stem). | `start_dmaMatmul` |
| **configuration** | CEs in the trace (one `CeState` probe each) × CRs, LLC and L1-D sizes from `parameters.md` next to the generated RTL, and the clock (§1). | 4 CEs × 1 CR, LLC 512 KB, L1-D 8 KB, 100 MHz |
| **result** | Co in cycles and µs, γe, ρ (§5). | 552,166 cycles = 5,521.7 µs; γe = 0.237; ρ = 2.45 |
| **compute / DMA overlap** | Over all CEs: Σ overlapped ÷ Σ DMA in flight; the share of prefetch iterations whose data arrived in time; DMA demand ÷ capacity ([compute_dma_overlap.md](compute_dma_overlap.md) §4). | 66 % hidden; 1 % ready; 1.46 |
| **findings** | Number of findings per severity (§4). | 1 correctness, 3 major, 1 minor, 2 info |
| **inputs** | Which optional inputs were found: ELF (symbols), `launch.py` (T_ops, D_i/D_o, buffer names), PC-marker headers (region names), `rtl_sim_results/buffers.txt` (buffer addresses). A missing one is named, with what it costs. | all four |
| **trace** | Log file name · number of probes declared in the trace (`DECL` lines) · last cycle of the trace. | `mm_phase.log` · 135 probes · 565,885 cycles |

**Detail.**
- *File paths and time base:* the full trace path, the time base and the address-regions file are in the HTML
  under "Trace details", not in the Markdown.
- *Overlap aggregate:* the "66 %" is Σ overlapped ÷ Σ in flight over the four CEs:
  1,024,029 ÷ 1,546,870 = 0.662.
- *"ready":* 6 of 504 iterations, i.e. 1.2 %.

---

## 4. Findings (what to look at first)

**Summary.** A ranked list built from the other tables by fixed rules ([`redefine_findings.py`](../redefine_findings.py)).
- *Order:* correctness issues first, then losses of CE time (largest first), then information.
- *Every finding gives:* evidence, a suggestion, the section with the details and the probes it rests on.
- *Two numeric columns* go with each loss:
  - **% of CE-cycles** = loss ÷ (CEs in the trace × Co)
  - **γe if fixed** = γe ÷ (1 − share), the efficiency if *only* this loss were removed (and the others stayed)

**Rules.** Losses are rows of the γe waterfall (§6.1). MIN_PCT = 3 %, MAJOR_PCT = 10 %.

| Rule | Trigger | Severity |
|---|---|---|
| Scratchpad hazard | any `spm_s3_overlap` event in the run. Titled "hardware, not a kernel race" when the overlap check finds none of them in the buffer the DMA was filling | correctness |
| CR-DMA request lost | any `dma_overflow` pulse: a descriptor was written while the next slot was still full | correctness |
| CR-DMA error response | any `dma_error` event (denied / corrupt D beat on the CR-DMA memory port) | correctness |
| Kernel race | a CE load into an LLC→SPM destination, or a store into an SPM→LLC source, while that request is in flight ([compute_dma_overlap.md](compute_dma_overlap.md) §4.7) | correctness |
| T_ops mismatch | declared T_ops differs from the retired FP ops by more than `tol` (10 %) | major |
| Unbalanced markers | a PC_MARK_END without its BEGIN, or regions left open when a HyperOp ends | minor |
| CEs idle | waterfall row "CEs idle / launching" ≥ 3 %. Adds "(serial setup)" when the setup phase is ≥ 10 % of the kernel with < 1.5 CEs active on average | major if ≥ 10 %, else minor |
| CEs suspended | waterfall row ≥ 3 %. The diagnosis comes from the overlap tables: "DMA not overlapped" (compute with DMA underneath < 20 %), "the shared CR-DMA cannot keep up" (demand ÷ capacity > 1), "data arrives late" (ready < 80 %). Without overlap data: "DMA latency-bound" (most common `dma_gate` reason is `waitGetD` and < 1.5 requests in flight) | as above |
| Busy in a region | a non-compute region's busy time ≥ 3 % (LLC flush, DMA calls, frame management, other) | as above |
| Compute below peak | the waterfall's below-peak row ≥ 3 %, with each compute region's flops/cycle as % of peak. Without per-region counters the evidence says so and the action is to add PC markers | as above |
| Double buffering in place | ≥ 50 % of compute has a transfer underneath and no race was found | info |
| First-touch LLC misses | a `GlobalData*` section with ≥ 64 LLC accesses and ≥ 90 % misses | info |
| Low L1-D hit rate | a CE with ≥ 1,000 L1-D accesses and < 90 % hits | minor |
| Scratchpad contention | (CE stalled by DMA + DMA stalled by CE) > 2 % of the window | minor |
| Missing inputs | no T_ops (no γe); no PC markers (no code regions, no overlap) | info |

**Example.** "Compute runs below peak: 23.5 % of the CE-cycles". The loss is 518,770 ÷ 2,208,664 = 23.5 %, so
γe if fixed = 0.2374 ÷ (1 − 0.235) = 0.310.

---

## 5. Efficiency (chapter 2)

### 5.1 Compute efficiency (γe) table

#### γe: compute efficiency
- **Summary.** γe = Ci ÷ Co: the share of the kernel time the CEs would need if every CE retired one FMA every
  cycle. 1.0 is the peak.
- **Detail.** This is the "Compute element utilization efficiency" of *REDEFINE Feature Metrics* (01.07.2026).
  Ci comes from the declared work, not from the trace. γe is therefore only as right as `t_ops` in `launch.py`;
  chapter 7 checks it against the retired FP ops.
- **Example.** 131,072 ÷ 552,166 = **0.2374**.

#### Co: measured cycles, and window from / to
- **Summary.** Co = w1 − w0, the kernel window in cycles (§1).
- **Detail.**
  - The window comes from the `CeState` change probe of every CE (`baseCr.orch.ceIfc_N`): from the earliest
    segment whose state ≠ Idle to the latest end of such a segment.
  - A CE still busy at the end of the trace cuts the window short, with a warning.
  - "window from / to" are w0 and w1, rounded down.
- **Example.** w0 = 13,712.5: CE0 goes PreLaunch for the `start` HyperOp. w1 = 565,878.5: CE0, the last CE, returns
  to Idle after `End`. Co = **552,166**.

#### Ci: ideal cycles
- **Summary.** Ci = T_ops ÷ (2 × N_CE): the cycles the work needs if every CE retires one FMA (2 flops) per cycle.
- **Example.** 1,048,576 ÷ (2 × 4) = **131,072**.

#### T_ops
- **Summary.** The floating-point operations the kernel must do. It is declared in `launch.py`
  (`KernelLaunch(t_ops=…)`) or passed with `t_ops=`. One FMA counts as 2.
- **Example.** 2 · T · P · Q = 2 × 32 × 128 × 128 = **1,048,576**.

#### N_CE
- **Summary.** N_CE = CEs in the trace × FMAs per CE per cycle (`macs_per_ce`, default 1: the Rocket FPU has one
  FMA pipe).
- **Detail.** "CEs in the trace" is the number of `CeState` probe instances. `n_ce=` overrides N_CE.
- **Example.** 4 × 1 = **4**.

#### kernel time (µs)
- **Summary.** t = Co ÷ F, with F = `freq_mhz` (default 100 MHz, the testbench clock).
- **Detail.** F is an input, not measured. The trace time base (10 ns per cycle) agrees with 100 MHz for sb-cr-sim.
- **Example.** 552,166 ÷ 100 = **5,521.66 µs**.

### 5.2 Off-chip data movement (ρ, φ) table

#### D_i / D_o
- **Summary.** Bytes of the kernel's input / output buffers, from the buffer args in `launch.py`. An INOUT buffer
  counts toward both.
- **Example.** D_i = A 16 KB + W 64 KB = **81,920**; D_o = Z 16 KB = **16,384**.

#### off-chip in / out / total (D_D)
- **Summary.** Bytes of the TileLink transactions on the LLC's memory-side port `LLC_out` that *start* inside the
  window:
  - **out** = PutFullData / PutPartialData
  - **in** = every other opcode (Gets; atomics count in both)
  - **total** = in + out
- **Detail.**
  - When no traced port matches the boundary, D_D, ρ and φ are empty (unknown), not 0, with a warning.
  - `LLC_out` (`Trace.tl` in `cr/Uncore.scala:154`) is the boundary between the CR and what stands in for
    off-chip memory. In the 1-CR simulation everything behind the LLC is that memory. Another boundary can be set
    with `boundary=<port glob>`.
  - The traffic includes LLC refills and writebacks *and* the CMU flush at the end of the kernel.
  - Transactions still open at the end of the trace are counted, with a warning.
- **Example.** in **92,928** (W 65,536 + A 16,384 + context memory 5,728 + code 5,088 + …), out **147,744**
  (group-local memory 81,952 + context memory 49,248 + Z 16,384 + …), total **240,672**.

#### ρ: data reuse index
- **Summary.** ρ = D_D ÷ (D_i + D_o). 1 means every byte crossed once. More than 1 means re-fetches or non-data
  traffic. Less than 1 means data that was already on chip.
- **Example.** 240,672 ÷ 98,304 = **2.448**. Most of the excess is written back by the final LLC `FLUSH_ALL`:
  - 81,888 of the 81,952 B of group-local memory: the staged copy of A and W
  - 49,216 of the 49,248 B of context memory written out

  Context memory read in adds another 5,728 B.

#### M_i and φ: memory penalty index
- **Summary.** M_i = LLC bytes × CRs, from `parameters.md` ("CR L2 Cache Configuration: Size is N KB"), or
  `m_i=`. φ = D_D ÷ M_i: how many LLC-fulls of data moved.
- **Example.** M_i = 524,288; φ = 240,672 ÷ 524,288 = **0.459**.

#### before window / after window
- **Summary.** `LLC_out` bytes of the transactions that start before w0 / after w1. They are not in D_D.
- **Example.** **1,248** B before: runtime data (1,216 B) and static metadata (32 B) read during start-up, before
  the first CE leaves Idle. **0** after.

### 5.3 Off-chip traffic by buffer / memory section

- **Summary.** The D_D bytes split by direction and by the kernel memory section of each transaction's address.
  Kernel buffers are named from `launch.py`; the last two rows add up the data sections and the code / runtime
  sections.
- **Detail.**
  - **Sections** come from `kernel_sections.json`, the address map of the generated config (`KernelSectionConst`):
    `GlobalDataLow[rN]`, `GroupLocalMemory`, `ContextMemory`, `KernelText`, …
  - **Buffer names:** a `GlobalData*` section that holds exactly one kernel buffer is named after it. The buffer
    addresses come from `<workload>/rtl_sim_results/buffers.txt`, written by the simulation, matched to the arg
    order in `launch.py`.
  - **Data sections** are GlobalData*, GroupLocalMemory, LocalMemory, StackMemory and ScratchpadMemory. Everything
    else is code / runtime.
  - **% of off-chip** = bytes ÷ D_D.
- **Example.** W (GlobalDataLow[r33]) **in 65,536** = W read exactly once. Group-local memory **out 81,952** = the
  staged A and W written back by the final flush. All data sections together: **180,352 (74.9 %)**.

### 5.4 Who drives the LLC traffic

- **Summary.** Bytes of the TileLink transactions on the LLC's input port `LLC_in` that start inside the window,
  by requester. **read bytes** = every opcode except Puts; **write bytes** = PutFullData / PutPartialData.
- **Detail.**
  - `LLC_in` (`Trace.tl`, `cr/Uncore.scala:153`) is the crossbar's port into the LLC.
  - The requester is the TileLink client that owns the `source` ID: CE I-caches and L1-Ds, CR-DMA, the
    Orchestrator's operand / instance-metadata / static-metadata caches, and the runtime frame manager.
  - These are on-chip bytes. They show *who* causes the LLC traffic behind D_D.
- **Example.**
  - CR-DMA **reads 622,592** = the group-local staging read 540,672 + W 65,536 + A 16,384.
  - CR-DMA **writes 98,304** = the group-local staging written 81,920 + Z 16,384.

---

## 6. Where the CE time went (chapter 3)

### 6.1 Where the CE-cycles went (γe waterfall)

**Summary.** The (CEs in the trace) × Co CE-cycles are split into losses, ending with the ideal Ci × N_CE (useful work). The
rows, top to bottom:
1. CEs idle / launching
2. CEs suspended
3. busy in each non-compute region (largest first)
4. compute below peak
5. ideal

**γe if removed** for a row = ideal ÷ (CEs × Co − this loss − every loss above it). The jump from one row to the
next is what removing that loss is worth.

**Detail.**
- **Idle / launching and suspended:** CE-cycles in `CeState` Idle + PreLaunch + Launch, and in Suspended, inside
  the window, summed over CEs.
- **Busy in a region:** the Busy CE-cycles whose *innermost open* PC-marker region (§7) is that region. Synthetic
  labels apply outside any marker: "busy, outside any marker".
- **Compute regions:** regions that retire at least 0.1 flops per busy cycle, or the `compute=` list.
- **Compute below peak** = busy cycles in the compute regions − ideal, never below 0 (an ideal above the
  measured compute time means T_ops or N_CE is wrong; the γe > 1 warning says so).
- **Ideal** = Ci × N_CE ÷ macs_per_ce = T_ops ÷ (2 × macs_per_ce), which is T_ops ÷ 2 for this build. Without
  T_ops it is the counted flops ÷ (2 × macs_per_ce).
- Rows under 1 % are folded into "busy in N other regions".
- *Without PC markers:* busy time is one row.
- *Without T_ops, and with no flop counters or counters that read 0 flops (a kernel with no FP work):* the ideal
  is unknown. The rows still split the CE time, the possibly-useful
  busy time is one "not split" row, and γe is empty.

**Example.**

| row | CE-cycles | % | γe if removed |
|---|---:|---:|---:|
| CEs idle / launching | 438,496 | 19.9 | 524,288 ÷ (2,208,664 − 438,496) = 0.296 |
| CEs suspended | 508,571 | 23.0 | 0.416 |
| busy in runtime · DMA LLC→SPM call | 100,484 | 4.55 | 0.452 |
| … LLC flush 55,311, DMA SPM→LLC call 22,870, 10 other regions 39,874 | | | |
| compute below peak (MICRO2X8) | 1,043,058 − 524,288 = 518,770 | 23.5 | 1 |
| ideal | 1,048,576 ÷ 2 = 524,288 | 23.7 | – |

The rows add up to 2,208,664 CE-cycles.

### 6.2 CE state per CE

- **Summary.** Per CE, the share of the window in each `CeState`: busy %, suspended %, idle %, and
  launching % (PreLaunch + Launch).
- **Detail.** The percentages are of Co. Uneven busy % across CEs means load imbalance.
- **Example.** CE0: Busy 358,455 ÷ 552,166 = **64.9 %**, Suspended 191,963 → **34.8 %**, Idle 1,527 → **0.28 %**,
  launching 80 + 141 = 221 → **0.04 %**. CE1–CE3 are idle ~26 % because they only start at ~102,300, after CE0's
  setup.

### 6.3 Timeline phases (setup / steady / tail)

- **Summary.** The window is split at two instants:
  - **setup** runs until every CE is active at the same time
  - **steady** runs until the first CE has finished for good
  - **tail** is the rest

  Per phase:
  - **mean active CEs** = non-Idle CE-cycles ÷ phase cycles
  - **flops** = FP ops retired in the phase
  - **local γe** = flops ÷ (2 × macs × N × cycles)
- **Detail.**
  - "Active" means `CeState` ≠ Idle, so PreLaunch and Launch count as active.
  - "Finished for good" is each CE's last non-Idle instant; the tail starts at the earliest of these.
  - Flops come from the `ce_marker` flops counters of each CE, interpolated linearly at the phase edges between
    the two nearest markers.
  - **LLC flush rows** list each cache-maintenance command: `llc_cmu` (start, with its opcode) to the next
    `llc_cmu_done`. % of kernel = its cycles ÷ Co. They overlap the phases.
- **Example.**
  - setup 13,712 → 102,475 (88,763 cycles, 16.1 %, 1.0 CE active, 0 flops: CE0 stages A and W and creates the
    workers).
  - steady → 507,088 (73.3 %, 4 CEs, 1,042,474 flops, local γe = 1,042,474 ÷ (2 × 4 × 404,613) = **0.322**).
  - tail → 565,878 (10.6 %).
  - LLC FLUSH_ALL 510,477 → 565,750 (55,273 cycles, 10 %).

---

## 7. Code regions (chapter 4)

**Summary.**
- *Regions:* a region is the code between `PC_MARK_BEGIN(id)` and `PC_MARK_END(id)`, which are `addi x0, x0, id`
  and `id|1`. The runtime adds `rt.*` regions itself.
- *Where time goes:* every cycle of every CE goes to the CE's *innermost open* region. Outside any region it goes
  to a synthetic label: busy outside any marker / suspended / idle / launching.
- *Per region:* its cycles split by `CeState`, the instructions and flops retired in it, and IPC and flops/cycle
  over its busy cycles.
- *Tables:* the "all CEs" table adds the CEs (CE-cycles); the "per CE" table is per CE.

**Detail.**
- **Markers** come from the `ce_marker` event probe of each core (`baseCr.core[_N].rockettile.core`), which fires
  when a marker instruction retires. It carries `imm` (the id) and the core's `instret` and `flops` counters (§2).
  Traces without the probe fall back to the commit log, aligned per CE (marked "approximate").
- **Region timeline.** The marker and `CeState` events are merged in time order, keeping a stack of open regions:
  - BEGIN pushes the region. END pops it, or closes the matching outer region with a warning.
  - The stack is cleared when the CE goes Idle or a new HyperOp starts (PreLaunch). A resume keeps it.
  - The result is a list of segments, each with a label (the innermost region), a state and the nesting path.
- **cycles / busy / suspended / idle-launch:** each segment's length inside the window goes to its label and
  state. **% of window** = cycles ÷ Co for one CE, ÷ (CEs in the trace × Co) for "all".
- **instructions, flops:** the counter deltas between two consecutive markers of a CE go to the label active at
  the first marker of the pair. Only pairs whose first marker is inside the window count; a pair whose second
  marker falls after the window end is charged pro rata (the counters are taken as linear between markers). The
  32-bit counters are unwrapped first.
  - **Counter sampling:** the counters are sampled *before* the marker's own retirement. A region's instructions
    therefore include its BEGIN marker and stop before its END marker.
  - **Flops:** fused FMA = 2, fadd / fsub / fmul = 1, fdiv / fsqrt and other FP ops = 0 (§2.1).
  - **Wrap:** the counters are 32-bit and wrap modulo 2³².
- **IPC** = instructions ÷ busy cycles. **flops/cycle** = flops ÷ busy cycles. Peak is 2 × macs_per_ce = 2.
- **limited by** checks the region against these rules, in order (thresholds can be set with plugin args):
  1. **waiting:** more than 50 % of the region's cycles are suspended / idle / launching
  2. **blocked in hardware:** IPC < 0.05 while busy (a flush, fence or blocking access)
  3. **stall-limited:** IPC < 0.6
  4. **control code:** no FP work
  5. **instruction-limited:** flops/cycle < 60 % of peak
  6. otherwise **compute-bound**

**Example.** MICRO2X8, all CEs:

| Value | Computation |
|---|---|
| busy | **1,043,058** CE-cycles (512 multiplies, 128 per CE, ~2,040 cycles each) |
| instructions | **920,832** |
| IPC | 920,832 ÷ 1,043,058 = **0.883** |
| flops | **1,048,576** (= T_ops) |
| flops/cycle | 1,048,576 ÷ 1,043,058 = **1.005**, which is 50 % of the peak of 2: below 1.2, so **instruction-limited** |

**`rt.LLC_FLUSH`:** 55,311 busy cycles with 5 instructions. IPC = 9 × 10⁻⁵ < 0.05, so **blocked in hardware**: the
CE waits on the blocking `FLUSH_ALL`.

**`runtime · suspend`:** the CE is Suspended inside `re_suspend()`, so its 508,571 suspended cycles land here and
it is **waiting**.

---

## 8. Data movement (CR-DMA, chapter 5)

The first four tables (compute / DMA overlap per CE, prefetch iterations, can the shared CR-DMA keep up, is the
overlap safe) are documented in [compute_dma_overlap.md](compute_dma_overlap.md). The tables below come from the
CR-DMA probes in `crDMA/nodeImp.scala` (group `CR_DMA`) and the engine's TileLink port.

### 8.1 DMA requests by type and CE

- **Summary.** One row per request type and CE:
  - **requests** = `dma_start` events
  - **bytes** = rows × rowBytes
  - **rows per request** (mean)
  - **slot wait** = `dma_start` − the latest `dma_queued` before it
  - **service** = the next `dma_done` − `dma_start` (p50, p95, max)
  - **B/cycle while serving** = Σ bytes ÷ Σ service
  - **done → sync** = the next `dma_retire` − `dma_done`
- **Detail.**
  - **When each event fires:**
    - `dma_queued`: a descriptor is written into the engine's one-deep *next* register.
    - `dma_start`: the *next* request moves to *exec*. Its payload is the descriptor: type, src, dst, rows,
      rowBytes, stride, dst geometry, sync address.
    - `dma_done`: the last write of the exec request is acknowledged.
    - `dma_retire`: the completion sync is acknowledged.
  - **Pairing:** each start is paired with the first done at or after it (the engine runs one request at a time).
  - **The CE** is the scratchpad side: the dst of an LLC2SPM or the src of an SPM2LLC, with CE n's scratchpad at
    0x3000_0000 + n × 0x2000 (`spm_base` / `spm_stride`). LLC2LLC has no CE ("–").
  - **rows / rowBytes are the source side of the descriptor.** An SPM2LLC that reads 128 contiguous scratchpad
    bytes and scatters them as 4 × 32 B rows shows 1 row.
  - **Slot wait** is only the last step of a request's wait. Requests of other CEs queue earlier, in the
    Orchestrator, which this probe does not see. The full wait is "wait for engine" in the prefetch table.
  - **Event timing:** `dma_queued` fires one cycle after the doorbell write, and `dma_start` in the first exec
    cycle. So with the engine idle, slot wait is 1 cycle.
  - **Errors:** a denied or corrupt response also ends the transfer with a `dma_done` (§2.2). Such a request's
    service time is up to the error.
- **Example.** LLC2SPM for CE1:
  - **requests:** 130 = 126 prefetches (32 rows × 32 B = 1 KB each) + 2 A blocks (4 rows × 512 B) + 2 first W
    chunks
  - **bytes:** 135,168
  - **rows per request:** (2 × 4 + 128 × 32) ÷ 130 = 31.6
  - **slot wait p50:** 566
  - **service p50 / p95 / max:** 712 / 718 / 1,421
  - **B/cycle while serving:** 1.44
  - **done → sync p50:** 4

### 8.2 DMA engine throughput

- **Summary.** Per phase (whole / setup / steady / tail), from the engine's memory-side TileLink master port
  `CR_DMA_mbus`. Each request becomes one Get per row chunk (read) and one Put per chunk (write into a scratchpad
  or the LLC). The columns:
  - **row Gets / Puts**
  - **bytes** = Σ 2^size
  - **bytes/cycle**, **read bytes/cycle**
  - **avg in flight** = Σ (min(end, phase end) − start) ÷ phase cycles
  - **active %** = cycles with ≥ 1 transaction open
  - **Get → Get gap** p50 and mean
  - **latency** p50 / p95 = total (start → last response beat + 1) of the completed transactions
- **Detail.**
  - Transactions are counted when their *start* is in [a, b).
  - The "latency" here is the transaction's **total** time, including request back-pressure. It is not the
    last-A-beat-to-first-D-beat latency of §1.
  - "avg in flight" near 1 with a Get → Get gap close to the latency means the engine keeps about one read
    outstanding: it is latency-bound.
- **Example.** Whole window:
  - 19,968 Gets + 19,968 Puts of 32 B = **1,277,952 B**
  - ÷ 552,166 = **2.31 B/cycle**; reads 638,976 ÷ 552,166 = **1.16**
  - avg in flight **1.05**, active **79.1 %**, Get → Get gap **22**, latency p50 **18** / p95 **30**
  - In the steady phase the engine reads 1.37 B/cycle.

### 8.3 DMA engine busy per phase

- **Summary.** **engine busy %** = |∪ [dma_start, dma_done)| ÷ phase cycles, over the requests that start in the
  phase. **requests** = those requests.
- **Example.** Whole window: 650 requests, **86.6 %**. Steady: 645 requests, **96.4 %**: the engine is almost
  never without a request while all four CEs run.

### 8.4 Why the DMA engine waits

- **Summary.** Cycles in each value of the `dma_gate` state probe (`crDMA/nodeImp.scala:1262`, group
  `CR_DMA_detail`, off by default) over the **whole trace**, and their share. Each value is the reason the engine
  issued no TileLink request that cycle, in the RTL's priority order (§2).
- **Detail.** This is the only CR-DMA table over the whole trace rather than the kernel window. Without
  `+rtltrace_CR_DMA_detail_on` the table is absent.
- **Example.**
  - waiting for read data from the LLC: **385,961 cycles (68.2 %)**
  - no request: 14.7 %
  - issuing: 7.1 %
  - read line not yet out of the FIFO: 4.5 %
  - TileLink request queue full: 3.4 %
  - other: 2.1 %

  The engine keeps one 32 B Get in flight and waits ~18 cycles for each: it is latency-bound (§8.2).

### 8.5 DMA traffic by buffer / memory section

- **Summary.** `CR_DMA_mbus` transactions that start inside the window, by opcode and by the kernel section of
  their address: **row requests** and **bytes**.
- **Example.**
  - **Group-local memory read 540,672 B:** each CE reads its A rows and every W chunk from the slab.
  - **W read 65,536 B:** the staging copy, exactly once.
  - **Each CE scratchpad written 135,168 B.**

### 8.6 DMA requests: source → destination

- **Summary.** Requests (`dma_start`) grouped by type and by the kernel sections of their src and dst addresses:
  **requests** and **bytes**.
- **Example.** LLC2LLC A → group-local 1 × 16,384 and W → group-local 1 × 65,536 (setup); LLC2SPM group-local →
  CEn scratchpad 130 each; SPM2LLC CEn scratchpad → Z 32 × 128 B each.

---

## 9. Memory hierarchy (chapter 6)

### 9.1 LLC hit/miss by buffer / memory section, and by requester

- **Summary.** The `llc_access` events inside the window, grouped by the kernel section of their `addr`, or by
  the requester that owns their `source`:
  - **accesses** = events
  - **hits / misses** = the `hit` field
  - **hit %** = hits ÷ accesses
- **Detail.**
  - `llc_access` fires in an LLC MSHR (`baseCr.uncore.cache.mshrs.mshrs_N`, `llc/Mshr.scala:371`) once per
    request, at its **first** lookup.
  - A miss is looked up again after its refill; that second lookup, which always hits, is not counted.
  - A request that waited in an MSHR's replay queue behind a miss to the same line is marked `fromRpq` and
    counted once, at its own first lookup.
  - The requester is the `LLC_in` client that owns the `source` ID.
- **Example.**
  - **Group-local memory:** 19,458 accesses, 16,895 hits = **86.8 %**. The 2,563 misses are the 2,561 staging *writes*
    (PutFullData of A and W into the slab; each line misses once and is allocated) and 2 reads: 16,895 of the
    16,897 CE-side reads hit.
  - **W, A, Z:** 2,048 / 512 / 512 accesses, **0 %**: each line is touched once.
  - **CR-DMA requester:** 22,528 accesses, 75.0 %.

### 9.2 LLC summary

- **Summary.** Totals over the window:
  - **accesses, hits, misses, hit %** (as above)
  - **read misses / write misses**: misses with `isGet` = 1 / 0
  - **merged**: accesses with `fromRpq` = 1
  - **dirty victim**: misses with `dirtyVictim` = 1
  - **refills / writebacks**: `llc_refill` / `llc_writeback` events (the MSHR issues a line fetch / a dirty-line
    write to memory)
  - **flush/clean ops**: `llc_cmu` commands that start in the window
- **Detail.** In the hardware appendix three consistency checks run on every report, and a warning is raised
  when one fails:
  - **expected refills** = read misses + write misses whose tag matched + fetch-modify misses with another tag;
    it must equal the refills
  - the `LLC_out` Gets outside CMU periods must equal the refills
  - the writebacks must equal the dirty victims
- **Example.**
  - accesses 26,869, hits 19,493, misses 7,376 → **72.55 %**
  - read misses 2,893, write misses 4,483, merged 66, dirty victims 4
  - refills **2,894** = expected 2,894, and = 2,904 `LLC_out` Gets − 10 inside the flush
  - writebacks 4 = dirty victims; 1 flush command

### 9.3 L1-D hit/miss per CE, and by buffer / memory section

- **Summary.** The `l1d_access` events of each CE's L1-D inside the window:
  - **accesses, hits, misses, hit %**
  - **read / write misses**: the `isWrite` field
  - **secondary misses**: misses to a line already being fetched
  - **L1-D refills at LLC**: Gets on `LLC_in` from client `Core n DCache`
  - **flush writebacks**: `l1d_cmu_wb` events
  - **MPKI** = 1000 × misses ÷ instructions the CE retired in the window
- **Detail.**
  - `l1d_access` (`L1Cache/NBDCache.scala:931`) fires once per *accepted cacheable* CE load / store. Scratchpad,
    uncached and Orchestrator maintenance accesses are excluded (§2).
  - **Instructions for MPKI** come from the CE's `ce_marker` instret counter, linear between the two markers
    around each window edge. A decoded commit log (spike-dasm output, `… inst=[..] fmadd.s …`), when present, is used for the whole-run
    window. Raw `DASM(…)` commit lines, as in `mm_phase.log`, are not decoded and are not used.
- **Example.** CE0: 156 accesses, 149 hits = **95.5 %**, 7 misses (5 read, 2 write, 2 secondary), 3 refills at
  the LLC, 1 flush writeback. MPKI = 1000 × 7 ÷ 239,802 = **0.029**. The compute loop keeps everything in
  registers and the scratchpad, so the L1-D only sees the stack.

### 9.4 Scratchpad: CE and DMA sharing it

- **Summary.** Per CE, from group `l1d_spm` (`+rtltrace_l1d_spm_on`, off by default):
  - **CE loads / stores**: `spm_access` events, split by `isWrite`
  - **DMA bytes in / out**: Put / Get bytes on the scratchpad's TileLink port `SPM_in`
  - **DMA beat latency p50**: median `SPM_in` latency, last A beat → first D beat
  - **CE stalled by DMA**: cycles with `spm_ce_blocked`
  - **DMA stalled by CE**: cycles with `spm_dma_waits`
  - **CE reads during DMA write**: `spm_s3_overlap` events
- **Detail.**
  - **Counting:** `spm_ce_blocked` and `spm_dma_waits` are pulse probes. They print one event per cycle the
    condition holds, so the count *is* cycles.
  - **"CE stalled by DMA"** counts the cycles in which a CE L1-D request (any address) was *refused* because a DMA
    beat was in progress. The core replays the instruction rather than holding the request, so this is a **lower
    bound** on the CE's stall.
  - **"DMA stalled by CE"** counts the cycles a DMA beat waited because a CE request had priority.
  - **"CE reads during DMA write"** also covers a load that hits a *cached* line while a DMA write into this CE's
    scratchpad is in progress (§2.3).
- **Example.** CE1:
  - 68,613 loads and 4,234 stores
  - 135,168 B in (the LLC2SPM data) and 4,096 B out (32 Z tiles of 128 B)
  - beat latency 3
  - CE stalled 109 cycles, DMA stalled 1,436 cycles
  - **141** reads during a DMA write; the hazard is analysed in [compute_dma_overlap.md](compute_dma_overlap.md) §4.7

---

## 10. Checks and tool warnings (chapter 7)

### 10.1 Declared vs retired FP work (T_ops check)

- **Summary.** Compares T_ops with the FP work the CEs actually retired.
  - **difference** = (retired − declared) ÷ declared, warning above `tol` = 10 %
  - **retired instructions**
  - **FMAs / other FP ops / integer multiplies**, only with a commit log
- **Detail.**
  - **With decoded commit lines** in the log (spike-dasm output, `… inst=[..] fmadd.s …`; raw `DASM(…)` lines
    do not count, which is why dmaMatmul uses the counters), each `fmadd`-family instruction counts 2 and each
    `fadd/fsub/fmul/fdiv/fsqrt` counts 1, over the whole log.
  - **Without them** the source is the `ce_marker` counters: Σ over CEs of the `flops` and `instret` values at
    each CE's *last* marker. That covers everything retired from reset up to the last marker, boot code included.
  - **The two sources differ for fdiv / fsqrt:** the commit log counts them as 1, the hardware counter as 0 (§2.1).
    A kernel heavy in divides or square roots shows a difference with the probe source.
- **Example.** 4 × 262,144 = **1,048,576** retired = declared, **+0.0 %**. **959,123** instructions.

### 10.2 Tool warnings

Each plugin lists what it could not measure or found inconsistent. Each warning names the probe or input
involved. In dmaMatmul the only warning is the scratchpad hazard (§4).

---

## 11. Appendix tables

### 11.1 Glossary

Every column name in the report, with its meaning and the JSON key or probe. It is generated from
`redefine_vocab.GLOSSARY`, which also feeds the per-table legends.

### 11.2 Probe groups in this trace

- **Summary.** One row per probe group:
  - **in this run:** a group is on when at least one of its probes printed a `DECL` line. DECLs print only when
    the group is enabled.
  - **default**, **what it gives** and **if it is off**, from `redefine_vocab.PROBE_GROUPS`
  - **plusarg to toggle**
  - **probes**: the number of distinct probe names
- **Example.** `l1d_spm` is **on** in this run but off by default: the run used `+rtltrace_l1d_spm_on`.

---

## 12. Values the summary does not show

These are in the HTML and the CSVs only:
- **The hardware-detail appendix** (generic rtltrace analyses): module input- vs output-side stall, ready/valid
  interface stalls, hangs, longest handshake waits, TileLink transactions by port / opcode / client, slowest
  transactions, traffic by address region.
- **The full LLC counters per window**, including the whole run.
- **The RTL name map**: every probe → RTL instance, probe name, kind, group and Chisel source line.

---

## 13. How the examples were verified

A script that does not import rtltrace or the plugins recomputed the example values from the raw probe events in
`mm_phase.perfetto_full.json` (`redefine_report.py --perfetto-full`) and compared them with the report's JSON. It
covered:
- the window, Co, Ci, γe and kernel time
- D_D in / out / before / after, ρ and φ, every by-section row and every requester row
- busy / suspended cycles per CE and the phase boundaries
- busy / instructions / flops / IPC of MICRO2X8, the DMA call, suspend and flush regions
- the waterfall's idle, suspended and below-peak rows
- the T_ops check
- the DMA request statistics and engine throughput (Gets, Puts, bytes, in flight, active, gap, latency), the
  `dma_gate` cycles
- the LLC totals and per-requester counts
- the L1-D counts and MPKI
- the scratchpad counts and latency

All ~80 values agreed exactly. The overlap metrics are cross-checked by [`overlap_xcheck.py`](../overlap_xcheck.py)
([compute_dma_overlap.md](compute_dma_overlap.md) §6).

---

## 14. Assumptions and decisions to review

These are the choices behind the numbers. They are reasonable defaults, but a reviewer may want them changed.

| # | Assumption / decision | Why | Effect, and how to change it |
|---|---|---|---|
| 1 | **Kernel window** = first CE leaves Idle → last CE back to Idle | the only kernel boundary visible in hardware | includes CE0's setup and the final LLC flush (both real kernel time). `--arg from=/to=` replace it; `--from/--to` only clip the trace (§1) |
| 2 | **N_CE** = CEs in the trace × 1 FMA per cycle; **peak** = 2 flops / CE / cycle | the Rocket FPU has one FMA pipe | γe and "below peak" assume it. `macs_per_ce=` / `n_ce=` |
| 3 | **Clock** 100 MHz for µs | the testbench clock; not measured | only the µs column. `freq_mhz=` |
| 4 | **Off-chip** = the `LLC_out` port | everything behind the LLC stands in for memory in the 1-CR sim | D_D includes the writebacks of the final flush that start inside the window. `boundary=` |
| 5 | **PutPartialData** counts at full size | the mask is not traced | D_D can be over-counted; a warning gives the count |
| 6 | **LLC counts** are per request at its first lookup; "merged" counts every replay-queue lookup, hit or miss | matches the MSHR's `llc_access` semantics | hit % is per request, not per tag lookup |
| 7 | **"CE stalled by DMA"** is a count of refused requests | the core replays instead of holding its request | a lower bound on the CE's stall |
| 8 | **T_ops check** uses the `ce_marker` counters when the log has no commit lines | the counters are always there | counts boot code; fdiv / fsqrt = 0 (commit log: 1) |
| 9 | **DMA request pairing:** `dma_start` → the next `dma_done` | the engine runs one request at a time | an errored request ends at its error; CR2P requests have no `dma_done` (not used by these kernels) |
| 10 | **DMA requester** = the scratchpad side's CE (`0x3000_0000 + n × 0x2000`) | the descriptor carries no CE id | LLC2LLC shows "–" (the overlap tables match it to the caller by markers). `spm_base=`, `spm_stride=` |
| 11 | **"Why the DMA engine waits"** covers the whole trace | the gate is a property of the engine, not of the kernel | includes the idle time before / after the kernel |
| 12 | **Percentiles** use `xs[min(n−1, ⌊q·n⌋)]` | a value that actually occurred | p50 of an even count is the upper median |
| 13 | **Region attribution:** time and counters go to the innermost open region; counter deltas go to the label active at the earlier marker | regions nest | parent regions show only their own (exclusive) time |
| 14 | **Thresholds:** compute region ≥ 0.1 flops / busy cycle; "limited by" 50 % waiting, IPC 0.05 / 0.6, 60 % of peak; findings ≥ 3 % (major ≥ 10 %), ready < 80 %, overlap < 20 % | engineering judgement, tuned on dmaMatmul and Fib | they change labels and rankings, never the measured numbers. `compute=<regions>`, and `wait=`, `idle_ipc=`, `stall_ipc=`, `compute_frac=` (ce_phases); findings constants in `redefine_findings.py` |
| 15 | **Phase split:** PreLaunch / Launch count as *active* for setup / steady / tail, but as *idle / launching* in the waterfall | a launching CE has started its work, but is not executing | the two tables classify those ~0.03 % of cycles differently |
| 16 | **Example verification** used a scratch script, not added to the repository | dmaMatmul-specific (region and CE names) | can be generalised into a tool like `overlap_xcheck.py` on request |
| 17 | **Several declared kernels in one window** (no idle gap to split them) are measured as one kernel named `a+b`, with the T_ops and D_i / D_o of all of them | before, only the first kernel's T_ops / D_i / D_o were used | T_ops is empty if any of them lacks it. Buffer *names* in the by-section tables still come from the first kernel only |
| 18 | **Same kernel launched twice:** the rows are named `mm`, `mm#2` | the evidence of the two launches was mixed | — |
| 19 | **T_ops check** is skipped when only some kernels declare T_ops | the retired count covers all kernels | a warning says so |
| 20 | **`compute=`** takes region names. In redefine_metrics and overlap_metrics a number there is ignored, and names that match no region give a warning (overlap_metrics then falls back to the regions that retire flops). ce_phases still reads a lone number as its `compute_frac=` threshold, the old spelling | `compute=0.6` was read as a region list and blamed every region | use `compute_frac=` for the threshold |
| 21 | **`dma_overflow` / `dma_error`** are correctness findings | a lost descriptor or an error response means the kernel's data is wrong | none fired in dmaMatmul or Fib |
