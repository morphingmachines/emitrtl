"""C7 · Plugin API: add system knowledge without touching the core.

A plugin is a Python file (versioned with the system it describes) defining a subclass of `Plugin`:

    from rtltrace.plugin import Plugin, Section

    class MyPlugin(Plugin):
        name = "my"
        requires = ["llc.in*"]                   # probe-name globs that must exist, else the plugin is skipped
        fields = {"*CmdExe*/cmAddr": "frame"}    # offline tagging: "probeGlob/fieldGlob" -> tag
        tags = {"frame"}                         # tags this plugin labels via annotate()

        def setup(self, ctx): ...                # once, before correlation: ctx.elf, ctx.args, ctx.trace
        def annotate(self, f, value, ev): ...    # -> (short, long) label or None
        def derive(self, model, out): ...        # add processes, tracks, spans, instants, counters, flows, links
        def report(self, model): ...             # -> [Section, ...] for the HTML/text report

Load with `rtltrace <cmd> ... --plugin path/to/file.py[:ClassName] [--plugin-arg key=value ...]`.
Plugins read the model; they never modify it. Everything they emit is tagged `plugin:<name>`.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

Label = tuple[str, str]  # (short, long)


@dataclass
class Section:
    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    text: str = ""


@dataclass
class Context:
    elf: Any  # rtltrace.elf.Elf or None
    args: dict[str, str]
    trace: Any  # rtltrace.reader.Trace
    regions: Any = None  # rtltrace.regions.RegionMap or None (--regions / kernel_sections.json)


class Plugin:
    name: str = "unnamed"
    requires: list[str] = []
    fields: dict[str, str] = {}
    tags: set[str] = set()

    def setup(self, ctx: Context) -> None:
        pass

    def annotate(self, f, value: int, ev) -> Label | None:
        return None

    def derive(self, model, out: "Emit") -> None:
        pass

    def report(self, model) -> list[Section]:
        return []


@dataclass
class _Proc:
    id: int
    name: str
    sort: int
    plugin: str


@dataclass
class _Track:
    id: int
    proc: int
    name: str
    sort: int


@dataclass
class _Span:
    id: int
    track: int
    t0: float
    t1: float
    name: str
    args: dict


class Emit:
    """Builder handed to `Plugin.derive`. Views render what is collected here."""

    def __init__(self):
        self.procs: list[_Proc] = []
        self.tracks: list[_Track] = []
        self.spans: list[_Span] = []
        self.instants: list[tuple[int, float, str, dict]] = []
        self.counters: list[tuple[int, str, list[tuple[float, float]]]] = []
        self.flows: list[tuple[list[int], str]] = []
        self.links: list = []
        self.labels: dict[tuple[int, str], Label] = {}
        self.plugin = "?"
        self._n = 0

    def _id(self) -> int:
        self._n += 1
        return self._n

    def process(self, name: str, sort: int = 100) -> int:
        p = _Proc(self._id(), name, sort, self.plugin)
        self.procs.append(p)
        return p.id

    def track(self, proc: int, name: str, sort: int = 0) -> int:
        t = _Track(self._id(), proc, name, sort)
        self.tracks.append(t)
        return t.id

    def span(self, track: int, t0: float, t1: float, name: str, args: dict | None = None) -> int:
        s = _Span(self._id(), track, t0, max(t1, t0 + 1), name, dict(args or {}, plugin=self.plugin))
        self.spans.append(s)
        return s.id

    def instant(self, track: int, t: float, name: str, args: dict | None = None) -> None:
        self.instants.append((track, t, name, dict(args or {}, plugin=self.plugin)))

    def counter(self, proc: int, name: str, points: list[tuple[float, float]]) -> None:
        self.counters.append((proc, name, list(points)))

    def flow(self, spans: list[int], name: str) -> None:
        self.flows.append(([s for s in spans if s], name))

    def link(self, a, b, kind: str, heuristic: bool = False, why: str = "") -> None:
        from .correlate import Link
        self.links.append(Link(kind, a, b, heuristic, why or f"plugin:{self.plugin}"))

    def label(self, ev, fname: str, label: Label) -> None:
        self.labels[(id(ev), fname)] = label


def load(spec: str) -> Plugin:
    """`path.py` (first Plugin subclass found) or `path.py:ClassName`."""
    path, _, cls = spec.partition(":")
    p = Path(path)
    name = f"rtltrace_plugin_{p.stem}"
    s = importlib.util.spec_from_file_location(name, p)
    if s is None or s.loader is None:
        raise ValueError(f"cannot load plugin {spec}")
    mod = importlib.util.module_from_spec(s)
    sys.modules[name] = mod
    s.loader.exec_module(mod)
    if cls:
        return getattr(mod, cls)()
    for v in vars(mod).values():
        if isinstance(v, type) and issubclass(v, Plugin) and v is not Plugin:
            return v()
    raise ValueError(f"{spec}: no Plugin subclass found")


class Plugins:
    """Runs plugins with isolation: a plugin that raises is disabled with a warning; the core output continues."""

    def __init__(self, plugins: list[Plugin], warn=print):
        self.all = plugins
        self.warn = warn
        self.active: list[Plugin] = []

    def _guard(self, p: Plugin, what: str, fn, *a):
        try:
            return fn(*a)
        except Exception:
            self.warn(f"[rtltrace] plugin '{p.name}' failed in {what}; disabled.\n" + traceback.format_exc(limit=3))
            if p in self.active:
                self.active.remove(p)
            return None

    def setup(self, ctx: Context):
        for p in self.all:
            missing = [r for r in p.requires if not any(fnmatch(pr.name, r) for pr in ctx.trace.probes.values())]
            if missing:
                self.warn(f"[rtltrace] plugin '{p.name}' skipped: no probe matches {missing}")
                continue
            self.active.append(p)
            self._guard(p, "setup", p.setup, ctx)

    def tags_for(self, probe_name: str, field_name: str) -> list[str]:
        return [t for p in self.active for k, t in p.fields.items()
                if fnmatch(probe_name, k.partition("/")[0]) and fnmatch(field_name, k.partition("/")[2])]

    def annotate(self, f, tags: list[str], value: int, ev) -> Label | None:
        for p in list(self.active):
            if p.tags & set(tags):
                r = self._guard(p, "annotate", p.annotate, f, value, ev)
                if r:
                    return r
        return None

    def derive(self, model, out: Emit):
        for p in list(self.active):
            out.plugin = p.name
            self._guard(p, "derive", p.derive, model, out)

    def report(self, model) -> list[Section]:
        out = []
        for p in list(self.active):
            r = self._guard(p, "report", p.report, model)
            out.extend(r or [])
        return out
