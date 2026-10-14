"""Address regions: label a traced address with the named region it falls in (e.g. a kernel memory section).

A regions file is JSON (REDEFINE writes one per RTL config as kernel_sections.json, from KernelSectionConst):

    {"addr_width": 32, "kernel_id_width": 0, "cr_id_width": 0,
     "sections": [{"name": "StackMemory", "value": "0x38000000", "care": "0xfc000000", "offset_mask": "0xffffff",
                   "fields": [{"name": "ce", "hi": 26, "lo": 24, "add": 0}], "scope": "PRIVATE", ...}, ...]}

A section matches when (addr & care) == value; the first match in file order wins. `fields` are bit-slices decoded
per address (plus `add`). Addresses wider than addr_width are extended logical addresses: the bits above addr_width
carry the kernel id (kernel_id_width bits) and then the CR id (cr_id_width bits); they are reported as fields.
Every other key of a section (scope, caching, ...) is passed through as an attribute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FILENAME = "kernel_sections.json"


@dataclass
class Region:
    name: str
    offset: int
    fields: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)

    def label(self) -> str:
        """StackMemory[ce2]+0x1f0, GlobalDataLow[r33]+0x40, KernelText+0x64 (kernel/CR ids are left to args)."""
        tag = "".join(f"[{'r' if k == 'region' else k}{v}]" for k, v in self.fields.items()
                      if k not in ("kernel_id", "cr_id"))
        return f"{self.name}{tag}+0x{self.offset:x}"


@dataclass
class _Section:
    name: str
    value: int
    care: int
    offset_mask: int
    fields: list
    attrs: dict


class RegionMap:
    def __init__(self, doc: dict, source: str = ""):
        self.source = source
        self.addr_width = int(doc.get("addr_width", 32))
        self.kernel_id_width = int(doc.get("kernel_id_width", 0))
        self.cr_id_width = int(doc.get("cr_id_width", 0))
        self.sections = []
        for s in doc["sections"]:
            num = lambda v: int(v, 0) if isinstance(v, str) else int(v)  # noqa: E731
            attrs = {k: v for k, v in s.items() if k not in ("name", "value", "care", "offset_mask", "fields")}
            self.sections.append(_Section(s["name"], num(s["value"]), num(s["care"]),
                                          num(s.get("offset_mask", "0xffffffff")), s.get("fields", []), attrs))
        self._cache: dict[int, Region | None] = {}

    def lookup(self, addr: int) -> Region | None:
        r = self._cache.get(addr, ...)
        if r is not ...:
            return r
        low = addr & ((1 << self.addr_width) - 1)
        high = addr >> self.addr_width
        r = None
        for s in self.sections:
            if low & s.care == s.value:
                f = {d["name"]: ((low >> d["lo"]) & ((1 << (d["hi"] - d["lo"] + 1)) - 1)) + d.get("add", 0)
                     for d in s.fields}
                if high:
                    f["kernel_id"] = high & ((1 << self.kernel_id_width) - 1) if self.kernel_id_width else high
                    if self.cr_id_width:
                        f["cr_id"] = (high >> self.kernel_id_width) & ((1 << self.cr_id_width) - 1)
                r = Region(s.name, low & s.offset_mask, f, s.attrs)
                break
        if len(self._cache) < 1 << 20:
            self._cache[addr] = r
        return r

    def label(self, addr: int) -> str | None:
        r = self.lookup(addr)
        return r.label() if r else None


def load(path) -> RegionMap:
    return RegionMap(json.loads(Path(path).read_text()), str(path))


def discover(*near) -> Path | None:
    """kernel_sections.json in the directory of any of the given files (catalog first, then traces)."""
    for p in near:
        if not p:
            continue
        d = Path(p).resolve()
        cand = (d if d.is_dir() else d.parent) / FILENAME
        if cand.is_file():
            return cand
    return None
