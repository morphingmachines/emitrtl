package emitrtl

import java.io._
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Paths}

/** Prints the parameters and their descriptions in a markdown file. Description for a parameter is optional. Also
  * contains overloaded function to add any arbitrary string.
  */

object ConfigPrinter {
  private val outFile = "parameters.md"

  private def mdEscape(s: String): String =
    Option(s).getOrElse("").replace("|", "&#124;").replace("\r", "").replace("\n", " ").trim

  /** Generic print that optionally uses an implicit ParamDescriber[T]. If no implicit describer is available,
    * descriptions are skipped.
    */
  def printParams[T <: Product](
    header: String,
    params: T,
  )(
    implicit describerOpt: Option[ParamDescriber[T]] = None,
  ): Unit = {
    val writer = new PrintWriter(new OutputStreamWriter(new FileOutputStream(outFile, true), StandardCharsets.UTF_8))
    try {
      val safeHeader = mdEscape(header)
      writer.append(s"## $safeHeader\n\n")

      val descs: Map[String, String] = describerOpt.map(_.describe(params)).getOrElse(Map.empty)

      val warnings = if (describerOpt.nonEmpty) {
        val missing = ParamDescriber.validate(params, descs, allowMissing = true)
        val extra   = descs.keys.filterNot(params.productElementNames.toSet).toSeq.sorted
        val msgs    = Seq.newBuilder[String]
        if (missing.nonEmpty) msgs += s"missing descriptions for: ${missing.mkString(", ")}"
        if (extra.nonEmpty) msgs += s"unknown field names in describer (typo?): ${extra.mkString(", ")}"
        msgs.result()
      } else Seq.empty

      if (warnings.nonEmpty)
        writer.append(s"> **WARNING** ($safeHeader): ${warnings.mkString("; ")}\n\n")

      val hasDescs = descs.nonEmpty
      if (hasDescs) {
        writer.append("| Parameter | Value | Description |\n")
        writer.append("|---|---:|---|\n")
      } else {
        writer.append("| Parameter | Value |\n")
        writer.append("|---|---:|\n")
      }

      params.productElementNames.zip(params.productIterator).foreach { case (name, value) =>
        val valueStr  = ParamDescriber.formatValue(value)
        val nameSafe  = mdEscape(name)
        val valueSafe = mdEscape(valueStr)
        if (hasDescs) {
          val descSafe = mdEscape(descs.getOrElse(name, ""))
          writer.append(s"| $nameSafe | $valueSafe | $descSafe |\n")
        } else {
          writer.append(s"| $nameSafe | $valueSafe |\n")
        }
      }
      writer.append("\n")
    } finally {
      writer.close()
    }
  }

  // Append an arbitrary string to parameters.md as a heading 2
  def printParams(printString: String): Unit = {
    val writer = new PrintWriter(new OutputStreamWriter(new FileOutputStream(outFile, true), StandardCharsets.UTF_8))
    try {
      writer.append(s"## $printString\n\n")
    } finally {
      writer.close()
    }
  }

  // Move parameters.md to out_dir. Falls back to parameters.txt for backward compatibility with older flows.
  // Silently does nothing if neither file exists.
  def moveParametersFile(out_dir: String): Unit = {
    val md  = Paths.get(outFile).toFile()
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
