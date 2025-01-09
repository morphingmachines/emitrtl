project = emitrtl

# Toolchains and tools
MILL = ./mill

TARGET ?= TestTop


# Targets
rtl:## Generates Verilog code from Chisel sources (output to ./generated_sv_dir)
	$(MILL) $(project).runMain emitrtl.genRTLMain $(TARGET)

lazyrtl:## Generates Verilog code from Chisel sources (output to ./generated_sv_dir)
	$(MILL) $(project).runMain emitrtl.genLazyRTLMain $(TARGET)

.PHONY: lint
lint: ## Formats code using scalafmt and scalafix
	$(MILL) $(project).fix
	$(MILL) $(project).reformat

.PHONY: scaladoc
scaladoc: ## Generates Scala API documentation that can view in a browser
	$(MILL) -i -j 0 $(project).docJar
	@echo "Scala documentation HTML files generated in ./out/$(project)/docJar.dest/javadoc"

.PHONY: clean
clean:   ## Clean all generated files
	$(MILL) clean
	@rm -rf test_run_dir generated_sv_dir
	@rm -rf out

.PHONY: cleanall
cleanall: clean  ## Clean all downloaded dependencies and cache
	@rm -rf project/.bloop
	@rm -rf project/project
	@rm -rf project/target
	@rm -rf .bloop .bsp .metals 

.PHONY: help
help:
	@echo "Makefile targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = "[:##]"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$4}'
	@echo ""

.DEFAULT_GOAL := help
