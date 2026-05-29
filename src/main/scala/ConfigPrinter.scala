package emitrtl

import java.io._
import java.nio.file.{Files, Paths}
import java.nio.charset.StandardCharsets
import ParamDescriber._

// Prints the parameters and their descriptions in a markdown file

object ConfigPrinter {
  private val outFile = "parameters.md"

  private def mdEscape(s: String): String =
    Option(s).getOrElse("").replace("|", "&#124;").replace("\r", "").replace("\n", " ").trim

  /** Generic print that optionally uses an implicit ParamDescriber[T].
    * If no implicit describer is available, descriptions are skipped.
    */
  def printParams[T <: Product](header: String, params: T)(implicit describerOpt: Option[ParamDescriber[T]] = None): Unit = {
    val writer = new PrintWriter(new OutputStreamWriter(new FileOutputStream(outFile, true), StandardCharsets.UTF_8))
    try {
      val safeHeader = mdEscape(header)
      writer.append(s"## $safeHeader\n\n")
      writer.append("| Parameter | Value | Description |\n")
      writer.append("|---|---:|---|\n") // parameter left, value right-aligned, description left

      val descs: Map[String, String] = describerOpt.map(_.describe(params)).getOrElse(Map.empty)
      if (describerOpt.nonEmpty) {
        val missing = ParamDescriber.validate(params, descs, allowMissing = true)
        if (missing.nonEmpty) writer.append(s"> **WARNING**: missing descriptions for ${missing.mkString(", ")}\n\n")
      }

      params.productElementNames.zip(params.productIterator).foreach { case (name, value) =>
        val valueStr = ParamDescriber.formatValue(value)
        val nameSafe = mdEscape(name)
        val valueSafe = mdEscape(valueStr)
        val descSafe = mdEscape(descs.getOrElse(name, ""))
        writer.append(s"| $nameSafe | $valueSafe | $descSafe |\n")
      }
      writer.append("\n")
    } finally {
      writer.close()
    }
  }

  // Append an arbitrary string to parameters.md (as a paragraph)
  def printParams(printString: String): Unit = {
    val writer = new PrintWriter(new OutputStreamWriter(new FileOutputStream(outFile, true), StandardCharsets.UTF_8))
    try {
      writer.append(printString + "\n\n")
    } finally {
      writer.close()
    }
  }

  // Move the generated parameters.md (preferred) or parameters.txt to out_dir (generated_sv_dir/topModule/)
  def moveParametersFile(out_dir: String): Unit = {
    val md = Paths.get(outFile).toFile()
    val txt = Paths.get("parameters.txt").toFile()
    if (md.exists()) md.renameTo(Paths.get(out_dir, outFile).toFile())
    else if (txt.exists()) txt.renameTo(Paths.get(out_dir, "parameters.txt").toFile())
  }

  // Delete the parameters.md file if it exists
  def deleteParametersFile: Unit = {
    val path = Paths.get(outFile)
    if (Files.exists(path)) Files.delete(path)
  }
}
