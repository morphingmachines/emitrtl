"""C5 · Generic analyses: point at bottlenecks without knowing what any module does."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .labels import Labeler
from .model import Model
from .plugin import RtlName


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
    key: str = ""  # stable CSV file name (default: derived from the title)


def analyze(m: Model, top: int = 10, regions=None, lab: Labeler | None = None) -> list[Table]:
    """Generic analyses. With `regions` (rtltrace.regions.RegionMap) TileLink tables also name the address region.
    With a Labeler that has plugins, modules / probes / ports / clients carry the plugins' friendly names (RtlName:
    the RTL identifier stays attached)."""
    tr = m.trace
    span_cycles = max(1.0, tr.end_ts - getattr(tr, "start_ts", 0.0))
    # waits and latencies are counted in the probe's (or port's) own clock; percentages divide by the span in that
    # same clock, so a module on a slower clock is not under-reported
    own_span = lambda period: max(1.0, span_cycles * tr.base_period / (period or tr.base_period))  # noqa: E731
    lab = lab or Labeler(regions=regions)
    short = lambda inst: lab.name("module", inst, tr.module_short(inst))  # noqa: E731
    probe = lambda p: RtlName(lab.display("probe", p.name) or p.name,  # noqa: E731
                              p.name + (f" ({p.meta['src']})" if p.meta.get("src") else ""))
    port_n = lambda pt: lab.name("port", pt.name)  # noqa: E731
    client_n = lambda c: lab.name("client", c) if c else c  # noqa: E731
    out: list[Table] = []

    # ------------------------------------------------------------------ per module: input vs output stall
    per_mod = defaultdict(lambda: {"in": 0, "out": 0, "in_n": 0, "out_n": 0})
    mod_period: dict[str, int] = {}
    for h in m.handshakes:
        mod_period[h.probe.inst] = h.probe.period
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
            mod_period.setdefault(port.inst, port.period)
            if a and a.direction in ("in", "out"):
                per_mod[port.inst][a.direction] += t.req_wait
                per_mod[port.inst][a.direction + "_n"] += 1
            if dd and dd.direction in ("in", "out") and t.resp_first is not None:
                per_mod[port.inst][dd.direction] += t.resp_wait
                per_mod[port.inst][dd.direction + "_n"] += 1
    rows = []
    for inst, s in per_mod.items():
        sp = own_span(mod_period.get(inst))
        ip, op = 100 * s["in"] / sp, 100 * s["out"] / sp
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
        sp = own_span(p.period)
        rows.append([short(p.inst), probe(p), p.direction or "-", len(hs), round(100 * len(hs) / sp, 1),
                     sum(waits), round(100 * sum(waits) / sp, 1), max(waits) if waits else 0,
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
            rows.append([short(port.inst), port_n(port), port.side, op, client_n(client), len(ts), pct(lat, .5), pct(lat, .95),
                         pct(lat, .99), max(lat) if lat else None, pct(tot, .5), max(tot) if tot else None,
                         round(nbytes / own_span(port.period), 2), sum(1 for t in ts if not t.done)])
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
        rows.append([short(port.inst), port_n(port), len(port.txns), peak, used, rng, len(port.orphans), port.at])
    out.append(Table("TileLink ports", ["module", "port", "transactions", "max in flight", "sources used",
                                        "source range", "orphan beats", "bound at"], rows,
                     "max in flight close to the source range means the client ran out of source IDs."))

    # ------------------------------------------------------------------ state probes
    rows = []
    per_state = defaultdict(lambda: defaultdict(float))
    for s in m.segments:
        name = lab.title(s.event)
        per_state[s.probe.key][name] += s.t1 - s.t0
    for key, d in per_state.items():
        p = tr.probes[key]
        total = sum(d.values()) or 1
        for v, c in sorted(d.items(), key=lambda kv: -kv[1]):
            rows.append([short(p.inst), probe(p), v, int(c), round(100 * c / total, 1)])
    out.append(Table("Time in each value (change probes)", ["module", "probe", "value", "cycles", "%"], rows))

    # ------------------------------------------------------------------ hangs and slowest
    rows = [[mk.kind, short(mk.probe.inst), probe(mk.probe), int(mk.t0), mk.cycles] for mk in m.markers]
    rows.sort(key=lambda r: (r[0] != "pending", -r[4]))
    out.append(Table("Hangs and long stalls", ["kind", "module", "probe", "from cycle", "cycles"], rows,
                     "pending = still waiting at the end of the trace; stall = waited at least the stall threshold."))
    txns = sorted((t for p in m.ports.values() for t in p.txns if t.total is not None), key=lambda t: -t.total)[:top]
    rlabel = (lambda a: RtlName(lab.region_label(r), r.label()) if (r := regions.lookup(a)) else "–") \
        if regions is not None else None  # noqa: E731
    out.append(Table(f"Slowest {top} TileLink transactions", [
        "module", "port", "opcode", "client", "source", "address"] + (["region"] if rlabel else []) +
        ["start cycle", "total", "latency"],
        [[short(t.port.inst), port_n(t.port), t.opcode, client_n(t.client), t.source, f"0x{t.address:x}"] +
         ([rlabel(t.address)] if rlabel else []) + [int(t.start), t.total, t.latency] for t in txns]))
    if regions is not None:
        by = defaultdict(list)
        for p in m.ports.values():
            for t in p.txns:
                if t.kind == "A":
                    r = regions.lookup(t.address)
                    by[(p.inst, p.name, r.name if r else "–", t.opcode)].append(t)
        rows = []
        for (inst, port, rg, op), ts in sorted(by.items(), key=lambda kv: (kv[0][0], kv[0][1], -len(kv[1]))):
            lat = sorted(t.latency for t in ts if t.latency is not None)
            q = (lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else None)  # noqa: E731
            rows.append([short(inst), port_n(ts[0].port), rg, op, len(ts), sum(t.nbytes for t in ts), q(0.5),
                         q(0.95)])
        out.append(Table("TileLink traffic by address region", ["module", "port", "region", "opcode", "transactions",
                                                                 "bytes", "latency p50", "latency p95"], rows,
                         f"Regions from {regions.source}."))
    hs = sorted(m.handshakes, key=lambda h: -h.wait)[:top]
    out.append(Table(f"Longest {top} handshake waits", ["module", "probe", "cycle", "wait"],
                     [[short(h.probe.inst), probe(h.probe), int(h.t1), h.wait] for h in hs if h.wait]))
    return out
