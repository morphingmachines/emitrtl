package emitrtl

import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.cde.config.Parameters
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
