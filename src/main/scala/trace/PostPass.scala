package emitrtl.trace

import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path, Paths}
import scala.jdk.CollectionConverters._
import scala.util.matching.Regex

/** A5b · SV post-pass: adds what Chisel 6.7 printf cannot express to the probe `$fwrite` statements.
  *
  * Only statements whose format string starts with the probe prefix are touched:
  * {{{
  *   $fwrite(fd, "@@EN:llc:1@@@RT@@TIME@@ EVENT @@INST@@ 17 %x\n", p);
  * becomes
  *   if (<enable>) $fwrite(fd, "@RT %0t EVENT %m 17 %x\n", $realtime, p);
  * }}}
  * `<enable>` is built from inline `$test$plusargs` calls, so it stays inside firtool's `ifndef SYNTHESIS` printf
  * block. firtool may wrap a long statement so the format string starts on the next line (the default in ChiselSim);
  * the string literal itself is never split. The pass is idempotent and fails if a placeholder is left anywhere.
  * Delete it once Chisel's native `HierarchicalModuleName` / `SimulationTime` printables are available.
  */
object PostPass {
  private val FWrite: Regex =
    """(\$fwrite\([^,\n]+,\s*)"@@EN:([^:"\n]+):([01])@@@RT@@TIME@@((?:[^"\\\n]|\\.)*)"""".r

  private val Leftover = Seq("@@EN:", "@@TIME@@", "@@INST@@")

  def enable(groups: String, on: String): String = {
    val gs             = groups.split('+').toSeq
    def t(arg: String) = s"""$$test$$plusargs("rtltrace_$arg")"""
    val wake           = (t("all_on") +: gs.map(g => t(s"${g}_on"))).mkString(" || ")
    if (on == "1") {
      val quiet = (t("all_off") +: gs.map(g => t(s"${g}_off"))).mkString(" || ")
      s"($wake || !($quiet))"
    } else s"($wake)"
  }

  /** Rewrite every probe `$fwrite` in `text`; returns the new text and the number of statements rewritten. */
  def rewrite(text: String): (String, Int) = {
    var n   = 0
    val out = FWrite.replaceAllIn(
      text,
      m => {
        n += 1
        val rest = m.group(4).replace("@@INST@@", "%m")
        Regex.quoteReplacement(s"""if (${enable(m.group(2), m.group(3))}) ${m.group(1)}"@RT %0t$rest", $$realtime""")
      },
    )
    (out, n)
  }

  /** Rewrite one file in place; returns the number of statements rewritten. */
  def runFile(p: Path): Int = {
    val src = new String(Files.readAllBytes(p), StandardCharsets.UTF_8)
    if (!Leftover.exists(src.contains)) return 0
    val (out, n) = rewrite(src)
    val left     = out.split("\n", -1).zipWithIndex.filter { case (l, _) => Leftover.exists(l.contains) }
    if (left.nonEmpty)
      throw new IllegalStateException(
        s"rtltrace post-pass: unprocessed placeholder(s) in $p:\n" +
          left.take(5).map { case (l, i) => s"  line ${i + 1}: ${l.trim.take(200)}" }.mkString("\n"),
      )
    if (n > 0) Files.write(p, out.getBytes(StandardCharsets.UTF_8))
    n
  }

  private def isSource(p: Path) = {
    val n = p.getFileName.toString
    n.endsWith(".sv") || n.endsWith(".v") || n.endsWith(".svh")
  }

  /** Rewrite every SystemVerilog source under `dir` (recursively); returns the number of statements rewritten. */
  def run(dir: String): Int = {
    val root = Paths.get(dir)
    if (!Files.isDirectory(root)) return 0
    val files = Files.walk(root).iterator().asScala.filter(p => Files.isRegularFile(p) && isSource(p)).toList
    val n     = files.map(runFile).sum
    if (n > 0) println(s"[rtltrace] post-pass: rewrote $n probe statement(s) under $dir")
    n
  }

  def main(args: Array[String]): Unit = args.foreach(run)
}
