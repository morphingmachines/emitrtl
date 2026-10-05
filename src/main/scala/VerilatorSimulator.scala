package emitrtl

import _root_.svsim
import chisel3.RawModule
import chisel3.simulator.{PeekPokeAPI, SingleBackendSimulator}
import org.scalatest.{Outcome, TestSuite}

import java.nio.file.Files

trait VerilatorSimulator extends TestSuite with PeekPokeAPI {

  private var currentTestName: String = ""

  abstract override def withFixture(test: NoArgTest): Outcome = {
    currentTestName = test.name.replace(" ", "-")
    super.withFixture(test)
  }

  private lazy val verilatorWithTiming: svsim.verilator.Backend = {
    val verilatorPath = scala.sys.process.Process(Seq("which", "verilator")).!!.trim
    val wrapper       = Files.createTempFile("verilator-timing-", ".sh")
    wrapper.toFile.setExecutable(true)
    Files.write(wrapper, s"#!/bin/sh\nexec $verilatorPath --timing \"$$@\"\n".getBytes)
    new svsim.verilator.Backend(wrapper.toFile.getAbsolutePath)
  }

  /** Runs the rtltrace SV post-pass on the elaborated sources right before Verilator compiles them. */
  private final class TracePostPassBackend(inner: svsim.verilator.Backend, primarySources: => String)
      extends svsim.Backend {
    type CompilationSettings = svsim.verilator.Backend.CompilationSettings
    def generateParameters(
      outputBinaryName:        String,
      topModuleName:           String,
      additionalHeaderPaths:   Seq[String],
      commonSettings:          svsim.CommonCompilationSettings,
      backendSpecificSettings: CompilationSettings,
    ): svsim.Backend.Parameters = {
      _root_.emitrtl.trace.PostPass.run(primarySources)
      inner.generateParameters(outputBinaryName, topModuleName, additionalHeaderPaths, commonSettings, backendSpecificSettings)
    }
  }

  def simulate[T <: RawModule](module: => T)(body: T => Unit): Unit = {
    val workDir = s"build/chiselsim/${getClass.getSimpleName}/$currentTestName"
    _root_.emitrtl.trace.Trace.reset()
    new SingleBackendSimulator[TracePostPassBackend] {
      val backend = new TracePostPassBackend(verilatorWithTiming, s"${os.pwd}/$workDir/primary-sources")
      val tag                       = "verilator"
      val workspacePath             = workDir
      val commonCompilationSettings = svsim.CommonCompilationSettings()
      val backendSpecificCompilationSettings = svsim.verilator.Backend.CompilationSettings(
        traceStyle = Some(svsim.verilator.Backend.CompilationSettings.TraceStyle.Vcd(traceUnderscore = true)),
      )
    }.simulate(module) { m =>
      m.controller.setTraceEnabled(true)
      body(m.wrapped)
    }.result
    // rtltrace catalog next to the workspace: decodes probes whose DECL line never printed (no reset pulse in the test)
    _root_.emitrtl.trace.Trace.writeCatalog(s"$workDir/trace_points.json")
  }
}
