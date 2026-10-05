package emitrtl.trace

import chisel3._
import chisel3.util.Cat

/** How a field is shown by the offline tool. It never changes the bits. */
sealed trait Fmt
object Fmt {
  case object Auto extends Fmt // h for UInt, s for SInt, b for Bool, e= for ChiselEnum
  case object Hex  extends Fmt
  case object Dec  extends Fmt
}

/** Value -> name table for a field that is a plain UInt code (e.g. TileLink opcodes). */
case class EnumTable(name: String, entries: Seq[(BigInt, String)])

/** One named field of a probe record. */
case class Field(
  name:  String,
  data:  Data,
  fmt:   Fmt = Fmt.Auto,
  tags:  Seq[String] = Nil,
  table: Option[EnumTable] = None)

/** Shorthand used at call sites: `F("addr", b.addr, tags = "addr")`. */
object F {
  def apply(name: String, data: Data, fmt: Fmt = Fmt.Auto, tags: String = "", table: EnumTable = null): Field =
    Field(name, data, fmt, tags.split(",").map(_.trim).filter(_.nonEmpty).toSeq, Option(table))
}

/** Result of encoding: the packed payload and the strings that describe it (all go into the DECL line). */
case class Encoded(payload: Option[UInt], width: Int, layout: String, enums: String)

/** A4 · Probe Encoder: the single definition of what a trace record is.
  *
  * Flattens fields into leaves (declaration order, `a.b[2].c` names), classifies each leaf, applies exclusions and the
  * width guard, packs the leaves MSB-first into one UInt, and describes them with the layout/enums grammar that the
  * `rtltrace` tool decodes.
  */
object Encoder {
  val WarnWidth = 256
  val MaxWidth  = 1024

  private[trace] case class Leaf(name: String, bits: Bits, width: Int, tpe: String, tags: Seq[String])

  /** Names keep only `[A-Za-z0-9_.[]]`; every other character becomes `_`. */
  def sanitize(name: String): String = name.map(c => if (c.isLetterOrDigit || "_.[]".contains(c)) c else '_')

  /** Field tags (`addr`, `code`, `key=<k>`) keep `=` as well. */
  def sanitizeTag(tag: String): String = tag.map(c => if (c.isLetterOrDigit || "_.[]=".contains(c)) c else '_')

  private val atomSpecial = " ,;|=/:~#%@+-\"\\"
  private val metaSpecial = " ;=%\"\\"

  private def escWith(set: String)(s: String): String =
    s.flatMap(c => if (set.contains(c) || c < ' ') f"~${c.toInt}%02X" else c.toString)

  /** Names inside structured values (enum labels, client/manager names): every separator character becomes `~XX`. */
  def esc(s: String): String = escWith(atomSpecial)(s)

  /** Meta values: only what breaks the `k=v;k=v` grammar or printf becomes `~XX`; structural separators such as
    * `@ | / : + -` inside a value are kept (names inside a value are escaped with [[esc]] by whoever builds it).
    */
  def escMeta(s: String): String = escWith(metaSpecial)(s)

  private def join(prefix: String, name: String) = if (prefix.isEmpty) name else s"$prefix.$name"

  /** Value -> name table of a ChiselEnum value. `EnumType.factory` is `private[chisel3]` in Chisel 6.7 (public in
    * bytecode), so it is read reflectively; `all` and `allNames` are public.
    */
  private def enumTableOf(e: EnumType): Option[EnumTable] =
    scala.util.Try(classOf[EnumType].getMethod("factory").invoke(e).asInstanceOf[ChiselEnum]).toOption.map { f =>
      val name = f.getClass.getName.stripSuffix("$").split("[.$]").last
      EnumTable(sanitize(name), f.all.map(_.litValue).zip(f.allNames))
    }

  private[trace] def flatten(prefix: String, d: Data, fmt: Fmt, tags: Seq[String], table: Option[EnumTable])
    : Seq[(Leaf, Option[EnumTable])] = d match {
    case r: Record =>
      // Bundle.elements lists fields last-declared first; reverse to get declaration order.
      val elems = r match {
        case _: Bundle => r.elements.toSeq.reverse
        case _ => r.elements.toSeq
      }
      elems.flatMap { case (n, x) => flatten(join(prefix, sanitize(n)), x, fmt, tags, table) }
    case v: Vec[_] =>
      v.zipWithIndex.flatMap { case (x, i) => flatten(s"$prefix[$i]", x, fmt, tags, table) }
    case e: EnumType =>
      if (e.getWidth == 0) Nil
      else
        enumTableOf(e) match {
          case Some(et) => Seq((Leaf(prefix, e.asUInt, e.getWidth, s"e=${et.name}", tags), Some(et)))
          case None     => Seq((Leaf(prefix, e.asUInt, e.getWidth, "h", tags), None))
        }
    case b: Bits =>
      val w = b.getWidth
      if (w == 0) Nil
      else {
        val tpe = (b, table, fmt) match {
          case (_, Some(t), _)  => s"e=${t.name}"
          case (_: Bool, _, _)  => "b"
          case (_: SInt, _, _)  => "s"
          case (_, _, Fmt.Dec)  => "d"
          case _                => "h"
        }
        Seq((Leaf(prefix, b, w, tpe, tags), table))
      }
    case _ => Nil // Clock, Reset, AsyncReset, Analog: not traceable values
  }

  private def globToRegex(g: String) = ("^" + g.split("\\*", -1).map(java.util.regex.Pattern.quote).mkString(".*") + "$").r

  /** Encode `fields` for probe `what` (used in error messages). */
  def encode(fields: Seq[Field], exclude: Seq[String] = Nil, allowWide: Boolean = false, what: String = ""): Encoded = {
    val ex     = exclude.map(globToRegex)
    val leaves = fields
      .flatMap(f => flatten(sanitize(f.name), f.data, f.fmt, f.tags, f.table))
      .filterNot { case (l, _) => ex.exists(_.findFirstIn(l.name).isDefined) }
    val width = leaves.map(_._1.width).sum
    if (width > MaxWidth && !allowWide) {
      val widest = leaves.sortBy(-_._1.width).take(4).map(l => s"${l._1.name}:${l._1.width}").mkString(", ")
      throw new IllegalArgumentException(
        s"rtltrace probe '$what' is $width bits wide (limit $MaxWidth). Widest fields: $widest. " +
          "Use exclude = Seq(...) or fields = ... to narrow it, or allowWide = true.",
      )
    }
    if (width > WarnWidth) println(s"[rtltrace] warning: probe '$what' is $width bits wide")
    val layout = leaves.map { case (l, _) =>
      val t = if (l.tpe == "h") "" else s":${l.tpe}"
      s"${l.name}:${l.width}$t" + l.tags.map(tg => s"#${sanitizeTag(tg)}").mkString
    }.mkString(",")
    val tables = leaves.flatMap(_._2).distinctBy(_.name)
    val enums  = tables.map(t => s"${t.name}=" + t.entries.map { case (v, n) => s"$v:${esc(n)}" }.mkString("/")).mkString(";")
    val payload = if (leaves.isEmpty) None else Some(Cat(leaves.map(_._1.bits.asUInt)))
    Encoded(payload, width, if (layout.isEmpty) "-" else layout, if (enums.isEmpty) "-" else enums)
  }
}
