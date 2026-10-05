"""C6 · Perfetto view: Chrome Trace Event JSON for ui.perfetto.dev (opens locally, nothing is uploaded).

Time axis: 1 us = 1 cycle of the fastest traced clock.
  process  = module instance (sorted by hierarchy)
  thread   = probe (handshake / valid / event slices, state timelines) or TileLink lane (one per source ID)
  counter  = in-flight transactions per TileLink port
  flow     = correlation links (TL hops, user links, plugin flows)
"""

from __future__ import annotations

import json
from collections import defaultdict

from .labels import Labeler
from .model import Model, Txn
from .plugin import Emit
from .reader import Event

KIND_ORDER = {"change": 0, "fire": 1, "valid": 2, "event": 3}


class Writer:
    def __init__(self, m: Model, lab: Labeler, emitted: Emit | None = None):
        self.m, self.lab, self.emitted = m, lab, emitted
        self.ev: list[dict] = []
        self.pos: dict[int, tuple[int, int, float]] = {}  # id(Event|Txn) or plugin span id -> (pid, tid, ts)
        self.flow_id = 0
        self.npid = 0
        self.end = m.end

    def _proc(self, name: str, sort: int) -> int:
        self.npid += 1
        pid = self.npid
        self.ev.append(dict(ph="M", name="process_name", pid=pid, args=dict(name=name)))
        self.ev.append(dict(ph="M", name="process_sort_index", pid=pid, args=dict(sort_index=sort)))
        return pid

    def _thread(self, pid: int, tid: int, name: str, sort: int):
        self.ev.append(dict(ph="M", name="thread_name", pid=pid, tid=tid, args=dict(name=name)))
        self.ev.append(dict(ph="M", name="thread_sort_index", pid=pid, tid=tid, args=dict(sort_index=sort)))

    def _x(self, pid, tid, ts, dur, name, cat="", args=None):
        self.ev.append(dict(ph="X", pid=pid, tid=tid, ts=_n(ts), dur=_n(max(dur, 0.001)), name=name, cat=cat,
                            args=args or {}))

    def _i(self, pid, tid, ts, name, args=None):
        self.ev.append(dict(ph="i", s="t", pid=pid, tid=tid, ts=_n(ts), name=name, args=args or {}))

    def _c(self, pid, name, ts, value):
        self.ev.append(dict(ph="C", pid=pid, ts=_n(ts), name=name, args={"value": value}))

    def _flow(self, points: list[tuple[int, int, float]], name: str, args=None):
        points = [p for p in points if p]
        if len(points) < 2:
            return
        self.flow_id += 1
        for k, (pid, tid, ts) in enumerate(points):
            ph = "s" if k == 0 else ("f" if k == len(points) - 1 else "t")
            e = dict(ph=ph, id=self.flow_id, pid=pid, tid=tid, ts=_n(ts), name=name, cat="flow")
            if ph != "s":
                e["bp"] = "e"
            if args and ph == "s":
                e["args"] = args
            self.ev.append(e)

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        m, tr = self.m, self.m.trace
        insts = sorted({p.inst for p in tr.probes.values()})
        for k, inst in enumerate(insts):
            pid = self._proc(tr.module_short(inst), 10 + k)
            probes = sorted((p for p in tr.probes.values() if p.inst == inst and not p.is_tl),
                            key=lambda p: (KIND_ORDER.get(p.kind, 9), p.name))
            tids = {}
            for j, p in enumerate(probes):
                tids[p.key] = 1 + j
                self._thread(pid, 1 + j, p.name, j)
            self._probes(pid, tids)
            ports = sorted((pt for pt in m.ports.values() if pt.inst == inst), key=lambda pt: pt.name)
            base = 1000
            for pt in ports:
                base = self._port(pid, pt, base) + 1000
            self._markers(pid, inst, tids)
        self._links()
        if self.emitted is not None:
            self._plugin_items()
        return dict(traceEvents=self.ev,
                    otherData=dict(generator="rtltrace", time_axis="1 us = 1 cycle of the fastest traced clock",
                                   warnings=tr.warnings))

    def _probes(self, pid: int, tids: dict):
        m, tr, lab = self.m, self.m.trace, self.lab
        for h in m.handshakes:
            tid = tids.get(h.probe.key)
            if tid is None:
                continue
            per = h.probe.period / tr.base_period
            args = {k: v[1] for k, v in lab.fields(h.event).items()}
            args.update(cycle=h.event.cycle, wait=h.wait)
            self._x(pid, tid, h.t0, h.t1 - h.t0 + per, lab.title(h.event), h.probe.groups[0], args)
            self.pos[id(h.event)] = (pid, tid, h.t0)
        for e in m.pulses:
            tid = tids.get(e.probe.key)
            if tid is None:
                continue
            args = {k: v[1] for k, v in lab.fields(e).items()}
            args["cycle"] = e.cycle
            self._x(pid, tid, e.ts, e.probe.period / tr.base_period, lab.title(e), e.probe.groups[0], args)
            self.pos[id(e)] = (pid, tid, e.ts)
        for s in m.segments:
            tid = tids.get(s.probe.key)
            if tid is None:
                continue
            args = {k: v[1] for k, v in lab.fields(s.event).items()}
            args.update(cycle=s.event.cycle, cycles=int(round((s.t1 - s.t0) * tr.base_period / s.probe.period)))
            self._x(pid, tid, s.t0, s.t1 - s.t0, lab.title(s.event), s.probe.groups[0], args)
            self.pos[id(s.event)] = (pid, tid, s.t0)

    def _port(self, pid: int, pt, tid: int) -> int:
        """One lane per source ID; a transaction that overlaps the previous one on its source (same-cycle source
        reuse) goes to an overflow sub-lane so slices still nest."""
        lab, m = self.lab, self.m
        lane_end: dict[tuple[int, int], float] = {}
        lane_tid: dict[tuple[int, int], int] = {}
        per = pt.period / pt.base
        first_tid = tid + 1

        def lane(src: int, t0: float, t1: float, kind: str) -> int:
            nonlocal tid
            k = 0
            while lane_end.get((src, k), -1) > t0:
                k += 1
            if (src, k) not in lane_tid:
                tid += 1
                lane_tid[(src, k)] = tid
                who = pt.client_of(src) if kind != "B" else "probe"
                self._thread(pid, tid, f"{pt.name} · {who} · src {src}" + (f" ({'abcdefgh'[k % 8]})" if k else ""),
                             tid)
            lane_end[(src, k)] = t1
            return lane_tid[(src, k)]

        for t in sorted(pt.txns, key=lambda t: t.start):
            t0 = t.start
            t1 = t.end if t.end is not None else self.end
            ln = lane(t.source, t0, t1, t.kind)
            addr = lab.sym(t.address)
            cov = lab.covers(t.address, t.nbytes)
            name = f"{t.opcode} {addr}" + ("" if t.end is not None else "  (no response)")
            args = dict(port=pt.name, side=pt.side, client=t.client, source=t.source, address=f"{addr} [0x{t.address:x}]",
                        bytes=t.nbytes, request_wait=t.req_wait, latency=t.latency, total=t.total,
                        response=t.resp_opcode, bound_at=pt.at)
            mgr = pt.manager_of(t.address)
            if mgr:
                args["manager"] = mgr
            if cov:
                args["covers"] = cov
            if t.denied:
                args["denied"] = True
            self._x(pid, ln, t0, t1 - t0, name, "tl", args)
            self.pos[id(t)] = (pid, ln, t0)
            if t.req_wait:
                self._x(pid, ln, t0, t.req_first - t0, "request wait (backpressure)", "tl")
            self._x(pid, ln, t.req_first, t.req_last - t.req_first + per, f"{'A' if t.kind == 'A' else t.kind} ×{t.req_beats}", "tl")
            if t.resp_first is not None:
                self._x(pid, ln, t.resp_first, t.resp_last - t.resp_first + per, f"{t.resp_opcode} ×{max(t.resp_beats, 1)}", "tl")
        for ts, level in m.inflight(pt):
            self._c(pid, f"in flight · {pt.name}", ts, level)
        for e in pt.orphans:
            self._i(pid, first_tid, e.ts, f"orphan {e.probe.channel.upper()} beat", {"source": e.vals.get("source")})
        return tid

    def _markers(self, pid: int, inst: str, tids: dict):
        for mk in self.m.markers:
            if mk.probe.inst != inst:
                continue
            tid = tids.get(mk.probe.key, 1000)
            what = f"⚠ stall {mk.cycles} cycles" if mk.kind == "stall" else f"⏸ pending {mk.cycles} cycles at end"
            self._i(pid, tid, mk.t0, what, {"probe": mk.probe.name, "kind": mk.kind})

    def _links(self):
        for ln in self.m.links:
            a, b = self.pos.get(id(ln.src)), self.pos.get(id(ln.dst))
            if a and b:
                self._flow([a, b], ln.kind, {"why": ln.why, "heuristic": ln.heuristic})

    def _plugin_items(self):
        em = self.emitted
        pmap, tmap = {}, {}
        for p in sorted(em.procs, key=lambda p: p.sort):
            pmap[p.id] = self._proc(p.name, p.sort - 1000)
        for t in em.tracks:
            tmap[t.id] = (pmap[t.proc], t.id)
            self._thread(pmap[t.proc], t.id, t.name, t.sort)
        for s in em.spans:
            pid, tid = tmap[s.track]
            self._x(pid, tid, s.t0, s.t1 - s.t0, s.name, f"plugin:{s.args.get('plugin', '')}", s.args)
            self.pos[s.id] = (pid, tid, s.t0)
        for tr_id, t, name, args in em.instants:
            pid, tid = tmap[tr_id]
            self._i(pid, tid, t, name, args)
        for proc, name, pts in em.counters:
            for t, v in pts:
                self._c(pmap[proc], name, t, v)
        for spans, name in em.flows:
            self._flow([self.pos.get(s) for s in spans], name)
        for ln in em.links:
            a, b = self.pos.get(id(ln.src)), self.pos.get(id(ln.dst))
            if a and b:
                self._flow([a, b], ln.kind, {"why": ln.why})


def _n(x: float):
    r = round(x, 3)
    return int(r) if r == int(r) else r


def export(m: Model, lab: Labeler, path: str, emitted: Emit | None = None) -> dict:
    doc = Writer(m, lab, emitted).build()
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    return doc
