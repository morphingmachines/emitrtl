package emitrtl

trait ParamDescriber[T <: Product] {
  def describe(instance: T): Map[String, String]
}

object ParamDescriber {
  def fromDescriptionsMap[T <: Product](m: Map[String, String]): ParamDescriber[T] =
    new ParamDescriber[T] { override def describe(instance: T) = m }

  // Returns missing field names; throws if allowMissing==false
  def validate(instance: Product, descriptions: Map[String, String], allowMissing: Boolean = true): Seq[String] = {
    val names = instance.productElementNames.toSeq
    val missing = names.filterNot(descriptions.contains)
    if (missing.nonEmpty && !allowMissing)
      throw new IllegalArgumentException(s"Missing parameter descriptions: ${missing.mkString(", ")}")
    missing
  }

  def formatValue(value: Any): String = value match {
    case bi: BigInt => "0x" + bi.toString(16)
    case other      => other.toString
  }
}
