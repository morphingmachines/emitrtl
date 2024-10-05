// See README.md for license details.

package emitrtl

import chisel3._
import chisel3.util._

/** Compute GCD using subtraction method. Subtracts the smaller from the larger until register y is zero. value in
  * register x is then the GCD
  */
class GCD extends Module {
  val io = IO(new Bundle {
    val value1        = Input(UInt(16.W))
    val value2        = Input(UInt(16.W))
    val loadingValues = Input(Bool())
    val outputGCD     = Output(UInt(16.W))
    val outputValid   = Output(Bool())
  })

  val x = Reg(UInt())
  val y = Reg(UInt())

  when(x > y)(x := x - y).otherwise(y := y - x)

  when(io.loadingValues) {
    x := io.value1
    y := io.value2
  }

  io.outputGCD   := x
  io.outputValid := y === 0.U
}

class MyModule extends Module {

  val input1 = IO(Input(UInt(6.W)))
  val output = IO(Output(UInt(4.W)))
  val tmp    = 5.U(3.W) -& 6.U(3.W)
  output := Cat(tmp, input1(0, 0))
}
