"""C4 · Symbolization: a minimal ELF32/ELF64 little-endian symbol reader (no external dependencies).

Generic only: address -> symbol+offset, and which variables a range of bytes covers. System-specific tables (for
example REDEFINE SMDs) are read by plugins through `Elf.read()` and `Elf.symbols`.
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Symbol:
    name: str
    addr: int
    size: int
    kind: str  # FUNC / OBJECT / NOTYPE


class Elf:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:4] != b"\x7fELF":
            raise ValueError(f"{path}: not an ELF file")
        self.is64 = self.data[4] == 2
        if self.data[5] != 1:
            raise ValueError(f"{path}: only little-endian ELF is supported")
        self._parse_sections()
        self._parse_symbols()

    def _parse_sections(self):
        d = self.data
        if self.is64:
            (shoff,) = struct.unpack_from("<Q", d, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3A)
            fmt = "<IIQQQQIIQQ"
        else:
            (shoff,) = struct.unpack_from("<I", d, 0x20)
            shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x2E)
            fmt = "<IIIIIIIIII"
        raw = [struct.unpack_from(fmt, d, shoff + i * shentsize) for i in range(shnum)]
        strtab_off = raw[shstrndx][4]

        def cstr(off):
            return d[off : d.index(b"\0", off)].decode(errors="replace")

        self.sections = [
            dict(name=cstr(strtab_off + s[0]), type=s[1], flags=s[2], addr=s[3], offset=s[4], size=s[5], link=s[6],
                 entsize=s[9])
            for s in raw
        ]

    def read(self, addr: int, size: int) -> bytes | None:
        """Bytes at a virtual address, from PROGBITS sections only."""
        for s in self.sections:
            if s["type"] == 1 and s["addr"] <= addr and addr + size <= s["addr"] + s["size"]:
                off = s["offset"] + addr - s["addr"]
                return self.data[off : off + size]
        return None

    def _parse_symbols(self):
        d = self.data
        self.symbols: list[Symbol] = []
        for s in self.sections:
            if s["type"] != 2:  # SHT_SYMTAB
                continue
            strtab = self.sections[s["link"]]
            for i in range(s["size"] // s["entsize"]):
                off = s["offset"] + i * s["entsize"]
                if self.is64:
                    name, info, _o, shndx, value, size = struct.unpack_from("<IBBHQQ", d, off)
                else:
                    name, value, size, info, _o, shndx = struct.unpack_from("<IIIBBH", d, off)
                typ = info & 0xF
                if typ not in (0, 1, 2) or shndx == 0 or name == 0:
                    continue
                start = strtab["offset"] + name
                sname = d[start : d.index(b"\0", start)].decode(errors="replace")
                if sname.startswith(("$", ".L")):
                    continue
                self.symbols.append(Symbol(sname, value, size, {0: "NOTYPE", 1: "OBJECT", 2: "FUNC"}[typ]))
        self.symbols.sort(key=lambda s: (s.addr, s.kind == "NOTYPE", -s.size))
        self._addrs = [s.addr for s in self.symbols]
        self._sized = sorted((s for s in self.symbols if s.size and s.kind in ("OBJECT", "FUNC")), key=lambda s: s.addr)

    def lookup(self, addr: int) -> tuple[Symbol, int] | None:
        """Symbol containing addr, with the offset into it."""
        i = bisect.bisect_right(self._addrs, addr) - 1
        while i >= 0:
            s = self.symbols[i]
            if s.addr <= addr < s.addr + max(s.size, 1):
                return s, addr - s.addr
            if s.size == 0 and s.kind == "NOTYPE":
                i -= 1
                continue
            return None
        return None

    def symbolize(self, addr: int) -> str | None:
        hit = self.lookup(addr)
        if hit is None:
            return None
        s, off = hit
        return s.name if off == 0 else f"{s.name}+0x{off:x}"

    def covers(self, addr: int, nbytes: int) -> list[str]:
        """Names of the variables/functions overlapping [addr, addr + nbytes)."""
        return [s.name for s in self._sized if s.addr < addr + nbytes and addr < s.addr + s.size]
