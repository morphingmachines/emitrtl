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

from rtltrace.plugin import Plugin, Section

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redefine_common import (_r, _ratio, ce_timelines, count_retired, kernel_windows, load_workload,  # noqa: E402
                             plugin_regions, section_of)

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

    # ------------------------------------------------------------------ windows
    def _windows(self, m) -> list[tuple[str, float, float]]:
        out = [("run", 0.0, m.end)]
        ces = ce_timelines(m)
        if ces or self.args.get("from") or self.args.get("to"):
            ws = kernel_windows(m, ces, max(1, len(self.kernels)), self.args, self.warnings)
            names = self.kernels if len(self.kernels) == len(ws) else [f"kernel{i}" for i in range(len(ws))]
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
            row = {"cache": m.trace.module_short(cache), **_ratios(dict(s))}
            if self.mode == "probe":
                # LLC_out Gets not issued by an MSHR come from the CMU (maintenance), inside its busy periods.
                port_inst = max((pi for pi in out_gets if cache.startswith(pi)), key=len, default=None)
                gets = [t for t in out_gets.get(port_inst, ()) if w0 <= t <= w1]
                row["llc_out_gets"] = len(gets)
                row["llc_out_gets_in_cmu"] = sum(1 for t in gets if any(c0 <= t <= c1 for c0, c1 in cmu))
                self._check(row)
            rows.append(row)
        by_client = [{"cache": m.trace.module_short(cache), "client": cl, **_ratios(dict(c))}
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
        overlaps = [e for e in m.events("spm_s3_overlap") if w0 <= e.ts <= w1]
        if overlaps and warn:
            self.warnings.append(f"spm_s3_overlap fired {len(overlaps)} times (first at cycle {overlaps[0].ts:.0f}, "
                                 f"{m.trace.module_short(overlaps[0].probe.inst)}): a CE load took the store path "
                                 "while a DMA write was in progress - possible scratchpad corruption")
        for e in overlaps:
            per[e.probe.inst]["s3_overlap"] += 1
        rows = []
        for inst in sorted(per):
            c, ls = per[inst], sorted(lat.get(inst, []))
            rows.append({"cache": m.trace.module_short(inst), "core": _core_of(inst),
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
        return [{"section": s, **_ratios(dict(c))} for s, c in sorted(per.items(), key=lambda x: -x[1]["accesses"])]

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
            row = {"cache": m.trace.module_short(inst), "core": core,
                   **_ratios({k: s[k] for k in ("accesses", "hits", "misses", "read_misses", "write_misses",
                                                "secondary")}),
                   "cmu_writebacks": sum(1 for t in l1d_cmu_wb.get(inst, ()) if w0 <= t <= w1),
                   "llc_in_dcache_gets": dcache_gets.get(core, 0) if core is not None else None,
                   "mpki": None}
            if whole_run and self.retired and core in self.retired and self.retired[core]:
                row["mpki"] = 1000 * s["misses"] / self.retired[core]
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
        proc = out.process("Cache metrics", sort=6)
        series = defaultdict(list)
        for cache, ts, v in llc:
            series[f"LLC {m.trace.module_short(cache)}"].append((ts, v.get("hit", 0)))
        for e in l1d_ev:
            series[f"L1-D {m.trace.module_short(e.probe.inst)}"].append((e.ts, e.vals.get("hit", 0)))
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
        return {"mode": self.mode, "windows": self.results, "warnings": self.warnings}

    def report(self, m) -> list[Section]:
        note = ("Counted at each request's first lookup. merged = waited behind a miss to the same line (RPQ). "
                "expected refills = Gets the misses must issue; it should equal refills.") if self.mode == "probe" \
            else "mode=infer (approximate): reads only, miss = a fill Get for the same line while the read was pending."
        cols = ["window", "cache", "accesses", "hits", "misses", "hit %", "miss %", "read misses", "write misses",
                "merged", "dirty victim", "fetch-modify", "refills", "expected refills", "writebacks", "CMU ops"]
        rows = [[r["window"], s["cache"], s["accesses"], s["hits"], s["misses"], _pct(s["hit_ratio"]),
                 _pct(s["miss_ratio"]), s["read_misses"], s["write_misses"], s["merged"], s["dirty_victim"],
                 s["fetch_modify"], s["refills"], s["expected_refills"], s["writebacks"], r["cmu_ops"]]
                for r in self.results for s in r["llc"]]
        if self.mode != "probe":
            cols, rows = cols[:7], [r[:7] for r in rows]  # the rest needs the cache probes
        out = [Section(f"Cache metrics · LLC hit/miss ({self.mode})", cols, rows, note)]
        rows = [[r["window"], s["cache"], s["client"], s["accesses"], s["hits"], s["misses"], _pct(s["hit_ratio"])]
                for r in self.results for s in r["llc_by_client"]]
        out.append(Section("Cache metrics · LLC hit/miss by requester",
                           ["window", "cache", "client", "accesses", "hits", "misses", "hit %"], rows))
        if self.mode == "probe":
            rows = [[r["window"], s["cache"], s["accesses"], s["hits"], s["misses"], _pct(s["hit_ratio"]),
                     _pct(s["miss_ratio"]), s["read_misses"], s["write_misses"], s["secondary"],
                     s["llc_in_dcache_gets"], s["cmu_writebacks"], _r(s["mpki"])]
                    for r in self.results for s in r["l1d"]]
            out.append(Section("Cache metrics · L1-D hit/miss per CE",
                               ["window", "cache", "accesses", "hits", "misses", "hit %", "miss %", "read misses",
                                "write misses", "secondary", "D$ Gets at LLC", "CMU writebacks", "MPKI"], rows,
                               "Cacheable loads/stores only (SPM, uncached and maintenance commands excluded). "
                               "secondary = miss merged into an in-flight MSHR. MPKI uses the whole log's retired "
                               "instructions, so it is given for the 'run' window only."))
        rows = [[r["window"], s["cache"], s["loads"], s["stores"], s["dma_write_bytes"], s["dma_read_bytes"],
                 s["dma_beat_latency_p50"], s["ce_blocked_cycles"], s["dma_wait_cycles"], s["s3_overlap"]]
                for r in self.results for s in r.get("spm", [])]
        if rows:
            out.append(Section("Cache metrics · scratchpad per CE",
                               ["window", "cache", "CE loads", "CE stores", "DMA bytes in", "DMA bytes out",
                                "DMA beat latency p50", "CE blocked (cycles)", "DMA waits (cycles)",
                                "s3 overlap"], rows,
                               "Group l1d_spm (+rtltrace_l1d_spm_on). CE blocked = CE request held off by a DMA "
                               "burst in progress; DMA waits = DMA beat held off because the CE won arbitration; "
                               "s3 overlap must be 0."))
        if self.regions is not None:
            for key, what in (("llc_by_section", "LLC"), ("l1d_by_section", "L1-D")):
                rows = [[r["window"], s["section"], s["accesses"], s["hits"], s["misses"], _pct(s["hit_ratio"])]
                        for r in self.results for s in r.get(key, [])]
                if rows:
                    out.append(Section(f"Cache metrics · {what} hit/miss by kernel section",
                                       ["window", "section", "accesses", "hits", "misses", "hit %"], rows,
                                       f"Sections from {self.regions.source}."))
        if self.warnings:
            out.append(Section("Cache metrics · warnings", ["warning"], [[w] for w in self.warnings]))
        return out


def _pct(x):
    return None if x is None else _r(100 * x, 4)
