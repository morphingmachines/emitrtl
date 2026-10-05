package emitrtl.trace

import chisel3.experimental.BaseModule
import org.chipsalliance.diplomacy.lazymodule.LazyModule

import scala.collection.mutable
import scala.util.Try

/** Probe ids and the A6 catalog (`trace_points.json`).
  *
  * A probe id identifies one `Trace.*` call site (module class + source line + probe name). Every instance of a module
  * gets the same id, so printf strings stay identical and firtool can still deduplicate the module; `%m` in the trace
  * tells instances apart.
  */
private[trace] object Registry {
  final case class Point(
    pid:    Int,
    kind:   String,
    name:   String,
    groups: Seq[String],
    on:     Boolean,
    src:    String,
    width:  Int,
    layout: String,
    enums:  String,
    meta:   String,
    module: BaseModule,
    lazyMod: Option[LazyModule])

  private val pids = mutable.LinkedHashMap[(String, String, String), Int]()
  private val pts  = mutable.ArrayBuffer[Point]()

  def pidFor(moduleClass: String, src: String, name: String): Int = synchronized {
    pids.getOrElseUpdate((moduleClass, src, name), pids.size + 1)
  }

  def add(p: Point): Unit = synchronized { pts += p }

  def clear(): Unit = synchronized { pids.clear(); pts.clear() }

  def points: List[Point] = synchronized { pts.toList }

  private def js(s: String): String =
    "\"" + s.flatMap {
      case '"'  => "\\\""
      case '\\' => "\\\\"
      case '\n' => "\\n"
      case c if c < ' ' => f"\\u${c.toInt}%04x"
      case c    => c.toString
    } + "\""

  /** Instance path once elaboration is complete (the LazyModule path when there is one). */
  private def pathOf(p: Point): String =
    p.lazyMod.flatMap(lm => Try(lm.pathName).toOption)
      .orElse(Try(p.module.pathName).toOption)
      .getOrElse(p.module.getClass.getName)

  def toJson: String = {
    val rows = points.map { p =>
      Seq(
        s""""pid": ${p.pid}""",
        s""""kind": ${js(p.kind)}""",
        s""""name": ${js(p.name)}""",
        s""""groups": [${p.groups.map(js).mkString(", ")}]""",
        s""""on": ${p.on}""",
        s""""module": ${js(pathOf(p))}""",
        s""""class": ${js(p.module.getClass.getName)}""",
        s""""src": ${js(p.src)}""",
        s""""width": ${p.width}""",
        s""""layout": ${js(p.layout)}""",
        s""""enums": ${js(p.enums)}""",
        s""""meta": ${js(p.meta)}""",
      ).mkString("    {", ", ", "}")
    }.distinct
    s"""{\n  "version": 1,\n  "points": [\n${rows.mkString(",\n")}\n  ]\n}\n"""
  }
}
