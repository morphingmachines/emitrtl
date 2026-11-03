# Emit RTL

This project contains useful methods of Chisel driver that generates FIRRTL.

## Generating Verilog

Verilog code can be generated from Chisel by using the `rtl` Makefile target.

```sh
make rtl
```

More targets can be listed by running `make`.

## Simulation trace probes (rtltrace)

`emitrtl.trace.Trace` adds simulation-only probes to Chisel designs; `Toplevel` and `VerilatorSimulator` run the SV
post-pass (`emitrtl.trace.PostPass`) that turns them into `$fwrite` trace lines. See [docs/trace.md](docs/trace.md)
for the probe API, plusargs and trace format. Reading the trace (Perfetto timeline, bottleneck reports, REDEFINE
metrics) is done by the separate `rtltrace` repo.
