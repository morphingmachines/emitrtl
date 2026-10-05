"""Example rtltrace plugin: classify requests on a cache's input port as hits or misses.

A request on the input port counts as a miss when the output port carries a request for the same cache line while
the input request is still waiting for its response. This is knowledge of one kind of module (a cache), which is
why it lives in a plugin and not in the core.

    python3 -m rtltrace report sim.stderr -o r.html --plugin examples/llc_miss.py \
        --plugin-arg in='*llc.in0' --plugin-arg out='*llc.out0' [--plugin-arg line=64]
"""

from rtltrace.plugin import Plugin, Section


class LlcMiss(Plugin):
    name = "llc-miss"

    def setup(self, ctx):
        self.inp = ctx.args.get("in", "*llc.in*")
        self.outp = ctx.args.get("out", "*llc.out*")
        self.line = int(ctx.args.get("line", "64"))
        self.n_req = self.n_miss = 0
        self.fill = []

    def derive(self, m, out):
        mask = ~(self.line - 1)
        fills = sorted(m.txns(self.outp), key=lambda t: t.req_first)
        p = out.process("LLC · hit/miss", sort=50)
        hit, miss = out.track(p, "hits"), out.track(p, "misses")
        for t in m.txns(self.inp):
            if t.kind != "A":
                continue
            self.n_req += 1
            until = t.resp_first if t.resp_first is not None else m.end
            f = next((o for o in fills if o.address & mask == t.address & mask and t.req_first <= o.req_first <= until),
                     None)
            end = t.end if t.end is not None else m.end
            if f is not None:
                self.n_miss += 1
                if f.total is not None:
                    self.fill.append(f.total)
                out.span(miss, t.start, end, f"miss 0x{t.address:x}", {"fill_cycles": f.total})
                out.link(t, f, "miss-fill")
            else:
                out.span(hit, t.start, end, "hit")

    def report(self, m):
        rate = round(100 * self.n_miss / self.n_req, 1) if self.n_req else 0
        avg = round(sum(self.fill) / len(self.fill), 1) if self.fill else None
        return [Section("LLC hit/miss (plugin)", ["requests", "misses", "miss %", "mean fill cycles"],
                        [[self.n_req, self.n_miss, rate, avg]],
                        f"input port '{self.inp}', output port '{self.outp}', {self.line} B lines")]
