package emitrtl.trace

import chisel3._
import chisel3.experimental.{BaseModule, SourceInfo, SourceLine}
import org.chipsalliance.diplomacy.lazymodule.LazyModuleImpLike

/** A5 · Emitter: turns an encoded probe into printf statements in the calling module.
  *
  * Every format string starts with three placeholders that the A5b post-pass rewrites in the generated SV:
  *   - `@@EN:<groups>:<on>@@` becomes an `if ($test$plusargs(...))` enable guard,
  *   - `@@TIME@@` becomes `%0t` with a `$realtime` argument,
  *   - `@@INST@@` becomes `%m`.
  * firtool already places printfs inside `ifndef SYNTHESIS` and gates them with `~reset`.
  */
private[trace] object Emit {
  private val firsts = new java.util.WeakHashMap[BaseModule, Bool]()

  def srcOf(si: SourceInfo): String = si match {
    case SourceLine(f, l, _) => s"${f.split('/').last}:$l"
    case _                   => "?"
  }

  /** One register per module: true in the first cycle after every reset (prints the DECL lines). */
  def firstCycle(): Bool = {
    val m = Module.currentModule.get
    Option(firsts.get(m)).getOrElse {
      val r = RegNext(false.B, true.B)
      r.suggestName("rtltrace_first")
      firsts.put(m, r)
      r
    }
  }

  def checkContext(what: String): Unit = {
    require(Module.currentModule.isDefined, s"rtltrace: $what must be called inside a Module")
    require(
      when.cond.litOption.contains(BigInt(1)),
      s"rtltrace: $what must not be called inside a when/elsewhen block; use Trace.event(name, cond, ...) for conditional records",
    )
  }

  def enTok(groups: Seq[String], on: Boolean): String = s"@@EN:${groups.mkString("+")}:${if (on) 1 else 0}@@"

  private def line(en: String, kind: String, pid: Int, rest: String, payload: Option[UInt]): Printable = {
    val head = PString(s"$en@RT@@TIME@@ $kind @@INST@@ $pid$rest")
    payload match {
      case Some(p) => Printables(Seq(head, PString(" "), Hexadecimal(p), PString("\n")))
      case None    => Printables(Seq(head, PString("\n")))
    }
  }

  final case class Probe(pid: Int, en: String)

  /** Registers the probe, prints its DECL line in the first cycle after reset, and returns the handle used to print
    * EVENT / STALL lines.
    */
  def declare(
    kind:   String,
    name:   String,
    groups: Seq[String],
    on:     Boolean,
    enc:    Encoded,
    meta:   Seq[(String, String)]
  )(
    implicit si: SourceInfo
  ): Probe = {
    val mod     = Module.currentModule.get
    val src     = srcOf(si)
    val pid     = Registry.pidFor(mod.getClass.getName, src, name)
    val metaStr = (Seq("v" -> "1", "src" -> src) ++ meta).map { case (k, v) => s"${Encoder.sanitize(k)}=${Encoder.escMeta(v)}" }
      .mkString(";")
    val lazyMod = mod match {
      case imp: LazyModuleImpLike => Some(imp.wrapper)
      case _                      => None
    }
    Registry.add(
      Registry.Point(pid, kind, name, groups, on, src, enc.width, enc.layout, enc.enums, metaStr, mod, lazyMod),
    )
    val en   = enTok(groups, on)
    val rest = s" $kind $name ${groups.mkString(",")} ${enc.width} ${enc.layout} ${enc.enums} $metaStr"
    val first = firstCycle()
    when(first)(printf(line(en, "DECL", pid, rest, None)))
    Probe(pid, en)
  }

  def event(p: Probe, cond: Bool, payload: Option[UInt])(implicit si: SourceInfo): Unit =
    when(cond)(printf(line(p.en, "EVENT", p.pid, "", payload)))

  def stall(p: Probe, cond: Bool)(implicit si: SourceInfo): Unit =
    when(cond)(printf(line(p.en, "STALL", p.pid, "", None)))
}
