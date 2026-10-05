package emitrtl.trace

import chisel3._
import chisel3.experimental.SourceInfo
import chisel3.reflect.DataMirror
import chisel3.util.{DecoupledIO, ReadyValidIO, Valid}
import freechips.rocketchip.tilelink._
import org.chipsalliance.diplomacy.lazymodule.LazyModuleImpLike
import org.chipsalliance.diplomacy.nodes.{EphemeralNode, MixedNode}

import scala.util.Try

/** Public probe API of the `rtltrace` infrastructure.
  *
  * Every call adds printf-based trace points inside the calling module. Output is simulation-only (firtool puts printfs
  * under `ifndef SYNTHESIS`), carries `%m` and `$realtime` once the SV post-pass has run, and is switched at runtime
  * with `+rtltrace_<group>_on`, `+rtltrace_<group>_off`, `+rtltrace_all_on`, `+rtltrace_all_off`.
  *
  * Setting the environment variable `RTLTRACE=off` at elaboration makes every call a no-op.
  */
object Trace {

  private val disabledScope = new scala.util.DynamicVariable[Boolean](false)

  /** Global kill switch (A7): `RTLTRACE=off` at elaboration removes every probe. */
  def enabled: Boolean = !disabledScope.value && !sys.env.get("RTLTRACE").exists(_.equalsIgnoreCase("off"))

  /** Elaborate `body` as if `RTLTRACE=off` were set (used by tests and flows that must not carry probes). */
  def withoutTracing[T](body: => T): T = disabledScope.withValue(true)(body)

  /** Forget probe ids and catalog entries (called at the start of each elaboration). */
  def reset(): Unit = Registry.clear()

  /** Write the A6 catalog. Does nothing when no probe was elaborated. */
  def writeCatalog(path: String): Unit = {
    if (Registry.points.nonEmpty) {
      val f = new java.io.File(path)
      Option(f.getParentFile).foreach(_.mkdirs())
      val w = new java.io.PrintWriter(f)
      try w.write(Registry.toJson)
      finally w.close()
    }
  }

  private def groupsOf(groups: String): Seq[String] = {
    val gs = groups.split(",").map(g => Encoder.sanitize(g.trim)).filter(_.nonEmpty).toSeq
    if (gs.isEmpty) Seq("misc") else gs
  }

  private def withTags(fields: Seq[Field], tags: Map[String, String]): Seq[Field] =
    if (tags.isEmpty) fields
    else
      fields.map { f =>
        tags.get(f.name).map(t => f.copy(tags = f.tags ++ t.split(",").map(_.trim).filter(_.nonEmpty))).getOrElse(f)
      }

  private def defaultFields(d: Data): Seq[Field] = d match {
    case r: Bundle => r.elements.toSeq.reverse.map { case (n, x) => Field(n, x) }
    case x         => Seq(Field("bits", x))
  }

  // DataMirror.modulePath is private[chisel3] in Chisel 6; it lists modules innermost first (head = owner of the port)
  private lazy val modulePath =
    DataMirror.getClass.getMethods.find(m => m.getName == "modulePath" && m.getParameterCount == 2).get

  /** Direction relative to the calling module, known only for its own ports. A child's port (e.g. the `enq` of a
    * Queue inside the module) or a wire has none; call sites can state it with `meta = Seq("dir" -> "in"|"out")`.
    */
  private def dirOf(d: Data): Option[String] = {
    val ownPort = Try(DataMirror.isIO(d) && (modulePath.invoke(DataMirror, d, None) match {
      case path: Seq[_] => path.headOption == Module.currentModule
      case _ => false
    }))
    if (!ownPort.getOrElse(false)) None
    else
      Try(DataMirror.directionOf(d)).toOption.collect {
        case ActualDirection.Input  => "in"
        case ActualDirection.Output => "out"
      }
  }

  private def dirMeta(d: Data, meta: Seq[(String, String)]): Seq[(String, String)] =
    if (meta.exists(_._1 == "dir")) meta else dirOf(d).map("dir" -> _).toSeq ++ meta

  // ------------------------------------------------------------------ A3 · custom-interface probes

  /** Handshake probe: one EVENT per transfer (valid && ready) with the payload, and one STALL line when valid is high
    * while ready is low (a stall begins). The tool derives wait cycles, stall alarms and pending handshakes from them.
    *
    * `fields` picks what is recorded (default: every field of `rv.bits`), e.g.
    * `Seq(F("id", q.io.enq.bits.id), F("addr", q.io.enq.bits.addr, tags = "addr"))`.
    */
  def fire[T <: Data](
    name:      String,
    rv:        ReadyValidIO[T],
    groups:    String = "",
    on:        Boolean = false,
    fields:    Seq[Field] = Nil,
    exclude:   Seq[String] = Nil,
    tags:      Map[String, String] = Map.empty,
    meta:      Seq[(String, String)] = Nil,
    allowWide: Boolean = false
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.fire(\"$name\")")
    val flds  = withTags(if (fields.isEmpty) defaultFields(rv.bits) else fields, tags)
    val enc   = Encoder.encode(flds, exclude, allowWide, name)
    val p     = Emit.declare("fire", Encoder.sanitize(name), groupsOf(groups), on, enc, dirMeta(rv.valid, meta))
    val stall = RegNext(rv.valid && !rv.ready, false.B)
    stall.suggestName(s"rtltrace_${Encoder.sanitize(name).replace('.', '_')}_stalled")
    Emit.stall(p, rv.valid && !rv.ready && !stall)
    Emit.event(p, rv.valid && rv.ready, enc.payload)
  }

  /** Valid probe: one EVENT (with the payload) in every cycle where valid is high. */
  def valid[T <: Data](
    name:    String,
    v:       Valid[T],
    groups:  String,
    on:      Boolean,
    exclude: Seq[String],
    tags:    Map[String, String]
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.valid(\"$name\")")
    val enc = Encoder.encode(withTags(defaultFields(v.bits), tags), exclude, what = name)
    val p   = Emit.declare("valid", Encoder.sanitize(name), groupsOf(groups), on, enc, dirMeta(v.valid, Nil))
    Emit.event(p, v.valid, enc.payload)
  }

  def valid[T <: Data](name: String, v: Valid[T])(implicit si: SourceInfo): Unit =
    valid(name, v, "", false, Nil, Map.empty[String, String])

  def valid[T <: Data](name: String, v: Valid[T], groups: String)(implicit si: SourceInfo): Unit =
    valid(name, v, groups, false, Nil, Map.empty[String, String])

  def valid[T <: Data](name: String, v: Valid[T], groups: String, on: Boolean)(implicit si: SourceInfo): Unit =
    valid(name, v, groups, on, Nil, Map.empty[String, String])

  /** Pulse probe: one EVENT (no payload) in every cycle where `pulse` is high. */
  def pulse(name: String, pulse: Bool, groups: String = "", on: Boolean = false)(implicit si: SourceInfo): Unit =
    if (enabled) {
      Emit.checkContext(s"Trace.pulse(\"$name\")")
      val p = Emit.declare("valid", Encoder.sanitize(name), groupsOf(groups), on, Encoder.encode(Nil), Nil)
      Emit.event(p, pulse, None)
    }

  /** Value-change probe: one EVENT with the value in the first cycle after reset and whenever it changes (FSM state
    * registers, bitmaps, counters). ChiselEnum values are decoded by name offline.
    */
  def change(
    name:    String,
    signal:  Data,
    groups:  String = "",
    on:      Boolean = false,
    exclude: Seq[String] = Nil,
    tags:    Map[String, String] = Map.empty
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.change(\"$name\")")
    val flds = withTags(signal match {
      case b: Bundle => defaultFields(b)
      case x         => Seq(Field("value", x))
    }, tags)
    val enc = Encoder.encode(flds, exclude, what = name)
    require(enc.payload.isDefined, s"rtltrace: Trace.change(\"$name\") has no traceable bits")
    val p    = Emit.declare("change", Encoder.sanitize(name), groupsOf(groups), on, enc, Nil)
    val cur  = enc.payload.get
    val prev = RegNext(cur)
    prev.suggestName(s"rtltrace_${Encoder.sanitize(name).replace('.', '_')}_prev")
    Emit.event(p, Emit.firstCycle() || cur =/= prev, enc.payload)
  }

  /** Conditional record with named fields: one EVENT whenever `cond` is high. */
  def event(
    name:   String,
    cond:   Bool,
    fields: Seq[(String, Data)] = Nil,
    groups: String = "",
    on:     Boolean = false,
    tags:   Map[String, String] = Map.empty
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.event(\"$name\")")
    val enc = Encoder.encode(withTags(fields.map { case (n, d) => Field(n, d) }, tags), what = name)
    val p   = Emit.declare("event", Encoder.sanitize(name), groupsOf(groups), on, enc, Nil)
    Emit.event(p, cond, enc.payload)
  }

  /** Probe every ReadyValid and Valid interface found in an IO tree, named by their path (`prefix.req`, `resp[1]`, ...),
    * recording their direction relative to the module. TileLink bundles are skipped; use [[tl]] for those.
    */
  def ports(
    io:      Data,
    groups:  String = "",
    on:      Boolean = false,
    prefix:  String = "",
    exclude: Seq[String] = Nil
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    def join(a: String, b: String) = if (a.isEmpty) b else s"$a.$b"
    def walk(path: String, d: Data): Unit = d match {
      case _:  TLBundle        => ()
      case rv: ReadyValidIO[_] => fire(path, rv, groups, on, exclude = exclude)
      case v:  Valid[_]        => valid(path, v, groups, on, exclude, Map.empty[String, String])
      case r:  Bundle          => r.elements.toSeq.reverse.foreach { case (n, x) => walk(join(path, n), x) }
      case r:  Record          => r.elements.toSeq.foreach { case (n, x) => walk(join(path, n), x) }
      case v:  Vec[_]          => v.zipWithIndex.foreach { case (x, i) => walk(s"$path[$i]", x) }
      case _ => ()
    }
    walk(prefix, io)
  }

  // ------------------------------------------------------------------ A2 · TileLink port probe

  private val tlA = EnumTable(
    "TLA",
    Seq(0 -> "PutFullData", 1 -> "PutPartialData", 2 -> "ArithmeticData", 3 -> "LogicalData", 4 -> "Get", 5 -> "Hint",
      6 -> "AcquireBlock", 7 -> "AcquirePerm").map { case (v, n) => BigInt(v) -> n },
  )
  private val tlB = EnumTable(
    "TLB",
    Seq(0 -> "PutFullData", 1 -> "PutPartialData", 2 -> "ArithmeticData", 3 -> "LogicalData", 4 -> "Get", 5 -> "Hint",
      6 -> "Probe").map { case (v, n) => BigInt(v) -> n },
  )
  private val tlC = EnumTable(
    "TLC",
    Seq(0 -> "AccessAck", 1 -> "AccessAckData", 2 -> "HintAck", 4 -> "ProbeAck", 5 -> "ProbeAckData", 6 -> "Release",
      7 -> "ReleaseData").map { case (v, n) => BigInt(v) -> n },
  )
  private val tlD = EnumTable(
    "TLD",
    Seq(0 -> "AccessAck", 1 -> "AccessAckData", 2 -> "HintAck", 4 -> "Grant", 5 -> "GrantData", 6 -> "ReleaseAck")
      .map { case (v, n) => BigInt(v) -> n },
  )

  private def edgeMeta(e: TLEdge, port: String, side: String): Seq[(String, String)] = {
    def hex(x: BigInt) = s"0x${x.toString(16)}"
    val clients =
      e.client.clients.map(c => s"${Encoder.esc(c.name)}@${c.sourceId.start}-${c.sourceId.end - 1}").mkString("|")
    val managers = e.manager.managers
      .map(m => s"${Encoder.esc(m.name)}@" + m.address.map(a => s"${hex(a.base)}/${hex(a.mask)}").mkString("+"))
      .mkString("|")
    Seq(
      "port"     -> port,
      "side"     -> side,
      "clients"  -> clients,
      "managers" -> managers,
      "beat"     -> e.manager.beatBytes.toString,
      "at"       -> Emit.srcOf(e.sourceInfo),
    )
  }

  /** Probe one TileLink link: every channel present (A and D; B, C and E on TL-C), always together so the tool can pair
    * requests with responses. `side` is "in" when the calling module is the manager on this link, "out" when it is the
    * client.
    */
  def tl(
    name:   String,
    bundle: TLBundle,
    edge:   TLEdge,
    groups: String,
    on:     Boolean,
    side:   String,
    data:   Boolean
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.tl(\"$name\")")
    val port = Encoder.sanitize(name)
    val gs   = groupsOf(groups)
    val base = edgeMeta(edge, port, side)
    def chan[T <: TLChannel](ch: String, d: DecoupledIO[T], flds: Seq[Field]): Unit = {
      val enc   = Encoder.encode(flds, what = s"$port.$ch", allowWide = data)
      val p     = Emit.declare("fire", s"$port.$ch", gs, on, enc, ("tl" -> ch) +: base)
      val stall = RegNext(d.valid && !d.ready, false.B)
      stall.suggestName(s"rtltrace_${port.replace('.', '_')}_${ch}_stalled")
      Emit.stall(p, d.valid && !d.ready && !stall)
      Emit.event(p, d.valid && d.ready, enc.payload)
    }
    val a = bundle.a.bits
    chan(
      "a",
      bundle.a,
      Seq(F("opcode", a.opcode, table = tlA), F("param", a.param), F("size", a.size, Fmt.Dec),
        F("source", a.source, Fmt.Dec), F("address", a.address, tags = "addr")) ++
        (if (data) Seq(F("mask", a.mask), F("data", a.data)) else Nil) :+ F("corrupt", a.corrupt),
    )
    if (bundle.params.hasBCE) {
      val b = bundle.b.bits
      chan(
        "b",
        bundle.b,
        Seq(F("opcode", b.opcode, table = tlB), F("param", b.param), F("size", b.size, Fmt.Dec),
          F("source", b.source, Fmt.Dec), F("address", b.address, tags = "addr")) ++
          (if (data) Seq(F("mask", b.mask), F("data", b.data)) else Nil) :+ F("corrupt", b.corrupt),
      )
      val c = bundle.c.bits
      chan(
        "c",
        bundle.c,
        Seq(F("opcode", c.opcode, table = tlC), F("param", c.param), F("size", c.size, Fmt.Dec),
          F("source", c.source, Fmt.Dec), F("address", c.address, tags = "addr")) ++
          (if (data) Seq(F("data", c.data)) else Nil) :+ F("corrupt", c.corrupt),
      )
    }
    val d = bundle.d.bits
    chan(
      "d",
      bundle.d,
      Seq(F("opcode", d.opcode, table = tlD), F("param", d.param), F("size", d.size, Fmt.Dec),
        F("source", d.source, Fmt.Dec), F("sink", d.sink, Fmt.Dec), F("denied", d.denied)) ++
        (if (data) Seq(F("data", d.data)) else Nil) :+ F("corrupt", d.corrupt),
    )
    if (bundle.params.hasBCE) chan("e", bundle.e, Seq(F("sink", bundle.e.bits.sink, Fmt.Dec)))
  }

  def tl(name: String, bundle: TLBundle, edge: TLEdge)(implicit si: SourceInfo): Unit =
    tl(name, bundle, edge, "", false, "-", false)

  def tl(name: String, bundle: TLBundle, edge: TLEdge, groups: String, on: Boolean)(implicit si: SourceInfo): Unit =
    tl(name, bundle, edge, groups, on, "-", false)

  /** Probe the TileLink links of a node owned by the calling LazyModule.
    *
    * `side = "auto"` probes a client node's out links, a manager node's in links, an identity node's in links (its in
    * and out are the same wires) and both sides of an adapter or nexus node (`name.in0`, `name.out0`, ...).
    */
  def tl(
    name:   String,
    node:   MixedNode[_, _, TLEdgeIn, TLBundle, _, _, TLEdgeOut, TLBundle],
    groups: String = "",
    on:     Boolean = false,
    side:   String = "auto",
    data:   Boolean = false
  )(
    implicit si: SourceInfo
  ): Unit = if (enabled) {
    Emit.checkContext(s"Trace.tl(\"$name\")")
    node match {
      case _: EphemeralNode[_, _, _, _, _] =>
        throw new IllegalArgumentException(
          s"rtltrace: Trace.tl(\"$name\") was given an ephemeral node (${node.name}); it has no wires. " +
            "Probe the real node it forwards to.",
        )
      case _ =>
    }
    Module.currentModule match {
      case Some(imp: LazyModuleImpLike) =>
        require(
          node.lazyModule eq imp.wrapper,
          s"rtltrace: Trace.tl(\"$name\"): node ${node.name} belongs to ${node.lazyModule.name}, not to the calling " +
            s"module ${imp.wrapper.name}. Probe it inside ${node.lazyModule.name}, or insert a TLNameNode tap in a " +
            "module you own and probe that.",
        )
      case _ =>
        throw new IllegalArgumentException(
          s"rtltrace: Trace.tl(\"$name\", node) must be called inside a LazyModuleImp; use Trace.tl(name, bundle, edge)",
        )
    }
    val ins  = node.in
    val outs = node.out
    val s = side match {
      case "auto" =>
        if (node.circuitIdentity) "in"
        else if (ins.isEmpty) "out"
        else if (outs.isEmpty) "in"
        else "both"
      case other => other
    }
    def many[E <: TLEdge](dir: String, links: Seq[(TLBundle, E)]): Unit = {
      val suffix = links.size > 1 || s == "both"
      links.zipWithIndex.foreach { case ((b, e), k) =>
        tl(if (suffix) s"$name.$dir$k" else name, b, e, groups, on, dir, data)
      }
    }
    if (s == "in" || s == "both") many("in", ins)
    if (s == "out" || s == "both") many("out", outs)
  }
}
