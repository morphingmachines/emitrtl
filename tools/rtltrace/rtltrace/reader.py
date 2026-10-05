"""C1 · Reader: parse rtltrace lines (from a simulation's stderr) into probes and decoded events.

Line format (emitrtl.trace, format v1):
    @RT <t> DECL  <%m> <pid> <kind> <name> <groups> <width> <layout> <enums|-> <meta>
    @RT <t> EVENT <%m> <pid> [<hex>]
    @RT <t> STALL <%m> <pid>
Any other line (today's printfs, assertion messages, simulator output) is ignored.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_MAJOR = "1"

_TILDE = re.compile(r"~([0-9A-Fa-f]{2})")
_META_CODES = {f"{ord(c):02X}" for c in " ;=%\"\\"}
_BLOCK = re.compile(r"\.unnamedblk\d+")


def unescape(s: str) -> str:
    """Undo name-level escaping (`~XX` for every escaped character)."""
    return _TILDE.sub(lambda m: chr(int(m.group(1), 16)), s)


def unescape_meta(s: str) -> str:
    """Undo meta-level escaping only; names inside structured values stay escaped until they are split out."""
    return _TILDE.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1).upper() in _META_CODES else m.group(0), s)


def normalize_inst(path: str) -> str:
    """`%m` inside a firtool `begin ... end` block (ChiselSim flow) carries `.unnamedblkN`; drop it."""
    return _BLOCK.sub("", path)


@dataclass
class Field:
    name: str
    width: int
    type: str  # h d s b e
    enum: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Probe:
    inst: str  # module instance path (%m)
    pid: int
    kind: str  # fire / valid / change / event
    name: str
    groups: list[str]
    width: int
    fields: list[Field]
    enums: dict[str, dict[int, str]]
    meta: dict[str, str]
    decl_times: list[int] = field(default_factory=list)  # one per reset epoch
    period: int | None = None  # simulation time units per cycle of this probe's clock
    from_catalog: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return (self.inst, self.pid)

    @property
    def is_tl(self) -> bool:
        return "tl" in self.meta

    @property
    def channel(self) -> str | None:
        return self.meta.get("tl")

    @property
    def port(self) -> str | None:
        return self.meta.get("port")

    @property
    def direction(self) -> str | None:
        """'in' when the module receives on this interface, 'out' when it sends (from dir= or TL side/channel)."""
        if "dir" in self.meta:
            return self.meta["dir"]
        side, ch = self.meta.get("side"), self.meta.get("tl")
        if side in ("in", "out") and ch:
            request = ch in ("a", "c", "e")  # client -> manager channels
            return ("in" if request else "out") if side == "in" else ("out" if request else "in")
        return None

    def decode(self, hexstr: str | None) -> dict[str, int]:
        if not hexstr or not self.fields:
            return {}
        v = int(hexstr, 16)
        pos = sum(f.width for f in self.fields)
        out = {}
        for f in self.fields:
            pos -= f.width
            raw = (v >> pos) & ((1 << f.width) - 1)
            if f.type == "s" and raw >> (f.width - 1):
                raw -= 1 << f.width
            out[f.name] = raw
        return out

    def enum_name(self, fname: str, value: int) -> str | None:
        f = next((f for f in self.fields if f.name == fname), None)
        if f is None or f.enum is None:
            return None
        return self.enums.get(f.enum, {}).get(value)

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass
class Event:
    probe: Probe
    t: int  # raw simulation time
    kind: str  # EVENT / STALL
    hex: str | None
    vals: dict[str, int]
    ts: float = 0.0  # time in base-clock cycles (common axis)
    cycle: int = 0  # int(ts)
    wait: int = 0  # fire EVENTs: cycles valid was high before the transfer (from the preceding STALL)


@dataclass
class Marker:
    probe: Probe
    kind: str  # "stall" (wait >= threshold) or "pending" (STALL never followed by a transfer)
    t0: float
    cycles: int
    event: Event | None


# ---------------------------------------------------------------------------------------------- parsing helpers

def parse_layout(layout: str) -> list[Field]:
    if layout in ("", "-"):
        return []
    out = []
    for item in layout.split(","):
        parts = item.split("#")
        name, width, *t = parts[0].split(":")
        typ = t[0] if t else "h"
        enum = None
        if typ.startswith("e="):
            enum, typ = typ[2:], "e"
        # emitrtl before sanitizeTag wrote `key=<k>` as `key_<k>`
        tags = ["key=" + tg[4:] if tg.startswith("key_") else tg for tg in parts[1:]]
        out.append(Field(name, int(width), typ, enum, tags))
    return out


def parse_enums(enums: str) -> dict[str, dict[int, str]]:
    if enums in ("", "-"):
        return {}
    out = {}
    for tab in enums.split(";"):
        name, _, body = tab.partition("=")
        entries = {}
        for e in body.split("/"):
            if ":" in e:
                v, lbl = e.split(":", 1)
                entries[int(v)] = unescape(lbl)
        out[name] = entries
    return out


def parse_meta(meta: str) -> dict[str, str]:
    out = {}
    for kv in meta.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = unescape_meta(v)
    return out


def parse_clients(value: str) -> list[tuple[str, int, int]]:
    out = []
    for c in filter(None, value.split("|")):
        name, _, rng = c.rpartition("@")
        lo, _, hi = rng.partition("-")
        out.append((unescape(name), int(lo), int(hi)))
    return out


def parse_managers(value: str) -> list[tuple[str, list[tuple[int, int]]]]:
    out = []
    for m in filter(None, value.split("|")):
        name, _, sets = m.rpartition("@")
        addrs = []
        for s in filter(None, sets.split("+")):
            base, _, mask = s.partition("/")
            addrs.append((int(base, 16), int(mask, 16)))
        out.append((unescape(name), addrs))
    return out


def _probe_from(inst, pid, kind, name, groups, width, layout, enums, meta, **kw) -> Probe:
    return Probe(inst=inst, pid=int(pid), kind=kind, name=name, groups=groups.split(","), width=int(width),
                 fields=parse_layout(layout), enums=parse_enums(enums), meta=parse_meta(meta), **kw)


def load_catalog(path: str | Path) -> dict[int, dict]:
    """trace_points.json: pid -> first entry (used when a probe's DECL line never printed)."""
    doc = json.loads(Path(path).read_text())
    out = {}
    for p in doc.get("points", []):
        out.setdefault(int(p["pid"]), p)
    return out


# ---------------------------------------------------------------------------------------------- the trace

class Trace:
    """All probes and events of one or more trace files, on a common time axis."""

    def __init__(self):
        self.probes: dict[tuple[str, int], Probe] = {}
        self.events: list[Event] = []
        self.markers: list[Marker] = []
        self.warnings: list[str] = []
        self.base_period = 1
        self.t_end = 0
        self.end_ts = 0.0

    # ------------------------------------------------------------------ helpers used by the model and views
    def cycles(self, t: int) -> float:
        return t / self.base_period

    def probe_cycles(self, p: Probe, dt: int) -> int:
        return int(round(dt / (p.period or self.base_period)))

    def by_name(self, pattern: str) -> list[Probe]:
        from fnmatch import fnmatch
        return [p for p in self.probes.values() if fnmatch(p.name, pattern)]

    def module_short(self, inst: str) -> str:
        return self._short.get(inst, inst)


def read(paths, catalog: str | Path | None = None, period: int | None = None, stall: int = 64,
         only: str | None = None, t_from: float | None = None, t_to: float | None = None) -> Trace:
    """Read trace files (stderr captures) into a Trace.

    catalog: trace_points.json, used for probes whose DECL never printed (e.g. no reset pulse in a ChiselSim test).
    period:  simulation time units per cycle (default: inferred per probe as the gcd of its event time deltas).
    stall:   stall-alarm threshold in cycles.
    only:    keep only probes whose instance path contains this substring.
    t_from/t_to: keep events in this cycle window (base clock).
    """
    tr = Trace()
    cat = load_catalog(catalog) if catalog else {}
    raw: list[tuple[int, str, str, int, str | None]] = []
    unprocessed = 0
    for path in ([paths] if isinstance(paths, (str, Path)) else paths):
        with open(path, errors="replace") as f:
            for line in f:
                at = line.find("@RT ")
                if at < 0:
                    if "@@EN:" in line and "@RT@@TIME@@" in line:
                        unprocessed += 1
                    continue
                line = line[at:]  # stdout text without a trailing newline may precede the record
                parts = line.rstrip("\n").split(" ")
                if len(parts) < 5:
                    continue
                t, kind, inst, pid = int(parts[1]), parts[2], normalize_inst(parts[3]), int(parts[4])
                if kind == "DECL":
                    if len(parts) < 12:
                        tr.warnings.append(f"malformed DECL: {line.strip()[:120]}")
                        continue
                    key = (inst, pid)
                    p = tr.probes.get(key)
                    if p is None:
                        p = _probe_from(inst, pid, *parts[5:12])
                        v = p.meta.get("v", FORMAT_MAJOR)
                        if v.split(".")[0] != FORMAT_MAJOR:
                            raise ValueError(f"unsupported rtltrace format v={v} (this tool reads v{FORMAT_MAJOR})")
                        tr.probes[key] = p
                    p.decl_times.append(t)
                elif kind in ("EVENT", "STALL"):
                    raw.append((t, kind, inst, pid, parts[5] if len(parts) > 5 else None))
    if unprocessed:
        tr.warnings.append(f"{unprocessed} line(s) were not post-processed (no instance path or time); "
                           "run the emitrtl post-pass (emitrtl.trace.PostPass) on the generated SV")

    # probes that never printed a DECL: fall back to the catalog
    for t, kind, inst, pid, _ in raw:
        if (inst, pid) not in tr.probes:
            c = cat.get(pid)
            if c is None:
                tr.warnings.append(f"no DECL and no catalog entry for probe {pid} in {inst}; its events are skipped")
                tr.probes[(inst, pid)] = None  # type: ignore[assignment]
                continue
            tr.probes[(inst, pid)] = _probe_from(inst, pid, c["kind"], c["name"], ",".join(c["groups"]), c["width"],
                                                 c["layout"], c["enums"], c["meta"], from_catalog=True)
    tr.probes = {k: p for k, p in tr.probes.items() if p is not None}
    if only:
        tr.probes = {k: p for k, p in tr.probes.items() if only in p.inst}

    # clock period per module instance (one module = one clock): gcd of all its event time deltas. The common axis
    # uses the gcd of those. A module keeps a slower period of its own only with enough samples to rule out a
    # merely sparse module on the fast clock; --period overrides everything.
    times: dict[str, set[int]] = {}
    for t, _, inst, pid, _ in raw:
        if (inst, pid) in tr.probes:
            times.setdefault(inst, set()).add(t)
    for p in tr.probes.values():
        times.setdefault(p.inst, set()).update(p.decl_times)
    inst_gcd = {}
    for inst, ts in times.items():
        s, g = sorted(ts), 0
        for a, b in zip(s, s[1:]):
            g = math.gcd(g, b - a)
        inst_gcd[inst] = g
    g_all = 0
    for g in inst_gcd.values():
        g_all = math.gcd(g_all, g)
    base = period or g_all or 1
    tr.base_period = base
    for p in tr.probes.values():
        g = inst_gcd.get(p.inst, 0)
        own = g >= 2 * base and len(times.get(p.inst, ())) >= 16
        p.period = period or (g if own else base)

    # events, with STALL folded into the following transfer's wait. A reset (a repeated DECL of the probe) between
    # the STALL and the transfer clears the handshake, so such a STALL does not count.
    def reset_between(p: Probe, t0: int, t1: int) -> bool:
        return any(t0 < d <= t1 for d in p.decl_times)

    pend: dict[tuple[str, int], Event] = {}
    for t, kind, inst, pid, hx in raw:
        p = tr.probes.get((inst, pid))
        if p is None:
            continue
        e = Event(p, t, kind, hx, p.decode(hx) if kind == "EVENT" else {})
        e.ts = t / base
        e.cycle = int(e.ts)
        if kind == "STALL":
            pend[p.key] = e
        elif p.kind == "fire":
            s = pend.pop(p.key, None)
            if s is not None and reset_between(p, s.t, t):
                s = None
            if s is not None:
                e.wait = tr.probe_cycles(p, t - s.t)
                if e.wait >= stall:
                    tr.markers.append(Marker(p, "stall", s.ts, e.wait, e))
        tr.events.append(e)
    tr.t_end = max([e.t for e in tr.events] + [t for p in tr.probes.values() for t in p.decl_times] + [0])
    tr.end_ts = tr.t_end / base + 1
    for s in pend.values():
        if not reset_between(s.probe, s.t, tr.t_end):
            tr.markers.append(Marker(s.probe, "pending", s.ts, tr.probe_cycles(s.probe, tr.t_end - s.t) + 1, s))

    if t_from is not None or t_to is not None:
        lo = t_from if t_from is not None else -math.inf
        hi = t_to if t_to is not None else math.inf
        tr.events = [e for e in tr.events if lo <= e.ts <= hi]
        tr.markers = [m for m in tr.markers if lo <= m.t0 <= hi]

    # short module names: drop the common prefix of all instance paths
    insts = sorted({p.inst for p in tr.probes.values()})
    common = ""
    if len(insts) > 1:
        parts = [i.split(".") for i in insts]
        k = 0
        while all(len(x) > k + 1 and x[k] == parts[0][k] for x in parts):
            k += 1
        common = ".".join(parts[0][:k])
    elif insts:
        common = ".".join(insts[0].split(".")[:-1])
    tr._short = {i: (i[len(common) + 1:] if common and i.startswith(common + ".") else i) or i for i in insts}
    return tr
