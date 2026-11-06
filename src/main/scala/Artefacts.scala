package emitrtl

import freechips.rocketchip.util.ElaborationArtefacts
import org.chipsalliance.diplomacy.lazymodule.LazyModule

import java.nio.file.{FileSystems, Paths}
import scala.util.Try

/** A category of artefact, defined by the `ElaborationArtefacts` registry keys it matches. */
sealed trait ArtefactKind {
  def matches(key: String): Boolean

  /** How the kind is written in a selection string, as `parse` accepts it. */
  def label: String
}

object ArtefactKind {
  /** A named kind matches a registry key equal to, or ending in `.` followed by, one of its suffixes. */
  sealed abstract class Named(val name: String, suffixes: String*) extends ArtefactKind {
    def label: String = name
    def matches(key: String): Boolean = suffixes.exists(s => key == s || key.endsWith(s".$s"))
  }

  case object Regmap    extends Named("regmap", "regmap.json")
  case object GraphML   extends Named("graphml", "graphml")
  case object Dts       extends Named("dts", "dts")
  case object MemMap    extends Named("mem-map", "memmap.json")
  case object PlusArgs  extends Named("plus-args", "plusArgs")
  case object Axi4IdMap extends Named("axi4-id-map", "axi4.json")
  case object L2        extends Named("l2", "l2.json")
  case object FpgaShell
    extends Named(
      "fpga-shell",
      "xdc",
      "sdc",
      "pdc",
      "assign.tcl",
      "shell.vivado.tcl",
      "old-shell.vivado.tcl",
      "debugjtag.vivado.tcl",
      "vc707chiplink.vivado.tcl"
    )

  /** The subsystem's device tree as JSON; its key is exactly `json`, so a suffix match would take every JSON key. */
  case object DtsJson extends Named("dts-json") {
    override def matches(key: String): Boolean = key == "json"
  }

  /** Vendor IP generation scripts, excluding the shell's own Tcl. */
  case object FpgaIp extends Named("fpga-ip", "vivado.tcl", "quartus.tcl", "libero.tcl") {
    override def matches(key: String): Boolean = super.matches(key) && !FpgaShell.matches(key)
  }

  /** Registry entries whose key matches `glob`, for producers outside the named kinds. */
  final case class ByPattern(glob: String) extends ArtefactKind {
    def label: String = s"$globPrefix$glob"
    // Built eagerly so a malformed glob is rejected when the selection is parsed, before elaboration
    private val matcher = FileSystems.getDefault.getPathMatcher(s"glob:$glob")
    def matches(key: String): Boolean = matcher.matches(Paths.get(key))
  }

  val named: Seq[Named] = Seq(Regmap, GraphML, Dts, DtsJson, MemMap, PlusArgs, Axi4IdMap, L2, FpgaShell, FpgaIp)

  private val globPrefix = "glob:"

  /** Parses a comma-separated selection such as `regmap,glob:*l2.json`. Names are case-insensitive; patterns need
    * the `glob:` prefix and are never inferred from glob characters.
    */
  def parse(s: String): Either[String, Seq[ArtefactKind]] = {
    val parsed = s.split(",").toSeq.map { token =>
      if (token.startsWith(globPrefix)) Try(ByPattern(token.stripPrefix(globPrefix))).toOption.toRight(token)
      else named.find(_.name == token.toLowerCase).toRight(token)
    }
    val unknown = parsed.collect { case Left(token) => token }
    if (unknown.isEmpty) Right(parsed.collect { case Right(kind) => kind })
    else
      Left(
        s"Unknown artefact kind(s) or malformed glob(s): ${unknown.mkString(", ")}. " +
          s"Valid names: ${named.map(_.name).mkString(", ")}, or $globPrefix<pattern>"
      )
  }
}

object Artefacts {
  import ArtefactKind._

  def emit(
    entries: Seq[(String, () => String)],
    root:    Option[LazyModule],
    kinds:   Seq[ArtefactKind],
    dir:     os.Path,
    prefix:  String
  ): Seq[os.Path] = {
    // Registry is newest first; write in the order artefacts were added
    val registry = entries.reverse
    def selected(key: String) = kinds.isEmpty || kinds.exists(_.matches(key))
    val all = root match {
      case Some(lm) =>
        val graph = if (selected("graphml")) Seq("graphml" -> (() => lm.graphML)) else Nil
        registry.filterNot(e => GraphML.matches(e._1)).filter(e => selected(e._1)) ++ graph
      case None => registry.filter(e => selected(e._1))
    }
    val keys = all.map(_._1).distinct
    kinds.filterNot(kind => keys.exists(kind.matches)).foreach { kind =>
      Console.err.println(s"WARNING: requested artefact kind '${kind.label}' was not produced by $prefix; skipping it")
    }
    keys.flatMap { key =>
      val contents = all.collect { case (`key`, c) => c() }.distinct
      if (contents.size > 1)
        Console.err.println(s"WARNING: ${contents.size} differing artefacts share registry key '$key'; numbering them")
      contents.zipWithIndex.map { case (c, n) =>
        val path = dir / numbered(s"$prefix.$key", n)
        os.write.over(path, c, createFolders = true)
        path
      }
    }
  }

  /** Empties the global registry so the next elaboration starts clean. Call after `emit`, not before. */
  def drain(): Unit = ElaborationArtefacts.files = Nil

  /** `CeTop.mem.axi4.json` numbered 1 is `CeTop.mem.axi4.1.json`; number 0 keeps the name. */
  private def numbered(file: String, n: Int): String =
    if (n == 0) file
    else {
      val dot = file.lastIndexOf('.')
      s"${file.take(dot)}.$n${file.drop(dot)}"
    }
}
