// import Mill dependency
import mill._
import scalalib._

// Load the plugin from Maven Central via ivy/coursier
import $ivy.`de.tototec::de.tobiasroeser.mill.vcs.version::0.4.1`
import de.tobiasroeser.mill.vcs.version.VcsVersion

// Global Scala Version
object ivys {
  // USE_CHISEL7 selects Chisel 7 (as chipyard's build.sbt); default stays on Chisel 6
  val useChisel7 = sys.env.contains("USE_CHISEL7")
  val sv = if (useChisel7) "2.13.18" else "2.13.12"
  val cv = if (useChisel7) "7.13.0" else "6.7.0"
  // Mill caches targets by code, not env: refuse to reuse an out/ built with the other Chisel.
  // Call from the top level of build.sc so it runs on every mill invocation.
  def checkOutDir(): Unit = {
    val stamp = os.pwd / "out" / "chisel-version"
    if (os.exists(stamp) && os.read(stamp) != cv)
      sys.error(s"out/ was built with Chisel ${os.read(stamp)}; run `rm -rf out` before building with Chisel $cv")
    os.write.over(stamp, cv, createFolders = true)
  }
  val cv1 = "3.6.1" // chipyard.tapeout
  val sv1 = "2.13.10" // chipyard.tapeout
  // the first version in this Map is the mainly supported version which will be used to run tests
  val chiselCrossVersions = Map(
    "7.13.0" -> (ivy"org.chipsalliance::chisel:7.13.0", ivy"org.chipsalliance:::chisel-plugin:7.13.0"),
    "6.7.0" -> (ivy"org.chipsalliance::chisel:6.7.0", ivy"org.chipsalliance:::chisel-plugin:6.7.0"),
    "3.6.1" -> (ivy"edu.berkeley.cs::chisel3:3.6.1", ivy"edu.berkeley.cs:::chisel-plugin:3.6.1") // chipyard.tapeout
  )

  val sourcecode = ivy"com.lihaoyi::sourcecode:0.3.1"

  val upickle = ivy"com.lihaoyi::upickle:1.3.15"
  val oslib = ivy"com.lihaoyi::os-lib:0.7.8"
  val pprint = ivy"com.lihaoyi::pprint:0.6.6"
  val utest = ivy"com.lihaoyi::utest:0.7.10"
  val jline = ivy"org.scala-lang.modules:scala-jline:2.12.1"
  val scalatest = ivy"org.scalatest::scalatest:3.2.19"
  val scalatestplus = ivy"org.scalatestplus::scalacheck-1-14:3.1.1.1"
  val scalacheck = ivy"org.scalacheck::scalacheck:1.14.3"
  val scopt = ivy"com.github.scopt::scopt:3.7.1"
  val playjson = ivy"com.typesafe.play::play-json:2.9.4"
  val breeze = ivy"org.scalanlp::breeze:1.1"
  val parallel = ivy"org.scala-lang.modules:scala-parallel-collections_3:1.0.4"
  val mainargs = ivy"com.lihaoyi::mainargs:0.5.0"
  val json4sJackson =
    if (useChisel7) ivy"io.github.json4s::json4s-jackson:4.1.0" else ivy"org.json4s::json4s-jackson:4.0.5"
  val scalaReflect = ivy"org.scala-lang:scala-reflect:${sv}"
}

// For modules not support mill yet, need to have a ScalaModule depend on our own repositories.
trait CommonModule extends ScalaModule {
  override def scalaVersion = ivys.sv

  def chiselIvy = Some(ivys.chiselCrossVersions(ivys.cv)._1)

  def chiselPluginIvy = Some(ivys.chiselCrossVersions(ivys.cv)._2)

  override def ivyDeps = T(super.ivyDeps() ++ chiselIvy)
  override def scalacPluginIvyDeps = T(super.scalacPluginIvyDeps() ++ chiselPluginIvy)

}