package emitrtl

import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class ArtefactKindSpec extends AnyFlatSpec with Matchers {
  import ArtefactKind._

  "ArtefactKind.parse" should "accept a single kind name" in {
    parse("regmap") shouldBe Right(Seq(Regmap))
  }

  it should "accept every kind name, comma-separated and case-insensitive" in {
    parse("Regmap,GRAPHML,dts,Dts-Json,mem-map,plus-args,axi4-id-map,l2,fpga-shell,FPGA-IP") shouldBe Right(
      Seq(Regmap, GraphML, Dts, DtsJson, MemMap, PlusArgs, Axi4IdMap, L2, FpgaShell, FpgaIp)
    )
  }

  it should "accept glob: patterns verbatim alongside names" in {
    parse("regmap,glob:*.L2.json") shouldBe Right(Seq(Regmap, ByPattern("*.L2.json")))
  }

  it should "not infer a pattern from glob characters" in {
    parse("*.regmap.json") shouldBe a[Left[_, _]]
  }

  it should "reject an unknown name with an error listing the valid names" in {
    val err = parse("regmap,regmaps").left.getOrElse(fail("expected Left"))
    err should include("regmaps")
    Seq("regmap", "graphml", "dts", "dts-json", "mem-map", "plus-args", "axi4-id-map", "l2", "fpga-shell", "fpga-ip",
      "glob:").foreach(n => err should include(n))
  }

  it should "reject a malformed glob before anything is elaborated" in {
    parse("glob:[") shouldBe a[Left[_, _]]
  }

  // An empty selection means everything, so a stray comma must not silently become one
  it should "reject empty entries" in {
    Seq(",", "regmap,", ",regmap", "regmap,,dts").foreach(s => parse(s) shouldBe a[Left[_, _]])
  }
}
