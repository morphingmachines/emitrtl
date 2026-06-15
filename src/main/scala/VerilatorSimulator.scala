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

  def simulate[T <: RawModule](module: => T)(body: T => Unit): Unit = {
    val workDir = s"build/chiselsim/${getClass.getSimpleName}/$currentTestName"
    new SingleBackendSimulator[svsim.verilator.Backend] {
      val backend                   = verilatorWithTiming
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
  }
}
