"""rtltrace plugin: hit/miss metrics of the REDEFINE LLC and L1-D caches.

rtltrace itself knows nothing about caches. The hit/miss decision is exported by RTL probes; this plugin knows what
to count:

  llc_access  (llc/Mshr.scala, group llc_cache)       one event per request, at its FIRST lookup. A miss is looked up
              hit isGet fromRpq tagMatch dirtyVictim   again after its refill; that always-hit lookup is not traced.
              fetchModify way source addr              fromRpq = waited in an MSHR's RPQ behind a miss to the same line.
  llc_refill / llc_writeback (Mshr)                    refill Get / dirty-victim writeback issued by an MSHR
  llc_cmu / llc_cmu_done (DistributedLlc)              flush/clean (cache maintenance) start and completion
  l1d_access  (L1Cache/NBDCache.scala, l1d_cache)      one event per accepted cacheable load/store (SPM, uncached and
              hit isWrite secondary addr               Orch maintenance commands excluded)
  l1d_cmu_wb                                           writeback caused by an L1-D maintenance command

Enable them in the sim with +rtltrace_llc_cache_on +rtltrace_l1d_cache_on (they are off by default).

Without those probes (traces captured before they existed) the plugin falls back to mode=infer: an LLC_in read is a
miss when a Get to the same 32 B line appears on LLC_out while it is outstanding. That is approximate: store misses
issue no Get here (write-allocate without fetch) and fetch-modify refills target the old line, so only reads are
classified and L1-D metrics are not available.

    python3 -m rtltrace report sim.log -o report.html --plugin cache_metrics.py \
        [--plugin-arg launch=<workload>/launch.py] [--plugin-arg log=sim.log] [--plugin-arg cache_json=cache.json]

Plugin args: launch (kernel names/count), log (retired instructions -> MPKI), cache_json (summary output),
mode=auto|probe|infer, llc_in / llc_out (port globs, default LLC?in / LLC?out), line (bytes, default 32),
bin (cycles per hit-ratio counter sample, default 1000), miss_marks=1 (one Perfetto instant per miss),
from= / to= (manual window).
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from collections import Counter, defaultdict
from fnmatch import fnmatch
from pathlib import Path

from rtltrace.plugin import HW_GROUP, Plugin, RtlName, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redefine_vocab as V  # noqa: E402
from redefine_common import (Names, _r, _ratio, _seg_at, ce_key, ce_label, ce_timelines, count_retired,  # noqa: E402
                             counters_in, kernel_windows, load_workload, phase_analysis, plugin_regions, publish,
                             section_of, window_names)

_MSHR = re.compile(r"\.mshrs(\.mshrs_\d+)?$")
_CORE = re.compile(r"\.core(?:_(\d+))?\.")
STATS = ["accesses", "hits", "misses", "read_misses", "write_misses", "merged", "dirty_victim", "fetch_modify",
         "refills", "writebacks", "expected_refills"]


def _llc_name(inst: str) -> str:
    return _MSHR.sub("", inst)


def _core_of(inst: str) -> int | None:
    mt = _CORE.search(inst)
    return None if mt is None else int(mt.group(1) or 0)


def _ratios(s: dict) -> dict:
    s["hit_ratio"] = _ratio(s["hits"], s["accesses"])
    s["miss_ratio"] = _ratio(s["misses"], s["accesses"])
    return s


class CacheMetrics(Plugin):
    name = "cache-metrics"

    def setup(self, ctx):
        a = ctx.args
        self.args = a
        self.warnings: list[str] = []
        self.kernels = [k.name for k in load_workload(a["launch"])] if a.get("launch") else []
        self.llc_in = a.get("llc_in", "LLC?in")
        self.llc_out = a.get("llc_out", "LLC?out")
        self.line = int(a.get("line", 32))
        self.bin = float(a.get("bin", 1000))
        rt = count_retired(a["log"]) if a.get("log") else None
        self.retired = dict(rt["retired_per_core"]) if rt else None
        have = {p.name for p in ctx.trace.probes.values()}
        mode = a.get("mode", "auto")
        self.mode = mode if mode != "auto" else ("probe" if {"llc_access", "l1d_access"} & have else "infer")
        self.regions = plugin_regions(ctx)
        self.results: list[dict] = []
        self.issues: list[dict] = []
        self.names = Names(a, self.regions, self.warnings, ctx.trace)
        self.src = {p.name: p.meta.get("src") for p in ctx.trace.probes.values()}

    # ------------------------------------------------------------------ windows
    def _windows(self, m) -> list[tuple[str, float, float]]:
        out = [("run", 0.0, m.end)]
        ces = ce_timelines(m)
        if ces or self.args.get("from") or self.args.get("to"):
            ws = kernel_windows(m, ces, max(1, len(self.kernels)), self.args, self.warnings)
            names = window_names(self.kernels, len(ws))
            out += [(n, w0, w1) for n, (w0, w1) in zip(names, ws)]
        return out

    def _ports(self, m, pattern):
        return [p for p in m.ports.values() if fnmatch(p.name, pattern) or fnmatch(f"{p.inst}/{p.name}", pattern)]

    def _client_port(self, m, cache_inst: str):
        """The LLC_in port in front of this LLC: the one whose module path is the longest prefix of the cache's."""
        ports = [p for p in self._ports(m, self.llc_in) if cache_inst.startswith(p.inst)]
        return max(ports, key=lambda p: len(p.inst)) if ports else None

    # ------------------------------------------------------------------ derive
    def derive(self, m, out):
        windows = self._windows(m)
        tls = ce_timelines(m)
        self.pa = phase_analysis(m, self.args, tls, []) if tls else None
        cmu = self._cmu_periods(m)
        if self.mode == "probe":
            llc_ev = sorted(m.events("llc_access"), key=lambda e: e.ts)
            l1d_ev = sorted(m.events("l1d_access"), key=lambda e: e.ts)
            refills = defaultdict(list)
            for e in m.events("llc_refill"):
                refills[_llc_name(e.probe.inst)].append(e.ts)
            wbs = defaultdict(list)
            for e in m.events("llc_writeback"):
                wbs[_llc_name(e.probe.inst)].append(e.ts)
            l1d_cmu_wb = defaultdict(list)
            for e in m.events("l1d_cmu_wb"):
                l1d_cmu_wb[e.probe.inst].append(e.ts)
            if not llc_ev:
                self.warnings.append("no llc_access events: run the sim with +rtltrace_llc_cache_on")
            if not l1d_ev:
                self.warnings.append("no l1d_access events: run the sim with +rtltrace_l1d_cache_on")
            llc = [(_llc_name(e.probe.inst), e.ts, e.vals) for e in llc_ev]
        else:
            llc, l1d_ev, refills, wbs, l1d_cmu_wb = self._infer(m), [], {}, {}, {}
            self.warnings.append("mode=infer: no llc_access probes in this trace. LLC read hit/miss is inferred from "
                                 "LLC_in/LLC_out (approximate); writes and the L1-D are not classified. Re-run the "
                                 "sim with +rtltrace_llc_cache_on +rtltrace_l1d_cache_on for exact numbers")

        out_gets = defaultdict(list)  # LLC instance -> start cycles of Gets on its LLC_out port
        for p in self._ports(m, self.llc_out):
            for t in p.txns:
                if t.kind == "A" and t.opcode == "Get":
                    out_gets[p.inst].append(t.start)

        for name, w0, w1 in windows:
            res = {"window": name, "from": w0, "to": w1, "llc": [], "llc_by_client": [], "l1d": [],
                   "cmu_ops": sum(1 for c0, _ in cmu if w0 <= c0 <= w1)}
            res["llc"], res["llc_by_client"] = self._llc_stats(m, llc, refills, wbs, out_gets, cmu, w0, w1)
            res["l1d"] = self._l1d_stats(m, l1d_ev, l1d_cmu_wb, w0, w1, name == "run")
            res["spm"] = self._spm_stats(m, w0, w1, name == "run")
            if self.regions is not None:
                res["llc_by_section"] = self._by_section(((ts, v) for _, ts, v in llc), w0, w1)
                res["l1d_by_section"] = self._by_section(((e.ts, e.vals) for e in l1d_ev), w0, w1)
            self.results.append(res)

        self._perfetto(m, out, llc, l1d_ev)
        publish(m, "cache", self.summary())
        if self.args.get("cache_json"):
            Path(self.args["cache_json"]).write_text(json.dumps(self.summary(), indent=2))

    def _cmu_periods(self, m) -> list[tuple[float, float]]:
        starts = sorted(e.ts for e in m.events("llc_cmu"))
        dones = sorted(e.ts for e in m.events("llc_cmu_done"))
        out = []
        for s in starts:
            i = bisect.bisect_left(dones, s)
            out.append((s, dones[i] if i < len(dones) else m.end))
        return out

    def _llc_stats(self, m, llc, refills, wbs, out_gets, cmu, w0, w1):
        per = defaultdict(lambda: Counter({k: 0 for k in STATS}))
        clients = defaultdict(lambda: Counter(accesses=0, hits=0, misses=0))
        for cache, ts, v in llc:
            if not w0 <= ts <= w1:
                continue
            s = per[cache]
            s["accesses"] += 1
            if v.get("hit"):
                s["hits"] += 1
            else:
                s["misses"] += 1
                s["read_misses" if v.get("isGet") else "write_misses"] += 1
                tm, fm = v.get("tagMatch", 0), v.get("fetchModify", 0)
                s["dirty_victim"] += bool(v.get("dirtyVictim"))
                s["fetch_modify"] += bool(fm)
                # Refill Gets the MSHR FSM issues for this miss: a read always fetches its line; a store fetches only
                # when the tag matches (partial line); a fetch-modify victim with another tag is fetched first.
                s["expected_refills"] += (1 if v.get("isGet") or tm else 0) + (1 if fm and not tm else 0)
            s["merged"] += bool(v.get("fromRpq"))
            port = self._client_port(m, cache)
            client = port.client_of(v["source"]) if port is not None and "source" in v else f"src{v.get('source')}"
            c = clients[(cache, client)]
            c["accesses"] += 1
            c["hits" if v.get("hit") else "misses"] += 1
        rows = []
        for cache in sorted(set(per) | set(refills)):
            s = per[cache]
            s["refills"] = sum(1 for t in refills.get(cache, ()) if w0 <= t <= w1)
            s["writebacks"] = sum(1 for t in wbs.get(cache, ()) if w0 <= t <= w1)
            row = {"cache": m.trace.module_short(cache), "rtl_path": cache, **_ratios(dict(s))}
            if self.mode == "probe":
                # LLC_out Gets not issued by an MSHR come from the CMU (maintenance), inside its busy periods.
                port_inst = max((pi for pi in out_gets if cache.startswith(pi)), key=len, default=None)
                gets = [t for t in out_gets.get(port_inst, ()) if w0 <= t <= w1]
                row["llc_out_gets"] = len(gets)
                row["llc_out_gets_in_cmu"] = sum(1 for t in gets if any(c0 <= t <= c1 for c0, c1 in cmu))
                self._check(row)
            rows.append(row)
        by_client = [{"cache": m.trace.module_short(cache), "client": cl, "client_name": str(self.names.client(cl)),
                      **_ratios(dict(c))}
                     for (cache, cl), c in sorted(clients.items(), key=lambda x: -x[1]["accesses"])]
        return rows, by_client

    def _spm_stats(self, m, w0, w1, warn: bool) -> list[dict]:
        """Scratchpad per CE (group l1d_spm): CE loads/stores, CR-DMA beats in, and contention between them."""
        per = defaultdict(Counter)
        for name, key in (("spm_ce_blocked", "ce_blocked_cycles"), ("spm_dma_waits", "dma_wait_cycles")):
            for e in m.events(name):
                if w0 <= e.ts <= w1:
                    per[e.probe.inst][key] += 1
        for e in m.events("spm_access"):
            if w0 <= e.ts <= w1:
                per[e.probe.inst]["stores" if e.vals.get("isWrite") else "loads"] += 1
        lat = defaultdict(list)
        for p in self._ports(m, "SPM?in"):
            for t in p.txns:
                if t.kind == "A" and w0 <= t.start <= w1:
                    per[p.inst]["dma_write_bytes" if t.opcode != "Get" else "dma_read_bytes"] += t.nbytes
                    if t.latency is not None:
                        lat[p.inst].append(t.latency)
        overlaps = sorted((e for e in m.events("spm_s3_overlap") if w0 <= e.ts <= w1), key=lambda e: e.ts)
        if overlaps and warn:
            self._overlap_issue(m, overlaps)
        for e in overlaps:
            per[e.probe.inst]["s3_overlap"] += 1
        rows = []
        for inst in sorted(per):
            c, ls = per[inst], sorted(lat.get(inst, []))
            rows.append({"cache": m.trace.module_short(inst), "rtl_path": inst, "core": _core_of(inst),
                         **{k: c.get(k, 0) for k in ("loads", "stores", "dma_write_bytes", "dma_read_bytes",
                                                     "ce_blocked_cycles", "dma_wait_cycles", "s3_overlap")},
                         "dma_beat_latency_p50": ls[len(ls) // 2] if ls else None})
        return rows

    def _by_section(self, items, w0, w1) -> list[dict]:
        per = defaultdict(lambda: Counter(accesses=0, hits=0, misses=0))
        for ts, v in items:
            if w0 <= ts <= w1:
                c = per[section_of(self.regions, v.get("addr")) or "–"]
                c["accesses"] += 1
                c["hits" if v.get("hit") else "misses"] += 1
        return [{"section": s, "name": str(self.names.section(s)) if s != "–" else s, **_ratios(dict(c))}
                for s, c in sorted(per.items(), key=lambda x: -x[1]["accesses"])]

    def _overlap_issue(self, m, evs):
        """spm_s3_overlap events -> one issue: count per CE, where (CE, PC region, address) the first one happened,
        and the RTL reference."""
        per_ce = Counter(ce_label(ce_key(e.probe.inst)) for e in evs if ce_key(e.probe.inst))
        regions = Counter()
        segs = {k: (v, [s.t0 for s in v]) for k, v in (self.pa or {}).get("phases", {}).items()}
        for e in evs:
            k = ce_key(e.probe.inst)
            sg = _seg_at(segs[k][0], segs[k][1], e.ts) if k in segs else None
            if sg is not None:
                regions[sg.label] += 1
        e0 = evs[0]
        addr = e0.vals.get("addr")
        sec = section_of(self.regions, addr) if addr is not None else None
        where = f"{self.names.section(sec)}" if sec and sec != "–" else (f"0x{addr:x}" if addr is not None else "?")
        r = self.regions.lookup(addr) if self.regions is not None and addr is not None else None
        if r is not None:
            where += f"+0x{r.offset:x}"
        k0 = ce_key(e0.probe.inst)
        issue = {"id": "spm_s3_overlap", "count": len(evs), "per_ce": dict(sorted(per_ce.items())),
                 "regions": {k: v for k, v in regions.most_common()}, "first_cycle": round(e0.ts),
                 "first_ce": ce_label(k0) if k0 else None, "first_addr": where, "rtl_path": e0.probe.inst,
                 "probe": "spm_s3_overlap", "src": self.src.get("spm_s3_overlap")}
        self.issues.append(issue)
        top = ", ".join(f"{V.region_name(k)} ×{v}" for k, v in regions.most_common(3))
        self.warnings.append(
            f"Scratchpad hazard: {len(evs)} CE loads hit a scratchpad while a DMA write into it was in flight "
            f"({', '.join(f'{c} ×{n}' for c, n in sorted(per_ce.items()))}); first at cycle {issue['first_cycle']} on "
            f"{issue['first_ce']} at {where}{f'; in regions {top}' if top else ''}. Such a load may return the DMA's "
            f"data instead of the old value. [RTL: {e0.probe.inst} · spm_s3_overlap · {issue['src']}]")

    def _check(self, row):
        what = f"{row['cache']}"
        if row["refills"] != row["expected_refills"]:
            self.warnings.append(f"{what}: {row['refills']} MSHR refills but the traced misses imply "
                                 f"{row['expected_refills']}: llc_access semantics or window edge")
        mshr_gets = row["llc_out_gets"] - row["llc_out_gets_in_cmu"]
        if mshr_gets != row["refills"]:
            self.warnings.append(f"{what}: {mshr_gets} LLC_out Gets outside maintenance but {row['refills']} MSHR "
                                 "refills")
        if row["writebacks"] != row["dirty_victim"]:
            self.warnings.append(f"{what}: {row['writebacks']} MSHR writebacks but {row['dirty_victim']} "
                                 "dirty-victim misses")

    def _l1d_stats(self, m, l1d_ev, l1d_cmu_wb, w0, w1, whole_run):
        per = defaultdict(Counter)
        for e in l1d_ev:
            if not w0 <= e.ts <= w1:
                continue
            s, v = per[e.probe.inst], e.vals
            s["accesses"] += 1
            if v.get("hit"):
                s["hits"] += 1
            else:
                s["misses"] += 1
                s["write_misses" if v.get("isWrite") else "read_misses"] += 1
                s["secondary"] += bool(v.get("secondary"))
        dcache_gets = Counter()  # D$ refill Gets seen at the LLC, per core
        for p in self._ports(m, self.llc_in):
            for t in p.txns:
                mt = re.match(r"Core (\d+) DCache$", t.client)
                if mt and t.kind == "A" and t.opcode == "Get" and w0 <= t.start <= w1:
                    dcache_gets[int(mt.group(1))] += 1
        rows = []
        for inst in sorted(per):
            s, core = per[inst], _core_of(inst)
            row = {"cache": m.trace.module_short(inst), "rtl_path": inst, "core": core,
                   **_ratios({k: s[k] for k in ("accesses", "hits", "misses", "read_misses", "write_misses",
                                                "secondary")}),
                   "cmu_writebacks": sum(1 for t in l1d_cmu_wb.get(inst, ()) if w0 <= t <= w1),
                   "llc_in_dcache_gets": dcache_gets.get(core, 0) if core is not None else None,
                   "mpki": None}
            if whole_run and self.retired and core in self.retired and self.retired[core]:
                row["mpki"] = 1000 * s["misses"] / self.retired[core]
            elif self.pa and ce_key(inst) in self.pa["points"]:  # ce_marker instret counters, any window
                ir = counters_in(self.pa["points"][ce_key(inst)], w0, w1)
                if ir and ir[0]:
                    row["mpki"] = 1000 * s["misses"] / ir[0]
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ infer mode (no cache probes)
    def _infer(self, m) -> list[tuple[str, float, dict]]:
        mask = ~(self.line - 1)
        fills = defaultdict(list)  # (llc port inst, line) -> [start, used]
        for p in self._ports(m, self.llc_out):
            for t in sorted(p.txns, key=lambda t: t.start):
                if t.kind == "A" and t.opcode == "Get":
                    fills[(p.inst, t.address & mask)].append([t.start, False])
        out = []
        for p in self._ports(m, self.llc_in):
            for t in sorted(p.txns, key=lambda t: t.start):
                if t.kind != "A" or t.opcode != "Get":
                    continue
                until = t.resp_first if t.resp_first is not None else m.end
                hit = True
                for f in fills.get((p.inst, t.address & mask), ()):
                    if not f[1] and t.start <= f[0] <= until:
                        f[1], hit = True, False
                        break
                out.append((p.inst, t.start, {"hit": int(hit), "isGet": 1, "source": t.source, "addr": t.address}))
        return out

    # ------------------------------------------------------------------ perfetto
    def _perfetto(self, m, out, llc, l1d_ev):
        proc = out.process("Cache hit rates", sort=4)
        series = defaultdict(list)
        for cache, ts, v in llc:
            series[str(self.names.module(cache, "LLC"))].append((ts, v.get("hit", 0)))
        for e in l1d_ev:
            k = ce_key(e.probe.inst)
            series[f"{ce_label(k) if k else m.trace.module_short(e.probe.inst)} L1-D"].append(
                (e.ts, e.vals.get("hit", 0)))
        marks = self.args.get("miss_marks") in ("1", "true", "yes")
        for name, pts in sorted(series.items()):
            bins = defaultdict(lambda: [0, 0])
            for ts, hit in pts:
                b = bins[int(ts // self.bin)]
                b[0] += 1
                b[1] += hit
            out.counter(proc, f"hit % · {name}",
                        [(k * self.bin, 100.0 * h / n) for k, (n, h) in sorted(bins.items())])
            if marks:
                tr = out.track(proc, f"misses · {name}")
                for ts, hit in pts:
                    if not hit:
                        out.instant(tr, ts, "miss")

    # ------------------------------------------------------------------ outputs
    def summary(self) -> dict:
        return {"mode": self.mode, "windows": self.results, "issues": self.issues, "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        G, b = V.G_MEM
        kern = [r for r in self.results if r["window"] != "run"] or self.results
        run = [r for r in self.results if r["window"] == "run"]
        out = []
        llc = lambda s_: self.names.module(s_["rtl_path"], "LLC") if s_.get("rtl_path") else s_["cache"]  # noqa
        ce = lambda s_: RtlName(f"CE{s_['core']}", s_.get("rtl_path", s_["cache"])) if s_.get("core") is not None \
            else s_["cache"]  # noqa: E731
        if self.regions is not None:
            rows = [[r["window"], self.names.section(s_["section"]) if s_["section"] != "–" else "other",
                     s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"])]
                    for r in kern for s_ in r.get("llc_by_section", [])]
            if rows:
                out.append(Section("LLC hit/miss by buffer / memory section",
                                   ["kernel", "buffer / section", "accesses", "hits", "misses", "hit %"], rows,
                                   "A kernel input read once from memory misses on first touch (hit % ≈ 0 is "
                                   "expected); a buffer read repeatedly should hit.",
                                   legend=V.legend("accesses / hits / misses", "hit %"), group=G, order=b,
                                   key="Cache metrics · LLC hit/miss by kernel section"))
        rows = [[r["window"], self.names.client(s_["client"]), s_["accesses"], s_["hits"], s_["misses"],
                 _pct(s_["hit_ratio"])] for r in kern for s_ in r["llc_by_client"]]
        out.append(Section("LLC hit/miss by requester", ["kernel", "requester", "accesses", "hits", "misses",
                                                         "hit %"], rows,
                           legend=V.legend("accesses / hits / misses", "hit %"), group=G, order=b + 1,
                           key="Cache metrics · LLC hit/miss by requester"))
        cols = ["kernel", "cache", "accesses", "hits", "misses", "hit %", "read misses", "write misses", "merged",
                "dirty victim", "refills", "writebacks", "flush/clean ops"]
        rows = [[r["window"], llc(s_), s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"]),
                 s_["read_misses"], s_["write_misses"], s_["merged"], s_["dirty_victim"], s_["refills"],
                 s_["writebacks"], r["cmu_ops"]] for r in kern for s_ in r["llc"]]
        if self.mode != "probe":
            cols, rows = cols[:8], [r_[:8] for r_ in rows]
        out.append(Section("LLC summary", cols, rows,
                           "Counted at each request's first lookup." if self.mode == "probe" else
                           "Inferred (no llc_cache probes): reads only; a miss = a refill Get for the same line "
                           "while the read was pending. Re-run with +rtltrace_llc_cache_on for exact numbers.",
                           legend=V.legend("accesses / hits / misses", "hit %", "merged", "dirty victim",
                                           "refills / writebacks"),
                           group=G, order=b + 2, key="Cache metrics · LLC summary"))
        if self.mode == "probe":
            rows = [[r["window"], ce(s_), s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"]),
                     s_["read_misses"], s_["write_misses"], s_["secondary"], s_["llc_in_dcache_gets"],
                     s_["cmu_writebacks"], _r(s_["mpki"])] for r in kern for s_ in r["l1d"]]
            out.append(Section("L1-D hit/miss per CE",
                               ["kernel", "CE", "accesses", "hits", "misses", "hit %", "read misses", "write misses",
                                "secondary misses", "L1-D refills at LLC", "flush writebacks", "MPKI"], rows,
                               "Cacheable CE loads/stores only: scratchpad, uncached and maintenance accesses are not "
                               "counted. MPKI uses the CE's retired instructions in the same window.",
                               legend=V.legend("accesses / hits / misses", "hit %", "secondary misses", "MPKI"),
                               group=G, order=b + 3, key="Cache metrics · L1-D hit/miss per CE"))
            if self.regions is not None:
                rows = [[r["window"], self.names.section(s_["section"]) if s_["section"] != "–" else "other",
                         s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"])]
                        for r in kern for s_ in r.get("l1d_by_section", [])]
                if rows:
                    out.append(Section("L1-D hit/miss by buffer / memory section",
                                       ["kernel", "buffer / section", "accesses", "hits", "misses", "hit %"], rows,
                                       group=G, order=b + 4, key="Cache metrics · L1-D hit/miss by kernel section"))
        rows = [[r["window"], ce(s_), s_["loads"], s_["stores"], s_["dma_write_bytes"], s_["dma_read_bytes"],
                 s_["dma_beat_latency_p50"], s_["ce_blocked_cycles"], s_["dma_wait_cycles"], s_["s3_overlap"]]
                for r in kern for s_ in r.get("spm", [])]
        if rows:
            out.append(Section("Scratchpad: CE and DMA sharing it",
                               ["kernel", "CE", "CE loads", "CE stores", "DMA bytes in", "DMA bytes out",
                                "DMA beat latency p50", "CE stalled by DMA (cycles)", "DMA stalled by CE (cycles)",
                                "CE reads during DMA write"], rows,
                               "Group l1d_spm (+rtltrace_l1d_spm_on). The CE and the CR-DMA share each CE's "
                               "scratchpad port.",
                               legend=V.legend("CE stalled by DMA", "DMA stalled by CE", "CE reads during DMA write"),
                               group=G, order=b + 5, key="Cache metrics · scratchpad per CE"))
        # hardware detail: full LLC counters (with the refill / writeback consistency inputs) for every window
        cols = ["window", "cache", "accesses", "hits", "misses", "hit %", "miss %", "read misses", "write misses",
                "merged", "dirty victim", "fetch-modify", "refills", "expected refills", "writebacks", "CMU ops"]
        rows = [[r["window"], llc(s_), s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"]),
                 _pct(s_["miss_ratio"]), s_["read_misses"], s_["write_misses"], s_["merged"], s_["dirty_victim"],
                 s_["fetch_modify"], s_["refills"], s_["expected_refills"], s_["writebacks"], r["cmu_ops"]]
                for r in self.results for s_ in r["llc"]]
        if self.mode != "probe":
            cols, rows = cols[:7], [r_[:7] for r_ in rows]
        out.append(Section(f"LLC hit/miss, all windows ({self.mode})", cols, rows,
                           "'run' = the whole trace including loading inputs and reading results back. expected "
                           "refills = Gets the traced misses must issue; it should equal refills.",
                           group=HW_GROUP, order=920, level="hw", key=f"Cache metrics · LLC hit/miss ({self.mode})"))
        if run and self.mode == "probe":
            rows = [[r["window"], ce(s_), s_["accesses"], s_["hits"], s_["misses"], _pct(s_["hit_ratio"]),
                     _r(s_["mpki"])] for r in run for s_ in r["l1d"]]
            out.append(Section("L1-D hit/miss over the whole run", ["window", "CE", "accesses", "hits", "misses",
                                                                    "hit %", "MPKI"], rows,
                               group=HW_GROUP, order=920.1, level="hw", key="Cache metrics · L1-D whole run"))
        if self.warnings:
            G, b = V.G_CHECK
            out.append(Section("Tool warnings · caches", ["warning"], [[w] for w in self.warnings],
                               group=G, order=b + 13, key="Cache metrics · warnings"))
        return out


def _pct(x):
    return None if x is None else _r(100 * x, 4)
