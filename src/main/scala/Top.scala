package emitrtl

import chisel3._
import circt.stage.ChiselStage
import freechips.rocketchip.unittest.UnitTest
import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.diplomacy.lazymodule.LazyModule

import java.io._
import java.nio.file._

package top {
  trait TestHarnessShell extends Module {
    val io = IO(new Bundle { val success = Output(Bool()) })
  }

  // dut is passed as call-by-name parameter as Module instantiate should be wrapped in Module()
  class TestHarness(dut: => TestHarnessShell) extends Module {
    val io   = IO(new Bundle { val success = Output(Bool()) })
    val inst = Module(dut)
    io.success := inst.io.success
    /*
  val count = RegInit(0.U(1.W))
  inst.io.start := false.B
  when(count =/= 1.U) {
    inst.io.start := true.B
    count         := count + 1.U
  }
     */
  }
}

package unit {
  // For Module level unit testing
  class TestHarness(dut: => UnitTest) extends Module {
    val io   = IO(new Bundle { val success = Output(Bool()) })
    val inst = Module(dut)
    io.success := inst.io.finished
    val count = RegInit(0.U(1.W))
    inst.io.start := false.B
    when(count =/= 1.U) {
      inst.io.start := true.B
      count         := count + 1.U
    }
  }
}

trait Toplevel {
  def topModule: chisel3.RawModule
  def topModule_name = topModule.getClass().getName().split("\\$").mkString(".")

  def out_dir = s"generated_sv_dir/${topModule_name}"

  /** For firtoolOpts run `firtool --help` There is an overlap between ChiselStage args and firtoolOpts.
    *
    * TODO: Passing "--Split-verilog" "--output-annotation-file" to firtool is not working.
    */

  lazy val chiselArgs   = Array("--full-stacktrace", "--target-dir", out_dir, "--split-verilog")
  lazy val firtroolArgs = Array("-dedup")

  def chisel2firrtl() = {
    val str_firrtl = ChiselStage.emitCHIRRTL(topModule, args = Array("--full-stacktrace"))
    Files.createDirectories(Paths.get("generated_sv_dir"))
    val pw = new PrintWriter(new File(s"${out_dir}.fir"))
    pw.write(str_firrtl)
    pw.close()
  }

  // Call this only after calling chisel2firrtl()
  def firrtl2sv() =
    os.proc(
      "firtool",
      s"${out_dir}.fir",
      "--disable-annotation-unknown",
      "--split-verilog",
      s"-o=${out_dir}",
      s"--output-annotation-file=${out_dir}/${topModule_name}.anno.json",
    ).call(stdout = os.Inherit) // check additional options with "firtool --help"

}

trait LazyToplevel extends Toplevel {
  def lazyTop: LazyModule
  override def topModule      = lazyTop.module.asInstanceOf[chisel3.RawModule]
  override def topModule_name = lazyTop.getClass().getName().split("\\$").mkString(".")

  def genDiplomacyGraph() = {
    ElaborationArtefacts.add("graphml", lazyTop.graphML)
    Files.createDirectories(Paths.get(out_dir))
    ElaborationArtefacts.files.foreach {
      case ("graphml", graphML) =>
        val fw = new FileWriter(new File(s"${out_dir}", s"${lazyTop.className}.graphml"))
        fw.write(graphML())
        fw.close()
      case _ =>
    }
  }

  def showModuleComposition(gen: => LazyModule) = {
    println("List of Diplomatic Nodes (Ports)")
    gen.getNodes.map(x => println(s"Class Type:  ${x.getClass.getName()} | node: ${x.name} (${x.description})"))
    println("")
    println("List of Sub Modules")
    // def hierarchyName(x: => LazyModule) :String = {
    //   x.parents.map(_.name).foldRight(".")(_ + _)
    // }
    gen.getChildren.map(x => println("Class Type: " + x.getClass.getName() + "| Instance name:" + x.name))
  }

}

trait VerilateTestHarness { this: Toplevel =>

  val rocketchip_resource_path = s"${os.pwd.toString()}/../playground/dependencies/rocket-chip/src/main/resources"

  val extra_rtl_src = Seq("/vsrc/EICG_wrapper.v").map(i => rocketchip_resource_path + i)
  // riscv-tools installation path
  val spike_install_path = sys.env.get("RISCV") match {
    case Some(value) => value
    case None        => throw new Exception("Environment variable \"RISCV\" is no defined!")
  }

  def CFLAGS(extra_flags: Seq[String]): Seq[String] = {
    val default = Seq("-std=c++17", "-DVERILATOR", s"-I${spike_install_path}/include")
    val opts    = default ++ extra_flags
    opts.map(i => Seq("-CFLAGS", i)).flatten
  }

  def LDFLAGS(extra_flags: Seq[String]): Seq[String] = {
    val default = Seq(s"-L${spike_install_path}/lib", "-lfesvr", "-lriscv", "-lpthread")
    val opts    = default ++ extra_flags
    opts.map(i => Seq("-LDFLAGS", i)).flatten
  }

  def verilate(
    emitrtl_path:  String = s"${os.pwd.toString()}/dependencies/emitrtl",
    extra_CFLAGS:  Seq[String] = Seq(),
    extra_LDFLAGS: Seq[String] = Seq(),
    extras_src:    Seq[String] = Seq(),
  ) = {
    val cmd =
      Seq("verilator", "-Wno-LATCH", "-Wno-WIDTH", "--cc") ++ CFLAGS(extra_CFLAGS) ++ LDFLAGS(
        extra_LDFLAGS,
      ) ++
        extras_src ++ extra_rtl_src ++
        Seq(
          "-f",
          "filelist.f",
          "--top-module",
          s"TestHarness",
          "--trace",
          "--vpi",
          "--exe",
          s"${emitrtl_path}/src/main/resources/csrc/test_tb_top.cpp",
        )
    println(s"LOG: command invoked \"${cmd.mkString(" ")}\"")
    os.proc(cmd).call(cwd = os.Path(s"${os.pwd.toString()}/${out_dir}"), stdout = os.Inherit)
  }

  def build() = {
    val cmd = Seq("make", "-j", "-C", "obj_dir/", "-f", s"VTestHarness.mk")
    println(s"LOG: command invoked \"${cmd.mkString(" ")}\"")
    os.proc(cmd).call(cwd = os.Path(s"${os.pwd.toString()}/${out_dir}"), stdout = os.Inherit)
    println(s"VTestHarness executable in ./generated_sv_dir/${topModule_name}/obj_dir directory.")
    println(s"Run simulation using: ./VTestHarness <foo.elf")
  }
}

trait WithLazyModuleDUT { this: VerilateTestHarness with LazyToplevel =>
  def dut                     = lazyTop.module.asInstanceOf[top.TestHarnessShell]
  override lazy val topModule = new top.TestHarness(dut)
}

trait WithUnitTestDUT { this: VerilateTestHarness with Toplevel =>
  val dut: UnitTest
  lazy val topModule          = new unit.TestHarness(dut)
  override def topModule_name = dut.getClass().getName().split("\\$").mkString(".")
}

/** To run from a terminal shell
  * {{{
  * mill emitrtl.runMain emitrtl.genRTLMain
  * }}}
  */
object genRTLMain extends App with Toplevel {
  val str = if (args.length == 0) "" else args(0)
  lazy val topModule = str match {
    case "TestTop" => new GCD
    case _         => throw new Exception("Unknown Module Name!")
  }
  chisel2firrtl()
  firrtl2sv()
}

/** To run from a terminal shell
  * {{{
  * mill emitrtl.runMain emitrtl.genLazyRTLMain
  * }}}
  */
object genLazyRTLMain extends App with LazyToplevel {
  import org.chipsalliance.cde.config.Parameters
  val lazyTop = args(0) match {
    case "TestTop" => LazyModule(new AdderTestHarness()(Parameters.empty))
    case _         => throw new Exception("Unknown Module Name!")
  }
  chisel2firrtl()
  firrtl2sv()
  genDiplomacyGraph()

  showModuleComposition(lazyTop)
}
