# Emit RTL

This project contains useful methods of Chisel driver that generates FIRRTL.

## Generating Verilog

Verilog code can be generated from Chisel by using the `rtl` Makefile target.

```sh
make rtl
```

More targets can be listed by running `make`.

## Artefacts

After elaboration, `chisel2firrtl()` and `chipyardAnno()` write the Top's artefact selection, such as regmaps and graphML, to `<out_dir>/artefacts/`. A Top sets its selection with `override def artefactKinds = Seq(...)`. An empty selection writes everything.

### Overriding the selection at run time

Set `EMITRTL_ARTEFACTS` to replace the selection of every Top elaborated in the run:

```sh
EMITRTL_ARTEFACTS=regmap make rtl TARGET=CE
```

- The value is a comma-separated list, with no spaces, of kebab-case kind names, matched case-insensitively: `regmap`, `graphml`, `dts`, `dts-json`, `mem-map`, `plus-args`, `axi4-id-map`, `l2`, `fpga-shell`, `fpga-ip`.
- A `glob:` entry selects registry keys by pattern, for example `glob:*.l2.json`. Glob characters without the prefix are not treated as a pattern.
- An unset or empty value uses the Top's `artefactKinds`.
- An unknown name fails the run before elaboration. The error lists the valid names.
- `genDiplomacyGraph()` and the stage's own `<out_dir>/<outFileName>.<key>` files ignore the variable.
