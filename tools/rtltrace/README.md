# rtltrace — transaction traces without waveforms

`emitrtl.trace` adds simulation-only probes to Chisel designs. `rtltrace` (this directory) turns the resulting trace
into a Perfetto timeline, a bottleneck report and a decoded log. The core knows protocols (ready/valid, valid,
value change, TileLink) and nothing about what modules do; system knowledge is added with plugins.

## 1. Add probes (Chisel, any repo that depends on emitrtl)

```scala
import emitrtl.trace.Trace

// inside a LazyModuleImp: every TileLink link of a node this module owns
Trace.tl("llc", outer.node, groups = "llc", on = true)

// custom interfaces, in LazyModuleImp or plain Module
Trace.fire("req", q.io.enq, groups = "dma")                  // ready/valid: transfers + stalls
Trace.valid("resp", respValid, "dma")                        // Valid[T]: every cycle valid is high
Trace.pulse("flush", flushPulse, groups = "dma")             // Bool pulse
Trace.change("state", fsmReg, groups = "dma", on = true)     // value changes (ChiselEnum names decoded offline)
Trace.event("hit", cond, Seq("addr" -> a, "way" -> w), groups = "dma")
Trace.ports(io, groups = "dma.ports")                        // every ReadyValid / Valid in an IO tree
```

- `on` is the default when no plusarg says otherwise. Probes cost no synthesizable logic: firtool puts printfs under
  `ifndef SYNTHESIS` and the few helper registers have no loads in synthesis.
- A link between modules you cannot edit: insert a named identity node in a parent you own and probe it:
  `val tap = TLNameNode("l1d_0"); mbus := TLBuffer() := tap := tile.masterNode` then `Trace.tl("l1d_0", tap)`.
- Call probes outside `when` blocks (use `Trace.event(name, cond, ...)` for conditional records).
- `Trace.fire(..., fields = Seq(F("id", q.io.enq.bits.id), F("addr", q.io.enq.bits.addr, tags = "addr")))` records
  selected or derived values instead of every field of `bits`. Tags: `addr`/`code` are symbolized with `--elf`,
  `key=<k>` links events of different probes that carry the same value (e.g. a frame id through a pipeline).
- The in/out direction used for the bottleneck ranking is automatic for the module's own ports only. For a child's
  port (e.g. the `enq` of a Queue inside the module) state it: `meta = Seq("dir" -> "out")`.
- `RTLTRACE=off` in the environment at elaboration removes every probe (RTL identical to having none).

emitrtl `Toplevel` writes `trace_points.json` next to the RTL and runs the post-pass that adds `%m`, the simulation
time and the plusarg enables to the probe printfs (`emitrtl.trace.PostPass`). `emitrtl.VerilatorSimulator` runs the
post-pass too.

## 2. Run the simulation

Probe output goes to **stderr**, one line per event, prefixed `@RT`; capture it (`2> sim.stderr`). Other stderr lines
(printfs, assertions) are ignored by the tool.

| plusarg | effect |
|---|---|
| `+rtltrace_<group>_on` | enable a group whose probes are off by default |
| `+rtltrace_<group>_off` | silence a group |
| `+rtltrace_all_on` / `+rtltrace_all_off` | everything on / off |

`python3 -m rtltrace args --on llc,ce --off crdma` prints the plusargs for you.

## 3. Analyze

```sh
cd emitrtl/tools/rtltrace        # or put this directory on PYTHONPATH
python3 -m rtltrace perfetto sim.stderr -o trace.json --elf prog.elf --tl-hops   # open in ui.perfetto.dev
python3 -m rtltrace report   sim.stderr -o report.html [--csv tables/]
python3 -m rtltrace text     sim.stderr --groups llc
python3 -m rtltrace points   generated_sv_dir/<top>/trace_points.json --grep llc
```

Common options: `--elf` (symbolize addresses), `--catalog trace_points.json` (decode probes whose DECL line never
printed, e.g. a ChiselSim test without a reset pulse), `--only <instance substring>`, `--from/--to <cycle>`,
`--period <time units per cycle>`, `--stall <cycles>`, `--links rtltrace_links.json`, `--plugin file.py`.
The clock period is inferred from the event timestamps; pass `--period` when a trace is very short (a handful of
events cannot reveal it).

What you get:
- **Perfetto**: one process per module instance; tracks per probe (handshake slices include the wait, state
  timelines, pulses); TileLink lanes per source ID with request/response beats, in-flight counters, symbolized
  addresses; markers for long stalls and handshakes still pending at the end; flows for correlation links.
- **Report**: input-side vs output-side stall per module (bottleneck ranking), per-interface throughput and stalls,
  TileLink latency percentiles and bandwidth per port/opcode/client, source-ID usage, time in each FSM state, hangs,
  slowest transactions.
- **Relationships**: TileLink request/response pairing is exact (protocol). `--tl-hops` links the same cache line
  across ports (heuristic: the downstream transaction is nested in the upstream one). `--links` declares others:
  `[{"name": "enq2deq", "from": {"probe": "*enq", "key": "id"}, "to": {"probe": "*deq", "key": "id"}, "pairing": "fifo"}]`.

## 4. Plugins (system knowledge)

See `rtltrace/plugin.py` for the contract and `examples/llc_miss.py` for a complete example:

```sh
python3 -m rtltrace report sim.stderr -o r.html --plugin examples/llc_miss.py \
    --plugin-arg in='*llc.in0' --plugin-arg out='*llc.out0'
```

## Trace format v1

```
@RT <t> DECL  <%m> <pid> <kind> <name> <groups> <width> <layout> <enums|-> <meta>
@RT <t> EVENT <%m> <pid> [<hex>]
@RT <t> STALL <%m> <pid>
```
`layout` = `name:width[:type][#tag],…` MSB first (types `h d s b e=<Enum>`), `enums` = `Name=v:label/…;…`,
`meta` = `k=v;…`. Names inside structured values escape separators as `~XX`.

## Tests

```sh
cd emitrtl/tools/rtltrace && python3 -m unittest discover -s tests     # tool (fixtures from the emitrtl self-test)
mill emitrtl.test                                                        # emitrtl self-test (firtool + verilator)
```
