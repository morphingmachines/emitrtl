package chisel3.simulator

import chisel3.RawModule
import svsim._

import java.nio.file.Files

/** Provides a simple API for "ephemeral" invocations (where you don't care about the artifacts after the invocation
  * completes) to simulate Chisel modules. To keep things really simple, `EphemeralSimulator` simulations can only be
  * controlled using the peek/poke API, which provides enough control while hiding some of the lower-level svsim
  * complexity.
  * @example
  *   {{{
  * import chisel3.simulator.EphemeralSimulator._
  * ...
  * simulate(new MyChiselModule()) { module => ... }
  *   }}}
  */
object VCDHackedEphemeralSimulator extends PeekPokeAPI {

  def simulate[T <: RawModule](
    module: => T,
  )(
    body: (T) => Unit,
  ): Unit =
    makeSimulator
      .simulate(module) { module =>
        // HACK enable tracing
        module.controller.setTraceEnabled(true)
        body(module.wrapped)
      }
      .result

  private class DefaultSimulator(val workspacePath: String) extends SingleBackendSimulator[verilator.Backend] {
    val backend                   = verilator.Backend.initializeFromProcessEnvironment()
    val tag                       = "default"
    val commonCompilationSettings = CommonCompilationSettings()
    // HACK to enable VCD dumping
    val backendSpecificCompilationSettings = verilator.Backend.CompilationSettings(
      traceStyle = Some(verilator.Backend.CompilationSettings.TraceStyle.Vcd(traceUnderscore = true)),
    )

    // HACK don't delete temporary workspace to keep VCD
    // Try to clean up temporary workspace if possible
    sys.addShutdownHook {
      // (new Directory(new File(workspacePath))).deleteRecursively()
    }
  }
  private def makeSimulator: DefaultSimulator = {
    // TODO: Use ProcessHandle when we can drop Java 8 support
    // val id = ProcessHandle.current().pid().toString()
    // val id        = java.lang.management.ManagementFactory.getRuntimeMXBean().getName()
    // val id        =  CallerUtils.callerClassName
    val className = CallerUtils.callerClassName
    // HACK: use $PWD/test_run_dir like in old versions of Chisel
    // new DefaultSimulator(Files.createTempDirectory(s"${className}_${id}_").toString)
    new DefaultSimulator(
      Files.createDirectories(java.nio.file.Paths.get(s"test_run_dir/${className}")).toAbsolutePath.toString,
    )
  }
}

object CallerUtils {
  import java.lang.StackWalker
  import java.lang.StackWalker.Option.RETAIN_CLASS_REFERENCE

  def callerClassName: String = {
    val walker = StackWalker.getInstance(RETAIN_CLASS_REFERENCE)
    walker.walk { frames =>
      val it = frames.iterator()

      // frame(0) = callerClassName (this method)
      it.next()

      // frame(1) = method inside this object that the caller invoked
      it.next()

      // frame(2) = the actual caller VCDHackedEphemeralSimulator
      it.next()

      // frame(3) = the actual caller
      val cls = it.next().getDeclaringClass
      if (cls.getName.endsWith("$")) cls.getName.stripSuffix("$") else cls.getName
    }
  }
}
