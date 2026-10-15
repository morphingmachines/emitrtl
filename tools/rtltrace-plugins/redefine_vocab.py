"""Vocabulary of the REDEFINE reports: one place for every friendly name, legend text and glossary entry.

The plugins name RTL things (instance paths, TileLink clients and ports, probes, kernel sections, PC-marker regions,
DMA gate reasons) through this module, so the HTML, CSV, Markdown and Perfetto views use the same words. The RTL
identifier is never dropped: callers wrap names in rtltrace.plugin.RtlName, and the report's RTL name map lists every
probe with its instance path and Chisel source line.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------------------------- report chapters
# (group title, base order). Sections inside a chapter use base + 0..99.

G_FIND = ("1 · Findings", 100)
G_EFF = ("2 · Efficiency", 200)
G_TIME = ("3 · Where the CE time went", 300)
G_REGION = ("4 · Code regions (PC_MARK)", 400)
G_DMA = ("5 · Data movement and compute overlap (CR-DMA)", 500)
G_MEM = ("6 · Memory hierarchy (LLC, L1-D, scratchpad)", 600)
G_CHECK = ("7 · Checks and tool warnings", 700)
G_GLOSS = ("Appendix · Glossary", 900)
G_MAP = ("Appendix · RTL name map", 950)

# ---------------------------------------------------------------------------------------------- RTL modules
# (regex on the instance path, name template). {n} = CE index from the first group (0 when the group is empty).
# Several instances may share a name on purpose (all LLC MSHRs -> one "LLC MSHRs" process in Perfetto).

MODULES = [
    (r"\.orch\.ceIfc_(\d+)$", "CE{n} ↔ Orchestrator interface"),
    (r"\.core(?:_(\d+))?\.rockettile\.core$", "CE{n} core"),
    (r"\.core(?:_(\d+))?\.rockettile\.dcache$", "CE{n} L1-D / scratchpad"),
    (r"\.core(?:_(\d+))?\.rockettile\.frontend.*$", "CE{n} front end (I-cache)"),
    (r"\.core(?:_(\d+))?\.rockettile$", "CE{n} tile"),
    (r"\.uncore\.cache\.mshrs\.mshrs_\d+$", "LLC MSHRs (miss handlers)"),
    (r"\.uncore\.cache$", "LLC"),
    (r"\.uncore\.crDma\.mbusMaster$", "CR-DMA engine (memory side)"),
    (r"\.uncore\.crDma\.regNode$", "CR-DMA request queue"),
    (r"\.uncore\.crDma$", "CR-DMA"),
    (r"\.uncore$", "Uncore (LLC and CR-DMA ports)"),
    (r"\.orch\.launcher\.builtInEventExe$", "Orchestrator · built-in event executor"),
    (r"\.orch\.launcher$", "Orchestrator · HyperOp launcher"),
    (r"\.orch\.rdStg\.hyperOpFrameManager$", "Orchestrator · frame manager"),
    (r"\.orch\.rdStg$", "Orchestrator · context-memory read stage"),
    (r"\.orch\.exeStg$", "Orchestrator · command execute stage"),
    (r"\.orch\.controlIfcAdapter\.excptUnit$", "Orchestrator · exception unit"),
    (r"\.orch$", "Orchestrator"),
    (r"(^|\.)baseCr$", "CR top level (CR-DMA ↔ Orchestrator / NoC ports)"),
]

# ---------------------------------------------------------------------------------------------- TileLink
# Client names as declared in the Chisel TLMasterParameters (see the RTL name map for where each is defined).

CLIENTS = [
    (r"^Core (\d+) DCache$", "CE{n} L1-D"),  # L1Cache/CeHellaCache.scala: s"Core ${tileId} DCache", tile i = CE i
    (r"^Core (\d+) ICache$", "CE{n} I-cache"),
    (r"^\[CR_DMA\] MBus Client$", "CR-DMA (data)"),  # crDMA/nodeDef.scala
    (r"^\[CR_DMA\] Orch Client$", "CR-DMA (completion sync)"),
    (r"^OperandCacheMasterNode$", "Orchestrator operand cache (context memory)"),  # cmem/OperandCache.scala
    (r"^OrchIMDCacheMasterNode$", "Orchestrator instance-metadata cache"),  # cmem/InstanceMetaDataCache.scala
    (r"^SMDCache$", "Orchestrator static-metadata cache"),  # cmem/SMDCache.scala
    (r"^RuntimeSystemMemory$", "Runtime frame manager"),  # orchestrator/HyperOpFrameManager, EWMU/EventFrameManager
]

PORTS = {
    "LLC_in": "into the LLC",
    "LLC_out": "LLC → memory",
    "CR_DMA_mbus": "CR-DMA ↔ LLC / scratchpads",
    "CR_DMA_orch": "CR-DMA → Orchestrator",
    "CR_DMA_ni": "CR-DMA ↔ NoC",
    "SPM_in": "into the scratchpad (DMA)",
}

# ---------------------------------------------------------------------------------------------- probes
# probe name -> (friendly name, one-line meaning)

PROBES = {
    "CeState": ("CE state", "Orchestrator's view of a CE: Idle / PreLaunch / Launch / Busy / Suspended"),
    "ce_marker": ("PC marker", "a PC_MARK (addi x0,x0,id) retired by a CE, with its instruction and flop counters"),
    "Ce2Orch": ("CE → Orchestrator command", "commands a CE sends (FALLOC, SYNC, END, ...)"),
    "Orch2Ce": ("Orchestrator → CE response", "register write-backs to a CE"),
    "Orch2L1D": ("Orchestrator → L1-D command", "scratchpad / cache commands from the Orchestrator"),
    "Launch": ("HyperOp launch", "a HyperOp (or resume) launched on a CE"),
    "HL": ("HyperOp launch selection", "launcher picks a HyperOp instance for a CE"),
    "CeFreeBitMap": ("free CEs", "bitmap of CEs free for a new HyperOp"),
    "OrchCmdExe": ("Orchestrator command executed", ""),
    "SchedQ.Enq": ("ready-queue enqueue", ""),
    "FrmMngr.Alloc": ("frame alloc", "HyperOp frame allocated"),
    "FrmMngr.Free": ("frame free", "HyperOp frame freed"),
    "AccelConfig": ("accelerator config write", "built-in event writes an accelerator (CR-DMA) register"),
    "BuiltIn.Cmd": ("built-in command", ""),
    "CMemReq": ("context-memory request", ""),
    "CMemHLResp": ("context-memory launch response", ""),
    "CMemCmdResp": ("context-memory command response", ""),
    "ExceptionInfo": ("exception", ""),
    "llc_access": ("LLC lookup", "one per request at its first lookup: hit / miss"),
    "llc_refill": ("LLC refill", "line fetched from memory for a miss"),
    "llc_writeback": ("LLC writeback", "dirty victim written to memory"),
    "llc_cmu": ("LLC flush/clean start", ""),
    "llc_cmu_done": ("LLC flush/clean done", ""),
    "l1d_access": ("L1-D access", "cacheable CE load/store: hit / miss"),
    "l1d_cmu_wb": ("L1-D flush writeback", ""),
    "spm_access": ("scratchpad access (CE)", ""),
    "spm_ce_blocked": ("CE stalled by DMA at scratchpad", "CE request held off by a DMA burst in progress"),
    "spm_dma_waits": ("DMA stalled by CE at scratchpad", "DMA beat held off because the CE won arbitration"),
    "spm_s3_overlap": ("scratchpad read during DMA write (hazard)",
                       "a CE load hit the scratchpad while a DMA write was in flight: may return DMA data"),
    "dma_queued": ("DMA request queued", ""),
    "dma_overflow": ("DMA queue overflow", ""),
    "dma_start": ("DMA request started", "type, source, destination, row size, stride, rows"),
    "dma_done": ("DMA request done", "last write acknowledged"),
    "dma_retire": ("DMA request retired", "completion sync acknowledged by the Orchestrator"),
    "dma_error": ("DMA error", "TileLink denied/corrupt response"),
    "dma_gate": ("DMA issue gate", "why the DMA engine issues no request in a cycle"),
}

# probe group -> (default on, what it gives, what is missing when it is off)
PROBE_GROUPS = {
    "hyperop": (True, "CE states, HyperOp launches, frames: kernel window, CE time split",
                "no kernel window: nothing works"),
    "ce_phase": (True, "PC markers with instruction/flop counters: code regions, IPC, flops/cycle",
                 "no code regions (only CE states); IPC/flops need the commit log"),
    "LLC": (True, "TileLink traffic into and out of the LLC: off-chip bytes, who drives traffic",
            "no ρ / φ, no traffic attribution"),
    "CR_DMA": (True, "CR-DMA request lifecycle and its memory port: DMA throughput, queue/service times",
               "no DMA section (inferred from LLC traffic only)"),
    "orch.cmd": (True, "Orchestrator commands and context-memory traffic", "hardware detail only"),
    "error": (True, "exceptions", "exceptions not reported"),
    "l1d_spm_check": (True, "scratchpad read-during-DMA-write hazard check", "hazard not checked"),
    "llc_cache": (False, "LLC hit/miss per lookup, refills, writebacks, flushes",
                  "no LLC hit/miss tables (+rtltrace_llc_cache_on)"),
    "l1d_cache": (False, "L1-D hit/miss per access", "no L1-D hit/miss tables (+rtltrace_l1d_cache_on)"),
    "l1d_spm": (False, "scratchpad accesses and CE/DMA contention", "no scratchpad table (+rtltrace_l1d_spm_on)"),
    "CR_DMA_detail": (False, "why the DMA engine waits, cycle by cycle (dma_gate)",
                      "no 'why the DMA waits' table (+rtltrace_CR_DMA_detail_on)"),
    "ce": (False, "every CE ↔ Orchestrator port handshake", "hardware detail only (+rtltrace_ce_on)"),
}

# ---------------------------------------------------------------------------------------------- kernel sections
# KernelSectionConst names (kernel_sections.json) -> what a kernel developer calls them. {i} = the section's field.

SECTIONS = {
    "ScratchpadMemory": "CE{ce} scratchpad",
    "StackMemory": "CE{ce} stack",
    "GroupLocalMemory": "group-local memory (shared, in LLC)",
    "LocalMemory": "local memory",
    "GlobalDataLow": "global data region {r}",
    "GlobalDataHigh": "global data region {r}",
    "KernelText": "kernel code",
    "ContextMemory": "context memory (Orchestrator)",
    "LocalRuntimeSystemMemory": "runtime data (local)",
    "GroupRuntimeSystemMemory": "runtime data (group)",
    "GlobalRuntimeSystemMemory": "runtime data (global)",
    "StaticMetadata": "static metadata",
    "VolatileMemory": "volatile registers (sync / MMIO)",
}

# ---------------------------------------------------------------------------------------------- PC-marker regions

RUNTIME_REGIONS = {
    "DMA_LLC2SPM": "DMA LLC→SPM call", "DMA_SPM2LLC": "DMA SPM→LLC call", "DMA_LLC2LLC": "DMA LLC→LLC call",
    "DMA_SCATTER": "DMA scatter call", "DMA_GATHER": "DMA gather call", "CHAN_SEND": "channel send",
    "CHAN_RECV": "channel receive", "SUSPEND": "suspend (wait for wake-up)", "FRAME_ALLOC": "frame alloc",
    "FRAME_BIND": "frame bind", "LLC_FLUSH": "LLC flush", "CE_FLUSH": "CE L1-D flush", "END_KERNEL": "end kernel",
}
SYNTHETIC_REGIONS = {
    "untagged": "busy, outside any marker", "suspended": "suspended, outside any marker", "idle": "idle",
    "launch": "launching", "busy": "busy",
}


def region_name(label: str) -> str:
    """rt.DMA_LLC2SPM -> 'runtime · DMA LLC→SPM call'; untagged -> 'busy, outside any marker'; kernel names as is."""
    if label in SYNTHETIC_REGIONS:
        return SYNTHETIC_REGIONS[label]
    if label.startswith("rt."):
        return "runtime · " + RUNTIME_REGIONS.get(label[3:], label[3:])
    return label


# ---------------------------------------------------------------------------------------------- CR-DMA

DMA_TYPES = {1: "LLC2SPM", 2: "SPM2LLC", 3: "LLC2LLC", 4: "GRP_SCATTER", 5: "GRP_GATHER", 6: "CR2CR", 7: "P2CR"}
DMA_GATE = {  # crDMA nodeImp.scala DmaGate, in the RTL's priority order
    "idle": "no request to work on",
    "issue": "issuing a row Get/Put",
    "aQueueFull": "TileLink request queue full (back-pressure from the LLC / scratchpads)",
    "waitPut": "write data ready, Put blocked",
    "waitGetD": "waiting for read data from the LLC",
    "waitLineDrain": "read line not yet out of the data FIFO (FIFO latency, or its write pending)",
    "fifoFull": "data FIFO full",
    "other": "other (incl. the end of a request: last write ack, completion sync)",
}

# ---------------------------------------------------------------------------------------------- PC-region classes

LIMITED_BY = {
    "waiting": "waiting (suspended / idle)",
    "blocked": "blocked in hardware (IPC≈0)",
    "stall": "stall-limited (memory, dependencies)",
    "control": "control / data-movement code (no FP)",
    "instruction": "instruction-limited (few FMAs per cycle)",
    "compute": "compute-bound",
}

# ---------------------------------------------------------------------------------------------- glossary
# term -> (meaning, how to read it, internal name). Section legends pick entries from here.

GLOSSARY = {
    "γe (compute efficiency)": ("Ideal cycles Ci ÷ measured cycles Co.",
                                "1.0 = every CE did one FMA every cycle of the kernel. Higher is better.", "gamma_e"),
    "Ci (ideal cycles)": ("T_ops ÷ (2 × N_CE): cycles if every CE retired one FMA (2 flops) per cycle.", "", "C_i"),
    "Co (measured cycles)": ("Kernel window: first CE leaves Idle → last CE back to Idle.", "", "C_o"),
    "T_ops": ("Floating-point operations the kernel must do (launch.py t_ops). FMA = 2.", "", "T_ops"),
    "N_CE": ("Compute elements × FMAs per CE per cycle.", "", "N_CE"),
    "kernel time (µs)": ("Co ÷ clock frequency.", "", "t_i_us"),
    "D_i / D_o (bytes)": ("Bytes of the input / output buffers declared in launch.py.", "", "D_i, D_o"),
    "off-chip bytes (D_D)": ("Bytes crossing LLC ↔ memory during the kernel; in = into the LLC, out = written back.",
                             "Compare with D_i + D_o.", "D_D, D_D_in, D_D_out"),
    "ρ (data reuse index)": ("off-chip bytes ÷ (D_i + D_o).",
                             "1 = each byte moved once. > 1 = data re-fetched or extra traffic (code, runtime "
                             "state, flushes). < 1 = data already on chip.", "rho"),
    "φ (memory penalty index)": ("off-chip bytes ÷ on-chip memory M_i.", "How many LLC-fulls of data moved.", "phi"),
    "M_i": ("On-chip memory: LLC bytes × CRs.", "", "M_i"),
    "CE-cycles": ("N_CE × Co: one per CE per cycle of the kernel window.", "", ""),
    "γe if removed": ("γe reached if this loss and every loss above it were eliminated.",
                      "The jump from the previous row is what fixing this loss is worth.", "gamma_e_if_removed"),
    "setup / steady / tail": ("setup = until every CE is active; steady = all CEs active; tail = after the first CE "
                              "has finished.", "A long setup or tail means serial work.", "sub_windows"),
    "mean active CEs": ("Average number of CEs not Idle in the phase.", "", "mean_active_ces"),
    "local γe": ("γe inside the phase only (flops ÷ (2 × N_CE × cycles)).", "", "local_gamma_e"),
    "region (PC_MARK)": ("Code between PC_MARK_BEGIN(id) and PC_MARK_END(id). 'runtime · …' regions are added by "
                         "the runtime library; regions nest and time goes to the innermost one.", "", "region"),
    "busy / suspended / idle": ("CE state cycles (Orchestrator CeState). Suspended = waiting for a wake-up "
                                "(DMA completion, sync).", "", "busy, suspended, idle"),
    "instructions": ("Instructions retired by the CE in the region (ce_marker counters).", "", "instret"),
    "IPC": ("Instructions per busy cycle.", "Rocket CE peak ≈ 1.", "ipc"),
    "flops/cycle": ("Floating-point ops per busy cycle: fused multiply-add = 2, fadd / fsub / fmul = 1 (the "
                    "ce_marker flops counter; fdiv / fsqrt are not counted).", "Peak = 2 × FMAs per CE.",
                    "flops_per_cycle"),
    "limited by": ("What bounds the region: waiting (> 50 % suspended/idle); blocked in hardware (IPC < 0.05 while "
                   "busy, e.g. a flush); stall-limited (IPC < 0.6); control code (no FP); instruction-limited "
                   "(flops/cycle < 60 % of peak); compute-bound.", "", "class"),
    "DMA requests": ("dma_start events: one per DMA call (rows × row bytes).", "", "requests"),
    "slot wait": ("dma_queued → dma_start: cycles in the engine's one-deep 'next request' register while the "
                  "previous request runs.", "Only the last step of the wait: requests of other CEs queue earlier, "
                  "in the Orchestrator (see 'wait for engine').", "queue_wait"),
    "service time": ("Cycles from start to last write acknowledged.", "", "service"),
    "B/cycle while serving": ("Request bytes ÷ service cycles.", "", ""),
    "done → sync": ("Cycles from the last write acknowledged (dma_done) to the Orchestrator acknowledging the "
                    "completion sync (dma_retire).", "", "done_to_sync"),
    "row Gets / Puts": ("TileLink requests the engine issues: a Get reads a row chunk, a Put writes it.", "",
                        "gets, puts"),
    "bytes/cycle": ("Bytes moved per cycle of the window (reads + writes).", "", "bytes_per_cycle"),
    "avg in flight": ("Average TileLink requests outstanding.",
                      "< 1.5 with long waits = the engine is latency-bound.", "avg_in_flight"),
    "active %": ("Share of the window with at least one request outstanding.", "", "active"),
    "Get → Get gap": ("Cycles between consecutive read requests (median).", "", "get_interval_p50"),
    "latency p50 / p95": ("Request → response cycles (median / 95th percentile).", "", "latency_p50, latency_p95"),
    "engine busy %": ("Share of the window with a DMA request between start and done.", "", "engine_busy"),
    "DMA in flight (CE)": ("Cycles with at least one of this CE's DMA requests between the CE issuing it (end of "
                           "its runtime DMA call) and the last write acknowledged.", "", "inflight"),
    "overlapped with compute": ("DMA-in-flight cycles during which the CE was inside a compute region.", "",
                                "overlap"),
    "DMA hidden %": ("overlapped ÷ DMA in flight: how much of the data movement compute covers.",
                     "100 % = transfers fully hidden; the rest is exposed.", "hidden"),
    "compute with DMA underneath %": ("overlapped ÷ compute: how much of the compute runs while a transfer is in "
                                      "flight.", "Near 0 % = no prefetch: data is moved, then computed on.",
                                      "covered"),
    "exposed DMA": ("DMA-in-flight cycles not covered by compute (mostly the CE suspended, waiting for it).", "",
                    "exposed"),
    "prefetch iteration": ("A compute region together with the DMA requests the CE issued just before it "
                           "(double buffering: fetch the next chunk, compute on the current one).", "",
                           "iterations"),
    "ready before compute ended %": ("Iterations whose prefetched data had fully arrived when the compute region "
                                     "ended (no wait).", "100 % = perfectly hidden.", "ready"),
    "wait for engine": ("Issue → DMA start: time the request waited for the shared CR-DMA (requests of other CEs "
                        "ahead of it).", "", "engine_wait"),
    "transfer": ("DMA start → last write acknowledged.", "", "service"),
    "exposed wait per iteration": ("Data arrival − compute end, when positive: the CE sits suspended.", "",
                                   "exposed_wait"),
    "wake-up latency": ("Last write acknowledged → CE running again (completion sync + relaunch).", "", "wake"),
    "DMA demand vs capacity": ("Bytes/cycle the CEs need to keep computing (bytes per iteration ÷ compute cycles, "
                               "summed over CEs) ÷ bytes/cycle the engine delivers while transferring.",
                               "> 1: no amount of overlap can hide the transfers; more work per byte or a faster "
                               "engine is needed.", "demand_ratio"),
    "DMA wait reason": ("Per cycle, why the engine issued nothing (dma_gate).", "", "gate"),
    "accesses / hits / misses": ("Cache lookups and their outcome (first lookup of each request).", "", ""),
    "hit %": ("hits ÷ accesses.", "", "hit_ratio"),
    "merged": ("Lookups of requests that waited in an LLC miss handler's replay queue behind a request to the same "
               "line (fromRpq); mostly hits on the line just fetched.", "", "merged"),
    "dirty victim": ("Misses that evicted a modified line (needs a writeback).", "", "dirty_victim"),
    "refills / writebacks": ("Lines fetched from / written to memory by the LLC miss handlers.", "",
                             "refills, writebacks"),
    "secondary misses": ("L1-D misses merged into a miss handler already fetching that line.", "", "secondary"),
    "MPKI": ("Misses per 1000 instructions retired.", "", "mpki"),
    "CE stalled by DMA": ("Cycles a CE L1-D request (any address) was refused because a DMA beat into the "
                          "scratchpad was in progress.", "A lower bound on the CE's stall: the core replays the "
                          "instruction rather than holding its request.", "ce_blocked_cycles"),
    "DMA stalled by CE": ("Cycles a DMA beat into the scratchpad waited because a CE request had priority.", "",
                          "dma_wait_cycles"),
    "CE reads during DMA write": ("Hazard check: a CE load that hits (scratchpad, or a cached line) while a DMA write "
                                  "into this CE's scratchpad is in progress.",
                                  "Must be 0: the load may return the DMA's data.", "s3_overlap"),
    "stall cycles (hardware)": ("Cycles an interface had valid high and ready low.", "", ""),
    "latency vs total (TileLink)": ("latency = last request beat → first response beat; total = request offered "
                                    "(incl. back-pressure) → last response beat.", "", ""),
}


def legend(*terms: str) -> dict[str, str]:
    """{term: meaning + how to read} for a section legend."""
    out = {}
    for t in terms:
        m, how, _ = GLOSSARY[t]
        out[t] = f"{m} {how}".strip()
    return out


# ---------------------------------------------------------------------------------------------- name functions

def _sub(rules, text: str) -> str | None:
    for rx, tpl in rules:
        mt = re.search(rx, text)
        if mt:
            g = mt.group(1) if mt.groups() else None
            return tpl.format(n=int(g) if g and g.isdigit() else 0)
    return None


def module_name(inst: str) -> str | None:
    return _sub(MODULES, inst)


def client_name(client: str) -> str | None:
    return _sub(CLIENTS, client)


def port_name(port: str) -> str | None:
    return PORTS.get(port)


def probe_name(probe: str) -> str | None:
    return PROBES.get(probe, (None,))[0]


_SEC = re.compile(r"^(\w+)((?:\[[a-z_]+\d+\])*)$")


def section_name(label: str | None, buffers: dict[str, str] | None = None) -> str | None:
    """Kernel-section label (GlobalDataLow[r33], ScratchpadMemory[ce1]) -> buffer name from launch.py when the
    section holds exactly one kernel buffer, else a readable section name."""
    if not label:
        return None
    if buffers and label in buffers:
        return buffers[label]
    mt = _SEC.match(label)
    if not mt:
        return None
    tpl = SECTIONS.get(mt.group(1))
    if tpl is None:
        return None
    fields = dict(re.findall(r"\[([a-z_]+?)(\d+)\]", mt.group(2)))
    try:
        return tpl.format(**fields)
    except KeyError:
        return tpl.split("{")[0].strip()


def ce_of_name(text: str) -> str | None:
    """'CE3 L1-D' -> 'CE3' (for grouping)."""
    mt = re.match(r"CE\d+", text or "")
    return mt.group(0) if mt else None
