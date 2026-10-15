"""C3 · Correlation: relationships beyond protocol pairing (which lives in the model).

  user links   rtltrace_links.json: [{"name": .., "from": {"probe": glob, "key": field},
                                      "to": {"probe": glob, "key": field}, "pairing": "fifo|nearest", "window": N}]
  key links    fields tagged `key=<k>` on different probes are linked by equal value (nearest later event)
  TL hops      heuristic: a transaction on one port and the next transaction on another port for the same cache
               line while the first is still outstanding (e.g. an LLC miss going out to memory)
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .model import Model, Txn
from .reader import Event


@dataclass
class Link:
    kind: str
    src: Any  # Event or Txn
    dst: Any
    heuristic: bool = False
    why: str = ""


def ts_of(x) -> float:
    return x.ts if isinstance(x, Event) else x.req_first


def user_links(m: Model, spec: str | Path) -> list[Link]:
    rules = json.loads(Path(spec).read_text())
    out: list[Link] = []
    for r in rules:
        name = r.get("name", "link")
        fa, fb = r["from"], r["to"]
        window = r.get("window")
        src = [e for e in m.events() if fnmatch(e.probe.name, fa["probe"]) and fa["key"] in e.vals]
        dst = [e for e in m.events() if fnmatch(e.probe.name, fb["probe"]) and fb["key"] in e.vals]
        if r.get("pairing", "fifo") == "fifo":
            q: dict[int, deque] = defaultdict(deque)
            # same cycle: 'from' first, so a flow-through (enq and deq in one cycle) still pairs
            roles = [(e.ts, 0, "from", e) for e in src] + [(e.ts, 1, "to", e) for e in dst]
            for _, _, role, e in sorted(roles, key=lambda x: (x[0], x[1])):
                if role == "from":
                    q[e.vals[fa["key"]]].append(e)
                elif q[e.vals[fb["key"]]]:
                    a = q[e.vals[fb["key"]]].popleft()
                    if window is None or e.ts - a.ts <= window:
                        out.append(Link(name, a, e, False, "user link (fifo)"))
        else:  # nearest later event with the same key
            by_key: dict[int, list[Event]] = defaultdict(list)
            for e in dst:
                by_key[e.vals[fb["key"]]].append(e)
            for lst in by_key.values():
                lst.sort(key=lambda e: e.ts)
            for a in src:
                lst = by_key.get(a.vals[fa["key"]], [])
                i = bisect.bisect_right([e.ts for e in lst], a.ts)
                if i < len(lst) and (window is None or lst[i].ts - a.ts <= window):
                    out.append(Link(name, a, lst[i], False, "user link (nearest)"))
    return out


def key_links(m: Model) -> list[Link]:
    tagged: dict[str, list[tuple[Event, str]]] = defaultdict(list)
    for e in m.events():
        for f in e.probe.fields:
            for t in f.tags:
                if t.startswith("key=") and f.name in e.vals:
                    tagged[t[4:]].append((e, f.name))
    out = []
    for k, lst in tagged.items():
        lst.sort(key=lambda x: x[0].ts)
        last: dict[int, dict[str, Event]] = defaultdict(dict)  # key value -> probe name -> latest event
        for e, fname in lst:
            v = e.vals[fname]
            open_ = last[v]
            for probe in [pn for pn in open_ if pn != e.probe.name]:
                out.append(Link(f"key:{k}", open_.pop(probe), e, False, f"same {k}={v:#x}"))
            open_[e.probe.name] = e
    return out


def tl_hops(m: Model, line: int = 64) -> list[Link]:
    """Heuristic hop links. A downstream copy of a request (the same line on another port) is *nested* in the
    upstream transaction: it starts no earlier and finishes no later. The immediate next hop is the largest nested
    transaction. Identical intervals (a combinational path) follow the traffic direction: a module's in side to its
    own out side, or one module's out side to another module's in side."""
    mask = ~(line - 1)
    ports = list(m.ports.values())
    by_line: dict[int, list[Txn]] = defaultdict(list)
    for p in ports:
        for t in p.txns:
            if t.kind == "A" and t.end is not None:
                by_line[t.address & mask].append(t)
    for lst in by_line.values():
        lst.sort(key=lambda t: t.start)
    out = []
    for lst in by_line.values():
        starts = [t.start for t in lst]
        for x in lst:
            best = None
            i = bisect.bisect_left(starts, x.start)
            while i < len(lst) and lst[i].start <= x.end:
                y = lst[i]
                i += 1
                if y.port is x.port or y.end > x.end:
                    continue
                same = y.start == x.start and y.end == x.end
                if same:  # combinational path: follow the traffic direction
                    within = x.port.inst == y.port.inst and x.port.side == "in" and y.port.side == "out"
                    across = x.port.inst != y.port.inst and x.port.side == "out" and y.port.side == "in"
                    if not (within or across):
                        continue
                size = (y.end - y.start, y.port.side == "in", -y.start)
                if best is None or size > best[0]:
                    best = (size, y)
            if best is not None:
                out.append(Link("tl-hop", x, best[1], True,
                                f"same {line} B line 0x{x.address & mask:x}, nested in the upstream transaction"))
    return out
