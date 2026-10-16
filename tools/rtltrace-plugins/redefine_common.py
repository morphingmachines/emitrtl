"""Shared helpers for the REDEFINE rtltrace plugins (redefine_metrics.py, cache_metrics.py).

Workload descriptors (launch.py / launches JSON), RTL config (parameters.md), retired-instruction counts from the
sim log, and kernel windows from the orchestrator's CeState probes.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import importlib.util
import json
import re
import sys
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Kernel:
    name: str
    t_ops: int | None
    op_kind: str
    args: list[tuple[object, int, str]] = field(default_factory=list)  # (shared key or None, size, dir)
    buffers: list[tuple[int, str | None, int, str]] = field(default_factory=list)  # (arg index, name, size, dir)

    @property
    def d_in(self) -> int:
        return sum(size for _, size, d in self.args if d in ("in", "inout"))

    @property
    def d_out(self) -> int:
        return sum(size for _, size, d in self.args if d in ("out", "inout"))


# ---------------------------------------------------------------------------------------------- workload inputs

def _dir(d) -> str:
    return str(getattr(d, "value", d)).lower()


def _kernel_launch_dir(launch: Path) -> Path | None:
    for parent in launch.resolve().parents:
        for cand in (parent / "kernel-launch-descriptor", parent / "mm-baremetal-examples" / "kernel-launch-descriptor"):
            if (cand / "kernel_launch.py").is_file():
                return cand
    return None


def load_workload(path: str) -> list[Kernel]:
    """Kernels of a launch.py (LAUNCHES = [KernelLaunch(...)]) or of a launches JSON written by build.py."""
    p = Path(path)
    if p.suffix == ".json":
        out = []
        for i, k in enumerate(json.loads(p.read_text())["kernels"]):
            args = [(a.get("shared_id"), a["size"], a["dir"]) for a in k["args"]
                    if a["kind"] in ("buffer", "shared_buffer")]
            bufs = [(j, a.get("name"), a["size"], a["dir"]) for j, a in enumerate(k["args"])
                    if a["kind"] in ("buffer", "shared_buffer")]
            out.append(Kernel(k.get("entry") or Path(k["elf"]).stem or f"kernel{i}", k.get("t_ops"),
                              k.get("op_kind", "fp"), args, bufs))
        return out
    kl = _kernel_launch_dir(p)
    if kl is not None:
        sys.path.insert(0, str(kl))
    try:
        spec = importlib.util.spec_from_file_location("rtltrace_redefine_launch", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if kl is not None:
            sys.path.remove(str(kl))
    out = []
    for i, launch in enumerate(mod.LAUNCHES):
        args, bufs = [], []
        for j, a in enumerate(launch.args):
            if hasattr(a, "shared"):
                args.append((id(a.shared), a.shared.size, _dir(a.dir)))
                bufs.append((j, getattr(a, "name", None) or getattr(a.shared, "name", None), a.shared.size,
                             _dir(a.dir)))
            elif hasattr(a, "size") and hasattr(a, "dir"):
                args.append((None, a.size, _dir(a.dir)))
                bufs.append((j, getattr(a, "name", None), a.size, _dir(a.dir)))
        out.append(Kernel(getattr(launch, "entry", None) or Path(launch.elf).stem or f"kernel{i}",
                          getattr(launch, "t_ops", None), getattr(launch, "op_kind", "fp"), args, bufs))
    return out


def parse_params(path: str) -> dict:
    """The parameters.md written next to the generated RTL (sizes in bytes)."""
    s = Path(path).read_text()
    hw = {}
    for key, rx, scale in (("llc_bytes", r"CR L2 Cache Configuration:\s*Size is\s*(\d+)\s*KB", 1024),
                           ("l1d_bytes", r"L1-DCache Configuration:\s*Size is\s*(\d+)\s*KB", 1024),
                           ("n_ces", r"\|\s*nCEs\s*\|\s*(\d+)", 1),
                           ("nX", r"\|\s*nX\s*\|\s*(\d+)", 1),
                           ("nY", r"\|\s*nY\s*\|\s*(\d+)", 1)):
        mt = re.search(rx, s)
        if mt:
            hw[key] = int(mt.group(1)) * scale
    return hw


_COMMIT = re.compile(r"^C(\d+):\s+(\d+)\s+\[1\].*?inst=\[([0-9a-f]+)\]\s+([a-z][\w.]*)")
_FMA = re.compile(r"^fn?m(add|sub)\.[shdq]$")
_FP1 = re.compile(r"^f(add|sub|mul|div|sqrt)\.[shdq]$")
_MUL = re.compile(r"^mul(h|hu|hsu|w)?$")
SAMPLE_EVERY = 1024  # commit-log counter samples per core (IPC / flops timelines)
GAP = 64  # cycles without a commit that count as "the core stopped" (commit-log alignment)


@dataclass
class CoreLog:
    """One CE's commit stream, reduced: totals, PC markers, counter samples and stop points (commit cycles)."""
    retired: int = 0
    fma: int = 0
    fp_other: int = 0
    int_mul: int = 0
    flops: int = 0  # fused = 2, other FP arithmetic = 1 (same convention as T_ops)
    marks: list = field(default_factory=list)  # (cycle, imm, instret before, flops before)
    samples: list = field(default_factory=list)  # (cycle, instret, flops)
    stops: list = field(default_factory=list)  # cycle of the last commit before a gap > GAP
    first: int | None = None
    last: int | None = None


def is_marker(inst: int) -> int:
    """Immediate of a PC marker (`addi x0, x0, imm`, imm != 0), else 0."""
    return inst >> 20 if inst & 0xfffff == 0x13 and inst >> 20 else 0


def commit_log(path: str) -> dict[int, CoreLog] | None:
    """The Rocket commit lines (`C<core>: <cycle> [1] pc=[..] ... inst=[..] <op>`) of a sim log, per core.

    Cached per (path, size, mtime): several plugins read the same (possibly 100+ MB) log in one rtltrace run.
    Callers must not mutate the result.
    """
    st = Path(path).stat()
    return _commit_log(str(path), st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=4)
def _commit_log(path: str, _size: int, _mtime_ns: int) -> dict[int, CoreLog] | None:
    cores: dict[int, CoreLog] = defaultdict(CoreLog)
    prev: dict[int, int] = {}
    with open(path, errors="replace") as f:
        for line in f:
            if line[:1] != "C":
                continue
            mt = _COMMIT.match(line)
            if not mt:
                continue
            core, cyc, op = int(mt.group(1)), int(mt.group(2)), mt.group(4)
            c = cores[core]
            if c.first is None:
                c.first = cyc
            p = prev.get(core)
            if p is not None and cyc - p > GAP:
                c.stops.append(p)
            prev[core] = cyc
            c.last = cyc
            imm = is_marker(int(mt.group(3), 16))
            if imm:
                c.marks.append((cyc, imm, c.retired, c.flops))
            if c.retired % SAMPLE_EVERY == 0:
                c.samples.append((cyc, c.retired, c.flops))
            c.retired += 1
            if _FMA.match(op):
                c.fma += 1
                c.flops += 2
            elif _FP1.match(op):
                c.fp_other += 1
                c.flops += 1
            elif _MUL.match(op):
                c.int_mul += 1
    for core, c in cores.items():
        c.samples.append((c.last, c.retired, c.flops))
    return dict(sorted(cores.items())) or None


# Instruction classes of the commit stream (index = value stored per retired instruction).
MIX_CLASSES = ("other", "fma", "fp_other", "load", "store")
_OTHER, _FMA_I, _FP1_I, _LOAD, _STORE = range(5)
_COMMIT_ANY = re.compile(r"^C(\d+):\s+\d+\s+\[1\].*?inst=\[([0-9a-f]+)\]")


def inst_class(w: int) -> int:
    """Class of one RISC-V instruction word (32-bit, or 16-bit RVC when the low two bits are not 11):
    fused multiply-add (fmadd/fmsub/fnmsub/fnmadd), other FP arithmetic (fadd/fsub/fmul/fdiv/fsqrt), load, store,
    else other (integer, branches, fmv/compare/convert, CSR, the PC-marker nops)."""
    if w & 3 == 3:
        op = w & 0x7f
        if op in (0x03, 0x07):
            return _LOAD
        if op in (0x23, 0x27):
            return _STORE
        if op in (0x43, 0x47, 0x4b, 0x4f):
            return _FMA_I
        if op == 0x53 and (w >> 27) in (0, 1, 2, 3, 0b01011):
            return _FP1_I
        return _OTHER
    quad, f3 = w & 3, (w >> 13) & 7
    if quad in (0, 2):  # c.lw / c.flw (c.ld) / c.fld and c.lwsp ...; c.sw / c.fsw (c.sd) / c.fsd and c.swsp ...
        if f3 in (1, 2, 3):
            return _LOAD
        if f3 in (5, 6, 7):
            return _STORE
    return _OTHER


@dataclass
class MixLog:
    """Instruction classes of the valid commits of one core, in retirement order (index = the CE's instret
    counter), and where its PC-marker nops are."""
    cls: bytearray = field(default_factory=bytearray)
    markers: list = field(default_factory=list)  # (index, imm)


class MixIndex:
    """Prefix sums of MixLog.cls: instructions of each class among the instret range [a, b)."""

    def __init__(self, log: MixLog):
        self.n = len(log.cls)
        self.cum = []
        for k in range(len(MIX_CLASSES)):
            acc, run = array("I", [0]), 0
            for c in log.cls:
                run += c == k
                acc.append(run)
            self.cum.append(acc)

    def delta(self, a: int, b: int) -> dict[str, int]:
        a, b = max(0, min(a, self.n)), max(0, min(b, self.n))
        return {name: self.cum[k][b] - self.cum[k][a] for k, name in enumerate(MIX_CLASSES)}


def commit_mix(path: str) -> dict[int, MixLog] | None:
    """Per core: the class of every valid (`[1]`) commit line of a sim log, from the raw instruction word (the
    DASM(...) text does not need to be decoded). Cached like commit_log."""
    st = Path(path).stat()
    return _commit_mix(str(path), st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=4)
def _commit_mix(path: str, _size: int, _mtime_ns: int) -> dict[int, MixLog] | None:
    cores: dict[int, MixLog] = defaultdict(MixLog)
    with open(path, errors="replace") as f:
        for line in f:
            if line[:1] != "C":
                continue
            mt = _COMMIT_ANY.match(line)
            if not mt:
                continue
            c = cores[int(mt.group(1))]
            w = int(mt.group(2), 16)
            imm = is_marker(w)
            if imm:
                c.markers.append((len(c.cls), imm))
            c.cls.append(inst_class(w))
    return dict(sorted(cores.items())) or None


def count_retired(path: str) -> dict | None:
    """Operations retired by the CEs, from the Rocket commit lines (spike-dasm decoded) in the sim log."""
    cores = commit_log(path)
    if not cores:
        return None
    fma = sum(c.fma for c in cores.values())
    fp1 = sum(c.fp_other for c in cores.values())
    return {"retired": sum(c.retired for c in cores.values()), "fma": fma, "fp_other": fp1, "fp_ops": 2 * fma + fp1,
            "int_mul": sum(c.int_mul for c in cores.values()),
            "fp_ops_per_core": {k: c.flops for k, c in cores.items()},
            "retired_per_core": {k: c.retired for k, c in cores.items()}}


# ---------------------------------------------------------------------------------------------- helpers

def _r(x, nd=4):
    if x is None:
        return None
    if isinstance(x, float):
        return float(f"{x:.{nd}g}")
    return x


def _ratio(a, b):
    return None if a is None or not b else a / b


def _region(t) -> str:
    for name, sets in t.port.managers:
        for base, mask in sets:
            if (t.address & ~mask) == base:
                return f"{name}@0x{base:x}"
    return f"0x{t.address >> 24:02x}xxxxxx"


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


# ---------------------------------------------------------------------------------------------- kernel windows

def ce_timelines(m) -> dict[str, list[tuple[float, float, str]]]:
    """(t0, t1, state name) segments of every CE's CeState probe, keyed by CE instance path."""
    ces = defaultdict(list)
    for s in m.states("CeState"):
        v = s.event.vals.get("value", 0)
        ces[s.probe.inst].append((s.t0, s.t1, s.probe.enum_name("value", v) or str(v)))
    return dict(sorted(ces.items()))


def kernel_windows(m, ces, n_kernels: int, args: dict, warnings: list[str]) -> list[tuple[float, float]]:
    """One (start, end) cycle window per kernel: first CE leaving Idle -> last CE back to Idle.

    With several kernels the CE activity is split at the n-1 longest all-Idle gaps (kernels are separated by host
    work). `from=`/`to=` in args override this with a single manual window.
    """
    if args.get("from") or args.get("to"):
        return [(float(args.get("from", 0)), float(args.get("to", m.end)))]
    busy = sorted((t0, t1) for segs in ces.values() for t0, t1, st in segs if st != "Idle")
    iv: list[list[float]] = []
    for t0, t1 in busy:
        if iv and t0 <= iv[-1][1]:
            iv[-1][1] = max(iv[-1][1], t1)
        else:
            iv.append([t0, t1])
    if not iv:
        return []
    n = max(1, n_kernels)
    if n == 1 or len(iv) < n:
        if n > 1:
            warnings.append(f"{n} kernels declared but only {len(iv)} CE activity intervals; "
                            "measuring them as one window")
        return [(iv[0][0], iv[-1][1])]
    cuts = sorted(sorted(range(1, len(iv)), key=lambda i: iv[i][0] - iv[i - 1][1], reverse=True)[:n - 1])
    out, first = [], 0
    for c in cuts + [len(iv)]:
        out.append((iv[first][0], iv[c - 1][1]))
        first = c
    return out


def compute_arg(args: dict) -> set[str]:
    """Region names of compute=<regions>. A number there is the old spelling of ce_phases' compute_frac= and is not
    a region name."""
    out = set()
    for x in filter(None, args.get("compute", "").split(",")):
        try:
            float(x)
        except ValueError:
            out.add(x)
    return out


def window_names(names: list[str], n_windows: int) -> list[str]:
    """Kernel name per measured window, the same in every plugin (the JSON summaries are joined by it):
    several declared kernels measured as one window -> "a+b"; missing names -> kernel<i>; a kernel launched twice ->
    "mm", "mm#2"."""
    if n_windows == 1 and len(names) > 1:
        out = ["+".join(names)]
    else:
        out = (list(names) + [f"kernel{i}" for i in range(len(names), n_windows)])[:n_windows]
    seen, n = Counter(out), Counter()
    for i, x in enumerate(out):
        n[x] += 1
        if seen[x] > 1 and n[x] > 1:
            out[i] = f"{x}#{n[x]}"
    return out


def window_kernels(kernels: list[Kernel], n_windows: int) -> list[Kernel]:
    """window_names() for Kernel records: a merged window gets the T_ops (None if any is missing) and the buffers of
    all its kernels."""
    names = window_names([k.name for k in kernels], n_windows)
    if n_windows == 1 and len(kernels) > 1:
        ks = kernels
        return [Kernel(names[0], sum(k.t_ops for k in ks) if all(k.t_ops is not None for k in ks) else None,
                       ks[0].op_kind if len({k.op_kind for k in ks}) == 1 else "mixed",
                       [a for k in ks for a in k.args], [b for k in ks for b in k.buffers])]
    ks = (list(kernels) + [Kernel("", None, "fp") for _ in range(len(kernels), n_windows)])[:n_windows]
    return [dataclasses.replace(k, name=nm) for k, nm in zip(ks, names)]


# ---------------------------------------------------------------------------------------------- CE phases (PC markers)
#
# A PC marker is `addi x0, x0, imm` (a nop). BEGIN carries an even id, END id|1; regions nest. Markers come from the
# RTL `ce_marker` probe (@RT time, with instret/flops counters) or, for traces without it, from the commit lines of
# the sim log, aligned to @RT time per CE (see align_offset). Phases are keyed per CE as (CR path prefix, CE index).

_DEFINE = re.compile(r"^\s*#\s*define\s+(\w+)\s+\(?\s*(0x[0-9a-fA-F]+|\d+)\s*\)?\s*(?:/[/*].*)?$")
IDLE_STATES = ("Idle", "PreLaunch", "Launch")


@dataclass
class Mark:
    t: float  # @RT cycles
    imm: int
    instret: int | None = None  # retired before the marker
    flops: int | None = None


@dataclass
class PhaseSeg:
    t0: float
    t1: float
    label: str  # innermost open region, or untagged / suspended / idle / launch outside any region
    state: str  # CeState during the segment
    path: tuple  # open regions, outermost first


def ce_key(inst: str) -> tuple[str, int] | None:
    """(CR prefix, CE index) of an orchestrator CE interface (…orch.ceIfc_N) or a CE tile path (…core[_N]…)."""
    mt = re.match(r"(.*)\.orch\.ceIfc_(\d+)$", inst)
    if mt:
        return mt.group(1), int(mt.group(2))
    mt = re.match(r"(.*?)\.core(?:_(\d+))?(?:\.|$)", inst)
    if mt:
        return mt.group(1), int(mt.group(2) or 0)
    return None


def ce_label(key: tuple[str, int], multi_cr: bool = False) -> str:
    """CE<i>, prefixed with the CR instance (the path component above baseCr) when the trace has several CRs."""
    prefix, i = key
    if not multi_cr:
        return f"CE{i}"
    parts = prefix.split(".")
    if parts[-1] == "baseCr":
        parts = parts[:-1]
    # the nearest component that numbers the CR (…CR_Fabric_3.cr.baseCr -> CR_Fabric_3): the one right above
    # baseCr is often a fixed wrapper name ("cr") shared by every CR
    cr = next((x for x in reversed(parts) if re.search(r"\d", x)), parts[-1] if parts and parts[-1] else prefix)
    return f"{cr}.CE{i}"


def marker_names(paths) -> dict[int, str]:
    """BEGIN id -> name from C headers (`#define PCM_FOO 0x7e0`). RE_PCM_* (runtime) become `rt.FOO`."""
    names: dict[int, str] = {}
    for p in paths:
        if not p or not Path(p).is_file():
            continue
        for line in Path(p).read_text(errors="replace").splitlines():
            mt = _DEFINE.match(line)
            if not mt:
                continue
            name, v = mt.group(1), int(mt.group(2), 0)
            if not 0 < v < 0x800 or v & 1 or name.endswith(("_H", "_ENABLE")):
                continue
            if name.startswith("RE_PCM_"):
                name = "rt." + name[7:]
            elif name.startswith("PCM_"):
                name = name[4:]
            names.setdefault(v, name)
    return names


def find_marker_headers(*anchors) -> list[Path]:
    """pc_markers.h next to the workload (launch.py / ELF) and the SDK runtime's redefine_pc_markers.h."""
    out = []
    for a in anchors:
        if not a:
            continue
        a = Path(a).resolve()
        d = a if a.is_dir() else a.parent
        out += sorted(d.glob("*pc_markers*.h"))
        for parent in d.parents:
            rt = parent / "CwithHyperOps" / "include" / "redefine_pc_markers.h"
            if rt.is_file():
                out.append(rt)
                break
    return list(dict.fromkeys(out))


def probe_marks(m) -> dict[tuple, list[Mark]]:
    out = defaultdict(list)
    for e in m.events("ce_marker"):
        k = ce_key(e.probe.inst)
        if k is not None:
            out[k].append(Mark(e.ts, e.vals.get("imm", 0), e.vals.get("instret"), e.vals.get("flops")))
    for v in out.values():
        v.sort(key=lambda x: x.t)
    return dict(out)


def align_offset(stops: list[int], leave_busy: list[float]) -> tuple[float | None, float]:
    """Offset (@RT cycle - commit cycle) of one CE: the commit stream stops when the CE leaves Busy (suspend, end of
    hyperOp), so the most common difference between commit-stop points and Busy->other edges is the offset.
    Returns (offset, fraction of edges matched within ±2 cycles)."""
    if not stops or not leave_busy:
        return None, 0.0
    h = Counter()
    for s in stops:
        for e in leave_busy:
            h[round(e - s)] += 1
    best = max(h, key=lambda d: sum(h[d + j] for j in range(-2, 3)))
    near = sum(1 for e in leave_busy if any(abs(e - s - best) <= 2 for s in stops))
    return float(best), near / len(leave_busy)


def log_marks(cores: dict[int, "CoreLog"], timelines: dict, warnings: list[str]) -> tuple[dict, dict]:
    """Markers and counter samples of the commit log, moved onto @RT time. Single-CR traces only."""
    keys = {ce_key(inst): inst for inst in timelines}
    prefixes = {k[0] for k in keys if k}
    if len(prefixes) != 1:
        warnings.append(f"commit-log markers need a single-CR trace ({len(prefixes)} CRs): CE phases not built")
        return {}, {}
    prefix = prefixes.pop()
    marks, samples = {}, {}
    for core, c in cores.items():
        k = (prefix, core)
        if k not in keys:
            continue
        leave = [t1 for t0, t1, st in timelines[keys[k]] if st == "Busy"]
        off, q = align_offset(c.stops, leave)
        if off is None:
            if c.marks:
                warnings.append(f"{ce_label(k)}: cannot align the commit log to the trace (no suspends/ends)")
            continue
        if q < 0.5 and c.marks:
            warnings.append(f"{ce_label(k)}: commit-log alignment is weak ({100 * q:.0f}% of Busy ends matched)")
        if c.marks:
            marks[k] = [Mark(cyc + off, imm, ir, fl) for cyc, imm, ir, fl in c.marks]
        samples[k] = [(cyc + off, ir, fl) for cyc, ir, fl in c.samples]
    if marks:
        warnings.append("CE phases come from the commit log (approximate timing, ±a few cycles); the RTL "
                        "ce_marker probe gives exact times")
    return marks, samples


def build_phases(timeline: list[tuple[float, float, str]], marks: list[Mark], names: dict[int, str],
                 problems: Counter) -> list[PhaseSeg]:
    """Segments of one CE: CeState edges and markers in time order, with a stack of open regions.

    The stack is cleared when the CE goes Idle or a new hyperOp starts (PreLaunch). A resume keeps it, because a
    resumable hyperOp continues where it stopped: Suspended -> Launch -> Busy, or Busy -> Launch -> Busy when the
    wake-up arrived before the CE had suspended.
    """
    ev = sorted([(t0, 0, st) for t0, _, st in timeline] + [(mk.t, 1, mk) for mk in marks], key=lambda x: (x[0], x[1]))
    if not ev:
        return []
    end = timeline[-1][1] if timeline else ev[-1][0]
    segs: list[PhaseSeg] = []
    stack: list[str] = []
    state = "Idle"
    cur = ev[0][0]

    def label():
        if stack:
            return stack[-1]
        return {"Busy": "untagged", "Suspended": "suspended", "Idle": "idle"}.get(state, "launch")

    def close(t):
        if t > cur:
            lab, path = label(), tuple(stack)
            if segs and segs[-1].label == lab and segs[-1].state == state and segs[-1].path == path \
                    and segs[-1].t1 == cur:
                segs[-1].t1 = t
            else:
                segs.append(PhaseSeg(cur, t, lab, state, path))

    for t, kind, x in ev:
        close(t)
        cur = max(cur, t)
        if kind == 0:
            if stack and x in ("Idle", "PreLaunch"):
                problems["regions left open at the end of a hyperOp"] += len(stack)
                stack.clear()
            state = x
        else:
            name = names.get(x.imm & ~1, f"mark_0x{x.imm & ~1:03x}")
            if not x.imm & 1:
                stack.append(name)
            elif stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                problems["END closed an outer region (inner regions not closed)"] += 1
                del stack[len(stack) - 1 - stack[::-1].index(name):]
            else:
                problems["END without a BEGIN"] += 1
    close(end)
    return segs


def _seg_at(segs: list[PhaseSeg], starts: list[float], t: float) -> PhaseSeg | None:
    import bisect
    i = bisect.bisect_right(starts, t) - 1
    return segs[i] if 0 <= i < len(segs) and segs[i].t0 <= t < segs[i].t1 else None


@dataclass
class PhaseStats:
    cycles: Counter = field(default_factory=Counter)  # state -> cycles
    instret: int = 0
    flops: int = 0
    counted: bool = False  # instret/flops known for this region
    mix: Counter = field(default_factory=Counter)  # instruction class (MIX_CLASSES) -> retired in the region

    @property
    def total(self) -> float:
        return sum(self.cycles.values())

    @property
    def busy(self) -> float:
        return self.cycles.get("Busy", 0)


def phase_stats(segs: list[PhaseSeg], points: list[tuple[float, int, int]], w0: float, w1: float,
                mix: "MixIndex | None" = None) -> dict[str, PhaseStats]:
    """Per-label cycles (by CeState) inside [w0, w1], and the instret/flops deltas between consecutive counter points
    (marker samples, commit-log samples) charged to the label active at the start of each interval. With `mix`
    (the CE's MixIndex) the retired instructions of each interval are also counted by class."""
    out: dict[str, PhaseStats] = defaultdict(PhaseStats)
    for s in segs:
        c = _overlap(s.t0, s.t1, w0, w1)
        if c:
            out[s.label].cycles[s.state] += c
    starts = [s.t0 for s in segs]
    pts = _unwrapped(points)
    for (ta, ia, fa), (tb, ib, fb) in zip(pts, pts[1:]):
        if not w0 <= ta < w1:
            continue
        s = _seg_at(segs, starts, ta)
        if s is None:
            continue
        st = out[s.label]
        # an interval that runs past the window end is charged pro rata (counters are linear between samples)
        f = 1.0 if tb <= w1 or tb <= ta else (w1 - ta) / (tb - ta)
        st.instret += round(f * (ib - ia))
        st.flops += round(f * (fb - fa))
        st.counted = True
        if mix is not None:
            for name, n in mix.delta(ia, ib).items():
                st.mix[name] += round(f * n)
    return dict(out)


def mix_cycles(st: PhaseStats) -> dict[str, float] | None:
    """Busy cycles of a region by what the CE retired: one cycle per retired instruction (the CE retires at most one
    per cycle), classes from MIX_CLASSES, and `stall` = busy cycles in which nothing retired. None without a mix."""
    if not st.mix:
        return None
    out = {k: float(st.mix.get(k, 0)) for k in MIX_CLASSES}
    out["stall"] = max(0.0, st.busy - sum(out.values()))
    return out


def _unwrapped(points) -> list[tuple[float, int, int]]:
    """Counter points in time order with the 32-bit instret/flops counters unwrapped (monotonic)."""
    out: list[tuple[float, int, int]] = []
    for t, i, f in sorted(p for p in points if p[1] is not None):
        f = f or 0
        if out:
            pt, pi, pf = out[-1]
            i = pi + (i - pi) % (1 << 32)
            f = pf + (f - pf) % (1 << 32)
        out.append((t, i, f))
    return out


def counters_in(points: list[tuple[float, int, int]], w0: float, w1: float) -> tuple[int, int] | None:
    """instret and flops retired inside [w0, w1] (linear between counter points)."""
    pts = _unwrapped(points)
    if len(pts) < 2:
        return None
    ts = [p[0] for p in pts]

    def at(t):
        i = bisect.bisect_right(ts, t)
        if i == 0:
            return pts[0][1], pts[0][2]
        if i == len(pts):
            return pts[-1][1], pts[-1][2]
        (ta, ia, fa), (tb, ib, fb) = pts[i - 1], pts[i]
        f = (t - ta) / (tb - ta) if tb > ta else 0
        return ia + f * (ib - ia), fa + f * (fb - fa)

    (i0, f0), (i1, f1) = at(w0), at(w1)
    return round(i1 - i0), round(f1 - f0)


def limited_by(st: PhaseStats, flops_per_cycle_peak: float, th: dict) -> str | None:
    """Key of redefine_vocab.LIMITED_BY: like classify(), but busy-and-not-retiring is "blocked" (a flush or fence,
    not waiting for a wake-up) and busy code with no FP work is "control"."""
    tot = st.total or 1
    waiting = (st.cycles.get("Suspended", 0) + sum(st.cycles.get(s, 0) for s in IDLE_STATES)) / tot
    if waiting > th.get("wait", 0.5):
        return "waiting"
    if not st.counted or not st.busy:
        return None
    ipc = st.instret / st.busy
    if ipc < th.get("idle_ipc", 0.05):
        return "blocked"
    if ipc < th.get("stall_ipc", 0.6):
        return "stall"
    if not st.flops:
        return "control"
    if st.flops / st.busy < th.get("compute", 0.6) * flops_per_cycle_peak:
        return "instruction"
    return "compute"


def classify(st: PhaseStats, flops_per_cycle_peak: float, th: dict) -> str:
    tot = st.total or 1
    waiting = (st.cycles.get("Suspended", 0) + sum(st.cycles.get(s, 0) for s in IDLE_STATES)) / tot
    if waiting > th.get("wait", 0.5):
        return "waiting"
    if not st.counted or not st.busy:
        return "–"
    ipc = st.instret / st.busy
    if ipc < th.get("idle_ipc", 0.05):
        return "waiting"
    if ipc < th.get("stall_ipc", 0.6):
        return "stall-bound"
    if st.flops / st.busy < th.get("compute", 0.6) * flops_per_cycle_peak:
        return "instruction-bound"
    return "compute-bound"


_PHASE_CACHE: dict = {}


def _mix_indexes(args: dict, marks: dict, timelines: dict, warnings: list[str]) -> dict:
    """MixIndex per CE key, from the commit lines of log= (single-CR traces). A core is used only when its marker
    nops sit at the instret values the markers report, i.e. the commit stream and the CE's counter agree."""
    if not args.get("log"):
        return {}
    try:
        logs = commit_mix(args["log"])
    except OSError:
        return {}
    if not logs:
        return {}
    keys = {ce_key(inst) for inst in timelines} - {None}
    prefixes = {k[0] for k in keys}
    if len(prefixes) != 1:
        warnings.append("instruction mix (loads / stores / FMAs) needs a single-CR trace: not computed")
        return {}
    prefix = prefixes.pop()
    out = {}
    for core, lg in logs.items():
        k = (prefix, core)
        mk = [x for x in marks.get(k, []) if x.instret is not None]
        if k not in keys or not mk:
            continue
        at = dict(lg.markers)  # instret before the marker -> immediate
        ok = sum(at.get(x.instret) == x.imm for x in mk)
        if ok < 0.99 * len(mk):
            warnings.append(f"{ce_label(k)}: the commit lines of log= do not line up with the CE's instret counter "
                            f"({ok} of {len(mk)} markers); instruction mix not computed")
            continue
        out[k] = MixIndex(lg)
    return out


def phase_analysis(m, args: dict, timelines: dict, warnings: list[str]) -> dict:
    """Markers (probe, else commit log), names and per-CE phase segments; shared by the plugins of one run."""
    ck = (id(m), args.get("log"), args.get("markers"), args.get("launch"), args.get("elf"))
    if ck in _PHASE_CACHE:
        res = _PHASE_CACHE[ck]
        warnings.extend(w for w in res["warnings"] if w not in warnings)
        return res
    w: list[str] = []
    marks, samples, source = probe_marks(m), {}, "probe"
    if not marks and args.get("log"):
        cores = commit_log(args["log"])
        if cores:
            marks, samples = log_marks(cores, timelines, w)  # samples (instret/flops) even without markers
            source = "commit log"
    if not marks:
        source = None
    hdrs = [Path(p) for p in args.get("markers", "").split(",") if p] or \
        find_marker_headers(args.get("launch"), args.get("elf"))
    names = marker_names(hdrs)
    problems = Counter()
    phases, points = {}, {}
    for inst, tl in timelines.items():
        k = ce_key(inst)
        if k is None:
            continue
        mk = marks.get(k, [])
        phases[k] = build_phases(tl, mk, names, problems)
        points[k] = sorted([(x.t, x.instret, x.flops) for x in mk if x.instret is not None] + samples.get(k, []))
    mixes = _mix_indexes(args, marks, timelines, w) if source else {}
    for what, n in problems.items():
        w.append(f"PC markers: {n} × {what}")
    if source and not names:
        w.append("no marker names (pass markers=<pc_markers.h,...>): regions are shown by id")
    res = {"source": source, "names": names, "headers": [str(h) for h in hdrs], "phases": phases, "points": points,
           "marks": marks, "mix": mixes,  # marks, mix: per CE key (the probe or commit log marks, MixIndex); not JSON
           "warnings": w}
    _PHASE_CACHE[ck] = res
    warnings.extend(x for x in w if x not in warnings)
    return res


# ---------------------------------------------------------------------------------------------- sub-windows / waterfall

def sub_windows(timelines: dict, w0: float, w1: float) -> list[tuple[str, float, float]]:
    """setup (until every CE is active), steady, tail (after the first CE has finished for good)."""
    n = len(timelines)
    steps = sorted([(max(t0, w0), 1) for tl in timelines.values() for t0, t1, st in tl if st != "Idle" and t1 > w0
                    and t0 < w1] +
                   [(min(t1, w1), -1) for tl in timelines.values() for t0, t1, st in tl if st != "Idle" and t1 > w0
                    and t0 < w1])
    active, t_all = 0, None
    for t, d in steps:
        active += d
        if active >= n and t_all is None:
            t_all = t
    ends = []
    for tl in timelines.values():
        busy = [t1 for t0, t1, st in tl if st != "Idle" and t0 < w1 and t1 > w0]
        if busy:
            ends.append(min(max(busy), w1))
    if t_all is None or not ends:
        return [("whole", w0, w1)]
    t_tail = max(t_all, min(ends))
    return [(name, a, b) for name, a, b in (("setup", w0, t_all), ("steady", t_all, t_tail), ("tail", t_tail, w1))
            if b > a]


def cmu_spans(m) -> list[tuple[float, float, int | None]]:
    """(start, done, opcode) of every LLC CMU command (FLUSH_ALL = 2, CLEAN_ALL = 3, range = 4/5, line = 0/1)."""
    import bisect
    starts = sorted((e.ts, e.vals.get("opcode")) for e in m.events("llc_cmu"))
    dones = sorted(e.ts for e in m.events("llc_cmu_done"))
    out = []
    for s, op in starts:
        i = bisect.bisect_left(dones, s)
        out.append((s, dones[i] if i < len(dones) else m.end, op))
    return out


# ---------------------------------------------------------------------------------------------- kernel sections
# Address regions (rtltrace --regions, kernel_sections.json from KernelSectionConst): which kernel memory section an
# address falls in. Data sections hold kernel data; the others hold code and runtime state.

DATA_SECTIONS = {"GlobalDataHigh", "GlobalDataLow", "GroupLocalMemory", "LocalMemory", "StackMemory",
                 "ScratchpadMemory"}


def plugin_regions(ctx):
    """The run's RegionMap (rtltrace --regions / auto-discovered), or the regions= plugin arg."""
    if getattr(ctx, "regions", None) is not None:
        return ctx.regions
    if ctx.args.get("regions"):
        from rtltrace.regions import load
        return load(ctx.args["regions"])
    return None


def section_of(regions, addr) -> str | None:
    """`StackMemory[ce2]`, `GlobalDataLow[r32]`, `KernelText`, or None without regions; `–` if no section matches."""
    if regions is None or addr is None:
        return None
    r = regions.lookup(addr)
    if r is None:
        return "–"
    tag = "".join(f"[{'r' if k == 'region' else k}{v}]" for k, v in r.fields.items() if k not in ("kernel_id", "cr_id"))
    return r.name + tag


def is_data_section(label: str | None) -> bool:
    return bool(label) and label.split("[")[0] in DATA_SECTIONS


# ---------------------------------------------------------------------------------------------- run registry
# The plugins of one rtltrace run share their summaries here (keyed by model id) so the view plugin can build the
# findings and the run card without re-computing anything.

RUN: dict[tuple[int, str], dict] = {}


def publish(m, name: str, summary: dict) -> None:
    RUN[(id(m), name)] = summary


def published(m, name: str) -> dict | None:
    return RUN.get((id(m), name))


# ---------------------------------------------------------------------------------------------- kernel buffers
# launch.py names the buffer args (buffer(..., name="W")); the sim's host driver assigns their addresses at run time
# and publishes them (kernel-exec-environment README, "Buffer address table"): one block per kernel launch in the sim
# log, and the same table without the marker in <workload>/rtl_sim_results/buffers.txt:
#
#   [kernel_buffers]
#   name                  param       dir     addr_start    addr_end      size_bytes      <- header, skipped
#   buf_arg_0.bin         A           in      0x60000000    0x60003fff    16384
#
# `param` is `-` when launch.py gave no name. Older drivers wrote the 5-column form without `param` and printed the
# block after `[load_kernel_env] buffer addresses:`; both are accepted. Together with the kernel sections they name the
# section each buffer lives in, so reports say "W" instead of "GlobalDataLow[r33]".

@dataclass
class Buffer:
    name: str
    arg: int
    dir: str
    start: int
    end: int
    size: int
    section: str | None = None


def _size(n: int) -> str:
    return f"{n // 1024} KB" if n % 1024 == 0 and n >= 1024 else f"{n} B"


def find_buffer_manifest(*anchors) -> Path | None:
    for a in anchors:
        if not a:
            continue
        d = Path(a).resolve()
        d = d if d.is_dir() else d.parent
        cand = d / "rtl_sim_results" / "buffers.txt"
        if cand.is_file():
            return cand
    return None


BUF_MARKERS = ("[kernel_buffers]", "[load_kernel_env] buffer addresses:")
_BUF_MANIFEST = re.compile(r"\[load_kernel_env\] buffer manifest -> (\S+)")
_BUF_DIRS = ("in", "out", "inout")
_BUF_CACHE: dict = {}
_BUF_LOG_CACHE: dict = {}


def parse_buffer_row(line: str, index: int) -> Buffer | None:
    """One table row (6-column `name param dir start end size` or 5-column `name dir start end size`), else None.
    `index` is the argument index used when the file name is not buf_arg_<i>.bin."""
    f = line.split()
    if len(f) == 6:
        fname, param, d, a0, a1, size = f
    elif len(f) == 5:
        (fname, d, a0, a1, size), param = f, "-"
    else:
        return None
    if d not in _BUF_DIRS:
        return None
    try:
        a0, a1, size = int(a0, 16), int(a1, 16), int(size)
    except ValueError:
        return None
    mt = re.match(r"buf_arg_(\d+)", fname)
    i = int(mt.group(1)) if mt else index
    return Buffer(param if param != "-" else f"arg{i}", i, d, a0, a1, size)


def read_buffer_file(path) -> list[Buffer]:
    """buffers.txt: every line that is a table row (the header and anything else is skipped)."""
    out = []
    for line in Path(path).read_text().splitlines():
        b = parse_buffer_row(line, len(out))
        if b:
            out.append(b)
    return out


def buffer_tables_from_log(log) -> tuple[list[list[Buffer]], list[str]]:
    """(one buffer list per `[kernel_buffers]` block in the sim log, buffers.txt paths the log names)."""
    p = Path(log)
    key = (str(p), p.stat().st_mtime_ns)
    if key in _BUF_LOG_CACHE:
        return _BUF_LOG_CACHE[key]
    tables, manifests, cur = [], [], None
    with open(p, errors="replace") as f:
        for line in f:
            if cur is not None:
                if cur == "header":
                    cur = []
                    continue
                b = parse_buffer_row(line, len(cur))
                if b:
                    cur.append(b)
                    continue
                if cur:
                    tables.append(cur)
                cur = None
            if line.startswith(BUF_MARKERS):
                cur = "header"
            elif "buffer manifest ->" in line:
                mt = _BUF_MANIFEST.search(line)
                if mt:
                    manifests.append(mt.group(1))
    if isinstance(cur, list) and cur:
        tables.append(cur)
    _BUF_LOG_CACHE[key] = (tables, manifests)
    return tables, manifests


def buffer_tables(args: dict, warnings: list[str] | None = None) -> tuple[list[list[Buffer]], str | None]:
    """Buffer addresses of every kernel launch, and where they came from. In order: buffers=<file>[,<file>...] (one
    per kernel), the `[kernel_buffers]` blocks in log=, the buffers.txt the log names, rtl_sim_results/buffers.txt next
    to launch= / elf=. A file is only a fallback: a later run may have overwritten it."""
    if args.get("buffers"):
        files = [f for f in str(args["buffers"]).split(",") if f]
        return [read_buffer_file(f) for f in files if Path(f).is_file()], ", ".join(files)
    manifests = []
    log = args.get("log")
    if log and Path(log).is_file():
        tables, manifests = buffer_tables_from_log(log)
        if tables:
            return tables, "sim log"
    man = next((Path(m) for m in manifests if Path(m).is_file()), None) or \
        find_buffer_manifest(args.get("launch"), args.get("elf"))
    if man is None:
        return [], None
    if log and warnings is not None:
        warnings.append(f"the sim log has no [kernel_buffers] table; buffer addresses read from {man} (written by "
                        "the last run of this workload: check it is this run)")
    return [read_buffer_file(man)], str(man)


def kernel_buffers(args: dict, regions, warnings: list[str] | None = None) -> list[Buffer]:
    """Buffers of the (first) kernel with addresses and kernel section (addresses from buffer_tables(); names from
    launch= when it names them, else the table's param column)."""
    key = (args.get("buffers"), str(args.get("log")), args.get("launch"), args.get("elf"),
           getattr(regions, "source", None))
    if key in _BUF_CACHE:
        return _BUF_CACHE[key]
    tables, src = buffer_tables(args, warnings)
    names = {}
    if args.get("launch"):
        ks = load_workload(args["launch"])
        if ks:
            names = {i: (n, size, d) for i, n, size, d in ks[0].buffers}
    out: list[Buffer] = []
    for b in (tables[0] if tables else []):
        n, lsize, _ = names.get(b.arg, (None, None, None))
        if lsize is not None and lsize != b.size and warnings is not None:
            warnings.append(f"{src}: arg {b.arg} has {b.size} B but launch.py declares {lsize} B; buffer names may "
                            "be stale (re-run the sim)")
        out.append(dataclasses.replace(b, name=n or b.name))
    if regions is not None:
        for b in out:
            s0, s1 = section_of(regions, b.start), section_of(regions, b.end)
            b.section = s0 if s0 == s1 else None
        secs = Counter(b.section for b in out)
        for b in out:
            if secs[b.section] > 1:
                b.section = None  # two buffers share the section: it cannot be named after one of them
    _BUF_CACHE[key] = out
    return out


def buffer_sections(bufs: list[Buffer]) -> dict[str, str]:
    """Kernel-section label -> 'W (in, 64 KB)' for sections that hold exactly one buffer."""
    return {b.section: f"{b.name} ({b.dir}, {_size(b.size)})" for b in bufs if b.section}


# ---------------------------------------------------------------------------------------------- friendly names

try:
    from rtltrace.plugin import RtlName
except ImportError:  # used outside an rtltrace run (metrics_sweep): plain strings
    class RtlName(str):  # type: ignore[no-redef]
        def __new__(cls, text, rtl=""):
            s = super().__new__(cls, text)
            s.rtl = rtl
            return s


class Names:
    """Friendly names (RtlName: the RTL identifier stays attached) for one run."""

    def __init__(self, args: dict, regions=None, warnings: list[str] | None = None, trace=None):
        import redefine_vocab as v
        self.v = v
        self.bufs = kernel_buffers(args, regions, warnings) if regions is not None else []
        self.buffers = buffer_sections(self.bufs)
        crs = {p.inst.split(".baseCr")[0] for p in trace.probes.values() if ".baseCr" in p.inst} if trace else set()
        self.multi_cr = len(crs) > 1

    def module(self, inst: str, short: str | None = None) -> RtlName:
        n = self.v.module_name(inst)
        if n and self.multi_cr and ".baseCr" in inst:
            n = f"{inst.split('.baseCr')[0].rsplit('.', 1)[-1]} · {n}"
        return RtlName(n or short or inst, inst)

    def ce(self, inst_or_key) -> RtlName:
        k = inst_or_key if isinstance(inst_or_key, tuple) else ce_key(inst_or_key)
        rtl = inst_or_key if isinstance(inst_or_key, str) else f"{k[0]}.core_{k[1]}" if k else "?"
        return RtlName(ce_label(k, self.multi_cr) if k else str(inst_or_key), rtl)

    def client(self, c) -> RtlName:
        return RtlName(self.v.client_name(str(c)) or str(c), str(c))

    def port(self, p: str) -> RtlName:
        return RtlName(self.v.port_name(p) or p, p)

    def section(self, label) -> RtlName | None:
        if label is None:
            return None
        return RtlName(self.v.section_name(label, self.buffers) or label, label)

    def region(self, label: str) -> RtlName:
        return RtlName(self.v.region_name(label), label)
