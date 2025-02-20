package emitrtl

import java.io._
import java.nio.file.Paths

// Helper functions to print the module parameters

object ConfigPrinter {

  // Iterates over the Product and appends the parameters to parameters.txt. BigInts are printed in Hex
  def printParams(header: String, params: Product): Unit = {
    val writer = new PrintWriter(new FileWriter("parameters.txt", true))
    writer.append("********" + header + "********\n")
    params.productElementNames.zip(params.productIterator).foreach { case (name, value) =>
      val valueStr = value match {
        case bigInt: BigInt => "0x" + bigInt.toString(16)
        case _ => value.toString
      }
      writer.append(s"$name: $valueStr\n")
    }
    writer.append(s"\n")
    writer.close()
  }
  // overloaded function to append any custom string format to parameters.txt
  def printParams(printString: String): Unit = {
    val writer = new PrintWriter(new FileWriter("parameters.txt", true))
    writer.append(printString + "\n")
    writer.close()
  }
  // move the generated parameters.txt to out_dir (generated_sv_dir/topModule/)
  def moveParametersFile(out_dir: String): Unit =
    Paths.get("parameters.txt").toFile().renameTo(Paths.get(out_dir, "parameters.txt").toFile())

}
