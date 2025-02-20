package emitrtl

import chisel3._
import circt.stage.ChiselStage
import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.diplomacy.lazymodule.LazyModule
import chipyard.stage._
//import tapeout.macros._

import java.io._
import java.nio.file._

import sys.process._

trait TestHarnessShell extends Module {
  val io = IO(new Bundle { val success = Output(Bool()) })
}

//dut is passed as call-by-name parameter as Module instantiate should be wrapped in Module()
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

trait SynthToplevel {
  def synthTopModule: chisel3.RawModule
  def synthTopModule_name = synthTopModule.getClass().getName().split("\\$").mkString(".")

  def synthConfig_name : String  // $(CONFIG_PACKAGE):$(CONFIG)
  def annoName = s"${synthTopModule_name}.${synthConfig_name.split(":")(1)}"

  def curr_dir : String = os.pwd.toString
  def synth_out_dir = s"generated_sv_dir/vlsi/${annoName}"
  def collateral_out_dir = s"${synth_out_dir}/gen-collateral"

  /** For firtoolOpts run `firtool --help` There is an overlap between ChiselStage args and firtoolOpts.
    *
    * TODO: Passing "--Split-verilog" "--output-annotation-file" to firtool is not working.
    */

  lazy val synthChiselArgs   = Array("--full-stacktrace", "--target-dir", synth_out_dir, "--split-verilog")
  lazy val synthFirtroolArgs = Array("-dedup")

  lazy val annoArgs = Array("--target-dir", synth_out_dir, "--name", annoName, 
    "--legacy-configs", synthConfig_name, "--top-module", synthTopModule_name)

  def mfc_top_hrchy_json = s"${curr_dir}"+
                           s"/${synth_out_dir}/top_module_hierarchy.json"

  def mfc_extra_anno_contents = {
    s"""
    [
      {
		    "class": "sifive.enterprise.firrtl.ModuleHierarchyAnnotation",
		    "filename": "${mfc_top_hrchy_json}"
	    }
    ]
    """
  }

  def mfc_lowering_options = s"emittedLineLength=2048,noAlwaysComb," +
                             s"disallowLocalVariables,verifLabels," +
                             s"disallowPortDeclSharing," +
                             s"locationInfoStyle=wrapInAtSquareBracket"

  def mfc_smems_conf  = s"${curr_dir}" + s"/${synth_out_dir}/${annoName}.mems.conf"
  def anno_file       = s"${curr_dir}" + s"/${synth_out_dir}/${annoName}.anno.json"
  def extra_anno_file = s"${curr_dir}" + s"/${synth_out_dir}/${annoName}.extrafirtool.anno.json"
  def final_anno_file = s"${curr_dir}" + s"/${synth_out_dir}/${annoName}.appended.anno.json"
  def top_mems_firFile = s"${curr_dir}" + s"/${synth_out_dir}/${annoName}.top.mems.fir"
  def top_mems_vFile   = s"${curr_dir}" + s"/${collateral_out_dir}/${annoName}.top.mems.v"
  // TODO: Change
  def sram_cache_json  = "chipyard/.conda-env/lib/python3.10/site-packages/hammer/technology/nangate45/sram-cache.json"

  lazy val macroCompilerArgs = Array("-n", mfc_smems_conf, 
    "-v", top_mems_vFile, 
    "-f", top_mems_firFile,
    "-l", sram_cache_json, 
    "--mode", "strict")

  def create_final_anno_file() : Unit = {
    os.proc(
      "rm",
      "-rf",
      s"${extra_anno_file}"
    ).call(stdout = os.Inherit)

    os.write(
      os.Path(s"${extra_anno_file}"),
      s"${mfc_extra_anno_contents}",
    )

    // This works:
    //Seq("jq","-s","[.[][]]", s"${anno_file}", s"${extra_anno_file}", ">", s"${final_anno_file}").!
    val jq_cmd = Seq("jq","-s","[.[][]]", s"${anno_file}", s"${extra_anno_file}", ">", s"${final_anno_file}")
    println(s"LOG: command invoked \"${jq_cmd.mkString(" ")}\"")
    os.proc(jq_cmd).call(stdout = os.Inherit)

    //os.proc(
    //  "jq -s '[.[][]]'",
    //  s"${anno_file}",
    //  s"${extra_anno_file}",
      //"\'firrtl\'",
      //"blah2.json"
      //"/home/sumana/Desktop/Workspace/redefine-workspace/RRM/generated_sv_dir/vlsi/rrm.syn.AccelRRM.RRMConfig/rrm.syn.AccelRRM.RRMConfig.anno.json"
      //"/home/sumana/Desktop/Workspace/redefine-workspace/RRM/generated_sv_dir/vlsi/rrm.syn.AccelRRM.RRMConfig/rrm.syn.AccelRRM.RRMConfig.extrafirtoo2.anno.json"
      //s"${anno_file}"
    //  ">",
    //  s"${final_anno_file}"
    //).call(stdout = os.Inherit)
  }

  def chipyardAnno(): Unit = {
    (new ChipyardStage).execute(annoArgs, Seq.empty)
    create_final_anno_file()
  }
  //def chiselAnno(): Unit = try {
  //    (new ChipyardStage).execute(args, Seq.empty)
  //} catch {
  //  case a: StageError =>
  //    System.exit(a.code.number)
  //  case a: OptionsException =>
  //    StageUtils.dramaticUsageError(a.message)
  //    System.exit(1)
  //}

  //def chisel2synthFirrtl() = {
  //  val str_synthFirrtl = ChiselStage.emitCHIRRTL(synthTopModule, args = Array("--full-stacktrace"))
  //  Files.createDirectories(Paths.get("generated_sv_dir/vlsi"))
  //  val pwSynth = new PrintWriter(new File(s"${synth_out_dir}.fir"))
  //  pwSynth.write(str_synthFirrtl)
  //  pwSynth.close()
  //}

  def fir_file = s"${curr_dir}/${synth_out_dir}/${annoName}.fir"

  // Call this only after calling chipyardAnno()
  def synthFirrtl2synthSV() : Unit = { 
    os.proc(
      "firtool",
      "--format=fir",                      //extra
      "--export-module-hierarchy",         //extra
		  "--verify-each=true",                //extra
		  "--warn-on-unprocessed-annotations", //extra
      "--disable-annotation-classless",    //extra
      "--disable-annotation-unknown",
      "--mlir-timing",                     //extra
      s"--lowering-options=${mfc_lowering_options}", //extra
      //"--strip-debug-info",
      //"--lower-memories",
      "--repl-seq-mem",                    //extra
		  s"--repl-seq-mem-file=${mfc_smems_conf}", //extra
		  s"--annotation-file=${final_anno_file}",
      "--split-verilog",
      s"-o=${collateral_out_dir}",
      s"${fir_file}",
    ).call(stdout = os.Inherit) // check additional options with "firtool --help"
    
    // Need trailing space for SFC macrocompiler
    val sed_cmd = Seq("sed", "-i", "s/.*/& /", s"${mfc_smems_conf}")
    println(s"LOG: command invoked \"${sed_cmd.mkString(" ")}\"")
    os.proc(sed_cmd).call(stdout = os.Inherit)

  }
  
  def firrtl_blackbox_filelist = s"${collateral_out_dir}/firrtl_black_box_resource_files.f"

  def uniquifyModuleNames() : Unit = {
    ////  if there are no BB's then the file might not be generated, instead always generate it
    //os.proc(
    //  "touch",
    //  s"${firrtl_blackbox_filelist}"
    //).call(stdout = os.Inherit)

    // Uniquify module names so there's no conflict between synthesis-runs and simulation-runs
    os.proc(
      "/home/sumana/Desktop/Workspace/redefine-workspace/playground/dependencies/chipyard/scripts/uniquify-module-names.py",
      //s"--model-hier-json ${}",
      s"--top-hier-json ${synth_out_dir}/top_module_hierarchy.json",
      s"--in-all-filelist ${collateral_out_dir}/filelist.f",
      s"--in-bb-filelist ${firrtl_blackbox_filelist}",
      s"--dut AccelRRM",
      //s"--model ",
      s"--target-dir ${collateral_out_dir}",
      s"--out-dut-filelist ${curr_dir}/${synth_out_dir}/${annoName}.top.f",
      //s"--out-model-filelist ",
      //s"--out-model-hier-json ",
      s"--gcpath ${collateral_out_dir}"
    ).call(stdout = os.Inherit)
  }

  //def chipyardMacroCompiler() : Unit = {
  //  (MacroCompiler).run(macroCompilerArgs)
  //}

}

trait LazyToplevel extends Toplevel with SynthToplevel {
  def lazyTop: LazyModule
  override def topModule      = lazyTop.module.asInstanceOf[chisel3.RawModule]
  override def synthTopModule = lazyTop.module.asInstanceOf[chisel3.RawModule]
  override def topModule_name = lazyTop.getClass().getName().split("\\$").mkString(".")
  override def synthTopModule_name = lazyTop.getClass().getName().split("\\$").mkString(".")

  // default synth Config
  override def synthConfig_name : String = "None:None" // $(CONFIG_PACKAGE):$(CONFIG)

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
  def dut: TestHarnessShell

  override def topModule      = new TestHarness(dut)
  override def topModule_name = dut.getClass().getName().split("\\$").mkString(".")

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
          "TestHarness",
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
  override def dut            = lazyTop.module.asInstanceOf[TestHarnessShell]
  override def topModule_name = lazyTop.getClass().getName().split("\\$").mkString(".")
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
