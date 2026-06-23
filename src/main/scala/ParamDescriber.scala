package emitrtl

/** Wraps an Int to be formatted as a hex string (e.g. 0x02200000) in the parameters markdown. */
case class HexInt(value: Int) extends AnyVal {
  override def toString: String = f"0x${value}%08X"
}

trait ParamDescriber[T <: Product] {
  def describe(instance: T): Map[String, String]
}
// Companion object to create a map of parameter names and their descriptions
object ParamDescriber {
  def fromDescriptionsMap[T <: Product](m: Map[String, String]): ParamDescriber[T] =
    new ParamDescriber[T] { override def describe(instance: T) = m }

  implicit def toOption[T <: Product](implicit d: ParamDescriber[T]): Option[ParamDescriber[T]] = Some(d)

  // Returns missing field names; throws if allowMissing==false
  def validate(instance: Product, descriptions: Map[String, String], allowMissing: Boolean = true): Seq[String] = {
    val names   = instance.productElementNames.toSeq
    val missing = names.filterNot(descriptions.contains)
    if (missing.nonEmpty && !allowMissing)
      throw new IllegalArgumentException(s"Missing parameter descriptions: ${missing.mkString(", ")}")
    missing
  }

  private val hexThreshold = 10000

  def formatValue(value: Any): String = value match {
    case bi: BigInt                             => "0x" + bi.toString(16)
    case hi: HexInt                             => hi.toString
    case i:  Int if math.abs(i) >= hexThreshold => s"$i (0x${i.toHexString.toUpperCase})"
    case other => other.toString
  }
}
