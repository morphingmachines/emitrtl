package emitrtl

import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class AnnoFileSpec extends AnyFlatSpec with Matchers {

  private class TempTop extends Toplevel {
    val dir                     = os.temp.dir()
    def topModule               = ??? // never elaborated; the name below stands in for it
    override def topModule_name = "emitrtl.GCD"
    override def out_dir        = dir.toString
  }

  private val hierarchyAnno =
    """"class":"sifive.enterprise.firrtl.ModuleHierarchyAnnotation""""

  private def compact(path: os.Path): String = os.read(path).filterNot(_.isWhitespace)

  "Toplevel.addHierarchy2AnnoFile" should "write only the hierarchy annotation when there is no input anno file" in {
    val top = new TempTop
    top.addHierarchy2AnnoFile(top.anno_file, top.final_anno_file)
    val out = compact(os.Path(top.final_anno_file))
    out should include(hierarchyAnno)
    out should include(s""""filename":"${top.out_dir}/top_module_hierarchy.json"""")
    os.list(top.dir).map(_.last) shouldBe Seq("GCD.appended.anno.json")
  }

  it should "append the hierarchy annotation to the input annotations" in {
    val top = new TempTop
    os.write(os.Path(top.anno_file), """[{"class":"firrtl.transforms.DedupGroupAnnotation","group":"g"}]""")
    top.addHierarchy2AnnoFile(top.anno_file, top.final_anno_file)
    val out = compact(os.Path(top.final_anno_file))
    out should startWith("""[{"class":"firrtl.transforms.DedupGroupAnnotation","group":"g"},{""")
    out should include(hierarchyAnno)
    os.list(top.dir).map(_.last).sorted shouldBe Seq("GCD.anno.json", "GCD.appended.anno.json")
  }

  it should "replace a stale output and a stale extra anno file" in {
    val top = new TempTop
    os.write(os.Path(top.final_anno_file), "stale")
    os.write(top.dir / "GCD.extrafirtool.anno.json", "stale")
    top.addHierarchy2AnnoFile(top.anno_file, top.final_anno_file)
    compact(os.Path(top.final_anno_file)) should include(hierarchyAnno)
    os.list(top.dir).map(_.last) shouldBe Seq("GCD.appended.anno.json")
  }
}
