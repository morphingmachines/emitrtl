package emitrtl

import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.cde.config.{Config, Parameters}
import org.chipsalliance.diplomacy.lazymodule.{LazyModule, LazyModuleImp}
import org.scalatest.BeforeAndAfterEach
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class ToplevelArtefactsSpec extends AnyFlatSpec with Matchers with BeforeAndAfterEach {

  // The registry is global; start and leave each test with it empty
  override def beforeEach(): Unit = Artefacts.drain()
  override def afterEach():  Unit = Artefacts.drain()

  /** Adds a registry entry while the Top is built and another while its module elaborates, as `l2.json` and regmap
    * producers do.
    */
  private class Producer(tag: String)(implicit p: Parameters) extends LazyModule {
    ElaborationArtefacts.add("l2.json", s"$tag l2")
    lazy val module = new LazyModuleImp(this) {
      ElaborationArtefacts.add("0x2000000.0.regmap.json", s"$tag regmap")
    }
  }

  private class DiplomacyTop(tag: String, kinds: Seq[ArtefactKind] = Nil) extends LazyToplevel {
    val dir                    = os.temp.dir()
    lazy val lazyTop           = LazyModule(new Producer(tag)(Parameters.empty))
    override def out_dir       = dir.toString
    override def artefactKinds = kinds
  }

  private def artefacts(top: DiplomacyTop): Map[String, String] =
    os.list(top.dir / "artefacts").map(p => p.last -> os.read(p)).toMap

  "LazyToplevel.chisel2firrtl" should "write graphML and every registry artefact to <out_dir>/artefacts" in {
    val top = new DiplomacyTop("a")
    top.chisel2firrtl()
    val files = artefacts(top)
    files.keySet shouldBe Set("Producer.l2.json", "Producer.0x2000000.0.regmap.json", "Producer.graphml")
    files("Producer.l2.json") shouldBe "a l2"
    files("Producer.graphml") should include("<graphml")
  }

  it should "write only the requested kinds" in {
    val top = new DiplomacyTop("a", Seq(ArtefactKind.Regmap))
    top.chisel2firrtl()
    artefacts(top) shouldBe Map("Producer.0x2000000.0.regmap.json" -> "a regmap")
  }

  it should "not leak one Top's artefacts into the next Top elaborated in the same JVM" in {
    val first  = new DiplomacyTop("first")
    val second = new DiplomacyTop("second")
    first.chisel2firrtl()
    second.chisel2firrtl()
    artefacts(second).values.filterNot(_.contains("<graphml")).toSet shouldBe Set("second l2", "second regmap")
  }

  "LazyToplevel.genDiplomacyGraph" should "write <out_dir>/<className>.graphml and leave the registry untouched" in {
    val top = new DiplomacyTop("a")
    top.chisel2firrtl()
    ElaborationArtefacts.add("dts", "tree")
    val before = ElaborationArtefacts.files
    top.genDiplomacyGraph()
    ElaborationArtefacts.files shouldBe before
    os.read(top.dir / s"${top.lazyTop.className}.graphml") should include("<graphml")
  }

  private class PlainTop extends Toplevel {
    val dir                    = os.temp.dir()
    lazy val topModule         = new GCD
    override def out_dir       = dir.toString
    override def artefactKinds = Seq(ArtefactKind.GraphML)
  }

  "Toplevel.chisel2firrtl" should "only warn when graphML is requested from a Top without a root LazyModule" in {
    val top = new PlainTop
    val err = new java.io.ByteArrayOutputStream
    Console.withErr(err)(top.chisel2firrtl())
    err.toString should include("graphml")
    os.exists(top.dir / "artefacts") shouldBe false
  }
}

/** Top-level so `ChipyardStage` can load it by name and build it through its `Parameters` constructor. Its `l2.json`
  * content tells an elaborated instance from one that was only built.
  */
class StageProducer(implicit p: Parameters) extends LazyModule {
  private var elaborated = false
  ElaborationArtefacts.add("l2.json", s"stage l2, elaborated=$elaborated")
  lazy val module = new LazyModuleImp(this) {
    elaborated = true
    ElaborationArtefacts.add("0x2000000.0.regmap.json", "stage regmap")
  }
}

/** The config `ChipyardStage` builds `StageProducer` with */
class StageProducerConfig extends Config(Parameters.empty)

class SynthToplevelArtefactsSpec extends AnyFlatSpec with Matchers with BeforeAndAfterEach {

  override def beforeEach(): Unit = Artefacts.drain()
  override def afterEach():  Unit = Artefacts.drain()

  private class StageTop(kinds: Seq[ArtefactKind] = Nil) extends LazyToplevel with SynthToplevel {
    val dir                    = os.temp.dir()
    lazy val lazyTop           = LazyModule(new StageProducer()(Parameters.empty))
    override def out_dir       = dir.toString
    override def configString  = "emitrtl:StageProducerConfig"
    override def artefactKinds = kinds
  }

  private def artefacts(dir: os.Path): Map[String, String] =
    os.list(dir / "artefacts").map(p => p.last -> os.read(p)).toMap

  /** Renumbers `LazyModule` ids by first appearance; they come from a global counter, and the reflection build on the
    * `ChipyardStage` path offsets them.
    */
  private def normaliseIds(graphML: String): String = {
    val ids = scala.collection.mutable.LinkedHashMap.empty[String, Int]
    """(id|source|target)="(\d+)""".r.replaceAllIn(
      graphML,
      m => s"""${m.group(1)}="${ids.getOrElseUpdate(m.group(2), ids.size + 1)}""",
    )
  }

  "SynthToplevel.chipyardAnno" should "write graphML and every registry artefact to <out_dir>/artefacts" in {
    val top = new StageTop
    top.chipyardAnno()
    val files = artefacts(top.dir)
    files.keySet shouldBe Set("StageProducer.l2.json", "StageProducer.0x2000000.0.regmap.json", "StageProducer.graphml")
    files("StageProducer.graphml") should include("<graphml")
  }

  it should "write each key once, from the stage's elaborated Top" in {
    val top = new StageTop
    top.chipyardAnno()
    artefacts(top.dir)("StageProducer.l2.json") shouldBe "stage l2, elaborated=true"
  }

  it should "still write the legacy <out_dir>/<outFileName>.<key> files" in {
    val top = new StageTop
    top.chipyardAnno()
    os.read(top.dir / "StageProducer.0x2000000.0.regmap.json") shouldBe "stage regmap"
    os.read(top.dir / "StageProducer.l2.json") shouldBe "stage l2, elaborated=true"
  }

  it should "match the chisel2firrtl artefacts by name and content, up to graphML LazyModule ids" in {
    val stage = new StageTop
    stage.chipyardAnno()
    val chisel = new StageTop
    chisel.chisel2firrtl()
    val (fromStage, fromChisel) = (artefacts(stage.dir), artefacts(chisel.dir))
    fromStage.keySet shouldBe fromChisel.keySet
    fromStage.removed("StageProducer.graphml") shouldBe fromChisel.removed("StageProducer.graphml")
    normaliseIds(fromStage("StageProducer.graphml")) shouldBe normaliseIds(fromChisel("StageProducer.graphml"))
  }

  it should "write only the requested kinds" in {
    val top = new StageTop(Seq(ArtefactKind.GraphML))
    top.chipyardAnno()
    artefacts(top.dir).keySet shouldBe Set("StageProducer.graphml")
  }
}
