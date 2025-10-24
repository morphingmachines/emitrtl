// import Mill dependency
import mill._
import mill.define.Sources
import mill.modules.Util
import mill.scalalib.TestModule.ScalaTest
import scalalib._
import publish._
// support BSP
import mill.bsp._

import scalafmt._
import $ivy.`com.goyeau::mill-scalafix::0.3.1`
import com.goyeau.mill.scalafix.ScalafixModule

import $file.builddefs

import $file.dependencies.cde.{build => cde_build}
import $file.dependencies.`rocket-chip`.{common => rocketchip_common}
import $file.dependencies.`berkeley-hardfloat`.{common => hardfloat_common}
import $file.dependencies.diplomacy.{common => diplomacy_common}

object macros extends rocketchip_common.MacrosModule with SbtModule {
  override def millSourcePath = os.pwd / "dependencies" / "rocket-chip" / "macros"
  def scalaVersion: T[String] = T(builddefs.ivys.sv)
  def scalaReflectIvy = builddefs.ivys.scalaReflect
}

object ivys {
  val cv = builddefs.ivys.cv
}

object mycde extends cde_build.CDE with PublishModule {
  override def millSourcePath = os.pwd / "dependencies" / "cde" / "cde"
  def scalaVersion: T[String] = T(builddefs.ivys.sv)
}

object mydiplomacy
  extends diplomacy_common.DiplomacyModule
  with builddefs.CommonModule {
  override def millSourcePath = os.pwd / "dependencies" / "diplomacy" / "diplomacy"
  override def scalaVersion   = builddefs.ivys.sv
  def chiselModule            = None
  def chiselPluginJar         = None
  def chiselIvy               = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._1)
  def chiselPluginIvy         = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._2)
  def sourcecodeIvy           = builddefs.ivys.sourcecode
  def cdeModule               = mycde
}

object myrocketchip extends rocketchip_common.RocketChipModule with SbtModule {

  override def millSourcePath = os.pwd / "dependencies" / "rocket-chip"

  override def scalaVersion = builddefs.ivys.sv

  def chiselModule = None

  def chiselPluginJar = None

  def chiselIvy = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._1)

  def chiselPluginIvy = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._2)

  override def ivyDeps             = T(super.ivyDeps() ++ chiselIvy)
  override def scalacPluginIvyDeps = T(super.scalacPluginIvyDeps() ++ chiselPluginIvy)

  def macrosModule = macros

  def hardfloatModule: ScalaModule = myhardfloat

  def cdeModule: ScalaModule = mycde

  def diplomacyModule: ScalaModule = mydiplomacy

  def mainargsIvy = builddefs.ivys.mainargs

  def json4sJacksonIvy = builddefs.ivys.json4sJackson

}

// UCB
object myhardfloat extends hardfloat_common.HardfloatModule with PublishModule {
  override def millSourcePath = os.pwd / "dependencies" / "berkeley-hardfloat" /"hardfloat"
  def scalaVersion            = builddefs.ivys.sv

  def chiselModule = None

  def chiselPluginJar = None

  def chiselIvy = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._1)

  def chiselPluginIvy = Some(builddefs.ivys.chiselCrossVersions(ivys.cv)._2)

  override def ivyDeps             = T(super.ivyDeps() ++ chiselIvy)
  override def scalacPluginIvyDeps = T(super.scalacPluginIvyDeps() ++ chiselPluginIvy)
  // remove test dep
  override def allSourceFiles = T(
    super.allSourceFiles().filterNot(_.path.last.contains("Tester")).filterNot(_.path.segments.contains("test")),
  )

  def publishVersion = de.tobiasroeser.mill.vcs.version.VcsVersion.vcsState().format()

  def pomSettings = PomSettings(
    description = artifactName(),
    organization = "edu.berkeley.cs",
    url = "http://chisel.eecs.berkeley.edu",
    licenses = Seq(License.`BSD-3-Clause`),
    versionControl = VersionControl.github("ucb-bar", "berkeley-hardfloat"),
    developers = Seq(
      Developer("jhauser-ucberkeley", "John Hauser", "https://www.colorado.edu/faculty/hauser/about/"),
      Developer("aswaterman", "Andrew Waterman", "https://aspire.eecs.berkeley.edu/author/waterman/"),
      Developer("yunsup", "Yunsup Lee", "https://aspire.eecs.berkeley.edu/author/yunsup/"),
    ),
  )
}

object chipyardAnnotations extends builddefs.CommonModule with SbtModule {
  override def millSourcePath = os.pwd / "dependencies" / "chipyard" / "tools" / "stage"
  override def moduleDeps     = super.moduleDeps ++ Seq(myrocketchip)
}
 
object chipyardTapeout extends builddefs.CommonModule with SbtModule {
  override def millSourcePath = os.pwd / "dependencies" / "chipyard" / "tools" / "tapeout"
  //override def millSourcePath = os.pwd / "dependencies" / "chipyard" / "tools" / "tapeout"
  override def scalaVersion = builddefs.ivys.sv1 // stuck on chisel3 2.13.10
  override def chiselIvy = Some(builddefs.ivys.chiselCrossVersions(builddefs.ivys.cv1)._1) // stuck on chisel3 and SFC
  override def chiselPluginIvy = Some(builddefs.ivys.chiselCrossVersions(builddefs.ivys.cv1)._1)
  //def playjsonIvy = ivys.playjson
  def playjsonIvy = ivy"com.typesafe.play::play-json:2.9.2"
  override def ivyDeps = T(super.ivyDeps() ++ Some(playjsonIvy))
  override def moduleDeps = super.moduleDeps
}


trait ScalacOptions extends ScalaModule {
  override def scalacOptions = T {
    super.scalacOptions() ++ Seq(
      "-unchecked",
      "-deprecation",
      "-language:reflectiveCalls",
      "-feature",
      "-Xcheckinit",
      "-Xfatal-warnings",
      "-Ywarn-dead-code",
      "-Ywarn-unused",
    )
  }
}

object emitrtl
  extends builddefs.CommonModule
  with SbtModule
  with ScalafmtModule
  with ScalafixModule
  with ScalacOptions {
  override def millSourcePath = os.pwd

  override def moduleDeps = super.moduleDeps ++ Seq(myrocketchip, chipyardAnnotations, chipyardTapeout)
}
