"""Field labels shared by every view: enum names, ELF symbols and address regions for #addr/#code fields, plugin
annotations."""

from __future__ import annotations

from .plugin import Emit, Label, Plugins
from .reader import Event, Field, Probe


class Labeler:
    def __init__(self, elf=None, plugins: Plugins | None = None, emitted: Emit | None = None, regions=None):
        self.elf = elf
        self.plugins = plugins
        self.emitted = emitted
        self.regions = regions  # rtltrace.regions.RegionMap or None

    def region(self, addr: int):
        return self.regions.lookup(addr) if self.regions is not None else None

    def addr(self, addr: int) -> tuple[str, str]:
        """(short, long) label of an address: ELF symbol, else region (StackMemory[ce2]+0x1f0), else hex."""
        s = self.sym(addr)
        r = self.region(addr)
        short = s if not s.startswith("0x") or r is None else r.label()
        parts = [x for x in (None if s.startswith("0x") else s, r.label() if r else None) if x]
        return short, (" · ".join(parts) + f" [0x{addr:x}]") if parts else f"0x{addr:x}"

    def sym(self, addr: int) -> str:
        if self.elf is not None:
            s = self.elf.symbolize(addr)
            if s:
                return s
        return f"0x{addr:x}"

    def covers(self, addr: int, nbytes: int) -> str:
        return ", ".join(self.elf.covers(addr, nbytes)) if self.elf is not None else ""

    def field(self, p: Probe, f: Field, value: int, ev: Event | None = None) -> Label:
        if self.emitted is not None and ev is not None:
            lbl = self.emitted.labels.get((id(ev), f.name))
            if lbl:
                return lbl
        tags = list(f.tags)
        if self.plugins is not None:
            tags += self.plugins.tags_for(p.name, f.name)
            lbl = self.plugins.annotate(f, tags, value, ev) if tags else None
            if lbl:
                return lbl
        if f.type == "e":
            n = p.enums.get(f.enum or "", {}).get(value)
            return (n, f"{n} ({value})") if n else (str(value), f"{value} (not in {f.enum})")
        if "addr" in tags or "code" in tags:
            return self.addr(value)
        if f.type in ("d", "s"):
            return str(value), str(value)
        if f.type == "b":
            return str(value), str(value)
        return f"0x{value:x}", f"0x{value:x}"

    def fields(self, e: Event) -> dict[str, Label]:
        p = e.probe
        return {f.name: self.field(p, f, e.vals[f.name], e) for f in p.fields if f.name in e.vals}

    def title(self, e: Event, limit: int = 4) -> str:
        """Short name for a slice: TL -> opcode + address; others -> first fields."""
        p = e.probe
        lab = self.fields(e)
        if p.is_tl:
            op = lab.get("opcode", ("?", ""))[0]
            return f"{op} {lab['address'][0]}" if "address" in lab else op
        if p.kind == "change":
            return " ".join(v[0] for v in lab.values()) or p.name
        items = [f"{k}={v[0]}" for k, v in list(lab.items())[:limit]]
        return f"{p.name} " + " ".join(items) if items else p.name
