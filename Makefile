project = emitrtl

# Toolchains and tools
MILL = ../playground/mill

TARGET ?= TestTop

-include ./../playground/Makefile.include

# Targets
rtl: check-firtool ## Generates Verilog code from Chisel sources (output to ./generated_sv_dir)
	$(MILL) $(project).runMain emitrtl.genRTLMain $(TARGET)

lazyrtl: check-firtool ## Generates Verilog code from Chisel sources (output to ./generated_sv_dir)
	$(MILL) $(project).runMain emitrtl.genLazyRTLMain $(TARGET)


