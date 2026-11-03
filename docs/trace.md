# rtltrace probes: the Chisel `Trace` API and the SV post-pass

`emitrtl.trace` adds simulation-only probes to Chisel designs. A post-pass over the generated SystemVerilog turns
them into `$fwrite` trace lines. Reading those lines (Perfetto timeline, bottleneck report, decoded log, plugins) is
done by the separate rtltrace repo; this document covers the
emitrtl side and the trace format that links the two.

Code: `src/main/scala/trace/` (`Trace`, `Emit`, `Encoder`, `Registry`, `PostPass`); self-test:
`src/test/scala/trace/TraceSelfTest.scala` (`mill emitrtl.test`, needs firtool and verilator).

## 1. Add probes (Chisel, any repo that depends on emitrtl)

```scala
import emitrtl.trace.Trace

// inside a LazyModuleImp: every TileLink link of a node this module owns
Trace.tl("llc", outer.node, groups = "llc", on = true)
// a TileLink bundle you already hold (e.g. a tap node's in(0)), in any module
val (bundle, edge) = outer.tap.in(0)
Trace.tl("llc_in", bundle, edge, "llc", true)

// custom interfaces, in LazyModuleImp or plain Module
Trace.fire("req", q.io.enq, groups = "dma")                  // ready/valid: transfers + stalls
Trace.valid("resp", respValid, "dma")                        // Valid[T]: every cycle valid is high
Trace.pulse("flush", flushPulse, groups = "dma")             // Bool pulse
Trace.change("state", fsmReg, groups = "dma", on = true)     // value changes (ChiselEnum names decoded offline)
Trace.event("hit", cond, Seq("addr" -> a, "way" -> w), groups = "dma", tags = Map("addr" -> "addr"))
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
- `Trace.tl(name, node, ...)` picks the side automatically (`side = "auto"`: a client node's out links, a manager
  node's in links, an identity node's in links, both sides of an adapter/nexus as `name.in0`, `name.out0`, ...).
  `data = true` also records `mask`/`data` (wide; off by default).
- Probe and port names are sanitized: anything other than letters, digits and `_.[]` becomes `_`, so
  `Trace.tl("CR-DMA-mbus", ...)` appears as `CR_DMA_mbus`. Plugin port globs should use `?` (`CR?DMA?mbus`).
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


## 3. Analyze the trace

Use the rtltrace repo (`python3 -m rtltrace ...`). `python3 -m rtltrace args
--on llc,ce --off crdma` prints the plusargs for you.

## Trace format v1

```
@RT <t> DECL  <%m> <pid> <kind> <name> <groups> <width> <layout> <enums|-> <meta>
@RT <t> EVENT <%m> <pid> [<hex>]
@RT <t> STALL <%m> <pid>
```
`layout` = `name:width[:type][#tag],…` MSB first (types `h d s b e=<Enum>`), `enums` = `Name=v:label/…;…`,
`meta` = `k=v;…`. Names inside structured values escape separators as `~XX`.

This format is defined by `Encoder`/`Emit`/`PostPass` and read by `rtltrace/reader.py` in the rtltrace repo. The
fixtures in the rtltrace repo's `tests/data` are produced by `TraceSelfTest`: when the format changes, regenerate them.
