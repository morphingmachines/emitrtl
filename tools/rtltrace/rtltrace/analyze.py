"""C5 · Generic analyses: point at bottlenecks without knowing what any module does."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .model import Model


def pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    note: str = ""


def analyze(m: Model, top: int = 10, regions=None) -> list[Table]:
    """Generic analyses. With `regions` (rtltrace.regions.RegionMap) TileLink tables also name the address region."""
    tr = m.trace
    span_cycles = max(1.0, tr.end_ts - min([min(p.decl_times) / tr.base_period for p in tr.probes.values()
                                             if p.decl_times] + [0.0]))
    short = tr.module_short
    out: list[Table] = []

    # ------------------------------------------------------------------ per module: input vs output stall
    per_mod = defaultdict(lambda: {"in": 0, "out": 0, "in_n": 0, "out_n": 0})
    for h in m.handshakes:
        d = h.probe.direction
        if d in ("in", "out"):
            per_mod[h.probe.inst][d] += h.wait
            per_mod[h.probe.inst][d + "_n"] += 1
    for port in m.ports.values():
        for t in port.txns:
            if t.kind != "A":
                continue
            a = port.probes.get("a")
            dd = port.probes.get("d")
            if a and a.direction in ("in", "out"):
                per_mod[port.inst][a.direction] += t.req_wait
                per_mod[port.inst][a.direction + "_n"] += 1
            if dd and dd.direction in ("in", "out") and t.resp_first is not None:
                per_mod[port.inst][dd.direction] += t.resp_wait
                per_mod[port.inst][dd.direction + "_n"] += 1
    rows = []
    for inst, s in per_mod.items():
        ip, op = 100 * s["in"] / span_cycles, 100 * s["out"] / span_cycles
        rows.append([short(inst), s["in_n"], s["in"], round(ip, 1), s["out_n"], s["out"], round(op, 1), round(ip - op, 1)])
    rows.sort(key=lambda r: -r[-1])
    out.append(Table("Modules: input-side vs output-side stall", [
        "module", "in transfers", "in stall cycles", "in stall %", "out transfers", "out stall cycles",
        "out stall %", "bottleneck score"], rows,
        "Stall cycles are cycles an interface had valid high while ready was low. A module whose inputs stall while "
        "its outputs flow freely (high score) is the limiting stage; high output stall means it is being back-pressured "
        "from downstream."))

    # ------------------------------------------------------------------ per interface
    per_if = defaultdict(list)
    for h in m.handshakes:
        per_if[h.probe.key].append(h)
    rows = []
    for key, hs in per_if.items():
        p = hs[0].probe
        waits = [h.wait for h in hs]
        stalled = [w for w in waits if w]
        rows.append([short(p.inst), p.name, p.direction or "-", len(hs), round(100 * len(hs) / span_cycles, 1),
                     sum(waits), round(100 * sum(waits) / span_cycles, 1), max(waits) if waits else 0,
                     round(sum(stalled) / len(stalled), 1) if stalled else 0])
    rows.sort(key=lambda r: -r[5])
    out.append(Table("Interfaces (ready/valid)", [
        "module", "probe", "dir", "transfers", "transfers / 100 cycles", "stall cycles", "stall %", "max wait",
        "mean wait (stalled)"], rows))

    # ------------------------------------------------------------------ TileLink
    rows = []
    for port in m.ports.values():
        groups = defaultdict(list)
        for t in port.txns:
            groups[(t.opcode, t.client)].append(t)
        for (op, client), ts in sorted(groups.items()):
            lat = [t.latency for t in ts if t.latency is not None]
            tot = [t.total for t in ts if t.total is not None]
            nbytes = sum(t.nbytes for t in ts)
            rows.append([short(port.inst), port.name, port.side, op, client, len(ts), pct(lat, .5), pct(lat, .95),
                         pct(lat, .99), max(lat) if lat else None, pct(tot, .5), max(tot) if tot else None,
                         round(nbytes / span_cycles, 2), sum(1 for t in ts if not t.done)])
    out.append(Table("TileLink transactions by port, opcode and client", [
        "module", "port", "side", "opcode", "client", "n", "latency p50", "p95", "p99", "max", "total p50", "total max",
        "bytes / cycle", "unfinished"], rows,
        "latency = last request beat to first response beat; total = request offered (incl. backpressure) to last "
        "response beat; cycles of the port's clock."))
    rows = []
    for port in m.ports.values():
        level = m.inflight(port)
        peak = max((v for _, v in level), default=0)
        used = len({t.source for t in port.txns if t.kind == "A"})
        rng = sum(hi - lo + 1 for _, lo, hi in port.clients)
        rows.append([short(port.inst), port.name, len(port.txns), peak, used, rng, len(port.orphans), port.at])
    out.append(Table("TileLink ports", ["module", "port", "transactions", "max in flight", "sources used",
                                        "source range", "orphan beats", "bound at"], rows,
                     "max in flight close to the source range means the client ran out of source IDs."))

    # ------------------------------------------------------------------ state probes
    rows = []
    per_state = defaultdict(lambda: defaultdict(float))
    from .labels import Labeler
    lab = Labeler()
    for s in m.segments:
        name = lab.title(s.event)
        per_state[s.probe.key][name] += s.t1 - s.t0
    for key, d in per_state.items():
        p = tr.probes[key]
        total = sum(d.values()) or 1
        for v, c in sorted(d.items(), key=lambda kv: -kv[1]):
            rows.append([short(p.inst), p.name, v, int(c), round(100 * c / total, 1)])
    out.append(Table("Time in each value (change probes)", ["module", "probe", "value", "cycles", "%"], rows))

    # ------------------------------------------------------------------ hangs and slowest
    rows = [[mk.kind, short(mk.probe.inst), mk.probe.name, int(mk.t0), mk.cycles] for mk in m.markers]
    rows.sort(key=lambda r: (r[0] != "pending", -r[4]))
    out.append(Table("Hangs and long stalls", ["kind", "module", "probe", "from cycle", "cycles"], rows,
                     "pending = still waiting at the end of the trace; stall = waited at least the stall threshold."))
    txns = sorted((t for p in m.ports.values() for t in p.txns if t.total is not None), key=lambda t: -t.total)[:top]
    rlabel = (lambda a: regions.label(a) or "–") if regions is not None else None  # noqa: E731
    out.append(Table(f"Slowest {top} TileLink transactions", [
        "module", "port", "opcode", "client", "source", "address"] + (["region"] if rlabel else []) +
        ["start cycle", "total", "latency"],
        [[short(t.port.inst), t.port.name, t.opcode, t.client, t.source, f"0x{t.address:x}"] +
         ([rlabel(t.address)] if rlabel else []) + [int(t.start), t.total, t.latency] for t in txns]))
    if regions is not None:
        by = defaultdict(list)
        for p in m.ports.values():
            for t in p.txns:
                if t.kind == "A":
                    r = regions.lookup(t.address)
                    by[(short(p.inst), p.name, r.name if r else "–", t.opcode)].append(t)
        rows = []
        for (mod, port, rg, op), ts in sorted(by.items(), key=lambda kv: (kv[0][0], kv[0][1], -len(kv[1]))):
            lat = sorted(t.latency for t in ts if t.latency is not None)
            q = (lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else None)  # noqa: E731
            rows.append([mod, port, rg, op, len(ts), sum(t.nbytes for t in ts), q(0.5), q(0.95)])
        out.append(Table("TileLink traffic by address region", ["module", "port", "region", "opcode", "transactions",
                                                                 "bytes", "latency p50", "latency p95"], rows,
                         f"Regions from {regions.source}."))
    hs = sorted(m.handshakes, key=lambda h: -h.wait)[:top]
    out.append(Table(f"Longest {top} handshake waits", ["module", "probe", "cycle", "wait"],
                     [[short(h.probe.inst), h.probe.name, int(h.t1), h.wait] for h in hs if h.wait]))
    return out
