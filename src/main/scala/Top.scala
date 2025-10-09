package emitrtl

import chipyard.stage._
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

  def configString: String = "None:None" // $(CONFIG_PACKAGE):$(CONFIG)

  val curr_dir: String = os.pwd.toString
  def rel_out_dir     = s"generated_sv_dir/${topModule_name}.${configString.split(":")(1)}"
  def out_dir         = s"${curr_dir}/${rel_out_dir}"
  def firtool_out_dir = s"${out_dir}/chisel_gen_rtl"
  def outFileName     = topModule_name.split('.').last

  def fir_file       = s"${out_dir}/${outFileName}.fir"
  def mfc_smems_conf = s"${out_dir}/${outFileName}.mems.conf"

  def anno_file       = s"${out_dir}/${outFileName}.anno.json"
  def final_anno_file = s"${out_dir}/${outFileName}.appended.anno.json"

  def addHierarchy2AnnoFile(annIn: String, annOut: String): Unit = {
    def mfc_extra_anno_contents =
      s"""
      [
        {
		      "class": "sifive.enterprise.firrtl.ModuleHierarchyAnnotation",
		      "filename": "${out_dir}/top_module_hierarchy.json"
	      }
      ]
      """
    def extra_anno_file = s"${out_dir}/${outFileName}.extrafirtool.anno.json"
    os.proc(
      "rm",
      "-rf",
      s"${extra_anno_file}",
    ).call(stdout = os.Inherit)

    os.write(
      os.Path(s"${extra_anno_file}"),
      s"${mfc_extra_anno_contents}",
    )

    val inAnnoFile = new File(annIn)
    new File(annOut)

    if (inAnnoFile.exists()) {
      val jq_cmd = Seq("jq", "-s", "[.[][]]", s"${annIn}", s"${extra_anno_file}")
      println(s"LOG: command invoked \"${jq_cmd.mkString(" ")}\"")
      os.proc(jq_cmd).call(stdout = os.Path(annOut))
    } else {
      os.proc(
        "mv",
        s"${extra_anno_file}",
        s"${annOut}",
      ).call(stdout = os.Inherit)
    }

    os.proc(
      "rm",
      "-rf",
      s"${extra_anno_file}",
    ).call(stdout = os.Inherit)
  }

  def mfc_lowering_options = s"emittedLineLength=2048,noAlwaysComb," +
    s"disallowLocalVariables,verifLabels," +
    s"disallowPortDeclSharing," +
    s"locationInfoStyle=wrapInAtSquareBracket"

  lazy val defaultFirtoolArgs = Seq(
    "--export-module-hierarchy",
    "--verify-each=true",
    "--warn-on-unprocessed-annotations",
    "--disable-annotation-classless",
    "--disable-annotation-unknown",
    s"--lowering-options=${mfc_lowering_options}",
    s"--annotation-file=${final_anno_file}",
  )

  lazy val otherFirtoolArgs = Seq(
    "--strinp-debug-info",
    "--mlir-timing",
    s"--output-annotation-file=${out_dir}/${outFileName}.output.anno.json",
  )

  def chisel2firrtl() = {
    val str_firrtl = ChiselStage.emitCHIRRTL(topModule, args = Array("--full-stacktrace"))
    Files.createDirectories(Paths.get(s"${out_dir}"))
    val pw = new PrintWriter(new File(fir_file))
    pw.write(str_firrtl)
    pw.close()
    addHierarchy2AnnoFile(anno_file, final_anno_file)
  }

  def firrtl2sv(args: Seq[String] = defaultFirtoolArgs) = {
    // check additional options with "firtool --help"
    val cmd = Seq("firtool", "--format=fir") ++ args ++ Seq("--split-verilog", s"-o=${firtool_out_dir}", s"${fir_file}")
    println(s"Log: ${cmd.mkString(" ")}")
    os.proc(cmd).call(stdout = os.Inherit)
  }

}

trait SynthToplevel { this: Toplevel =>

  lazy val annoArgs = Array(
    "--target-dir",
    out_dir,
    "--name",
    outFileName,
    "--legacy-configs",
    configString,
    "--top-module",
    topModule_name,
  )

  def top_mems_firFile = s"${out_dir}/${outFileName}.top.mems.fir"
  def top_mems_vFile   = s"${firtool_out_dir}/${outFileName}.top.mems.v"
  def hammer_ir_file   = s"${out_dir}/${outFileName}.mems.hammer.json"

  def chipyardAnno(): Unit = {
    (new ChipyardStage).execute(annoArgs, Seq.empty)
    addHierarchy2AnnoFile(anno_file, final_anno_file)
  }

  // Call this only after calling chipyardAnno()
  def synthFirrtl2synthSV(args: Seq[String] = defaultFirtoolArgs) = {

    lazy val replSeqMemFirtoolArgs = Seq(
      "--repl-seq-mem",                        // Replace SeqMem instances into external module references.
      s"--repl-seq-mem-file=${mfc_smems_conf}",// List of external module references input to MacroCompiler
    )
    firrtl2sv(args ++ replSeqMemFirtoolArgs)

    // Need trailing space for SFC macrocompiler
    val sed_cmd = Seq("sed", "-i", "s/.*/& /", s"${mfc_smems_conf}")
    println(s"LOG: command invoked \"${sed_cmd.mkString(" ")}\"")
    os.proc(sed_cmd).call(stdout = os.Inherit)

  }

  def firrtl_blackbox_filelist = s"${firtool_out_dir}/firrtl_black_box_resource_files.f"

  def uniquifyModuleNames(): Unit =
    ////  if there are no BB's then the file might not be generated, instead always generate it
    // os.proc(
    //  "touch",
    //  s"${firrtl_blackbox_filelist}"
    // ).call(stdout = os.Inherit)

    // Uniquify module names so there's no conflict between synthesis-runs and simulation-runs
    os.proc(
      s"${curr_dir}/../playground/dependencies/chipyard/scripts/uniquify-module-names.py",
      // s"--model-hier-json ${}",
      s"--top-hier-json ${out_dir}/top_module_hierarchy.json",
      s"--in-all-filelist ${firtool_out_dir}/filelist.f",
      s"--in-bb-filelist ${firrtl_blackbox_filelist}",
      s"--dut ${outFileName}",
      // s"--model ",
      s"--target-dir ${firtool_out_dir}",
      s"--out-dut-filelist ${curr_dir}/${out_dir}/${outFileName}.top.f",
      // s"--out-model-filelist ",
      // s"--out-model-hier-json ",
      s"--gcpath ${firtool_out_dir}",
    ).call(stdout = os.Inherit)

  def chipyardMacroCompiler(sram_mdf_json_file: String): Unit = {
    val sram_cache_json = s"${sram_mdf_json_file}"
    val macroCompilerArgs =
      Array(
        "-n",
        mfc_smems_conf,
        "-v",
        top_mems_vFile,
        "-f",
        top_mems_firFile,
        "-l",
        sram_cache_json,
        "-hir",
        hammer_ir_file,
      )

    val cmd =
      Seq("./../playground/mill", "chipyardTapeout.runMain", "tapeout.macros.MacroCompiler") ++ macroCompilerArgs
    println(s"LOG: command invoked \"${cmd.mkString(" ")}\"")
    os.proc(cmd).call(stdout = os.Inherit)
  }

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
    os.proc(cmd).call(cwd = os.Path(s"${firtool_out_dir}"), stdout = os.Inherit)
  }

  def build() = {
    val cmd = Seq("make", "-j", "-C", "obj_dir/", "-f", s"VTestHarness.mk")
    println(s"LOG: command invoked \"${cmd.mkString(" ")}\"")
    os.proc(cmd).call(cwd = os.Path(s"${firtool_out_dir}"), stdout = os.Inherit)
    println(s"VTestHarness executable in ./${rel_out_dir}/chisel_gen_rtl/obj_dir directory.")
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
