"""Findings: what a kernel developer should look at first, ranked, with evidence and a suggestion.

`findings(metrics, phases, dma, cache, srcs)` is a pure function over the plugins' summaries (the dicts they publish
in-process or write as <stem>.metrics.json / .phases.json / .dma.json / .cache.json). Rules, in report order:

  correctness  hazards the probes caught (scratchpad read during DMA write), T_ops mismatch, unbalanced PC markers
  performance  every γe-waterfall loss of at least MIN_PCT % of the CE-cycles, explained with the evidence from the
               CE-phase, CR-DMA and cache summaries (idle CEs / serial setup, suspended CEs / DMA-bound, LLC flush on
               the critical path, DMA call overhead, compute regions below peak)
  memory       LLC first-touch misses on kernel buffers, poor L1-D hit rate, scratchpad contention
  inputs       what the report cannot show for this run and how to get it (no T_ops: no γe; no PC markers: no
               code regions or compute / DMA overlap)

Each finding: severity, title, evidence, action, impact (% of CE-cycles, and the γe reached if only this loss were
removed: γe / (1 - share)), the report section to read next, and the RTL probes it is based on.
"""

from __future__ import annotations

import redefine_vocab as V
from redefine_common import ce_key, ce_label

CRIT, MAJOR, MINOR, INFO = "🔴 correctness", "🟠 major", "🟡 minor", "ℹ info"
MIN_PCT = 3.0  # waterfall losses below this share of CE-cycles are not reported
MAJOR_PCT = 10.0


def _f(x, nd=3) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:.{nd}g}"
    return f"{x:,}" if isinstance(x, int) and abs(x) >= 10000 else str(x)


def _ref(srcs: dict, *probes: str) -> str:
    return "; ".join(f"{p} ({srcs[p]})" if srcs.get(p) else p for p in probes)


def _phase_rows(phases: dict | None, kernel: str, ce: str = "all") -> list[dict]:
    return [r for r in (phases or {}).get("rows", []) if r["kernel"] == kernel and r["ce"] == ce]


def _dma_window(dma: dict | None, kernel: str, name: str) -> dict | None:
    return next((w for w in (dma or {}).get("windows", []) if w["kernel"] == kernel and w["window"] == name), None)


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def _overlap(overlap: dict | None, kernel: str) -> dict | None:
    """Kernel-level aggregate of the dma-overlap summary (all CEs)."""
    rows = [r for r in (overlap or {}).get("ces", []) if r["kernel"] == kernel]
    its = [x for x in (overlap or {}).get("iterations", []) if x["kernel"] == kernel]
    if not rows:
        return None
    s = lambda key: sum(r[key] for r in rows)  # noqa: E731
    eng = next((e for e in (overlap or {}).get("engine", []) if e["kernel"] == kernel), {})
    return {"hidden": s("overlap") / s("inflight") if s("inflight") else None,
            "covered": s("overlap") / s("compute") if s("compute") else None,
            "iterations": len(its), "ready": sum(x["ready"] for x in its) / len(its) if its else None,
            "compute": _median(x["compute"] for x in its), "latency": _median(x["latency"] for x in its),
            "engine_wait": _median(x["engine_wait"] for x in its), "service": _median(x["service"] for x in its),
            "exposed": _median(x["exposed_wait"] for x in its), "ahead": _median(x["ahead"] for x in its),
            "ratio": eng.get("demand_ratio"), "demand": eng.get("demand"), "capacity": eng.get("capacity")}


def findings(metrics: dict | None, phases: dict | None = None, dma: dict | None = None, cache: dict | None = None,
             srcs: dict | None = None, overlap: dict | None = None) -> list[dict]:
    srcs = srcs or {}
    out: list[dict] = []

    def add(sev, title, evidence, action, see, rtl, pct=None, gamma=None, kernel=None):
        out.append({"severity": sev, "title": title, "evidence": evidence, "action": action, "see": see,
                    "rtl": rtl, "pct": pct, "gamma_if_fixed": gamma, "kernel": kernel})

    # ------------------------------------------------------------------ correctness
    s3 = (overlap or {}).get("s3") or {}
    for iss in (cache or {}).get("issues", []):
        if iss.get("id") == "spm_s3_overlap":
            per = ", ".join(f"{c} ×{n}" for c, n in iss.get("per_ce", {}).items())
            regs = ", ".join(f"{V.region_name(r)} ×{n}" for r, n in list(iss.get("regions", {}).items())[:3])
            hw = s3 and not s3.get("same_buffer") and s3.get("other_buffer")
            ev = (f"{per}. First at cycle {_f(iss.get('first_cycle'))} on {iss.get('first_ce')} at "
                  f"{iss.get('first_addr')}." + (f" Code regions: {regs}." if regs else ""))
            if s3:
                ev += (f" {s3.get('same_buffer', 0)} of them read the buffer the DMA was filling (kernel race), "
                       f"{s3.get('other_buffer', 0)} read another buffer.")
            add(CRIT, f"Scratchpad hazard: {iss['count']} CE loads overlapped a DMA write into the same scratchpad"
                + (" (hardware, not a kernel race)" if hw else ""), ev,
                ("The kernel's buffers are race-free: the loads read other buffers while the DMA filled the next "
                 "one. The hazard is the hardware's: a load during any DMA write into that scratchpad may return "
                 "the DMA's data. Check the output against a reference and report it to the hardware team with the "
                 "RTL reference. Avoiding it in software means no scratchpad loads during DMA writes, which gives "
                 "up the compute / DMA overlap." if hw else
                 "Such a load can return the DMA's data instead of the old value. Check the kernel output against "
                 "a reference. If it is wrong, report it to the hardware team with the RTL reference; as a "
                 "workaround avoid CE scratchpad loads while a DMA writes into that scratchpad."),
                "Scratchpad: CE and DMA sharing it",
                f"{iss.get('rtl_path')} · {iss.get('probe')}" + (f" · {iss['src']}" if iss.get("src") else ""))
    for iss in (dma or {}).get("issues", []):
        if iss["id"] == "dma_overflow":
            add(CRIT, f"CR-DMA request lost: {iss['count']} descriptor(s) overwritten before they ran",
                f"First at cycle {_f(iss['first_cycle'])}. A request was written while the CR-DMA's next slot "
                "still held a queued one, so that one never ran: its buffer was not moved.",
                "Wait for the previous request to be promoted (or completed) before issuing the next one: the "
                "CR-DMA queues one request behind the running one.", "Tool warnings · CR-DMA",
                f"{iss['rtl_path']} · {iss['probe']}" + (f" · {iss['src']}" if iss.get("src") else ""))
        elif iss["id"] == "dma_error":
            flags = " and ".join(x for x in ("denied", "corrupt") if iss.get(x)) or "error"
            add(CRIT, f"CR-DMA got {iss['count']} TileLink error response(s)",
                f"First at cycle {_f(iss['first_cycle'])}: {flags} on source {iss.get('source')}. The data of "
                "that transfer is not valid.",
                "Check the transfer's addresses and size (a request outside any memory region is denied); "
                "if they are valid, report it to the hardware team with the RTL reference.",
                "Tool warnings · CR-DMA",
                f"{iss['rtl_path']} · {iss['probe']}" + (f" · {iss['src']}" if iss.get("src") else ""))
    for x in (overlap or {}).get("safety", []):
        if x["loads_in_flight_dst"] or x["stores_in_flight_src"]:
            f0 = x.get("first") or {}
            add(CRIT, f"Kernel race on {x['ce']}: scratchpad buffer used while its DMA is in flight",
                f"{x['loads_in_flight_dst']} loads from a buffer still being filled, {x['stores_in_flight_src']} "
                f"stores into a buffer still being sent; first at cycle {f0.get('cycle')} ({f0.get('type')}, "
                f"{f0.get('addr')}).",
                "Wait for the DMA (its completion / re_suspend) before touching that buffer, or use another buffer "
                "(double buffering).", "Is the overlap safe? (scratchpad buffers in flight)",
                _ref(srcs, "spm_access", "dma_start", "dma_done"), kernel=x["kernel"])
    for w in (metrics or {}).get("warnings", []):
        if "declared T_ops" in w:
            add(MAJOR, "γe may be wrong: declared T_ops does not match the FP work the CEs retired", w,
                "Fix t_ops in launch.py (FMA = 2 ops) so the efficiency numbers are meaningful.",
                "Declared vs retired FP work (T_ops check)", _ref(srcs, "ce_marker"))
    for w in (phases or {}).get("warnings", []):
        if w.startswith("PC markers:"):
            add(MINOR, "Unbalanced PC markers", w,
                "Every PC_MARK_BEGIN(id) needs a PC_MARK_END(id) on every path (early returns, loop exits), with "
                "inner regions closed first. Region times are unreliable until fixed.",
                "Code regions · all CEs", _ref(srcs, "ce_marker"))

    # ------------------------------------------------------------------ performance (γe waterfall)
    for k in (metrics or {}).get("kernels", []):
        kn, co = k["kernel"], k.get("C_o") or 1
        rows = _phase_rows(phases, kn)
        by_region = {r["region"]: r for r in rows}
        n_ce = len(k.get("ce_time", {})) or 1
        for w in k.get("waterfall", []):
            step, pct = w["step"], w.get("pct") or 0
            if step.startswith("ideal") or pct < MIN_PCT:
                continue
            ge = k.get("gamma_e")
            g = ge / (1 - pct / 100) if ge is not None and pct < 100 else None  # only this loss removed
            sev = MAJOR if pct >= MAJOR_PCT else MINOR
            gtxt = f"Removing only this loss: γe {_f(ge)} → {_f(g)}" if g else ""
            if step.startswith("CEs idle"):
                idle = {}
                keys = {inst: ce_key(inst) for inst in k.get("ce_time", {})}
                multi = len({kk[0] for kk in keys.values() if kk}) > 1
                for inst, st in k.get("ce_time", {}).items():
                    idle[ce_label(keys[inst], multi) if keys[inst] else inst] = 100 * sum(
                        st.get(s, 0) for s in ("Idle", "PreLaunch", "Launch")) / co
                setup = next((s for s in k.get("sub_windows", []) if s["name"] == "setup"), None)
                ev = "Idle per CE: " + ", ".join(f"{c} {v:.1f} %" for c, v in sorted(idle.items())) + "."
                serial = setup and setup["pct"] >= MAJOR_PCT and setup["mean_active_ces"] < 1.5
                if setup:
                    ev += (f" Setup phase (until all CEs run) = {setup['pct']:.1f} % of the kernel with "
                           f"{setup['mean_active_ces']:.1f} CE active on average.")
                act = ("Setup runs on one CE while the others wait: split the staging copies / initialisation "
                       "across the CEs, or start the other CEs' independent work earlier." if serial else
                       "CEs wait for work: check the work split across CEs and launch overhead.")
                add(sev, f"CEs are idle {pct:.1f} % of the CE-cycles" + (" (serial setup)" if serial else ""),
                    ev + (f" {gtxt}." if gtxt else ""), act, "CE state per CE (% of the kernel window)",
                    _ref(srcs, "CeState"), pct, g, kn)
            elif step.startswith("CEs suspended"):
                susp = sorted((r for r in rows if r.get("suspended")), key=lambda r: -r["suspended"])
                ev = []
                if susp:
                    ev.append("Suspended in " + ", ".join(f"{V.region_name(r['region'])} ({_f(r['suspended'])} "
                                                         "CE-cycles)" for r in susp[:2]) + ".")
                sw = _dma_window(dma, kn, "steady") or _dma_window(dma, kn, "whole")
                gate = (dma or {}).get("gate") or {}
                busy_gate = {r: c for r, c in gate.items() if r != "idle"}
                top = max(busy_gate, key=busy_gate.get) if busy_gate else None
                share = 100 * busy_gate[top] / sum(gate.values()) if top else None
                reqs = [r for r in (dma or {}).get("requests", []) if r.get("type") == "LLC2SPM"]
                qw, svc = _median(r.get("queue_wait") for r in reqs), _median(r.get("service") for r in reqs)
                if sw:
                    ev.append(f"CR-DMA in the {sw['window']} phase: engine busy {_f(100 * (sw.get('engine_busy') or 0))} "
                              f"%, {_f(sw.get('bytes_per_cycle'))} B/cycle, {_f(sw.get('avg_in_flight'))} requests "
                              f"in flight, Get latency p50 {_f(sw.get('latency_p50'))} cycles.")
                if top:
                    ev.append(f"Most common wait: {V.DMA_GATE.get(top, top)} ({share:.0f} % of cycles).")
                if qw is not None and svc is not None and not (overlap or {}).get("iterations"):
                    ev.append(f"LLC→SPM requests: slot wait p50 {_f(qw)} vs service p50 {_f(svc)} cycles.")
                latency_bound = top == "waitGetD" and sw and (sw.get("avg_in_flight") or 0) < 1.5
                ov = _overlap(overlap, kn)
                diag = ""
                if ov and ov.get("iterations"):
                    ev.insert(0, f"Prefetch overlap: a transfer runs under {100 * (ov['covered'] or 0):.0f} % of "
                                 f"the compute and compute hides {100 * (ov['hidden'] or 0):.0f} % of the DMA time, "
                                 f"but the data arrives before the compute ends in only "
                                 f"{100 * (ov['ready'] or 0):.0f} % of {ov['iterations']} iterations: issue → data "
                                 f"arrived p50 {_f(ov['latency'])} cycles (waiting for the engine {_f(ov['engine_wait'])}"
                                 f" behind ~{_f(ov['ahead'])} other CEs' requests, then {_f(ov['service'])} transfer) vs "
                                 f"compute p50 {_f(ov['compute'])}, so each iteration waits ~{_f(ov['exposed'])} "
                                 "cycles." + (f" The CEs need {_f(ov['demand'])} B/cycle; the engine delivers "
                                              f"{_f(ov['capacity'])} ({_f(ov['ratio'])}×)." if ov["ratio"] else ""))
                    if (ov["covered"] or 0) < 0.2:
                        diag, act = " — DMA not overlapped with compute", (
                            "Transfers and compute take turns. Double-buffer: issue the DMA for the next chunk "
                            "before computing on the current one, and wait only after the compute.")
                    elif ov["ratio"] and ov["ratio"] > 1:
                        diag, act = " — the shared CR-DMA cannot keep up", (
                            f"The overlap is in place, but the CEs need {_f(ov['ratio'])}× the bytes/cycle the "
                            "shared engine delivers, so no prefetch depth can hide the transfers. Move fewer bytes "
                            "per flop: reuse each transferred chunk for more work (a larger tile, e.g. more rows of "
                            "A per W chunk), keep reused data in the scratchpad, or fewer CEs streaming at once.")
                    elif (ov["ready"] or 0) < 0.8:
                        diag, act = " — data arrives late", (
                            "The engine has spare capacity but the data arrives after the compute: issue the "
                            "prefetch earlier (more buffers in flight) or make each chunk's compute longer.")
                    else:
                        act = None
                    if act:
                        add(sev, f"CEs are suspended (waiting) {pct:.1f} % of the CE-cycles" + diag,
                            " ".join(ev) + (f" {gtxt}." if gtxt else ""), act, "Prefetch iterations (double buffering)",
                            _ref(srcs, "CeState", "ce_marker", "dma_start", "dma_done", "dma_gate"), pct, g, kn)
                        continue
                if latency_bound:
                    act = ("The CR-DMA is latency-bound: it keeps about one read in flight and waits for LLC data "
                           "most of the time, so CEs wait for their DMA. Move more data per request (more / longer "
                           "rows), prefetch further ahead (more buffers in flight), or give each CE more compute "
                           "per DMA chunk so the transfer is hidden.")
                elif sw and (sw.get("engine_busy") or 0) > 0.8:
                    act = ("The CR-DMA is busy almost all the time: the kernel is DMA-bandwidth-bound. Reduce bytes "
                           "moved (reuse data already in the scratchpad) or overlap more compute per byte.")
                else:
                    act = ("CEs wait for wake-ups (DMA completion, sync). Check which waits are on the critical path "
                           "in the Perfetto kernel view (CE code regions + CR-DMA requests).")
                add(sev, f"CEs are suspended (waiting) {pct:.1f} % of the CE-cycles" +
                    (" — DMA latency-bound" if latency_bound else ""),
                    " ".join(ev) + (f" {gtxt}." if gtxt else ""), act, "Why the DMA engine waits",
                    _ref(srcs, "CeState", "dma_start", "dma_done", "dma_gate"), pct, g, kn)
            elif step.startswith("busy in "):
                lab = step[8:]
                r = by_region.get(lab, {})
                name = V.region_name(lab)
                lim = V.LIMITED_BY.get(r.get("limited_by"), "–")
                ev = f"{_f(w['ce_cycles'])} CE-cycles, IPC {_f(r.get('ipc'))}, limited by: {lim}."
                if lab == "rt.LLC_FLUSH":
                    cmu = [c for c in k.get("cmu", [])]
                    if cmu:
                        ev += " " + ", ".join(f"LLC {c['op']} {_f(c['cycles'])} cycles ({100 * c['cycles'] / co:.1f}"
                                              " % of the kernel)" for c in cmu) + "."
                    act = ("A full LLC flush runs on the critical path at the end of the kernel. Clean / flush only "
                           "the output buffer range if the runtime allows, or start it earlier.")
                    ref = _ref(srcs, "ce_marker", "llc_cmu", "llc_cmu_done")
                elif lab.startswith("rt.DMA_"):
                    act = ("Issuing DMA requests costs CE time (the CE stalls while it programs the engine). Issue "
                           "fewer, larger requests.")
                    ref = _ref(srcs, "ce_marker", "AccelConfig")
                elif lab.startswith("rt.FRAME_"):
                    act = "Runtime frame management on the hot path: allocate / bind frames once, outside loops."
                    ref = _ref(srcs, "ce_marker", "FrmMngr.Alloc")
                else:
                    act = {"stall": "Reduce stalls in this region: memory latency, FP dependencies.",
                           "blocked": "The CE is busy but retires nothing: a fence, flush or blocking access.",
                           "control": "Control / data-movement code: move it out of the hot loop or overlap it."
                           }.get(r.get("limited_by"), "Look at this region in the Perfetto kernel view.")
                    ref = _ref(srcs, "ce_marker")
                add(sev, f"{pct:.1f} % of the CE-cycles are spent in {name}", ev + (f" {gtxt}." if gtxt else ""),
                    act, "Code regions · all CEs", ref, pct, g, kn)
            elif step.startswith("compute below peak"):
                labs = step[step.index("(") + 1:-1].split(", ")
                parts, acts = [], set()
                for lab in labs:
                    r = by_region.get(lab)
                    if not r or r.get("flops_per_cycle") is None:
                        continue
                    peak = 2.0 * ((metrics or {}).get("config", {}).get("macs_per_ce") or 1)
                    parts.append(f"{V.region_name(lab)}: {_f(r['flops_per_cycle'])} flops/cycle = "
                                 f"{100 * r['flops_per_cycle'] / peak:.0f} % of peak, IPC {_f(r.get('ipc'))} "
                                 f"({V.LIMITED_BY.get(r.get('limited_by'), '–')})")
                    acts.add(r.get("limited_by"))
                mx = next((w.get(x) for x in ("mix",) if w.get(x)), None)
                cap = (f"Even with no stalls the issue slot would cap this at {100 * mx['fma'] / mx['instret']:.0f} % "
                       f"of peak ({_f(mx['fma'])} FMAs among {_f(mx['instret'])} retired instructions). "
                       if mx and mx.get("instret") else "")
                act = (cap + "Raise FMAs per load: a larger register tile if the registers allow (an m×n tile loads "
                       "(m+n) values per m·n FMAs) and unroll the loop so the pointer updates and the branch are "
                       "shared by more FMAs. Separately, the cycles with nothing retired are stalls (load-use "
                       "latency, FP latency, scratchpad contention): schedule loads earlier and check the "
                       "scratchpad section."
                       if "instruction" in acts and cap else
                       "Raise FMA density in the compute loop: unroll further, keep more accumulators in "
                       "registers, and cut loads / address arithmetic per FMA (a larger register tile)."
                       if "instruction" in acts else
                       "Reduce stalls in the compute loop: more independent accumulators (FP latency), fewer "
                       "dependent loads." if "stall" in acts else
                       "Compare IPC and flops/cycle of the compute regions in Code regions · all CEs." if parts else
                       "Add PC markers around the compute loop (ce_marker probe, group ce_phase, on) to see which "
                       "region runs below peak and why.")
                mtxt = ""
                if mx and mx.get("busy"):
                    b = mx["busy"]
                    mtxt = (f" Of the {_f(b)} busy compute-region cycles, {100 * mx['fma'] / b:.0f} % retire FMAs, "
                            f"{100 * mx['load'] / b:.0f} % loads, {100 * mx['store'] / b:.1f} % stores, "
                            f"{100 * (mx['other'] + mx['fp_other']) / b:.0f} % other instructions (loop control, "
                            f"address math, fmv) and {100 * mx['stall'] / b:.0f} % retire nothing (stalls).")
                ev = ("; ".join(parts) + ".") if parts else (
                    "No per-region flop counters (no PC markers, or the ce_marker probe was off): this is the busy "
                    "time beyond the ideal Ci × N_CE, not split by region.")
                add(sev, f"Compute runs below peak: {pct:.1f} % of the CE-cycles lost inside compute regions",
                    ev + mtxt + (f" {gtxt}." if gtxt else ""), act, "Code regions · all CEs",
                    _ref(srcs, "ce_marker"), pct, g, kn)

    # ------------------------------------------------------------------ overlap health (what is working)
    for k in (metrics or {}).get("kernels", []):
        ov = _overlap(overlap, k["kernel"])
        safe = [x for x in (overlap or {}).get("safety", []) if x["kernel"] == k["kernel"]]
        if ov and ov.get("iterations") and (ov["covered"] or 0) >= 0.5 and not any(
                x["loads_in_flight_dst"] or x["stores_in_flight_src"] for x in safe):
            add(INFO, "Double buffering is in place" + (" and race-free" if safe else ""),
                f"{ov['iterations']} prefetch iterations; a transfer runs under {100 * ov['covered']:.0f} % of the "
                "compute." + (f" No load read a buffer still being filled and no store hit a buffer still being sent "
                              f"({sum(x['spm_accesses'] for x in safe):,} scratchpad accesses checked)." if safe
                              else " (Turn on +rtltrace_l1d_spm_on to also check the buffers by address.)"),
                "Nothing to fix in the structure; see the suspended-time finding for whether the transfers are "
                "fully hidden.", "Compute / DMA overlap per CE", _ref(srcs, "ce_marker", "dma_start", "spm_access"),
                kernel=k["kernel"])

    # ------------------------------------------------------------------ memory
    for win in (cache or {}).get("windows", []):
        if win["window"] == "run":
            continue
        cold = [s for s in win.get("llc_by_section", []) if s["section"].startswith("GlobalData")
                and s["accesses"] >= 64 and (s.get("miss_ratio") or 0) >= 0.9]
        if cold:
            add(INFO, "Kernel buffers miss in the LLC on first touch",
                ", ".join(f"{s.get('name', s['section'])}: {_f(s['accesses'])} accesses, "
                          f"{100 * (s['miss_ratio'] or 0):.0f} % miss" for s in cold) + ".",
                "Expected when data is streamed once from memory. Only a problem if the kernel reads a buffer more "
                "than once; then keep it on chip (group-local memory or scratchpad).",
                "LLC hit/miss by buffer / memory section", _ref(srcs, "llc_access"), kernel=win["window"])
        for s in win.get("l1d", []):
            if s["accesses"] >= 1000 and s.get("hit_ratio") is not None and s["hit_ratio"] < 0.9:
                add(MINOR, f"Low L1-D hit rate on CE{s.get('core')}",
                    f"{100 * s['hit_ratio']:.1f} % hits over {_f(s['accesses'])} accesses.",
                    "Keep the working set in the scratchpad or improve locality.", "L1-D hit/miss per CE",
                    _ref(srcs, "l1d_access"), kernel=win["window"])
        span = (win.get("to") or 0) - (win.get("from") or 0)
        for s in win.get("spm", []):
            c = (s.get("ce_blocked_cycles") or 0) + (s.get("dma_wait_cycles") or 0)
            if span and c / span > 0.02:
                add(MINOR, f"CE{s.get('core')} and the CR-DMA contend for the scratchpad",
                    f"{_f(c)} stall cycles ({100 * c / span:.1f} % of the kernel).",
                    "Avoid CE scratchpad accesses while DMA bursts target the same scratchpad.",
                    "Scratchpad: CE and DMA sharing it", _ref(srcs, "spm_ce_blocked", "spm_dma_waits"),
                    kernel=win["window"])

    # ------------------------------------------------------------------ inputs the report could not use
    for k in (metrics or {}).get("kernels", []):
        if k.get("gamma_e") is None and not k.get("T_ops"):
            add(INFO, "γe is not computed: the kernel declares no T_ops",
                "Without the kernel's operation count the useful work is unknown: the report shows where the CE "
                "time went, but not the compute efficiency or how far compute is below peak.",
                "Declare the operation count in launch.py: KernelLaunch(..., t_ops=<FP ops>) (one FMA = 2 ops).",
                "Compute efficiency (γe)", "launch.py", kernel=k["kernel"])
        if phases is not None and not phases.get("source"):
            add(INFO, "No PC markers: CE time is not split by code region",
                "The trace has no ce_marker events (and the log no marker instructions): busy time is one block, "
                "and compute / DMA overlap cannot be measured.",
                "Put PC_MARK_BEGIN(id) / PC_MARK_END(id) (pc_markers.h) around the kernel's phases: loads, "
                "compute loop, stores. Runtime calls (DMA, suspend, flush) are marked automatically.",
                "Code regions · all CEs", _ref(srcs, "ce_marker"), kernel=k["kernel"])

    rank = {CRIT: 0, MAJOR: 1, MINOR: 2, INFO: 3}
    out.sort(key=lambda f: (rank[f["severity"]], -(f["pct"] or 0)))
    return out
