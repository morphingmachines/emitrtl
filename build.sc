// Standalone build for developing emitrtl, checked out next to playground (see playground's ADR 0001).
// Playground itself builds emitrtl from playground_modules.sc; this build adds formatting, linting and
// strict compiler warnings on top of the same shared dependencies.
import mill._
import mill.scalalib._
import mill.scalalib.scalafmt.ScalafmtModule
import $ivy.`com.goyeau::mill-scalafix::0.3.1`
import com.goyeau.mill.scalafix.ScalafixModule

import $file.^.playground.{builddefs => playground_build}
import $file.^.playground.playground_modules

playground_build.ivys.checkOutDir()

// Shared dependencies from ../playground. Not a RootModule: playground_modules also defines an
// `emitrtl`, which builds playground's copy rather than this checkout.
object shared extends playground_modules.PlaygroundModules

object emitrtl extends playground_build.CommonModule with SbtModule with ScalafmtModule with ScalafixModule {
  override def millSourcePath = os.pwd

  // Chisel-version-specific sources (e.g. VerilatorSimulator), selected by USE_CHISEL7
  override def sources = T.sources {
    super.sources() :+ PathRef(
      millSourcePath / "src" / "main" / (if (playground_build.ivys.useChisel7) "scala-chisel7" else "scala-chisel6")
    )
  }

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
      // Chisel 7 deprecates all of package firrtl, but ChipyardStage still returns firrtl.AnnotationSeq
      "-Wconf:cat=deprecation&msg=All APIs in package firrtl are deprecated:s",
    )
  }

  override def moduleDeps =
    super.moduleDeps ++ Seq(shared.mycde, shared.mydiplomacy, shared.myrocketchip, shared.chipyardAnnotations)

  override def ivyDeps = T(super.ivyDeps() ++ Agg(playground_build.ivys.scalatest))

  object test extends SbtModuleTests with TestModule.ScalaTest
}
