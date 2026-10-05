package emitrtl.trace

import _root_.circt.stage.ChiselStage
import chisel3._
import chisel3.util._
import chisel3.util.random.LFSR
import freechips.rocketchip.diplomacy.AddressSet
import freechips.rocketchip.tilelink._
import org.chipsalliance.cde.config.Parameters
import org.chipsalliance.diplomacy.lazymodule._
import org.scalatest.freespec.AnyFreeSpec

import scala.util.Try

// ---------------------------------------------------------------------------------------------- designs under test

object Mode extends ChiselEnum { val Idle, Run, Drain = Value }

class Payload extends Bundle {
  val id    = UInt(8.W)
  val delta = SInt(6.W)
  val tag   = Mode()
}

class RtNested extends Bundle {
  val x = Bool()
  val e = Mode()
  val w = UInt(4.W)
}

/** Plain Module with a Decoupled pipeline stage, an FSM and a Valid output. Instantiated twice (dedup check). */
class Stage(traced: Boolean) extends Module {
  val io = IO(new Bundle {
    val in   = Flipped(Decoupled(new Payload))
    val out  = Decoupled(new Payload)
    val done = Valid(UInt(8.W))
  })
  val q = Module(new Queue(new Payload, 2))
  q.io.enq <> io.in
  io.out <> q.io.deq
  val state = RegInit(Mode.Idle)
  switch(state) {
    is(Mode.Idle)(when(io.in.fire)(state := Mode.Run))
    is(Mode.Run)(when(!io.in.valid)(state := Mode.Drain))
    is(Mode.Drain)(when(!q.io.deq.valid)(state := Mode.Idle))
  }
  io.done.valid := io.out.fire
  io.done.bits  := io.out.bits.id
  if (traced) {
    Trace.fire("in", io.in, groups = "stage", on = true)
    Trace.fire("q.deq", q.io.deq, groups = "stage", on = true)                              // child port: no direction
    Trace.fire("q.enq", q.io.enq, groups = "stage", on = true, meta = Seq("dir" -> "in")) // direction stated at the call
    Trace.change("state", state, groups = "stage", on = true)
    Trace.valid("done", io.done, "stage.done") // dormant unless +rtltrace_stage.done_on
  }
}

/** Second clock domain: a counter traced on its own clock. */
class SlowCounter(traced: Boolean) extends Module {
  val io  = IO(new Bundle { val n = Output(UInt(4.W)) })
  val cnt = RegInit(0.U(4.W))
  cnt  := cnt + 1.U
  io.n := cnt
  if (traced) Trace.change("slow", cnt, groups = "slow", on = true)
}

/** Every ReadyValid / Valid in an IO tree, probed with one call. */
class PortsDemo(traced: Boolean) extends Module {
  val io = IO(new Bundle {
    val req   = Flipped(Decoupled(UInt(8.W)))
    val resp  = Valid(UInt(8.W))
    val lanes = Vec(2, Decoupled(UInt(4.W)))
  })
  io.req.ready    := true.B
  io.resp.valid   := io.req.fire
  io.resp.bits    := io.req.bits
  io.lanes.foreach { l => l.valid := io.req.fire; l.bits := io.req.bits(3, 0) }
  if (traced) Trace.ports(io, groups = "ports")
}

class ProbeTop(traced: Boolean) extends Module {
  val clk2 = IO(Input(Clock()))
  val io = IO(new Bundle {
    val drain       = Input(Bool())
    val fires       = Output(UInt(16.W))
    val stallStarts = Output(UInt(16.W))
    val waitSum     = Output(UInt(16.W))
  })
  val lfsr = LFSR(16)
  val a    = Module(new Stage(traced))
  val b    = Module(new Stage(traced))

  // Decoupled source: valid stays high until the transfer happens
  val srcValid = RegInit(false.B)
  val srcId    = RegInit(0.U(8.W))
  when(a.io.in.fire)(srcId := srcId + 1.U)
  when(!srcValid || a.io.in.fire)(srcValid := lfsr(0) && !io.drain)
  a.io.in.valid      := srcValid
  a.io.in.bits.id    := srcId
  a.io.in.bits.delta := srcId(5, 0).asSInt
  a.io.in.bits.tag   := Mux(srcId(0), Mode.Run, Mode.Idle)
  b.io.in <> a.io.out
  b.io.out.ready := Mux(io.drain, true.B, lfsr(1) && lfsr(2)) // slow sink => backpressure

  val slow = withClock(clk2)(Module(new SlowCounter(traced)))
  val pd   = Module(new PortsDemo(traced))
  pd.io.req.valid := lfsr(3)
  pd.io.req.bits  := srcId
  pd.io.lanes.foreach(_.ready := true.B)

  // A4 round trip: known relations between fields
  val cyc = RegInit(0.U(16.W))
  cyc := cyc + 1.U
  val c      = cyc(7, 0)
  val nested = Wire(new RtNested)
  nested.x := c(0)
  nested.e := Mux(c(1), Mode.Drain, Mode.Idle)
  nested.w := c(7, 4)
  val vec = VecInit(c(1, 0), c(3, 2))

  // a handshake that never completes (pending at end)
  val stuckValid = RegInit(false.B)
  when(cyc === 150.U)(stuckValid := true.B)
  val stuck = Wire(Decoupled(UInt(4.W)))
  stuck.valid := stuckValid
  stuck.bits  := 5.U
  stuck.ready := false.B

  if (traced) {
    Trace.event("rt", c(2, 0) === 0.U, Seq("u" -> c, "s" -> c.asSInt, "v" -> vec, "n" -> nested), groups = "rt", on = true)
    Trace.fire("stuck", stuck, groups = "stuck", on = true)
  }

  // hardware scoreboard for the 'a.in' handshake
  val fires       = RegInit(0.U(16.W))
  val stallStarts = RegInit(0.U(16.W))
  val waitSum     = RegInit(0.U(16.W))
  val stalledLast = RegNext(a.io.in.valid && !a.io.in.ready, false.B)
  when(a.io.in.fire)(fires := fires + 1.U)
  when(a.io.in.valid && !a.io.in.ready && !stalledLast)(stallStarts := stallStarts + 1.U)
  when(a.io.in.valid && !a.io.in.ready)(waitSum := waitSum + 1.U)
  io.fires       := fires
  io.stallStarts := stallStarts
  io.waitSum     := waitSum
}

/** Pass-through adapter that probes its own node on both sides. */
class TLAdapterProbe(implicit p: Parameters) extends LazyModule {
  val node = TLAdapterNode()
  lazy val module = new LazyModuleImp(this) {
    node.in.zip(node.out).foreach { case ((i, _), (o, _)) => o <> i }
    Trace.tl("adp", node, groups = "tl.adp", on = true)
  }
}

class TLDut(implicit p: Parameters) extends LazyModule {
  val fuzz  = LazyModule(new TLFuzzer(nOperations = 200, inFlight = 4))
  val xbar  = LazyModule(new TLXbar)
  val adp   = LazyModule(new TLAdapterProbe)
  val xbar2 = LazyModule(new TLXbar)
  val ram0  = LazyModule(new TLRAM(AddressSet(0x0, 0xfff), beatBytes = 8))
  val ram1  = LazyModule(new TLRAM(AddressSet(0x1000, 0xfff), beatBytes = 8))
  val tapIn = TLNameNode("tapIn")
  val tap0  = TLNameNode("tap0")
  val tap1  = TLNameNode("tap1")
  xbar.node  := tapIn := fuzz.node
  xbar2.node := adp.node := TLBuffer() := xbar.node
  ram0.node  := TLFragmenter(8, 64) := tap0 := xbar2.node
  ram1.node  := TLFragmenter(8, 64) := tap1 := xbar2.node
  lazy val module = new TLDutImp(this)
}

class TLDutImp(outer: TLDut) extends LazyModuleImp(outer) {
  val io = IO(new Bundle { val finished = Output(Bool()) })
  io.finished := outer.fuzz.module.io.finished
  Trace.tl("tapIn", outer.tapIn, groups = "tl", on = true)
  Trace.tl("tap0", outer.tap0, groups = "tl", on = true)
  Trace.tl("tap1", outer.tap1, groups = "tl", on = true)
}

class TLTop extends Module {
  val io = IO(new Bundle { val finished = Output(Bool()) })
  implicit val p: Parameters = Parameters.empty
  val dut = Module(LazyModule(new TLDut).module)
  io.finished := dut.io.finished
}

// negative cases
class ProbesChildNode(implicit p: Parameters) extends LazyModule {
  val fuzz = LazyModule(new TLFuzzer(nOperations = 1))
  val adp  = LazyModule(new TLAdapterProbe)
  val ram  = LazyModule(new TLRAM(AddressSet(0x0, 0xfff), beatBytes = 8))
  ram.node := TLFragmenter(8, 64) := adp.node := fuzz.node
  lazy val module = new LazyModuleImp(this) { Trace.tl("child", adp.node) }
}

class ProbesEphemeral(implicit p: Parameters) extends LazyModule {
  val fuzz = LazyModule(new TLFuzzer(nOperations = 1))
  val ram  = LazyModule(new TLRAM(AddressSet(0x0, 0xfff), beatBytes = 8))
  val eph  = TLEphemeralNode()
  ram.node := TLFragmenter(8, 64) := eph := fuzz.node
  lazy val module = new LazyModuleImp(this) { Trace.tl("eph", eph) }
}

class ProbeInsideWhen extends Module {
  val io = IO(new Bundle { val in = Flipped(Decoupled(UInt(4.W))); val c = Input(Bool()) })
  io.in.ready := true.B
  when(io.c)(Trace.fire("in", io.in))
}

// ---------------------------------------------------------------------------------------------- flow + trace reader

object TraceTestFlow {
  val firtoolArgs = Seq(
    "--format=fir",
    "--verify-each=true",
    "--disable-annotation-classless",
    "--disable-annotation-unknown",
    "--lowering-options=emittedLineLength=2048,noAlwaysComb,disallowLocalVariables,verifLabels,disallowPortDeclSharing,locationInfoStyle=wrapInAtSquareBracket",
    "--split-verilog",
  )

  def hasTool(t: String): Boolean = Try(os.proc("which", t).call(check = false).exitCode == 0).getOrElse(false)

  /** Elaborate, write the catalog, run firtool and the post-pass. Returns the SV directory. */
  def generate(dir: os.Path, gen: => RawModule, traced: Boolean = true): os.Path = {
    os.makeDir.all(dir)
    Trace.reset()
    val fir = if (traced) ChiselStage.emitCHIRRTL(gen) else Trace.withoutTracing(ChiselStage.emitCHIRRTL(gen))
    os.write.over(dir / "top.fir", fir)
    Trace.writeCatalog((dir / "trace_points.json").toString)
    val sv = dir / "sv"
    os.remove.all(sv)
    os.proc(Seq("firtool") ++ firtoolArgs ++ Seq(s"-o=$sv", (dir / "top.fir").toString)).call(cwd = dir)
    PostPass.run(sv.toString)
    sv
  }

  def svFiles(sv: os.Path): Seq[os.Path] =
    os.walk(sv).filter(p => os.isFile(p) && (p.ext == "sv" || p.ext == "v")).sortBy(_.toString)

  def build(dir: os.Path, sv: os.Path, tb: String): os.Path = {
    os.write.over(dir / "tb.sv", tb)
    val cmd = Seq("verilator", "--binary", "--timing", "-Wno-fatal", "-Wno-lint", "-Wno-style", "--top-module", "tb",
      "-Mdir", (dir / "obj").toString, (dir / "tb.sv").toString) ++ svFiles(sv).map(_.toString)
    os.proc(cmd).call(cwd = dir, stdout = dir / "build.log", mergeErrIntoOut = true)
    dir / "obj" / "Vtb"
  }

  def simulate(dir: os.Path, bin: os.Path, plusargs: String*): Seq[String] =
    os.proc(bin.toString +: plusargs).call(cwd = dir, stderr = os.Pipe).err.lines()

  final case class Decl(t: Long, inst: String, pid: Int, kind: String, name: String, groups: Seq[String], width: Int,
    layout: String, enums: String, meta: Map[String, String])
  final case class Ev(t: Long, kind: String, inst: String, pid: Int, hex: Option[String])

  def parse(lines: Seq[String]): (Seq[Decl], Seq[Ev]) = {
    val rt    = lines.filter(_.startsWith("@RT ")).map(_.split(" ").toSeq)
    val decls = rt.collect { case Seq(_, t, "DECL", inst, pid, kind, name, gs, w, layout, enums, meta) =>
      Decl(t.toLong, inst, pid.toInt, kind, name, gs.split(","), w.toInt, layout, enums,
        meta.split(";").map(_.split("=", 2)).collect { case Array(k, v) => k -> v }.toMap)
    }
    val evs = rt.collect {
      case Seq(_, t, k @ ("EVENT" | "STALL"), inst, pid)      => Ev(t.toLong, k, inst, pid.toInt, None)
      case Seq(_, t, k @ ("EVENT" | "STALL"), inst, pid, hex) => Ev(t.toLong, k, inst, pid.toInt, Some(hex))
    }
    (decls, evs)
  }

  /** Decode a payload with its DECL layout: name -> value (signed fields sign-extended). */
  def decode(d: Decl, hex: String): Map[String, BigInt] = {
    val v      = BigInt(hex, 16)
    val fields = d.layout.split(",").map(_.split("#").head.split(":")).map(a => (a(0), a(1).toInt, a.lift(2).getOrElse("h")))
    var pos    = fields.map(_._2).sum
    fields.map { case (n, w, t) =>
      pos -= w
      val raw = (v >> pos) & ((BigInt(1) << w) - 1)
      n -> (if (t == "s" && raw.testBit(w - 1)) raw - (BigInt(1) << w) else raw)
    }.toMap
  }

  def enumName(d: Decl, table: String, v: BigInt): String =
    d.enums.split(";").find(_.startsWith(s"$table=")).flatMap { e =>
      e.drop(table.length + 1).split("/").map(_.split(":", 2)).collectFirst { case Array(k, n) if BigInt(k) == v => n }
    }.getOrElse("?")
}

// ---------------------------------------------------------------------------------------------- tests

class TraceSelfTest extends AnyFreeSpec {
  import TraceTestFlow._

  private val root  = os.pwd / "build" / "rtltrace-selftest"
  private val tools = hasTool("firtool") && hasTool("verilator")
  private val clk   = 10000L // ps, tb clock period 10 ns
  private val clk2  = 30000L

  private val probeTb =
    """`timescale 1ns/1ps
      |module tb;
      |  reg clock = 0, clk2 = 0, reset = 1, drain = 0;
      |  always #5 clock = ~clock;
      |  always #15 clk2 = ~clk2;
      |  wire [15:0] fires, stallStarts, waitSum;
      |  ProbeTop dut(.clock(clock), .reset(reset), .clk2(clk2), .io_drain(drain), .io_fires(fires),
      |               .io_stallStarts(stallStarts), .io_waitSum(waitSum));
      |  integer cyc = 0;
      |  always @(posedge clock) begin
      |    cyc <= cyc + 1;
      |    if (cyc == 3) reset <= 0;
      |    if (cyc == 60) reset <= 1;
      |    if (cyc == 62) reset <= 0;
      |    if (cyc == 400) drain <= 1;
      |    if (cyc == 460) begin
      |      $fwrite(32'h80000002, "SB fires=%0d stall_starts=%0d wait=%0d\n", fires, stallStarts, waitSum);
      |      $finish;
      |    end
      |  end
      |endmodule
      |""".stripMargin

  private lazy val probe = {
    val dir = root / "probe"
    val sv  = generate(dir, new ProbeTop(traced = true))
    (dir, sv, build(dir, sv, probeTb))
  }

  "zero footprint: probes under RTLTRACE=off give the same SV as no probes" in {
    assume(tools, "firtool/verilator not on PATH")
    val a  = generate(root / "zf_plain", new ProbeTop(traced = false))
    val b  = generate(root / "zf_off", new ProbeTop(traced = true), traced = false)
    val fa = svFiles(a).map(p => p.relativeTo(a).toString -> os.read(p)).toMap
    val fb = svFiles(b).map(p => p.relativeTo(b).toString -> os.read(p)).toMap
    assert(fa.keySet == fb.keySet)
    fa.foreach { case (k, v) => assert(v == fb(k), s"$k differs") }
  }

  "post-pass leaves no placeholder and keeps dedup" in {
    assume(tools, "firtool/verilator not on PATH")
    val (_, sv, _) = probe
    val names      = svFiles(sv).map(_.last)
    assert(names.contains("Stage.sv") && !names.exists(_.startsWith("Stage_")), s"Stage was not deduplicated: $names")
    val all = svFiles(sv).map(os.read(_)).mkString
    assert(!all.contains("@@"))
    assert(all.contains("\"@RT %0t EVENT %m "))
    assert(PostPass.run(sv.toString) == 0, "post-pass must be idempotent")
  }

  "synthesis view has no simulation constructs" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, sv, _) = probe
    val pre = os.proc(Seq("verilator", "-E", "-P", "-DSYNTHESIS") ++ svFiles(sv).map(_.toString)).call(cwd = dir).out.text()
    Seq("$test$plusargs", "$fwrite", "$realtime", "@RT").foreach(k => assert(!pre.contains(k), s"$k visible with SYNTHESIS defined"))
  }

  "handshake counts and waits match the hardware scoreboard" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, _, bin) = probe
    val out           = simulate(dir, bin)
    val sb            = out.find(_.startsWith("SB ")).get
    val Seq(fires, stalls, waitSum) =
      "fires=(\\d+) stall_starts=(\\d+) wait=(\\d+)".r.findFirstMatchIn(sb).get.subgroups.map(_.toInt)
    val (decls, evs) = parse(out)
    val in           = decls.filter(d => d.inst.endsWith("dut.a") && d.name == "in")
    assert(in.size == 2, "DECL must be printed after the initial reset and after the reset pulse")
    val after = in.map(_.t).max
    val mine  = evs.filter(e => e.inst.endsWith("dut.a") && e.pid == in.head.pid && e.t >= after)
    var pend  = Option.empty[Long]
    var waits = Seq.empty[Long]
    mine.foreach {
      case Ev(t, "STALL", _, _, _) => pend = Some(t)
      case Ev(t, "EVENT", _, _, _) => waits :+= pend.map(s => (t - s) / clk).getOrElse(0L); pend = None
      case _ =>
    }
    assert(waits.size == fires)
    assert(mine.count(_.kind == "STALL") == stalls)
    assert(waits.sum == waitSum)
    assert(fires > 20 && stalls > 5, s"too little traffic: $sb")
  }

  "records decode to the driven values (A4 round trip)" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, _, bin) = probe
    val (decls, evs)  = parse(simulate(dir, bin))
    val rt            = decls.find(_.name == "rt").get
    assert(rt.layout == "u:8,s:8:s,v[0]:2,v[1]:2,n.x:1:b,n.e:2:e=Mode,n.w:4")
    val recs = evs.filter(e => e.pid == rt.pid && e.kind == "EVENT").map(e => decode(rt, e.hex.get))
    assert(recs.size > 20)
    recs.foreach { r =>
      val u = r("u")
      assert(r("s") == (if (u >= 128) u - 256 else u))
      assert(r("v[0]") == (u & 3) && r("v[1]") == ((u >> 2) & 3))
      assert(r("n.x") == (u & 1) && r("n.w") == (u >> 4))
      assert(enumName(rt, "Mode", r("n.e")) == (if (u.testBit(1)) "Drain" else "Idle"))
    }
    val st = decls.find(d => d.name == "state" && d.inst.endsWith("dut.a")).get
    assert(st.enums == "Mode=0:Idle/1:Run/2:Drain")
  }

  "instances, pending handshakes, reset and two clocks" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, _, bin) = probe
    val (decls, evs)  = parse(simulate(dir, bin))
    assert(decls.filter(_.name == "in").map(_.inst).toSet.map((i: String) => i.split('.').last) == Set("a", "b"))
    assert(decls.filter(_.name == "in").map(_.pid).toSet.size == 1, "one probe id shared by both instances")
    val stuck = decls.find(_.name == "stuck").get
    val se    = evs.filter(_.pid == stuck.pid)
    assert(se.lastOption.exists(_.kind == "STALL") && !se.exists(_.kind == "EVENT"), "stuck port must end pending")
    val ts = (decls.map(_.t) ++ evs.map(_.t))
    assert(evs.map(_.t) == evs.map(_.t).sorted, "timestamps must be monotonic")
    assert(ts.forall(_ >= 0))
    val slow = decls.find(_.name == "slow").get
    val st   = evs.filter(_.pid == slow.pid).map(_.t)
    assert(st.size > 5 && st.zip(st.tail).forall { case (x, y) => (y - x) % clk2 == 0 })
    assert(evs.filter(_.pid == decls.find(_.name == "in").get.pid).forall(e => (e.t - 5000) % clk == 0))
  }

  "plusargs switch groups at runtime" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, _, bin) = probe
    def names(args: String*) = parse(simulate(dir, bin, args: _*))._1.map(_.name).toSet
    val dflt = names()
    assert(dflt.contains("in") && dflt.contains("state") && dflt.contains("rt") && !dflt.contains("done"))
    assert(names("+rtltrace_stage.done_on").contains("done"))
    val off = names("+rtltrace_stage_off")
    assert(!off.contains("in") && !off.contains("state") && off.contains("rt"))
    assert(names("+rtltrace_all_off").isEmpty)
    val all = names("+rtltrace_all_on")
    assert(all.contains("done") && all.contains("req") && all.contains("lanes[1]"))
  }

  "catalog lists every probe with its layout" in {
    assume(tools, "firtool/verilator not on PATH")
    val (dir, _, bin) = probe
    val cat           = os.read(dir / "trace_points.json")
    val (decls, _)    = parse(simulate(dir, bin, "+rtltrace_all_on"))
    decls.foreach(d => assert(cat.contains(s""""pid": ${d.pid}, "kind": "${d.kind}", "name": "${d.name}""""), d.name))
    assert(cat.contains(""""name": "resp"""") && cat.contains("dir=out") && cat.contains("dir=in"))
    def dirOf(n: String) = decls.find(_.name == n).get.meta.get("dir")
    assert(dirOf("in").contains("in") && dirOf("q.deq").isEmpty && dirOf("q.enq").contains("in"))
  }

  "TileLink ports: every request pairs with one response" in {
    assume(tools, "firtool/verilator not on PATH")
    val dir = root / "tl"
    val sv  = generate(dir, new TLTop)
    val tb =
      """`timescale 1ns/1ps
        |module tb;
        |  reg clock = 0, reset = 1;
        |  wire finished;
        |  always #5 clock = ~clock;
        |  TLTop dut(.clock(clock), .reset(reset), .io_finished(finished));
        |  integer cyc = 0;
        |  always @(posedge clock) begin
        |    cyc <= cyc + 1;
        |    if (cyc == 3) reset <= 0;
        |    if ((!reset && finished && cyc > 10) || cyc == 20000) begin
        |      $fwrite(32'h80000002, "TL finished=%0d cycles=%0d\n", finished, cyc);
        |      $finish;
        |    end
        |  end
        |endmodule
        |""".stripMargin
    val out          = simulate(dir, build(dir, sv, tb))
    assert(out.exists(_.startsWith("TL finished=1")), out.takeRight(5).mkString("\n"))
    val (decls, evs) = parse(out)
    val ports        = decls.map(_.meta("port")).distinct
    assert(ports.toSet == Set("tapIn", "tap0", "tap1", "adp.in0", "adp.out0"), ports)
    assert(decls.find(_.name == "adp.in0.a").get.meta("side") == "in")
    val t0 = decls.find(_.name == "tap0.a").get.meta
    assert(t0("clients") == "Fuzzer@0-3" && t0("managers") == "ram0@0x0/0xfff" && t0("beat") == "8", t0)
    assert(t0("src").matches("TraceSelfTest.scala:\\d+") && t0("at").matches("TraceSelfTest.scala:\\d+"), t0)
    val dataA = Set("PutFullData", "PutPartialData", "ArithmeticData", "LogicalData")
    val dataD = Set("AccessAckData", "GrantData")
    val count = ports.map { port =>
      val a     = decls.find(d => d.meta("port") == port && d.meta("tl") == "a").get
      val d     = decls.find(d => d.meta("port") == port && d.meta("tl") == "d").get
      val beat  = a.meta("beat").toInt
      val chans = evs.filter(e => e.kind == "EVENT" && (e.pid == a.pid || e.pid == d.pid))
        .sortBy(e => (e.t, if (e.pid == a.pid) 1 else 0)) // same cycle: the response frees the source first
      // source -> (request beats still to come, response beats still to come); a transaction completes when both are 0
      val open = scala.collection.mutable.Map[BigInt, (Int, Int)]()
      var done = 0
      def settle(s: BigInt): Unit = if (open.get(s).contains((0, 0))) { open.remove(s); done += 1 }
      chans.foreach { e =>
        if (e.pid == a.pid) {
          val f  = decode(a, e.hex.get)
          val s  = f("source")
          val op = enumName(a, "TLA", f("opcode"))
          val nb = math.max(1, (1 << f("size").toInt) / beat)
          open.get(s) match {
            case Some((aLeft, dLeft)) if aLeft > 0 => open(s) = (aLeft - 1, dLeft)
            case Some(_) => fail(s"$port: source $s reused before its response")
            case None =>
              val dBeats = if (op == "Get" || op == "ArithmeticData" || op == "LogicalData") nb else 1
              open(s) = ((if (dataA(op)) nb else 1) - 1, dBeats)
          }
          settle(s)
        } else {
          val f              = decode(d, e.hex.get)
          val s              = f("source")
          val (aLeft, dLeft) = open.getOrElse(s, fail(s"$port: response without request (source $s)"))
          assert(dLeft > 0, s"$port: extra response beat for source $s")
          if (!dataD(enumName(d, "TLD", f("opcode")))) assert(dLeft == 1, s"$port: data-less response for a burst")
          open(s) = (aLeft, dLeft - 1)
          settle(s)
        }
      }
      assert(open.isEmpty, s"$port: unanswered requests $open")
      port -> done
    }.toMap
    // conservation: every transaction crosses the input tap, the adapter, and exactly one RAM tap
    assert(count("tapIn") >= 50, count)
    assert(count("adp.in0") == count("tapIn") && count("adp.out0") == count("tapIn"), count)
    assert(count("tap0") + count("tap1") == count("tapIn") && count("tap0") > 0 && count("tap1") > 0, count)
  }

  "elaboration rejects nodes it cannot probe" in {
    implicit val p: Parameters = Parameters.empty
    val child = intercept[Exception](ChiselStage.emitCHIRRTL(LazyModule(new ProbesChildNode).module))
    assert(Iterator.iterate[Throwable](child)(_.getCause).takeWhile(_ != null).exists(_.getMessage.contains("belongs to")))
    val eph = intercept[Exception](ChiselStage.emitCHIRRTL(LazyModule(new ProbesEphemeral).module))
    assert(Iterator.iterate[Throwable](eph)(_.getCause).takeWhile(_ != null).exists(_.getMessage.contains("ephemeral")))
    val w = intercept[Exception](ChiselStage.emitCHIRRTL(new ProbeInsideWhen))
    assert(Iterator.iterate[Throwable](w)(_.getCause).takeWhile(_ != null).exists(_.getMessage.contains("when")))
  }

  "escaping keeps names decodable and meta values readable" in {
    assert(Encoder.esc("Core 0 DCache") == "Core~200~20DCache")
    assert(Encoder.esc("[CR_DMA] Orch Client") == "[CR_DMA]~20Orch~20Client")
    assert(Encoder.esc("a@b|c") == "a~40b~7Cc")
    assert(Encoder.escMeta("x@0-3|y@4-7") == "x@0-3|y@4-7")
    assert(Encoder.escMeta("a b;c=d%") == "a~20b~3Bc~3Dd~25")
    assert(Encoder.sanitize("lanes[1].bits/x") == "lanes[1].bits_x")
  }

  "post-pass rewrite of firtool statements" in {
    def rw(t: String) = PostPass.rewrite(t)._1
    val in  = """      $fwrite(32'h80000002, "@@EN:llc+mem:1@@@RT@@TIME@@ EVENT @@INST@@ 17 %x\n", payload);	// @[x.scala:1:2]"""
    val out = rw(in)
    assert(
      out == """      if (($test$plusargs("rtltrace_all_on") || $test$plusargs("rtltrace_llc_on") || $test$plusargs("rtltrace_mem_on") || """ +
        """!($test$plusargs("rtltrace_all_off") || $test$plusargs("rtltrace_llc_off") || $test$plusargs("rtltrace_mem_off")))) """ +
        """$fwrite(32'h80000002, "@RT %0t EVENT %m 17 %x\n", $realtime, payload);	// @[x.scala:1:2]""",
    )
    assert(rw(out) == out)
    val dormant = rw("""$fwrite(32'h80000002, "@@EN:x:0@@@RT@@TIME@@ STALL @@INST@@ 3\n");""")
    assert(dormant == """if (($test$plusargs("rtltrace_all_on") || $test$plusargs("rtltrace_x_on"))) $fwrite(32'h80000002, "@RT %0t STALL %m 3\n", $realtime);""")
    // firtool without emittedLineLength wraps the statement: the format string starts on the next line
    val wrapped = "        $fwrite(32'h80000002,\n                \"@@EN:g:1@@@RT@@TIME@@ EVENT @@INST@@ 5 %x\\n\",\n                {a, b});"
    val (w, n)  = PostPass.rewrite(wrapped)
    assert(n == 1 && !w.contains("@@"))
    assert(w.contains("$fwrite(32'h80000002,\n                \"@RT %0t EVENT %m 5 %x\\n\", $realtime,\n                {a, b});"), w)
    val plain = """$fwrite(32'h80000002, "hello %d\n", x);"""
    assert(rw(plain) == plain)
  }
}

/** The post-pass hook inside emitrtl.VerilatorSimulator (the ChiselSim flow used by e.g. OrchTest). */
class TraceChiselSimTest extends AnyFreeSpec with _root_.emitrtl.VerilatorSimulator {
  "VerilatorSimulator runs the post-pass before compiling" in {
    assume(TraceTestFlow.hasTool("verilator"), "verilator not on PATH")
    simulate(new Stage(traced = true)) { dut =>
      dut.io.in.valid.poke(true.B)
      dut.io.in.bits.id.poke(7.U)
      dut.io.out.ready.poke(true.B)
      dut.clock.step(10)
    }
    val ws   = os.pwd / "build" / "chiselsim" / "TraceChiselSimTest"
    assert(os.walk(ws).exists(p => p.last == "trace_points.json" && os.read(p).contains(""""name": "in"""")))
    val srcs = os.walk(ws).filter(p => p.last == "Stage.sv")
    assert(srcs.nonEmpty)
    srcs.foreach { p =>
      val t = os.read(p)
      assert(!t.contains("@@") && t.contains("\"@RT %0t EVENT %m "), p.toString)
    }
  }
}
