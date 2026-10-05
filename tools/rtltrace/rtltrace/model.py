"""C2 · Generic model: handshake intervals, value timelines and TileLink transactions. No knowledge of modules.

TileLink pairing (per probed port, exact, protocol rules only):
  A -> D by source (Get/Put/Atomic/Hint/Acquire), with beat counts from size and beatBytes;
  Grant -> GrantAck (E) by sink; Release (C) -> ReleaseAck (D) by source; Probe (B) -> ProbeAck (C) by address.
Within one cycle a response is processed before a request, because a client may reuse a source in the cycle its
response completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .reader import Event, Marker, Probe, Trace, parse_clients, parse_managers

A_DATA = {"PutFullData", "PutPartialData", "ArithmeticData", "LogicalData"}
D_DATA = {"AccessAckData", "GrantData"}
C_DATA = {"ProbeAckData", "ReleaseData"}
GET_LIKE = {"Get", "ArithmeticData", "LogicalData", "AcquireBlock"}  # responses carry data


@dataclass
class Handshake:
    probe: Probe
    t0: float  # valid rose (transfer cycle minus wait), base cycles
    t1: float  # transfer cycle
    wait: int
    event: Event


@dataclass
class Segment:
    probe: Probe
    t0: float
    t1: float
    event: Event


@dataclass
class Txn:
    port: "Port"
    kind: str  # "A" (A->D), "C" (Release->ReleaseAck), "B" (Probe->ProbeAck)
    source: int
    opcode: str
    address: int
    size: int
    req_first: float
    req_last: float
    req_wait: int
    req_beats: int
    resp_first: float | None = None
    resp_last: float | None = None
    resp_wait: int = 0
    resp_opcode: str | None = None
    resp_beats: int = 0
    denied: bool = False
    ack: float | None = None  # GrantAck (E)
    sink: int | None = None
    req_left: int = 0
    resp_left: int = 0

    @property
    def client(self) -> str:
        return self.port.client_of(self.source)

    @property
    def done(self) -> bool:
        return self.req_left == 0 and self.resp_left == 0 and self.resp_first is not None

    @property
    def start(self) -> float:
        return self.req_first - self.req_wait * self.port.period / self.port.base

    @property
    def end(self) -> float | None:
        return None if self.resp_last is None else self.resp_last + self.port.period / self.port.base

    @property
    def latency(self) -> int | None:
        """Cycles from the last request beat to the first response beat."""
        if self.resp_first is None:
            return None
        return int(round((self.resp_first - self.req_last) * self.port.base / self.port.period))

    @property
    def total(self) -> int | None:
        """Cycles from the moment the request was offered to the last response beat."""
        if self.end is None:
            return None
        return int(round((self.end - self.start) * self.port.base / self.port.period))

    @property
    def nbytes(self) -> int:
        return 1 << self.size


@dataclass
class Port:
    inst: str
    name: str
    side: str
    beat: int
    at: str
    clients: list[tuple[str, int, int]]
    managers: list[tuple[str, list[tuple[int, int]]]]
    period: int
    base: int
    probes: dict[str, Probe] = field(default_factory=dict)  # channel -> probe
    txns: list[Txn] = field(default_factory=list)
    orphans: list[Event] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.inst, self.name)

    def client_of(self, source: int) -> str:
        for n, lo, hi in self.clients:
            if lo <= source <= hi:
                return n
        return f"src{source}"

    def manager_of(self, address: int) -> str | None:
        for n, sets in self.managers:
            for base, mask in sets:
                if (address & ~mask) == base:
                    return n
        return None

    def beats(self, size: int, has_data: bool) -> int:
        return max(1, (1 << size) // self.beat) if has_data else 1


class Model:
    def __init__(self, trace: Trace):
        self.trace = trace
        self.handshakes: list[Handshake] = []
        self.segments: list[Segment] = []
        self.pulses: list[Event] = []
        self.ports: dict[tuple[str, str], Port] = {}
        self.links: list = []  # filled by correlate
        self.markers: list[Marker] = list(trace.markers)
        self._build()

    # ------------------------------------------------------------------ queries (also used by plugins)
    def probes(self, pattern: str = "*"):
        from fnmatch import fnmatch
        return [p for p in self.trace.probes.values() if fnmatch(p.name, pattern) or fnmatch(p.inst, pattern)]

    def events(self, pattern: str = "*", t0: float | None = None, t1: float | None = None) -> list[Event]:
        keys = {p.key for p in self.probes(pattern)}
        return [e for e in self.trace.events if e.probe.key in keys and e.kind == "EVENT"
                and (t0 is None or e.ts >= t0) and (t1 is None or e.ts <= t1)]

    def txns(self, pattern: str = "*") -> list[Txn]:
        from fnmatch import fnmatch
        return [t for p in self.ports.values() if fnmatch(p.name, pattern) or fnmatch(f"{p.inst}/{p.name}", pattern)
                for t in p.txns]

    def states(self, pattern: str = "*") -> list[Segment]:
        keys = {p.key for p in self.probes(pattern)}
        return [s for s in self.segments if s.probe.key in keys]

    @property
    def end(self) -> float:
        return self.trace.end_ts

    # ------------------------------------------------------------------ build
    def _build(self):
        tr = self.trace
        last_change: dict[tuple[str, int], Event] = {}
        tl_events: dict[tuple[str, str], list[Event]] = {}
        for e in tr.events:
            p = e.probe
            if e.kind != "EVENT":
                continue
            if p.is_tl:
                key = (p.inst, p.port)
                if key not in self.ports:
                    self.ports[key] = Port(p.inst, p.port, p.meta.get("side", "-"), int(p.meta.get("beat", "8")),
                                           p.meta.get("at", ""), parse_clients(p.meta.get("clients", "")),
                                           parse_managers(p.meta.get("managers", "")), p.period, tr.base_period)
                self.ports[key].probes[p.channel] = p
                tl_events.setdefault(key, []).append(e)
            elif p.kind == "fire":
                self.handshakes.append(Handshake(p, e.ts - e.wait * p.period / tr.base_period, e.ts, e.wait, e))
            elif p.kind == "change":
                prev = last_change.get(p.key)
                if prev is not None:
                    self.segments.append(Segment(p, prev.ts, e.ts, prev))
                last_change[p.key] = e
            else:
                self.pulses.append(e)
        for p_key, e in last_change.items():
            self.segments.append(Segment(e.probe, e.ts, tr.end_ts, e))
        self.segments.sort(key=lambda s: (s.probe.key, s.t0))
        order = {"d": 0, "e": 1, "c": 2, "b": 3, "a": 4}  # responses first within a cycle
        for key, evs in tl_events.items():
            evs.sort(key=lambda e: (e.t, order.get(e.probe.channel, 9)))
            self._pair(self.ports[key], evs)

    def _pair(self, port: Port, evs: list[Event]):
        by_source: dict[int, Txn] = {}  # A -> D
        release: dict[int, Txn] = {}  # C Release -> D ReleaseAck
        probes: dict[int, Txn] = {}  # B Probe -> C ProbeAck (by address)
        grants: dict[int, Txn] = {}  # sink -> Grant waiting for E
        for e in evs:
            p, v, ch = e.probe, e.vals, e.probe.channel
            op = p.enum_name("opcode", v.get("opcode", -1)) or str(v.get("opcode"))
            if ch == "a":
                t = by_source.get(v["source"])
                if t is not None and t.req_left > 0:
                    t.req_left -= 1
                    t.req_last = e.ts
                    self._settle(by_source, v["source"], t)
                    continue
                if t is not None:
                    port.orphans.append(e)  # source reused before its response: protocol violation or lost beats
                    continue
                nb = port.beats(v["size"], op in A_DATA)
                t = Txn(port, "A", v["source"], op, v.get("address", 0), v["size"], e.ts, e.ts, e.wait, nb)
                t.req_left = nb - 1
                t.resp_left = port.beats(v["size"], op in GET_LIKE)
                by_source[v["source"]] = t
                port.txns.append(t)
            elif ch == "d":
                table = release if op == "ReleaseAck" else by_source
                t = table.get(v["source"])
                if t is None:
                    port.orphans.append(e)  # response without a traced request (tracing started mid-flight)
                    continue
                if t.resp_first is None:
                    t.resp_first, t.resp_wait, t.resp_opcode = e.ts, e.wait, op
                    t.resp_left = port.beats(v["size"], op in D_DATA)
                    t.resp_beats = t.resp_left
                t.resp_left -= 1
                t.resp_last = e.ts
                t.denied = t.denied or bool(v.get("denied", 0))
                if op in ("Grant", "GrantData") and t.resp_left == 0:
                    t.sink = v.get("sink")
                    grants[v.get("sink", 0)] = t
                self._settle(table, v["source"], t)
            elif ch == "e":
                t = grants.pop(v.get("sink", 0), None)
                if t is None:
                    port.orphans.append(e)
                else:
                    t.ack = e.ts
            elif ch == "b":
                t = Txn(port, "B", v["source"], op, v.get("address", 0), v["size"], e.ts, e.ts, e.wait, 1)
                probes[v.get("address", 0)] = t
                port.txns.append(t)
            elif ch == "c":
                if op in ("ProbeAck", "ProbeAckData"):
                    t = probes.pop(v.get("address", 0), None)
                    if t is None:
                        port.orphans.append(e)
                        continue
                    t.resp_first = t.resp_first or e.ts
                    t.resp_last, t.resp_opcode = e.ts, op
                    t.resp_beats += 1
                else:  # Release / ReleaseData
                    t = release.get(v["source"])
                    if t is not None and t.req_left > 0:
                        t.req_left -= 1
                        t.req_last = e.ts
                        continue
                    nb = port.beats(v["size"], op in C_DATA)
                    t = Txn(port, "C", v["source"], op, v.get("address", 0), v["size"], e.ts, e.ts, e.wait, nb)
                    t.req_left, t.resp_left = nb - 1, 1
                    release[v["source"]] = t
                    port.txns.append(t)
        for t in list(by_source.values()) + list(release.values()):
            if not t.done:
                self.markers.append(Marker(port.probes.get("a") or next(iter(port.probes.values())), "pending",
                                           t.start, int(round((self.trace.end_ts - t.start) * port.base / port.period)),
                                           None))

    @staticmethod
    def _settle(table: dict[int, Txn], source: int, t: Txn):
        if t.req_left == 0 and t.resp_left == 0 and t.resp_first is not None:
            table.pop(source, None)

    # ------------------------------------------------------------------ counters
    def inflight(self, port: Port) -> list[tuple[float, int]]:
        delta: dict[float, int] = {}
        for t in port.txns:
            if t.kind != "A":
                continue
            delta[t.req_first] = delta.get(t.req_first, 0) + 1
            end = t.end if t.end is not None else self.end
            delta[end] = delta.get(end, 0) - 1
        level, out = 0, []
        for ts in sorted(delta):
            level += delta[ts]
            out.append((ts, level))
        return out
