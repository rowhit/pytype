"""Tests for blocks.py."""

import unittest


from pytype import blocks
from pytype.pyc import loadmarshal
import unittest


class BlocksTest(unittest.TestCase):
  """Tests for blocks.py."""

  PYTHON_VERSION = (2, 7)

  def make_code(self, byte_array, name="testcode"):
    return loadmarshal.CodeType(
        argcount=0, kwonlyargcount=0, nlocals=0, stacksize=2, flags=0,
        consts=[None, 1, 2], names=[], varnames=[], filename="", name=name,
        firstlineno=1, lnotab=[], freevars=[], cellvars=[],
        code="".join(chr(c) for c in byte_array),
        python_version=self.PYTHON_VERSION)

  def test_trivial(self):
    # Disassembled from:
    # | return None
    co = self.make_code([
        0x64, 1, 0,  # 0 LOAD_CONST, arg=0 (None)
        0x53,  # 3 RETURN_VALUE, dead.
    ], name="trivial")
    ordered_code = blocks.order_code(co)
    b0, = ordered_code.order
    self.assertEquals(2, len(b0.code))
    self.assertItemsEqual([], b0.incoming)
    self.assertItemsEqual([], b0.outgoing)

  def test_yield(self):
    # Disassembled from:
    # | yield 1
    # | yield None
    co = self.make_code([
        # b0:
        0x64, 1, 0,  # 0 LOAD_CONST, arg=1 (1),
        0x56,  # 3 YIELD_VALUE,
        # b1:
        0x01,  # 4 POP_TOP,
        0x64, 0, 0,  # 15 LOAD_CONST, arg=0 (None),
        0x53,  # 18 RETURN_VALUE
    ], name="yield")
    ordered_code = blocks.order_code(co)
    self.assertEquals(ordered_code.co_name, "yield")
    b0, b1 = ordered_code.order
    self.assertItemsEqual(b0.outgoing, [b1])
    self.assertItemsEqual(b1.incoming, [b0])
    self.assertItemsEqual(b0.incoming, [])
    self.assertItemsEqual(b1.outgoing, [])

  def test_triangle(self):
    # Disassembled from:
    # | x = y
    # | if y > 1:
    # |   x -= 2
    # | return x
    co = self.make_code([
        # b0:
        0x7c, 0, 0,  # 0 LOAD_FAST, arg=0,
        0x7d, 1, 0,  # 3 STORE_FAST, arg=1,
        0x7c, 0, 0,  # 6 LOAD_FAST, arg=0,
        0x64, 1, 0,  # 9 LOAD_CONST, arg=1 (1),
        0x6b, 4, 0,  # 12 COMPARE_OP, arg=4,
        0x72, 31, 0,  # 15 POP_JUMP_IF_FALSE, dest=31,
        # b1:
        0x7c, 1, 0,  # 18 LOAD_FAST, arg=1,
        0x64, 2, 0,  # 21 LOAD_CONST, arg=2,
        0x38,  # 24 INPLACE_SUBTRACT,
        0x7d, 1, 0,  # 25 STORE_FAST, arg=1,
        0x6e, 0, 0,  # 28 JUMP_FORWARD, dest=31,
        # b2:
        0x7c, 1, 0,  # 31 LOAD_FAST, arg=1,
        0x53,  # 34 RETURN_VALUE
    ], name="triangle")
    ordered_code = blocks.order_code(co)
    self.assertEquals(ordered_code.co_name, "triangle")
    b0, b1, b2 = ordered_code.order
    self.assertItemsEqual(b0.incoming, [])
    self.assertItemsEqual(b0.outgoing, [b1, b2])
    self.assertItemsEqual(b1.incoming, [b0])
    self.assertItemsEqual(b1.outgoing, [b2])
    self.assertItemsEqual(b2.incoming, [b0, b1])
    self.assertItemsEqual(b2.outgoing, [])

  def test_diamond(self):
    # Disassembled from:
    # | x = y
    # | if y > 1:
    # |   x -= 2
    # | else:
    # |   x += 2
    # | return x
    co = self.make_code([
        # b0:
        0x7c, 0, 0,  # 0 LOAD_FAST, arg=0,
        0x7d, 1, 0,  # 3 STORE_FAST, arg=1,
        0x7c, 0, 0,  # 6 LOAD_FAST, arg=0,
        0x64, 1, 0,  # 9 LOAD_CONST, arg=1,
        0x6b, 4, 0,  # 12 COMPARE_OP, arg=4,
        0x72, 31, 0,  # 15 POP_JUMP_IF_FALSE, dest=31,
        # b1:
        0x7c, 1, 0,  # 18 LOAD_FAST, arg=1,
        0x64, 2, 0,  # 21 LOAD_CONST, arg=2,
        0x38,  # 24 INPLACE_SUBTRACT,
        0x7d, 1, 0,  # 25 STORE_FAST, arg=1,
        0x6e, 10, 0,  # 28 JUMP_FORWARD, dest=41,
        # b2:
        0x7c, 1, 0,  # 31 LOAD_FAST, arg=1,
        0x64, 2, 0,  # 34 LOAD_CONST, arg=2,
        0x37,  # 37 INPLACE_ADD,
        0x7d, 1, 0,  # 38 STORE_FAST, arg=1,
        # b3:
        0x7c, 1, 0,  # 41 LOAD_FAST, arg=1,
        0x53,  # 44 RETURN_VALUE
    ], name="diamond")
    ordered_code = blocks.order_code(co)
    self.assertEquals(ordered_code.co_name, "diamond")
    b0, b1, b2, b3 = ordered_code.order
    self.assertItemsEqual(b0.incoming, [])
    self.assertItemsEqual(b0.outgoing, [b1, b2])
    self.assertItemsEqual(b1.incoming, [b0])
    self.assertItemsEqual(b1.outgoing, [b3])
    self.assertItemsEqual(b2.incoming, [b0])
    self.assertItemsEqual(b2.outgoing, [b3])
    self.assertItemsEqual(b3.incoming, [b1, b2])
    self.assertItemsEqual(b3.outgoing, [])

  def test_raise(self):
    # Disassembled from:
    # | raise ValueError()
    # | return 1
    co = self.make_code([
        # b0:
        0x74, 0, 0,  # 0 LOAD_GLOBAL, arg=0,
        0x82, 1, 0,  # 6 RAISE_VARARGS, arg=1,
        0x64, 1, 0,  # 9 LOAD_CONST, arg=1, dead.
        0x53,  # 12 RETURN_VALUE, dead.
    ], name="raise")
    ordered_code = blocks.order_code(co)
    self.assertEquals(ordered_code.co_name, "raise")
    b0, = ordered_code.order
    self.assertEquals(2, len(b0.code))
    self.assertItemsEqual(b0.incoming, [])
    self.assertItemsEqual(b0.outgoing, [])

  def test_call(self):
    # Disassembled from:
    # | f()
    co = self.make_code([
        # b0:
        0x74, 0, 0,  # 0 LOAD_GLOBAL, arg=0,
        0x83, 0, 0,  # 3 CALL_FUNCTION, arg=0,
        # b1:
        0x01,  # 6 POP_TOP,
        0x64, 0, 0,  # 7 LOAD_CONST, arg=0,
        0x53,  # 10 RETURN_VALUE
    ], name="call")
    ordered_code = blocks.order_code(co)
    b0, b1 = ordered_code.order
    self.assertEquals(2, len(b0.code))
    self.assertEquals(3, len(b1.code))
    self.assertItemsEqual(b0.outgoing, [b1])

  def test_finally(self):
    # Disassembled from:
    # | try:
    # |   pass
    # | finally:
    # |   pass
    co = self.make_code([
        # b0:
        0x7a, 4, 0,  # 0 SETUP_FINALLY, dest=7,
        0x57,  # 3 POP_BLOCK,
        0x64, 0, 0,  # 4 LOAD_CONST, arg=0 (None),
        # b1:
        0x58,  # 7 END_FINALLY,
        # b2:
        0x64, 0, 0,  # 8 LOAD_CONST, arg=0 (None),
        0x53,  # 11 RETURN_VALUE
    ], name="finally")
    ordered_code = blocks.order_code(co)
    b0, b1, b2 = ordered_code.order
    self.assertEquals(3, len(b0.code))
    self.assertEquals(1, len(b1.code))
    self.assertEquals(2, len(b2.code))
    self.assertItemsEqual(b0.outgoing, [b1])

  @unittest.skip("Needs handling of stored jumps")
  def test_except(self):
    # Disassembled from:
    # | try:
    # |   pass
    # | except:
    # |   pass
    co = self.make_code([
        # b0:
        0x79, 4, 0,  # 0 SETUP_EXCEPT, dest=7,
        0x57,  # 3 POP_BLOCK,
        0x6e, 7, 0,  # 4 JUMP_FORWARD, dest=14,
        # b1:
        0x01,  # 7 POP_TOP,
        0x01,  # 8 POP_TOP,
        0x01,  # 9 POP_TOP,
        0x6e, 1, 0,  # 10 JUMP_FORWARD, dest=14,
        # b2:
        0x58,  # 13 END_FINALLY,
        # b3:
        0x64, 0, 0,  # 14 LOAD_CONST, arg=0,
        0x53,  # 17 RETURN_VALUE
    ], name="except")
    ordered_code = blocks.order_code(co)
    b0, b1, b2, b3 = ordered_code.order
    self.assertEquals(3, len(b0.code))
    self.assertEquals(4, len(b1.code))
    self.assertEquals(1, len(b2.code))
    self.assertEquals(2, len(b3.code))
    self.assertItemsEqual(b0.outgoing, [b3])
    self.assertItemsEqual(b1.outgoing, [b3])
    self.assertItemsEqual(b2.outgoing, [b3])

  def test_return(self):
    # Disassembled from:
    # | return None
    # | return None
    co = self.make_code([
        0x64, 1, 0,  # 0 LOAD_CONST, arg=0 (None)
        0x53,  # 3 RETURN_VALUE, dead.
        0x64, 1, 0,  # 4 LOAD_CONST, arg=0 (None), dead.
        0x53,  # 7 RETURN_VALUE, dead.
    ], name="return")
    ordered_code = blocks.order_code(co)
    b0, = ordered_code.order
    self.assertEquals(2, len(b0.code))

  @unittest.skip("Needs handling of stored jumps")
  def test_with(self):
    # Disassembled from:
    # | with None:
    # |   pass
    co = self.make_code([
        0x64, 0, 0,  # 0 LOAD_CONST, arg=0,
        0x8f, 5, 0,  # 3 SETUP_WITH, dest=11,
        0x01,  # 6 POP_TOP,
        0x57,  # 7 POP_BLOCK,
        0x64, 0, 0,  # 8 LOAD_CONST, arg=0,
        0x51,  # 11 WITH_CLEANUP,
        0x58,  # 12 END_FINALLY,
        0x64, 0, 0,  # 13 LOAD_CONST, arg=0,
        0x53,  # 16 RETURN_VALUE
    ], name="with")
    ordered_code = blocks.order_code(co)
    b0, b1, b2, b3 = ordered_code.order
    self.assertEquals(2, len(b0.code))
    self.assertEquals(3, len(b1.code))
    self.assertEquals(1, len(b2.code))
    self.assertEquals(3, len(b3.code))

if __name__ == "__main__":
  unittest.main()