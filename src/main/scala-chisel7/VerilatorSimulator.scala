package emitrtl

import _root_.svsim
import chisel3.RawModule
import chisel3.simulator.{PeekPokeAPI, Simulator}
import org.scalatest.{Outcome, TestSuite}

trait VerilatorSimulator extends TestSuite with PeekPokeAPI {

  private var currentTestName: String = ""

  abstract override def withFixture(test: NoArgTest): Outcome = {
    currentTestName = test.name.replace(" ", "-")
    super.withFixture(test)
  }

  def simulate[T <: RawModule](module: => T)(body: T => Unit): Unit = {
    val workDir = s"build/chiselsim/${getClass.getSimpleName}/$currentTestName"
    new Simulator[svsim.verilator.Backend] {
      val backend                   = svsim.verilator.Backend.initializeFromProcessEnvironment()
      val tag                       = "verilator"
      val workspacePath             = workDir
      val commonCompilationSettings = svsim.CommonCompilationSettings()
      val backendSpecificCompilationSettings = svsim.verilator.Backend.CompilationSettings.default
        .withTraceStyle(
          Some(
            svsim.verilator.Backend.CompilationSettings.TraceStyle(
              svsim.verilator.Backend.CompilationSettings.TraceKind.Vcd,
              traceUnderscore = true
            )
          )
        )
        .withTiming(Some(svsim.verilator.Backend.CompilationSettings.Timing.TimingEnabled))
    }.simulate(module) { m =>
      m.controller.setTraceEnabled(true)
      body(m.wrapped)
    }.result
  }
}
