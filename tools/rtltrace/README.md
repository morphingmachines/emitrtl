# rtltrace — transaction traces without waveforms

`emitrtl.trace` adds simulation-only probes to Chisel designs. `rtltrace` (this directory) turns the resulting trace
into a Perfetto timeline, a bottleneck report and a decoded log. The core knows protocols (ready/valid, valid,
value change, TileLink) and nothing about what modules do; system knowledge is added with plugins.

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

`python3 -m rtltrace args --on llc,ce --off crdma` prints the plusargs for you.

## 3. Analyze

```sh
cd emitrtl/tools/rtltrace        # or put this directory on PYTHONPATH
python3 -m rtltrace perfetto sim.stderr -o trace.json --elf prog.elf --tl-hops   # open in ui.perfetto.dev
python3 -m rtltrace perfetto sim.stderr -o small.json --view kernel --plugin p.py # plugin tracks only (small)
python3 -m rtltrace report   sim.stderr -o report.html [--csv tables/] [--md report.md] [--title "..."]
python3 -m rtltrace text     sim.stderr --groups llc
python3 -m rtltrace points   generated_sv_dir/<top>/trace_points.json --grep llc
python3 -m rtltrace args     --on llc_cache,l1d_cache --off ce      # plusargs for groups
python3 -m rtltrace elf      prog.elf --lookup 0x40000024           # ELF symbols / address lookup
```

Common options: `--elf` (symbolize addresses), `--catalog trace_points.json` (decode probes whose DECL line never
printed, e.g. a ChiselSim test without a reset pulse), `--regions` (address regions, below), `--only <instance
substring>`, `--from/--to <cycle>` (inclusive; percentages are over the window), `--period <time units per cycle>`, `--stall <cycles>`, `--links
rtltrace_links.json`, `--tl-hops [--line <bytes>]`, `--plugin file.py[:Class]` and `--plugin-arg key=value`
(both repeatable). `report` also takes `--csv <dir>`, `--md <file>` (the report body as Markdown, without the
hardware-detail appendix and the trace details: input file paths, time base), `--title` and `--top <n>` (rows in the slowest/longest tables). `perfetto` takes `--view
full|kernel` (kernel: plugin processes plus the probes the plugins list in `perfetto_keep`).
The clock period is inferred from the event timestamps; pass `--period` when a trace is very short (a handful of
events cannot reveal it).

What you get:
- **Perfetto**: one process per module instance; tracks per probe (handshake slices include the wait, state
  timelines, pulses); TileLink lanes per source ID with request/response beats, in-flight counters, symbolized
  addresses; markers for long stalls and handshakes still pending at the end; flows for correlation links.
- **Report**: input-side vs output-side stall per module (bottleneck ranking), per-interface throughput and stalls,
  TileLink latency percentiles and bandwidth per port/opcode/client, source-ID usage, time in each FSM state, hangs,
  slowest transactions, traffic by address region; then every plugin's sections. `--csv` writes each table, generic
  and plugin, as `<title>.csv` (or the section's `key`). When plugin sections set `order`, the report becomes
  chapters (`group`) with a table of contents and per-table legends, and the generic tables move to an
  "Appendix · Hardware detail".
- **Names and RTL traceability**: plugins may give friendly names to modules, TileLink clients/ports, probes and
  address regions (`Plugin.display`). A friendly name is an `RtlName`: the RTL identifier stays attached — an HTML
  tooltip (shown inline in hardware-detail tables), a `<column> (RTL)` CSV column, the Perfetto process name
  `Friendly  [rtl.path]` and the `rtl_path` / `probe` / `chisel_src` args on every module slice.
- **Relationships**: TileLink request/response pairing is exact (protocol). `--tl-hops` links the same cache line
  across ports (heuristic: the downstream transaction is nested in the upstream one). `--links` declares others:
  `[{"name": "enq2deq", "from": {"probe": "*enq", "key": "id"}, "to": {"probe": "*deq", "key": "id"}, "pairing": "fifo"}]`.

### Address regions

`--regions <file.json>` labels addresses with the named region they fall in:
- every `#addr` field (TileLink addresses, `Trace.event` fields tagged `addr`) is shown as `StackMemory[ce2]+0x1f0`
  in Perfetto names, the text log and the report (with `--elf`, a symbol wins when the address has one);
- TileLink slices get the args `region`, `region_offset`, the decoded fields (a field named `region` becomes
  `region_id`), `kernel_id` / `cr_id` for extended addresses, and `scope`, `caching`, `access`;
- the report gets a region column in the slowest-transactions table and a "TileLink traffic by address region" table
  (port × region × opcode: transactions, bytes, latency p50/p95);
- plugins get the map as `ctx.regions` (`ctx.regions.lookup(addr)` → `Region(name, offset, fields, attrs)`).

By default (`--regions auto`) a `kernel_sections.json` in the directory of `--catalog` or of a trace file is used;
`--regions none` disables it. The format (see `rtltrace/regions.py`):

```json
{"addr_width": 32, "kernel_id_width": 0, "cr_id_width": 0,
 "sections": [{"name": "StackMemory", "value": "0x38000000", "care": "0xfc000000", "offset_mask": "0xffffff",
               "fields": [{"name": "ce", "hi": 26, "lo": 24, "add": 0}], "scope": "PRIVATE"}]}
```
A section matches when `(addr & care) == value`; the first match in file order wins. `fields` are bit-slices decoded
per address; bits above `addr_width` are reported as `kernel_id` / `cr_id`. Other keys are passed through as
attributes. REDEFINE writes this file per RTL config from `KernelSectionConst` (next to `trace_points.json`, by
`redefine.header.kernelSections.KernelSectionsJson`); the REDEFINE plugins directory keeps a copy for RTL built before
that.

## 4. Plugins (system knowledge)

A plugin is a `rtltrace.plugin.Plugin` subclass: `setup(ctx)` (ELF, `--plugin-arg`s, trace, regions),
`annotate(field, value, event)` (custom labels), `derive(model, out)` (add Perfetto processes, tracks, spans,
counters, instants and flows through `out`), `report(model)` (return `Section`s for the HTML/CSV report),
`display(kind, name)` (friendly name for a `module` path, TileLink `client` / `port`, `probe` or address `region`;
None = no opinion), `card()` (entries for the report header) and `perfetto_keep` (probe globs kept in the Perfetto
kernel view). A `Section` may set `legend` (column → meaning), `group` (chapter), `order`, `level` (`"hw"` =
hardware detail: RTL names inline, left out of `--md`), `key` (stable CSV name) and `empty` (what to say when the
table has no rows, instead of "Nothing to show."). A plugin that raises is disabled
with a warning; the rest of the output continues. See `rtltrace/plugin.py` for the contract
and `examples/llc_miss.py` for a complete example:

```sh
python3 -m rtltrace report sim.stderr -o r.html --plugin examples/llc_miss.py \
    --plugin-arg in='*llc.in0' --plugin-arg out='*llc.out0'
```

The REDEFINE plugins (γe/ρ/φ metrics, CE phases from PC markers, CR-DMA utilisation, cache hit/miss, and the
one-command `redefine_report.py`) live in [`../rtltrace-plugins`](../rtltrace-plugins/README.md).

From Python, `rtltrace.open_trace(traces, elf=, catalog=, regions=None, plugins=[...], plugin_args={...})` (`regions="auto"` finds
`kernel_sections.json` as the CLI does; the CLI default is `auto`, the API default is no regions) runs the
same pipeline and returns a `Session` (`model`, `labeler`, `plugins`, `emitted`, `regions`, `warnings`).

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
