package emitrtl

import circt.stage.ChiselStage
import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.cde.config.Parameters
import org.chipsalliance.diplomacy.lazymodule.{LazyModule, LazyModuleImp}
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class ArtefactsSpec extends AnyFlatSpec with Matchers {
  import ArtefactKind._

  /** Registry-shaped entries, newest first as `ElaborationArtefacts.add` leaves them. */
  private def entries(kv: (String, String)*): Seq[(String, () => String)] =
    kv.reverse.map { case (k, v) => (k, () => v) }

  /** Every file in `dir`, by name, with its content. */
  private def written(dir: os.Path): Map[String, String] =
    os.list(dir).map(p => p.last -> os.read(p)).toMap

  private class Leaf(implicit p: Parameters) extends LazyModule {
    lazy val module = new LazyModuleImp(this) {}
  }

  /** A trivial elaborated root; `LazyModule` construction needs a Builder context. */
  private def root: LazyModule = {
    var lm: LazyModule = null
    ChiselStage.emitCHIRRTL { lm = LazyModule(new Leaf()(Parameters.empty)); lm.module }
    lm
  }

  /** Runs `body`, returning its result and what it printed to stderr. */
  private def capturingErr[T](body: => T): (T, String) = {
    val err    = new java.io.ByteArrayOutputStream
    val result = Console.withErr(err)(body)
    (result, err.toString)
  }

  "Artefacts.emit" should "write every entry as <prefix>.<key> when no kinds are requested" in {
    val dir = os.temp.dir()
    val paths = Artefacts.emit(
      entries("0x2000000.0.regmap.json" -> "regmap", "dts" -> "tree"),
      None,
      Nil,
      dir / "artefacts",
      "CeTop"
    )
    written(dir / "artefacts") shouldBe Map("CeTop.0x2000000.0.regmap.json" -> "regmap", "CeTop.dts" -> "tree")
    paths.toSet shouldBe Set(dir / "artefacts" / "CeTop.0x2000000.0.regmap.json", dir / "artefacts" / "CeTop.dts")
  }

  it should "write root graphML in place of registry graphml entries" in {
    val dir = os.temp.dir()
    val top = root
    Artefacts.emit(entries("graphml" -> "subsystem graph", "dts" -> "tree"), Some(top), Nil, dir, "CeTop")
    val files = written(dir)
    files.keySet shouldBe Set("CeTop.graphml", "CeTop.dts")
    files("CeTop.graphml") should include("<graphml")
    files("CeTop.graphml") shouldBe top.graphML
  }

  /** One registry key per producer in the artefact inventory (issue #5, "Inventory of elaboration artefacts"). */
  private val registry = entries(
    "0x2000000.0.regmap.json" -> "regmap a",
    "0x2000000.1.regmap.json" -> "regmap b",
    "graphml"                 -> "graph",
    "dts"                     -> "tree",
    "json"                    -> "tree json",
    "memmap.json"             -> "memmap",
    "plusArgs"                -> "plusargs",
    "mem.axi4.json"           -> "axi4 ids",
    "l2.json"                 -> "l2",
    "shell.xdc"               -> "xdc",
    "shell.sdc"               -> "sdc",
    "shell.io.pdc"            -> "pdc",
    "vc707debugjtag.xdc"      -> "jtag xdc",
    "old-shell.vivado.tcl"    -> "old shell tcl",
    "shell.vivado.tcl"        -> "shell tcl",
    "assign.tcl"              -> "quartus assign",
    "vcu118mig.vivado.tcl"    -> "mig ip",
    "alterapll.quartus.tcl"   -> "pll ip",
    "PolarFireCCC.libero.tcl" -> "ccc ip",
    "dromajo_params.h"        -> "dromajo"
  )

  private val selections: Seq[(ArtefactKind, Set[String])] = Seq(
    Regmap                    -> Set("0x2000000.0.regmap.json", "0x2000000.1.regmap.json"),
    GraphML                   -> Set("graphml"),
    Dts                       -> Set("dts"),
    DtsJson                   -> Set("json"),
    MemMap                    -> Set("memmap.json"),
    PlusArgs                  -> Set("plusArgs"),
    Axi4IdMap                 -> Set("mem.axi4.json"),
    L2                        -> Set("l2.json"),
    FpgaShell -> Set(
      "shell.xdc",
      "shell.sdc",
      "shell.io.pdc",
      "vc707debugjtag.xdc",
      "old-shell.vivado.tcl",
      "shell.vivado.tcl",
      "assign.tcl"
    ),
    FpgaIp                    -> Set("vcu118mig.vivado.tcl", "alterapll.quartus.tcl", "PolarFireCCC.libero.tcl"),
    ByPattern("dromajo_*.h")  -> Set("dromajo_params.h"),
    ByPattern("*.regmap.json") -> Set("0x2000000.0.regmap.json", "0x2000000.1.regmap.json")
  )

  selections.foreach { case (kind, keys) =>
    it should s"write exactly the $kind subset" in {
      val dir = os.temp.dir()
      Artefacts.emit(registry, None, Seq(kind), dir, "CeTop")
      written(dir).keySet shouldBe keys.map(k => s"CeTop.$k")
    }
  }

  it should "write the union of several requested kinds" in {
    val dir = os.temp.dir()
    Artefacts.emit(registry, None, Seq(Dts, L2), dir, "CeTop")
    written(dir).keySet shouldBe Set("CeTop.dts", "CeTop.l2.json")
  }

  it should "write root graphML only when graphML is selected" in {
    val top = root
    val withGraph = os.temp.dir()
    Artefacts.emit(registry, Some(top), Seq(GraphML), withGraph, "CeTop")
    written(withGraph) shouldBe Map("CeTop.graphml" -> top.graphML)

    val withoutGraph = os.temp.dir()
    Artefacts.emit(registry, Some(top), Seq(Dts), withoutGraph, "CeTop")
    written(withoutGraph).keySet shouldBe Set("CeTop.dts")
  }

  it should "write a duplicate key with identical content once, without a warning" in {
    val dir = os.temp.dir()
    val (paths, err) = capturingErr {
      Artefacts.emit(entries("plusArgs" -> "same", "plusArgs" -> "same"), None, Nil, dir, "CeTop")
    }
    written(dir) shouldBe Map("CeTop.plusArgs" -> "same")
    paths shouldBe Seq(dir / "CeTop.plusArgs")
    err shouldBe empty
  }

  it should "number differing duplicates before the last extension, in the order they were added, with a warning" in {
    val dir = os.temp.dir()
    val (_, err) = capturingErr {
      Artefacts.emit(
        entries("mem.axi4.json" -> "mig 0", "dts" -> "tree 0", "mem.axi4.json" -> "mig 1", "dts" -> "tree 1"),
        None,
        Nil,
        dir,
        "CeTop"
      )
    }
    written(dir) shouldBe Map(
      "CeTop.mem.axi4.json"   -> "mig 0",
      "CeTop.mem.axi4.1.json" -> "mig 1",
      "CeTop.dts"             -> "tree 0",
      "CeTop.1.dts"           -> "tree 1"
    )
    err should include("mem.axi4.json")
    err should include("dts")
  }

  it should "warn about, and not synthesise, a requested kind that matches nothing" in {
    val dir = os.temp.dir()
    val (_, err) = capturingErr {
      Artefacts.emit(
        entries("0x2000000.0.regmap.json" -> "regmap"),
        None,
        Seq(Regmap, Dts, GraphML, ByPattern("*.xyz")),
        dir,
        "CeTop"
      )
    }
    written(dir).keySet shouldBe Set("CeTop.0x2000000.0.regmap.json")
    err should include("dts")
    err should include("graphml")
    err should include("*.xyz")
    err should not include ("regmap")
  }

  "Artefacts.drain" should "empty the ElaborationArtefacts registry" in {
    ElaborationArtefacts.add("dts", "tree")
    Artefacts.drain()
    ElaborationArtefacts.files shouldBe empty
  }
}
