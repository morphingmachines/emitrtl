"""C6 · Perfetto view: Chrome Trace Event JSON for ui.perfetto.dev (opens locally, nothing is uploaded).

Time axis: 1 us = 1 cycle of the fastest traced clock.
  process  = module instance (sorted by hierarchy); a plugin's friendly name with the RTL path in brackets.
             Instances a plugin gives the same name (e.g. the 16 MSHRs of a cache) share one process.
  thread   = probe (handshake / valid / event slices, state timelines) or TileLink lane (one per source ID)
  counter  = in-flight transactions per TileLink port
  flow     = correlation links (TL hops, user links, plugin flows)
Every module slice carries rtl_path / probe / chisel_src args, so any slice leads back to the RTL (named so that
they never hide a probe field such as dma_start's src address).

view="kernel" keeps only the plugin processes and the module probes matching the plugins' `perfetto_keep` globs
(a small file for software developers); view="full" (default) keeps everything.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from fnmatch import fnmatch

from .labels import Labeler
from .model import Model, Txn
from .plugin import Emit
from .reader import Event

KIND_ORDER = {"change": 0, "fire": 1, "valid": 2, "event": 3}


class Writer:
    def __init__(self, m: Model, lab: Labeler, emitted: Emit | None = None, view: str = "full",
                 keep: list[str] | tuple = ()):
        self.m, self.lab, self.emitted = m, lab, emitted
        self.view, self.keep = view, list(keep)
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
    def _kept(self, name: str) -> bool:
        return self.view != "kernel" or any(fnmatch(name, g) for g in self.keep)

    def build(self) -> dict:
        m, tr, lab = self.m, self.m.trace, self.lab
        insts = sorted({p.inst for p in tr.probes.values()})
        groups: dict[str, list[str]] = {}  # process name -> instances (a plugin may give several the same name)
        for inst in insts:
            groups.setdefault(lab.display("module", inst) or tr.module_short(inst), []).append(inst)
        self.tids: dict = {}
        sort = 10
        for name, members in groups.items():
            probes = sorted((p for p in tr.probes.values() if p.inst in members and not p.is_tl and self._kept(p.name)),
                            key=lambda p: (KIND_ORDER.get(p.kind, 9), p.name, p.inst))
            ports = sorted((pt for pt in m.ports.values() if pt.inst in members and self._kept(pt.name)),
                           key=lambda pt: (pt.name, pt.inst))
            if not probes and not ports:
                continue
            shorts = [tr.module_short(i) for i in members]
            rtl = shorts[0] if len(shorts) == 1 else _common(shorts)
            pid = self._proc(name if name == rtl else f"{name}  [{rtl}]", sort)
            sort += 1
            tids = {}
            for j, p in enumerate(probes):
                tids[p.key] = 1 + j
                pn = lab.display("probe", p.name)
                tn = f"{pn} ({p.name})" if pn and pn != p.name else p.name
                if len(members) > 1:
                    tn = f"{tr.module_short(p.inst)[len(rtl.rstrip('*')):].lstrip('.') or p.inst} · {tn}"
                self._thread(pid, 1 + j, tn, j)
            self.tids.update(tids)
            self._probes(pid, tids)
            base = 1000
            for pt in ports:
                # instances grouped into one process may have ports of the same name: tell their tracks and
                # counters apart by the instance
                same = sum(1 for q in ports if q.name == pt.name) > 1
                who = tr.module_short(pt.inst)[len(rtl.rstrip('*')):].lstrip('.') or tr.module_short(pt.inst)
                base = self._port(pid, pt, base, who if same else "") + 1000
            for inst in members:
                self._markers(pid, inst, tids)
        if self.emitted is not None:
            self._plugin_items()
        self._links()  # after the plugin spans, so links that end on one resolve
        return dict(traceEvents=self.ev,
                    otherData=dict(generator="rtltrace", view=self.view,
                                   time_axis="1 us = 1 cycle of the fastest traced clock", warnings=tr.warnings))

    @staticmethod
    def _rtl(p) -> dict:
        return dict(rtl_path=p.inst, probe=p.name, **({"chisel_src": p.meta["src"]} if p.meta.get("src") else {}))

    def _probes(self, pid: int, tids: dict):
        m, tr, lab = self.m, self.m.trace, self.lab
        for h in m.handshakes:
            tid = tids.get(h.probe.key)
            if tid is None:
                continue
            per = h.probe.period / tr.base_period
            args = _merge(lab.fields(h.event), cycle=h.event.cycle, wait=h.wait, **self._rtl(h.probe))
            self._x(pid, tid, h.t0, h.t1 - h.t0 + per, lab.title(h.event), h.probe.groups[0], args)
            self.pos[id(h.event)] = (pid, tid, h.t0)
        for e in m.pulses:
            tid = tids.get(e.probe.key)
            if tid is None:
                continue
            args = _merge(lab.fields(e), cycle=e.cycle, **self._rtl(e.probe))
            self._x(pid, tid, e.ts, e.probe.period / tr.base_period, lab.title(e), e.probe.groups[0], args)
            self.pos[id(e)] = (pid, tid, e.ts)
        for s in m.segments:
            tid = tids.get(s.probe.key)
            if tid is None:
                continue
            args = _merge(lab.fields(s.event), cycle=s.event.cycle,
                          cycles=int(round((s.t1 - s.t0) * tr.base_period / s.probe.period)), **self._rtl(s.probe))
            self._x(pid, tid, s.t0, s.t1 - s.t0, lab.title(s.event), s.probe.groups[0], args)
            self.pos[id(s.event)] = (pid, tid, s.t0)

    def _port(self, pid: int, pt, tid: int, who_inst: str = "") -> int:
        """One lane per source ID; a transaction that overlaps the previous one on its source (same-cycle source
        reuse) goes to an overflow sub-lane so slices still nest."""
        lab, m = self.lab, self.m
        lane_end: dict[tuple[int, int], float] = {}
        lane_tid: dict[tuple[int, int], int] = {}
        per = pt.period / pt.base
        first_tid = tid + 1
        pname = (lab.display("port", pt.name) or pt.name) + (f" · {who_inst}" if who_inst else "")

        def lane(src: int, t0: float, t1: float, kind: str) -> int:
            nonlocal tid
            k = 0
            while lane_end.get((src, k), -1) > t0:
                k += 1
            if (src, k) not in lane_tid:
                tid += 1
                lane_tid[(src, k)] = tid
                who = pt.client_of(src) if kind != "B" else "probe"
                who = lab.display("client", who) or who
                self._thread(pid, tid, f"{pname} · {who} · src {src}" + (f" ({'abcdefgh'[k % 8]})" if k else ""),
                             tid)
            lane_end[(src, k)] = t1
            return lane_tid[(src, k)]

        for t in sorted(pt.txns, key=lambda t: t.start):
            t0 = t.start
            t1 = t.end if t.end is not None else self.end
            ln = lane(t.source, t0, t1, t.kind)
            addr, addr_long = lab.addr(t.address)
            cov = lab.covers(t.address, t.nbytes)
            name = f"{t.opcode} {addr}" + ("" if t.end is not None else "  (no response)")
            args = dict(port=pt.name, side=pt.side, client=t.client, source=t.source, address=addr_long,
                        bytes=t.nbytes, request_wait=t.req_wait, latency=t.latency, total=t.total,
                        response=t.resp_opcode, rtl_path=pt.inst, bound_at=pt.at)
            rg = lab.region(t.address)
            if rg is not None:
                args.update(region=rg.name, region_offset=f"0x{rg.offset:x}",
                            **{("region_id" if k == "region" else k): v for k, v in rg.fields.items()},
                            **{k: v for k, v in rg.attrs.items() if k in ("scope", "caching", "access")})
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
            self._x(pid, ln, t.req_first, t.req_last - t.req_first + per,
                    f"{'request' if t.kind == 'A' else t.kind} ({_beats(t.req_beats)})", "tl")
            if t.resp_first is not None:
                self._x(pid, ln, t.resp_first, t.resp_last - t.resp_first + per,
                        f"{t.resp_opcode} ({_beats(max(t.resp_beats, 1))})", "tl")
        for ts, level in m.inflight(pt):
            self._c(pid, f"in flight · {pname}", ts, level)
        for e in pt.orphans:
            self._i(pid, first_tid, e.ts, f"orphan {e.probe.channel.upper()} beat", {"source": e.vals.get("source")})
        return tid

    def _markers(self, pid: int, inst: str, tids: dict):
        for mk in self.m.markers:
            if mk.probe.inst != inst:
                continue
            if not self._kept(mk.probe.name) and not self._kept(mk.probe.port or ""):
                continue
            tid = tids.get(mk.probe.key, 1000)
            pn = self.lab.display("probe", mk.probe.name) or mk.probe.name
            what = f"⚠ {pn} stalled {mk.cycles} cycles" if mk.kind == "stall" else \
                f"⏸ {pn} pending {mk.cycles} cycles at end"
            self._i(pid, tid, mk.t0, what, dict(kind=mk.kind, **self._rtl(mk.probe)))

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
        drawn = {id(ln) for ln in self.m.links}  # the session already merged plugin links into m.links
        for ln in em.links:
            if id(ln) in drawn:
                continue
            a, b = self.pos.get(id(ln.src)), self.pos.get(id(ln.dst))
            if a and b:
                self._flow([a, b], ln.kind, {"why": ln.why})


def _merge(fields: dict, **meta) -> dict:
    """Slice args: the probe's decoded fields plus rtltrace's own keys. A field whose name collides with one of
    those keys (a probe field called `cycle`, `wait`, `probe`, ...) is kept as `field.<name>`."""
    args = {(f"field.{k}" if k in meta else k): v[1] for k, v in fields.items()}
    args.update(meta)
    return args


def _n(x: float):
    if not math.isfinite(x):
        return None
    r = round(x, 3)
    return int(r) if r == int(r) else r


def _beats(n: int) -> str:
    return f"{n} beat" if n == 1 else f"{n} beats"


def _common(paths: list[str]) -> str:
    """Common dotted prefix of instance paths, with a trailing '*': baseCr.uncore.cache.mshrs.*"""
    parts = [p.split(".") for p in paths]
    pre = []
    for xs in zip(*parts):
        if len(set(xs)) != 1:
            break
        pre.append(xs[0])
    return ".".join(pre) + ".*" if pre else "*"


def export(m: Model, lab: Labeler, path: str, emitted: Emit | None = None, view: str = "full",
           keep: list[str] | tuple = ()) -> dict:
    doc = Writer(m, lab, emitted, view, keep).build()
    # serialise first, so a bad value never leaves a truncated file behind. Plugin args may carry NaN/inf (not
    # valid JSON: Perfetto rejects the file) or non-JSON types such as sets: those become null / strings.
    try:
        text = json.dumps(doc, separators=(",", ":"), allow_nan=False, default=_jsonable)
    except ValueError:
        text = json.dumps(_finite(doc), separators=(",", ":"), allow_nan=False, default=_jsonable)
    with open(path, "w") as f:
        f.write(text)
    return doc


def _jsonable(x):
    if isinstance(x, (set, frozenset)):
        return sorted(x, key=str)
    return str(x)


def _finite(x):
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x
