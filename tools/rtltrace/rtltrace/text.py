"""C6 · Text view: decoded, symbolized, chronological log (one line per event)."""

from __future__ import annotations

from .labels import Labeler
from .model import Model


def render(m: Model, lab: Labeler, out, groups: set[str] | None = None):
    tr = m.trace
    width = max([len(tr.module_short(p.inst)) for p in tr.probes.values()] + [10])
    beat: dict[tuple, int] = {}
    stalls = {id(mk.event): mk for mk in m.markers if mk.kind == "stall" and mk.event is not None}
    pend = {id(mk.event): mk for mk in m.markers if mk.kind == "pending" and mk.event is not None}
    for e in tr.events:
        p = e.probe
        if groups and not groups.intersection(p.groups):
            continue
        mod = tr.module_short(p.inst)
        if e.kind == "STALL":
            if id(e) in pend:
                print(f"{e.cycle:>9}  {mod:<{width}}  {p.name:<16} STALL start  <-- PENDING AT END ({pend[id(e)].cycles} cycles)",
                      file=out)
            continue
        f = lab.fields(e)
        if p.is_tl:
            op = f.get("opcode", ("?", ""))[0]
            size = e.vals.get("size", 0)
            nb = 1
            port = next((pt for pt in m.ports.values() if pt.inst == p.inst and pt.name == p.port), None)
            if port is not None and op in ("PutFullData", "PutPartialData", "ArithmeticData", "LogicalData",
                                           "AccessAckData", "GrantData", "ProbeAckData", "ReleaseData"):
                nb = port.beats(size, True)
            key = (p.key, e.vals.get("source"))
            k = beat.get(key, 0) + 1
            beat[key] = 0 if k >= nb else k
            what = f"{p.channel.upper()} {op:<14} src={e.vals.get('source', '-'):<3}"
            if "address" in f:
                what += f" {f['address'][0]:<24}"
            what += f" {1 << size}B" + (f" beat {k}/{nb}" if nb > 1 else "")
            if port is not None:
                what += f"  [{port.client_of(e.vals.get('source', -1))}]"
            line = f"{e.cycle:>9}  {mod:<{width}}  {p.name:<16} {what}"
        else:
            vals = "  ".join(f"{k}={v[0]}" for k, v in f.items())
            line = f"{e.cycle:>9}  {mod:<{width}}  {p.name:<16} {vals}"
        if e.wait:
            line += f"  (waited {e.wait})"
        if id(e) in stalls:
            line += "  <-- LONG STALL"
        print(line, file=out)
    for w in tr.warnings:
        print(f"# warning: {w}", file=out)
