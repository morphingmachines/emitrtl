"""rtltrace: offline tools for emitrtl.trace probe traces (reader, model, correlation, analyses, views, plugins)."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import correlate
from .elf import Elf
from .labels import Labeler
from .model import Model
from .plugin import Context, Emit, Plugins, load
from .reader import read

__all__ = ["Session", "open_trace"]


@dataclass
class Session:
    model: Model
    labeler: Labeler
    plugins: Plugins
    emitted: Emit
    elf: Elf | None = None
    warnings: list[str] = field(default_factory=list)


def open_trace(traces, elf=None, catalog=None, period=None, stall=64, only=None, t_from=None, t_to=None,
               plugins=(), plugin_args=None, links=None, tl_hops=False, line=64, warn=print) -> Session:
    """Run the offline pipeline: C1 read -> C2 model -> plugin setup -> C3 correlate -> plugin derive."""
    tr = read(traces, catalog=catalog, period=period, stall=stall, only=only, t_from=t_from, t_to=t_to)
    m = Model(tr)
    e = Elf(elf) if elf else None
    ps = Plugins([load(p) if isinstance(p, str) else p for p in plugins], warn=warn)
    ps.setup(Context(e, dict(plugin_args or {}), tr))
    if links:
        m.links += correlate.user_links(m, links)
    m.links += correlate.key_links(m)
    if tl_hops:
        m.links += correlate.tl_hops(m, line)
    emitted = Emit()
    lab = Labeler(e, ps, emitted)
    ps.derive(m, emitted)
    m.links += emitted.links
    return Session(m, lab, ps, emitted, e, list(tr.warnings))
