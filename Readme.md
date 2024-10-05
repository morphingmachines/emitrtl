# Emit RTL

This project contains useful methods of Chisel driver that generates FIRRTL.

This project uses [playground]() as a library. `playground` and `emitrtl` directories should be at the same level, as shown below.
```
  workspace
  |-- playground
  |-- emitrtl
```
Make sure that you have a working [playground](https://github.com/morphingmachines/playground.git) project before proceeding further. And donot rename/modify `playground` directory structure.


## Generating Verilog

Verilog code can be generated from Chisel by using the `rtl` Makefile target.

```sh
make rtl
```

More targets can be listed by running `make`.
