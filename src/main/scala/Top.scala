package emitrtl

import circt.stage.ChiselStage
import freechips.rocketchip.diplomacy.LazyModule
import freechips.rocketchip.util.ElaborationArtefacts

import java.io._
import java.nio.file._

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
      // "--strip-debug-info",
      "--lower-memories",
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
