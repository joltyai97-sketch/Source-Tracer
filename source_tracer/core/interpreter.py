"""Tree-walking interpreter for C and a practical subset of C++.

Runs directly over the tree-sitter syntax tree of the preprocessed source.
Values carry their C type so integer widths, signedness and wrap-around
behave like on the target (sizes from the profile). Anything not supported
produces a warning and the run continues where possible.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from source_tracer.core import memory as M
from source_tracer.core.events import (
    CallEvent,
    Event,
    OutputText,
    RegisterAccess,
    ReturnEvent,
    RunResult,
    RunWarning,
    Transmit,
    VarWrite,
)
from source_tracer.core.hardware import Bus, HardwareInputs
from source_tracer.core.literals import char_value, number_value, string_value
from source_tracer.core.memory import (
    ArrayObj,
    Box,
    BytePtr,
    FuncPtr,
    MmioPtr,
    Ptr,
    StructObj,
    is_pointer_value,
    make_object,
)
from source_tracer.core.program import FunctionDef, Program, Prototype, Unit
from source_tracer.core.types import (
    VOID,
    ArrayType,
    CType,
    EnumType,
    FloatType,
    FunctionType,
    IntType,
    PointerType,
    ReferenceType,
    StructType,
    VoidType,
)
from source_tracer.frontend.parser import node_text

# --------------------------------------------------------------------------- control flow


class StepLimit(Exception):
    pass


class ExitProgram(Exception):
    def __init__(self, code: int):
        self.code = code


class InterpError(Exception):
    def __init__(self, message: str, node: Node | None = None):
        super().__init__(message)
        self.node = node


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Return(Exception):
    def __init__(self, value, ctype: CType):
        self.value = value
        self.ctype = ctype


# --------------------------------------------------------------------------- inputs


@dataclass
class RunInputs:
    """Everything the user can set before a run."""

    args: list = field(default_factory=list)  # values or text, one per parameter
    globals: dict[str, object] = field(default_factory=dict)  # name -> value/text (applied after initialisers)
    stubs: dict[str, list] = field(default_factory=dict)  # function -> return values in order (last repeats)
    hardware: HardwareInputs = field(default_factory=HardwareInputs)
    max_steps: int = 200_000
    max_events: int = 100_000
    record_register_reads: bool = False
    max_call_depth: int = 200


# --------------------------------------------------------------------------- lvalues


class LV:
    type: CType

    def get(self):
        raise NotImplementedError

    def set(self, value, vtype: CType) -> None:
        raise NotImplementedError

    def address(self):
        raise NotImplementedError


class SlotLV(LV):
    __slots__ = ("it", "seq", "index", "type")

    def __init__(self, it: Interpreter, seq: list, index: int, ctype: CType):
        self.it, self.seq, self.index, self.type = it, seq, index, ctype

    @property
    def slot(self):
        if not 0 <= self.index < len(self.seq):
            raise InterpError(f"index {self.index} is outside '{_base_name(self.seq)}' (size {len(self.seq)})")
        return self.seq[self.index]

    def get(self):
        s = self.slot
        if isinstance(s, Box):
            if s.type is not self.type and s.type != self.type and self.type.is_scalar and s.type.is_scalar:
                return self.it.convert(s.value, s.type, self.type)
            return s.value
        return s

    def set(self, value, vtype: CType) -> None:
        s = self.slot
        if isinstance(s, Box):
            self.it.store(s, value, vtype)
        else:
            self.it.store_aggregate(s, value)

    def address(self):
        return Ptr(self.seq, self.index, self.type)


class MmioLV(LV):
    __slots__ = ("it", "addr", "type")

    def __init__(self, it: Interpreter, addr: int, ctype: CType):
        self.it, self.addr, self.type = it, addr, ctype

    def get(self):
        t = self.type
        if isinstance(t, (StructType, ArrayType)):
            raise InterpError("copying a whole hardware block is not supported")
        size = max(t.size, 1)
        raw = self.it.bus.read(self.addr, size)
        if isinstance(t, FloatType):
            return M.scalar_from_bytes(t, raw.to_bytes(size, "big" if self.it.endian == "big" else "little"), self.it.endian)
        if isinstance(t, PointerType):
            return MmioPtr(raw, t.target) if raw else 0
        return t.wrap(raw) if isinstance(t, IntType) else raw

    def set(self, value, vtype: CType) -> None:
        t = self.type
        v = self.it.convert(value, vtype, t)
        size = max(t.size, 1)
        if isinstance(t, FloatType):
            raw = int.from_bytes(M.scalar_to_bytes(t, v, self.it.endian), "big" if self.it.endian == "big" else "little")
        else:
            raw = M.pointer_address(v) if is_pointer_value(v) else int(v)
        self.it.bus.write(self.addr, size, raw)

    def address(self):
        return MmioPtr(self.addr, self.type)


class ByteLV(LV):
    __slots__ = ("it", "seq", "index", "offset", "type")

    def __init__(self, it: Interpreter, seq: list, index: int, offset: int, ctype: CType):
        self.it, self.seq, self.index, self.offset, self.type = it, seq, index, offset, ctype

    def get(self):
        t = self.type
        size = max(t.size, 1)
        try:
            data = M.read_region(self.seq, self.index, self.offset, size, self.it.endian)
        except IndexError:
            raise InterpError(f"byte access outside '{_base_name(self.seq)}'") from None
        if isinstance(t, (StructType, ArrayType)):
            obj = make_object(t)
            M.from_bytes(obj, data, self.it.endian)
            return obj
        return M.scalar_from_bytes(t, data, self.it.endian)

    def set(self, value, vtype: CType) -> None:
        t = self.type
        if isinstance(value, (ArrayObj, StructObj)):
            data = M.to_bytes(value, self.it.endian)
        else:
            data = M.scalar_to_bytes(t, self.it.convert(value, vtype, t), self.it.endian)
        try:
            changed = M.write_region(self.seq, self.index, self.offset, data, self.it.endian)
        except IndexError:
            raise InterpError(f"byte access outside '{_base_name(self.seq)}'") from None
        for box in changed:
            self.it.note_write(box, None)

    def address(self):
        return BytePtr(self.seq, self.index, self.offset, self.type)


class ValueLV(LV):
    """A temporary (rvalue bound to a reference parameter)."""

    def __init__(self, value, ctype: CType):
        self.box = Box(ctype, value)
        self.type = ctype

    def get(self):
        return self.box.value

    def set(self, value, vtype):
        self.box.value = value

    def address(self):
        return Ptr(self.box.seq, 0, self.type)


def _base_name(seq: list) -> str:
    if seq:
        name = getattr(seq[0], "name", "") or "?"
        return name.rsplit("[", 1)[0]
    return "?"


# --------------------------------------------------------------------------- frames


class Frame:
    __slots__ = ("fn", "unit", "scopes", "this", "varargs")

    def __init__(self, fn: FunctionDef | None, unit: Unit | None, this=None):
        self.fn = fn
        self.unit = unit
        self.scopes: list[dict] = [{}]
        self.this = this
        self.varargs: list = []


_CASTS = {"static_cast", "reinterpret_cast", "const_cast", "dynamic_cast"}
_ASSIGN_OPS = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "&=": "&", "|=": "|", "^=": "^", "<<=": "<<", ">>=": ">>"}


# --------------------------------------------------------------------------- interpreter


class Interpreter:
    def __init__(self, program: Program, inputs: RunInputs | None = None):
        self.prog = program
        self.inputs = inputs or RunInputs()
        self.ints = program.ints
        self.INT = self.ints["int"]
        self.UINT = self.ints["unsigned int"]
        self.CHAR = self.ints["char"]
        self.DOUBLE = FloatType("double", program.profile.type_sizes.get("double", 8))
        self.endian = program.profile.endianness
        self.ptr_size = program.profile.type_sizes.get("pointer", 4)
        self.events: list[Event] = []
        self.steps = 0
        self.depth = 0
        self.coverage: Counter = Counter()
        self.frame = Frame(None, None)
        self.unit: Unit | None = program.units[0] if program.units else None
        self.cur_node: Node | None = None
        self.globals: dict[str, M.Slot] = {}
        self.unit_statics: dict[int, dict[str, M.Slot]] = {}
        self.static_locals: dict = {}
        self.global_box_ids: set[int] = set()
        self.string_literals: dict[int, ArrayObj] = {}
        self.stub_calls: Counter = Counter()
        self._decl_cache: dict = {}
        self._events_full = False
        self.bus = Bus(program.profile, self.inputs.hardware, self._on_bus)
        self._reset_globals()

    # ------------------------------------------------------------------ setup
    def _reset_globals(self) -> None:
        for g in self.prog.globals:
            obj = make_object(g.ctype, g.name)
            if g.static:
                self.unit_statics.setdefault(g.unit.index, {})[g.name] = obj
            else:
                self.globals[g.name] = obj
            for b in M.iter_boxes(obj):
                self.global_box_ids.add(id(b))
        # Initialisers run in file order, like a C runtime's .data/.bss setup.
        for g in self.prog.globals:
            obj = self._global_slot(g.name, g.unit)
            prev_unit, self.unit = self.unit, g.unit
            try:
                if g.init is not None:
                    self.cur_node = g.node
                    self.initialize(obj, g.ctype, g.init, record=False)
                elif isinstance(g.ctype, StructType):
                    self._construct(obj, g.ctype, g.ctor_args)
            except InterpError as e:
                self.warn(f"Initial value of '{g.name}' could not be computed: {e}", g.node)
            finally:
                self.unit = prev_unit
        # User-provided starting values override the code's initialisers.
        for name, value in self.inputs.globals.items():
            slot = self.globals.get(name)
            if slot is None:
                slot = next((d[name] for d in self.unit_statics.values() if name in d), None)
            if slot is None:
                self.warn(f"Variable '{name}' not found; its input value was ignored.")
                continue
            try:
                self.assign_input(slot, value)
            except (ValueError, InterpError) as e:
                self.warn(f"Input for '{name}' not understood ({e}); ignored.")

    def _global_slot(self, name: str, unit: Unit | None):
        if unit is not None:
            s = self.unit_statics.get(unit.index, {}).get(name)
            if s is not None:
                return s
        return self.globals.get(name)

    # ------------------------------------------------------------------ public API
    def run_function(self, name: str, args: list | None = None) -> RunResult:
        fn = self.prog.find_function(name)
        result = RunResult(function=name, stop_reason="error")
        result.globals_before = self.snapshot()
        if fn is None:
            result.message = f"Function '{name}' not found."
            return result
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 20000))
        try:
            arg_values = self._input_args(fn, args if args is not None else self.inputs.args)
            value, vtype = self.call(fn, arg_values, None)
            result.stop_reason = "returned"
            result.return_value = value
            result.return_text = "" if vtype is VOID or isinstance(vtype, VoidType) else self.fmt(value, vtype)
            result.message = f"{name}() finished after {self.steps} steps."
        except StepLimit:
            result.stop_reason = "step_limit"
            result.message = (
                f"Stopped after {self.steps} steps (the step limit). The code may be waiting in a loop "
                "for something that never happens, or it simply runs forever (like a main loop)."
            )
        except ExitProgram as e:
            result.stop_reason = "exit"
            result.message = f"Program called exit({e.code})."
        except InterpError as e:
            result.stop_reason = "error"
            loc = self.location(e.node or self.cur_node)
            where = f" at {loc[0].name}:{loc[1]}" if loc else ""
            result.message = f"Stopped{where}: {e}"
            self.warn(str(e), e.node)
        except RecursionError:
            result.stop_reason = "error"
            result.message = "Stopped: calls nested too deeply (runaway recursion?)."
        finally:
            sys.setrecursionlimit(old_limit)
        result.steps = self.steps
        result.events = self.events
        result.globals_after = self.snapshot()
        result.raw_globals_after = dict(self.globals)
        result.last_location = self.location(self.cur_node)
        for (uidx, row), n in self.coverage.items():
            f, line = self.prog.units[uidx].pre.origin(row)
            result.coverage[(f, line)] = result.coverage.get((f, line), 0) + n
        return result

    def snapshot(self) -> dict[str, str]:
        out = {}
        names = Counter(list(self.globals) + [n for d in self.unit_statics.values() for n in d])
        for name, slot in self.globals.items():
            out[name] = self.fmt_slot(slot)
        for uidx, d in self.unit_statics.items():
            for name, slot in d.items():
                key = name if names[name] == 1 else f"{name} ({self.prog.units[uidx].path.name})"
                out[key] = self.fmt_slot(slot)
        return out

    # ------------------------------------------------------------------ events & locations
    def location(self, node: Node | None) -> tuple[Path, int] | None:
        if node is None:
            return None
        unit = self.unit or (self.prog.units[0] if self.prog.units else None)
        if unit is None:
            return None
        return unit.pre.origin(node.start_point[0])

    def emit(self, cls, **kw) -> None:
        if self._events_full:
            return
        if len(self.events) >= self.inputs.max_events:
            self._events_full = True
            loc = self.location(self.cur_node)
            self.events.append(RunWarning(self.steps, loc[0] if loc else None, loc[1] if loc else None, self._fn_name(), f"Event log full ({self.inputs.max_events}); later events not recorded."))
            return
        loc = self.location(self.cur_node)
        self.events.append(cls(self.steps, loc[0] if loc else None, loc[1] if loc else None, self._fn_name(), **kw))

    def _fn_name(self) -> str:
        return self.frame.fn.qualname if self.frame.fn else ""

    def warn(self, message: str, node: Node | None = None) -> None:
        saved = self.cur_node
        if node is not None:
            self.cur_node = node
        if not any(isinstance(e, RunWarning) and e.message == message for e in self.events[-50:]):
            self.emit(RunWarning, message=message)
        self.cur_node = saved

    def note_write(self, box: Box, old) -> None:
        if id(box) in self.global_box_ids:
            self.emit(VarWrite, name=box.name, old=old, new=box.value, text=self.fmt(box.value, box.type))

    def _on_bus(self, kind, reg, address, value, size) -> None:
        if reg is None:
            return
        if kind == "write" and reg.role == "tx":
            self.emit(Transmit, channel=reg.channel or reg.name, register=reg.name, address=address, value=value, size=size)
        if kind == "write" or self.inputs.record_register_reads:
            self.emit(RegisterAccess, kind=kind, register=reg.name, address=address, value=value, size=size)

    # ------------------------------------------------------------------ storing
    def store(self, box: Box, value, vtype: CType) -> None:
        t = box.type
        v = self.convert(value, vtype, t)
        if box.bits and isinstance(t, IntType):
            mask = (1 << box.bits) - 1
            v &= mask
            if t.signed and v >> (box.bits - 1):
                v -= 1 << box.bits
        old = box.value
        box.value = v
        if box.union is not None:
            M.sync_union(box, self.endian)
        self.note_write(box, old)

    def store_aggregate(self, dst, src) -> None:
        if not isinstance(src, (ArrayObj, StructObj)):
            raise InterpError("cannot assign a plain value to a struct/array")
        before = [(b, b.value) for b in M.iter_boxes(dst)]
        M.copy_into(dst, src)
        for b, old in before:
            if b.value != old:
                self.note_write(b, old)

    def assign_input(self, slot, value) -> None:
        """Apply a user-provided value (text or Python value) to storage."""
        if isinstance(slot, Box):
            v = self.parse(value, slot.type) if isinstance(value, str) else value
            old = slot.value
            slot.value = self.convert(v, _type_of_py(v, self), slot.type)
            if slot.union is not None:
                M.sync_union(slot, self.endian)
            _ = old
            return
        if isinstance(slot, ArrayObj):
            items = parse_list(value, self.parse) if isinstance(value, str) else list(value)
            elem = slot.type.elem
            if isinstance(value, str) and _is_char(elem) and value.strip().startswith('"'):
                items = [ord(c) for c in _unquote(value.strip())] + [0]
            for target, v in zip(slot.items, items):
                self.assign_input(target, v)
            return
        if isinstance(slot, StructObj):
            parts = parse_struct(value) if isinstance(value, str) else dict(value)
            for k, v in parts.items():
                if k in slot.members:
                    self.assign_input(slot.members[k], v)
            return

    def parse(self, text, ctype: CType | None = None):
        """Parse user input, allowing names/expressions from the code (``FAULT_X``, ``BIT_A | BIT_B``)."""
        try:
            return parse_input_value(text, ctype)
        except ValueError:
            from source_tracer.frontend.consteval import evaluate

            names = dict(self.prog.macro_consts)
            names.update(self.prog.enum_consts)
            v = evaluate(str(text), names)
            if v is None:
                raise
            return float(v) if isinstance(ctype, FloatType) else v

    # ------------------------------------------------------------------ conversions
    def convert(self, v, src: CType | None, dst: CType):
        if isinstance(dst, IntType):
            if is_pointer_value(v):
                v = M.pointer_address(v)
            elif isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    v = 0
                v = int(v)
            elif v is None:
                v = 0
            elif isinstance(v, (ArrayObj, StructObj)):
                raise InterpError(f"cannot convert {v.type} to {dst}")
            return dst.wrap(int(v))
        if isinstance(dst, FloatType):
            if is_pointer_value(v):
                v = M.pointer_address(v)
            v = float(v or 0)
            if dst.size == 4:
                import struct

                try:
                    v = struct.unpack("f", struct.pack("f", v))[0]
                except OverflowError:
                    v = math.copysign(math.inf, v)
            return v
        if isinstance(dst, PointerType):
            if v is None or (isinstance(v, (int, float)) and not is_pointer_value(v)):
                n = int(v or 0)
                return 0 if n == 0 else MmioPtr(n, dst.target)
            if isinstance(v, ArrayObj):
                v = Ptr(v.items, 0, v.type.elem)
            return self.retarget(v, dst.target)
        if isinstance(dst, (StructType, ArrayType)):
            return v
        if isinstance(dst, VoidType):
            return None
        return v

    def retarget(self, p, target: CType):
        if isinstance(p, Ptr):
            if isinstance(target, (VoidType, FunctionType)):
                return Ptr(p.seq, p.index, target)
            slot = p.seq[p.index] if 0 <= p.index < len(p.seq) else None
            st = slot.type if slot is not None else p.elem
            if _compatible(target, st):
                return Ptr(p.seq, p.index, target)
            if isinstance(st, ArrayType) and _compatible(target, st.elem):
                return Ptr(slot.items, 0, target)
            return BytePtr(p.seq, p.index, 0, target)
        if isinstance(p, BytePtr):
            return BytePtr(p.seq, p.index, p.offset, target)
        if isinstance(p, MmioPtr):
            return MmioPtr(p.address, target)
        return p

    def truthy(self, v) -> bool:
        if is_pointer_value(v):
            return True
        if isinstance(v, (ArrayObj, StructObj)):
            return True
        return bool(v)

    # ------------------------------------------------------------------ declarations
    def decl_type(self, node: Node, base: CType) -> tuple[CType, str | None]:
        key = node.id
        hit = self._decl_cache.get(key)
        if hit is not None and hit[0] is base:
            return hit[1], hit[2]
        t, name = self.prog.apply_declarator(node, base, self.unit)
        name_s = node_text(name).rpartition("::")[2] if name is not None else None
        self._decl_cache[key] = (base, t, name_s)
        return t, name_s

    def base_type(self, node: Node | None) -> CType:
        if node is None:
            return self.INT
        key = ("base", node.id)
        hit = self._decl_cache.get(key)
        if hit is None:
            hit = self.prog.resolve_base(node, self.unit)
            self._decl_cache[key] = hit
        return hit

    def exec_declaration(self, node: Node) -> None:
        storage = [node_text(c) for c in node.children if c.type == "storage_class_specifier"]
        base = self.base_type(node.child_by_field_name("type"))
        scope = self.frame.scopes[-1]
        for d in node.children_by_field_name("declarator"):
            init, ctor_args, target = None, None, d
            if d.type == "init_declarator":
                v = d.child_by_field_name("value")
                if v is not None and v.type == "argument_list":
                    ctor_args = v
                else:
                    init = v
                target = d.child_by_field_name("declarator")
            t, name = self.decl_type(target, base)
            if name is None:
                continue
            if isinstance(t, FunctionType):
                continue  # local prototype
            if "extern" in storage:
                g = self._global_slot(name, self.unit)
                if g is not None:
                    scope[name] = g
                continue
            if isinstance(t, ArrayType) and t.length is None and init is not None:
                t = ArrayType(t.elem, _init_len(init))
            if "static" in storage:
                key = (target.id, name)
                obj = self.static_locals.get(key)
                if obj is None:
                    fname = self.frame.fn.name if self.frame.fn else ""
                    obj = make_object(t, f"{fname}.{name}" if fname else name)
                    for b in M.iter_boxes(obj):
                        self.global_box_ids.add(id(b))
                    self.static_locals[key] = obj
                    if init is not None:
                        self.initialize(obj, t, init, record=False)
                scope[name] = obj
                continue
            if isinstance(t, ReferenceType):
                if init is None:
                    raise InterpError(f"reference '{name}' must be initialised", d)
                lv = self.lvalue(init)
                scope[name] = lv.slot if isinstance(lv, SlotLV) else ValueLV(lv.get(), lv.type).box
                continue
            obj = make_object(t, name)
            scope[name] = obj
            if init is not None:
                self.initialize(obj, t, init, record=False)
            elif isinstance(t, StructType):
                self._construct(obj, t, ctor_args)

    def initialize(self, obj, t: CType, init: Node, record: bool = True) -> None:
        if init.type == "initializer_list":
            self._init_list(obj, t, init)
            return
        if init.type in ("string_literal", "concatenated_string") and isinstance(obj, ArrayObj):
            data = string_value(init) + [0]
            for box, b in zip(M.iter_boxes(obj), data):
                box.value = obj.type.elem.wrap(b) if isinstance(obj.type.elem, IntType) else b
            return
        v, vt = self.eval(init)
        if isinstance(obj, Box):
            if record:
                self.store(obj, v, vt)
            else:
                obj.value = self.convert(v, vt, obj.type)
                if obj.union is not None:
                    M.sync_union(obj, self.endian)
        else:
            if isinstance(v, (ArrayObj, StructObj)):
                M.copy_into(obj, v)
            else:
                raise InterpError(f"cannot initialise {t} from a {vt}", init)

    def _init_list(self, obj, t: CType, init: Node) -> None:
        items = [c for c in init.named_children if c.type != "comment"]
        if isinstance(obj, Box):  # `int x = {5};`
            if items:
                self.initialize(obj, t, items[0], record=False)
            return
        if isinstance(obj, ArrayObj):
            idx = 0
            for item in items:
                if item.type == "initializer_pair":
                    des = item.child_by_field_name("designator")
                    if des is not None and des.type == "subscript_designator":
                        idx = self.const_eval(des.named_children[0])
                    item = item.child_by_field_name("value")
                if idx >= len(obj.items):
                    break
                self.initialize(obj.items[idx], obj.type.elem, item, record=False)
                idx += 1
            return
        if isinstance(obj, StructObj):
            names = list(obj.members)
            pos = 0
            for item in items:
                if item.type == "initializer_pair":
                    des = item.child_by_field_name("designator")
                    fname = node_text(des.named_children[0]) if des is not None and des.named_children else None
                    if fname in obj.members:
                        pos = names.index(fname)
                    item = item.child_by_field_name("value")
                if pos >= len(names):
                    break
                member = obj.members[names[pos]]
                self.initialize(member, member.type, item, record=False)
                pos += 1
                if obj.type.is_union:
                    break

    def const_eval(self, node: Node) -> int:
        v, _ = self.eval(node)
        return int(v)

    def _construct(self, obj: StructObj, st: StructType, ctor_args: Node | None) -> None:
        ctors = st.find_methods(st.tag)
        if not ctors:
            if ctor_args is not None and ctor_args.named_children:
                self.warn(f"No constructor found for {st.tag}; arguments ignored.", ctor_args)
            return
        arg_nodes = ctor_args.named_children if ctor_args is not None else []
        fn = _overload(ctors, len(arg_nodes))
        args = self._eval_args(fn, arg_nodes)
        self.call(fn, args, Ptr(obj.seq, 0, st))

    # ------------------------------------------------------------------ name lookup
    def lookup(self, name: str, node: Node) -> LV | FunctionDef | None:
        for scope in reversed(self.frame.scopes):
            s = scope.get(name)
            if s is not None:
                return SlotLV(self, s.seq, 0, s.type)
        this = self.frame.this
        if this is not None:
            obj = self.deref(this, node)
            if isinstance(obj, SlotLV):
                o = obj.slot
                if isinstance(o, StructObj) and name in o.members:
                    m = o.members[name]
                    return SlotLV(self, m.seq, 0, m.type)
            if self.frame.fn and self.frame.fn.cls:
                ms = self.frame.fn.cls.find_methods(name)
                if ms:
                    return ms[0]
        s = self._global_slot(name, self.unit)
        if s is not None:
            return SlotLV(self, s.seq, 0, s.type)
        fn = self.prog.find_function(name, self.unit)
        if fn is not None:
            return fn
        return None

    def _implicit_global(self, name: str, node: Node) -> LV:
        self.warn(f"'{name}' is not declared anywhere in the project; treating it as a variable starting at 0.", node)
        box = Box(self.INT, 0, name)
        self.globals[name] = box
        self.global_box_ids.add(id(box))
        return SlotLV(self, box.seq, 0, self.INT)

    # ------------------------------------------------------------------ lvalues
    def lvalue(self, node: Node) -> LV:
        t = node.type
        if t == "identifier" or t == "qualified_identifier":
            name = node_text(node)
            short = name.rpartition("::")[2]
            r = self.lookup(short, node)
            if isinstance(r, LV):
                return r
            if r is None:
                if short in self.prog.enum_consts:
                    raise InterpError(f"cannot assign to constant '{short}'", node)
                return self._implicit_global(short, node)
            raise InterpError(f"'{short}' is a function, not a variable", node)
        if t == "parenthesized_expression":
            return self.lvalue(node.named_children[0])
        if t == "pointer_expression":
            op = node.children[0].type
            if op == "*":
                v, vt = self.eval(node.child_by_field_name("argument"))
                return self.deref(v, node, vt)
            raise InterpError("cannot assign to an address", node)
        if t == "subscript_expression":
            base, bt = self.eval(node.child_by_field_name("argument"))
            idx_node = node.child_by_field_name("indices") or node.child_by_field_name("index")
            inner = idx_node.named_children[0] if idx_node is not None and idx_node.type == "subscript_argument_list" else idx_node
            i, it = self.eval(inner)
            if not is_pointer_value(base) and is_pointer_value(i):  # i[arr]
                base, bt, i, it = i, it, base, bt
            return self.deref(self.ptr_add(base, int(i), node), node)
        if t == "field_expression":
            return self.field_lv(node)
        if t == "cast_expression":
            # (type)x as an lvalue (common old idiom); treat as the inner object
            return self.lvalue(node.child_by_field_name("value"))
        if t in ("assignment_expression",):
            self.eval(node)
            return self.lvalue(node.child_by_field_name("left"))
        if t == "update_expression" and node.children[0].type in ("++", "--"):
            self.eval(node)
            return self.lvalue(node.child_by_field_name("argument"))
        if t == "this":
            return ValueLV(self.frame.this, PointerType(self.frame.fn.cls if self.frame.fn and self.frame.fn.cls else VOID))
        if t == "conditional_expression":
            c, _ = self.eval(node.child_by_field_name("condition"))
            return self.lvalue(node.child_by_field_name("consequence" if self.truthy(c) else "alternative"))
        v, vt = self.eval(node)
        return ValueLV(v, vt)

    def deref(self, p, node: Node | None, ptype: CType | None = None) -> LV:
        if isinstance(p, Ptr):
            elem = p.elem
            if isinstance(elem, VoidType):
                slot = p.seq[p.index] if 0 <= p.index < len(p.seq) else None
                if slot is None:
                    raise InterpError("dereferencing a void pointer", node)
                elem = slot.type
            return SlotLV(self, p.seq, p.index, elem)
        if isinstance(p, BytePtr):
            return ByteLV(self, p.seq, p.index, p.offset, p.elem)
        if isinstance(p, MmioPtr):
            return MmioLV(self, p.address, p.elem)
        if isinstance(p, FuncPtr):
            raise InterpError("cannot read through a function pointer", node)
        if isinstance(p, ArrayObj):
            return SlotLV(self, p.items, 0, p.type.elem)
        if p is None or p == 0:
            raise InterpError("NULL pointer dereference", node)
        if isinstance(p, int):
            target = ptype.target if isinstance(ptype, PointerType) else self.INT
            return MmioLV(self, p, target)
        raise InterpError(f"cannot dereference {p!r}", node)

    def field_lv(self, node: Node) -> LV:
        arg = node.child_by_field_name("argument")
        fname = node_text(node.child_by_field_name("field")).rpartition("::")[2]
        op = node.children[1].type if len(node.children) > 1 else "."
        if op == "->":
            v, vt = self.eval(arg)
            base = self.deref(v, node, vt)
        else:
            base = self.lvalue(arg)
        return self.member(base, fname, node)

    def member(self, base: LV, fname: str, node: Node) -> LV:
        st = base.type
        if isinstance(st, ReferenceType):
            st = st.target
        if not isinstance(st, StructType):
            raise InterpError(f"'.{fname}' used on a value that is not a struct ({st})", node)
        ftype = st.field_type(fname)
        if ftype is None:
            raise InterpError(f"{st.tag} has no field '{fname}'", node)
        if isinstance(base, SlotLV):
            obj = base.slot
            if isinstance(obj, StructObj):
                m = obj.members.get(fname)
                if m is None:
                    raise InterpError(f"{st.tag} has no field '{fname}'", node)
                return SlotLV(self, m.seq, 0, ftype)
            raise InterpError(f"'{getattr(obj, 'name', '?')}' is not a struct", node)
        fl = st.field_layout(fname)
        off = fl.offset if fl is not None else 0
        if isinstance(base, MmioLV):
            if fl is not None and fl.bits is not None:
                return BitLV(MmioLV(self, base.addr + off, ftype), fl.shift, fl.bits, ftype)
            return MmioLV(self, base.addr + off, ftype)
        if isinstance(base, ByteLV):
            if fl is not None and fl.bits is not None:
                return BitLV(ByteLV(self, base.seq, base.index, base.offset + off, ftype), fl.shift, fl.bits, ftype)
            return ByteLV(self, base.seq, base.index, base.offset + off, ftype)
        if isinstance(base, ValueLV):
            obj = base.get()
            if isinstance(obj, StructObj):
                m = obj.members[fname]
                return SlotLV(self, m.seq, 0, ftype)
        raise InterpError(f"cannot access field '{fname}' here", node)

    # ------------------------------------------------------------------ pointer arithmetic
    def ptr_add(self, p, n: int, node: Node | None = None):
        if isinstance(p, (Ptr, BytePtr, MmioPtr)):
            return p.moved(n)
        if isinstance(p, ArrayObj):
            return Ptr(p.items, n, p.type.elem)
        if p == 0 or p is None:
            raise InterpError("arithmetic on a NULL pointer", node)
        raise InterpError(f"cannot index into {p!r}", node)

    def ptr_diff(self, a, b, node) -> int:
        if isinstance(a, Ptr) and isinstance(b, Ptr) and a.seq is b.seq:
            return a.index - b.index
        if isinstance(a, BytePtr) and isinstance(b, BytePtr) and a.seq is b.seq:
            return (a.offset - b.offset) // max(a.elem.size, 1)
        if isinstance(a, MmioPtr) and isinstance(b, MmioPtr):
            return (a.address - b.address) // max(a.elem.size, 1)
        return (M.pointer_address(a) - M.pointer_address(b)) // max(getattr(a, "elem", self.CHAR).size, 1)

    # ------------------------------------------------------------------ expressions
    def eval(self, node: Node):
        """Evaluate an expression; returns (value, CType)."""
        t = node.type
        h = getattr(self, "_e_" + t, None)
        if h is None:
            if t == "comment":
                return 0, self.INT
            self.warn(f"'{t.replace('_', ' ')}' is not supported yet; using 0.", node)
            return 0, self.INT
        return h(node)

    def rvalue(self, lv: LV):
        t = lv.type
        if isinstance(t, ReferenceType):
            t = t.target
        if isinstance(t, ArrayType):
            if isinstance(lv, SlotLV):
                arr = lv.slot
                return Ptr(arr.items, 0, t.elem), PointerType(t.elem, self.ptr_size)
            if isinstance(lv, MmioLV):
                return MmioPtr(lv.addr, t.elem), PointerType(t.elem, self.ptr_size)
            if isinstance(lv, ByteLV):
                return BytePtr(lv.seq, lv.index, lv.offset, t.elem), PointerType(t.elem, self.ptr_size)
        return lv.get(), t

    def _e_identifier(self, node: Node):
        name = node_text(node)
        r = self.lookup(name, node)
        if isinstance(r, LV):
            return self.rvalue(r)
        if isinstance(r, FunctionDef):
            return FuncPtr(r), PointerType(FunctionType(r.ret, [p for _, p in r.params]))
        if name in self.prog.enum_consts:
            return self.prog.enum_consts[name], self.INT
        if name in ("NULL", "nullptr"):
            return 0, PointerType(VOID)
        if name in self.prog.prototypes or name in BUILTINS:
            return FuncPtr(name), PointerType(FunctionType(self.INT, []))
        return self.rvalue(self._implicit_global(name, node))

    def _e_qualified_identifier(self, node: Node):
        name = node_text(node)
        short = name.rpartition("::")[2]
        if short in self.prog.enum_consts:
            return self.prog.enum_consts[short], self.INT
        fn = self.prog.find_function(name, self.unit)
        if fn is not None:
            return FuncPtr(fn), PointerType(FunctionType(fn.ret, [p for _, p in fn.params]))
        return self._e_identifier_name(short, node)

    def _e_identifier_name(self, name: str, node: Node):
        r = self.lookup(name, node)
        if isinstance(r, LV):
            return self.rvalue(r)
        return self.rvalue(self._implicit_global(name, node))

    def _e_number_literal(self, node: Node):
        v, is_float, longs, unsigned = number_value(node_text(node))
        if is_float:
            text = node_text(node).lower()
            return v, (FloatType("float", 4) if text.endswith("f") and not text.startswith("0x") else self.DOUBLE)
        candidates = ["int", "unsigned int", "long", "unsigned long", "long long", "unsigned long long"]
        text = node_text(node).lower()
        decimal = not (text.startswith("0x") or text.startswith("0b") or (len(text) > 1 and text.startswith("0")))
        for name in candidates:
            ct = self.ints[name]
            if longs == 1 and ct.rank < 4 or longs >= 2 and ct.rank < 5:
                continue
            if unsigned and ct.signed:
                continue
            if decimal and not unsigned and not ct.signed:
                continue
            if ct.min <= v <= ct.max:
                return v, ct
        return v, self.ints["unsigned long long"]

    def _e_char_literal(self, node: Node):
        return char_value(node), self.INT

    def _string_obj(self, node: Node) -> ArrayObj:
        obj = self.string_literals.get(node.id)
        if obj is None:
            data = string_value(node) + [0]
            arr_t = ArrayType(self.CHAR, len(data))
            obj = make_object(arr_t, f'"{_printable(data[:-1])}"')
            for b, v in zip(obj.items, data):
                b.value = self.CHAR.wrap(v)
            self.string_literals[node.id] = obj
        return obj

    def _e_string_literal(self, node: Node):
        obj = self._string_obj(node)
        return Ptr(obj.items, 0, self.CHAR), PointerType(self.CHAR, self.ptr_size)

    _e_concatenated_string = _e_string_literal
    _e_raw_string_literal = _e_string_literal

    def _e_true(self, node):
        return 1, self.ints["bool"]

    def _e_false(self, node):
        return 0, self.ints["bool"]

    def _e_null(self, node):
        return 0, PointerType(VOID)

    _e_nullptr = _e_null

    def _e_this(self, node):
        cls = self.frame.fn.cls if self.frame.fn else None
        return self.frame.this, PointerType(cls or VOID)

    def _e_parenthesized_expression(self, node: Node):
        inner = [c for c in node.named_children if c.type != "comment"]
        return self.eval(inner[-1]) if inner else (0, self.INT)

    def _e_comma_expression(self, node: Node):
        self.eval(node.child_by_field_name("left"))
        return self.eval(node.child_by_field_name("right"))

    def _e_conditional_expression(self, node: Node):
        c, _ = self.eval(node.child_by_field_name("condition"))
        if self.truthy(c):
            cons = node.child_by_field_name("consequence")
            return self.eval(cons) if cons is not None else (c, self.INT)
        return self.eval(node.child_by_field_name("alternative"))

    def _e_cast_expression(self, node: Node):
        t = self.type_descriptor(node.child_by_field_name("type"))
        v, vt = self.eval(node.child_by_field_name("value"))
        if isinstance(t, VoidType):
            return None, VOID
        return self.convert(v, vt, t), t

    def type_descriptor(self, node: Node) -> CType:
        key = ("td", node.id)
        hit = self._decl_cache.get(key)
        if hit is None:
            hit = self.prog.type_of_descriptor(node, self.unit)
            self._decl_cache[key] = hit
        return hit

    def _e_sizeof_expression(self, node: Node):
        tnode = node.child_by_field_name("type")
        if tnode is not None:
            t = self.type_descriptor(tnode)
        else:
            vnode = node.child_by_field_name("value")
            t = self.static_type(vnode)
        return max(t.size, 1) if not isinstance(t, VoidType) else 1, self.ints["unsigned long"]

    def static_type(self, node: Node) -> CType:
        """Type of an expression without side effects (for sizeof)."""
        while node.type == "parenthesized_expression" and node.named_children:
            node = node.named_children[0]
        if node.type == "identifier":
            name = node_text(node)
            # `sizeof(my_type_t)` parses like an expression; prefer a type of that name.
            if self.lookup(name, node) is None:
                t = self.prog.typedefs.get(name) or self.prog.structs.get(name)
                if t is not None:
                    return t
        try:
            if node.type in ("identifier", "field_expression", "subscript_expression", "pointer_expression"):
                saved = (self.steps, len(self.events))
                lv = self.lvalue(node)
                self.steps = saved[0]
                del self.events[saved[1]:]
                t = lv.type
                return t.target if isinstance(t, ReferenceType) else t
            if node.type in ("string_literal", "concatenated_string"):
                return self._string_obj(node).type
            _, t = self.eval(node)
            return t
        except InterpError:
            return self.INT

    _e_alignof_expression = _e_sizeof_expression

    def _e_offsetof_expression(self, node: Node):
        tnode = node.child_by_field_name("type")
        member = node.child_by_field_name("member")
        st = self.type_descriptor(tnode)
        if isinstance(st, StructType) and member is not None:
            return field_offset(st, node_text(member)), self.ints["unsigned long"]
        return 0, self.ints["unsigned long"]

    def _e_unary_expression(self, node: Node):
        op = node.children[0].type
        v, t = self.eval(node.child_by_field_name("argument"))
        if op == "!":
            return int(not self.truthy(v)), self.INT
        if isinstance(t, FloatType):
            return (-v if op == "-" else v), t
        if is_pointer_value(v):
            raise InterpError(f"'{op}' on a pointer", node)
        pt = self.promote(t)
        v = self.convert(v, t, pt)
        if op == "-":
            return pt.wrap(-v), pt
        if op == "~":
            return pt.wrap(~v), pt
        return v, pt

    def _e_pointer_expression(self, node: Node):
        op = node.children[0].type
        arg = node.child_by_field_name("argument")
        if op == "&":
            if arg.type in ("identifier", "qualified_identifier"):
                r = self.lookup(node_text(arg).rpartition("::")[2], arg)
                if isinstance(r, FunctionDef):
                    return FuncPtr(r), PointerType(FunctionType(r.ret, [p for _, p in r.params]))
            lv = self.lvalue(arg)
            t = lv.type.target if isinstance(lv.type, ReferenceType) else lv.type
            return lv.address(), PointerType(t, self.ptr_size)
        v, vt = self.eval(arg)
        if isinstance(v, FuncPtr):
            return v, vt
        return self.rvalue(self.deref(v, node, vt))

    def _e_update_expression(self, node: Node):
        prefix = node.children[0].type in ("++", "--")
        op = (node.children[0] if prefix else node.children[-1]).type
        lv = self.lvalue(node.child_by_field_name("argument"))
        old, t = self.rvalue(lv)
        if is_pointer_value(old):
            new = old.moved(1 if op == "++" else -1)
        elif isinstance(t, FloatType):
            new = old + (1 if op == "++" else -1)
        else:
            new = old + (1 if op == "++" else -1)
        lv.set(new, t)
        return (lv.get() if prefix else old), t

    def _e_assignment_expression(self, node: Node):
        op = node.child_by_field_name("operator")
        op = node_text(op) if op is not None else "="
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        lv = self.lvalue(left)
        if op == "=":
            if right.type == "initializer_list":
                obj = make_object(lv.type)
                self.initialize(obj, lv.type, right, record=False)
                lv.set(obj if not isinstance(obj, Box) else obj.value, lv.type)
                return lv.get(), lv.type
            v, vt = self.eval(right)
        else:
            cur, ct = self.rvalue(lv)
            rv, rt = self.eval(right)
            v, vt = self.binop(_ASSIGN_OPS[op], cur, ct, rv, rt, node)
        target_t = lv.type.target if isinstance(lv.type, ReferenceType) else lv.type
        if isinstance(v, (ArrayObj, StructObj)):
            lv.set(v, vt)
        else:
            lv.set(v, vt)
        if isinstance(target_t, (StructType, ArrayType)):
            return lv.get(), target_t
        return self.convert(v, vt, target_t) if target_t.is_scalar else v, target_t

    def _e_binary_expression(self, node: Node):
        op = node_text(node.child_by_field_name("operator"))
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if op == "&&":
            a, _ = self.eval(left)
            if not self.truthy(a):
                return 0, self.INT
            b, _ = self.eval(right)
            return int(self.truthy(b)), self.INT
        if op == "||":
            a, _ = self.eval(left)
            if self.truthy(a):
                return 1, self.INT
            b, _ = self.eval(right)
            return int(self.truthy(b)), self.INT
        a, at = self.eval(left)
        b, bt = self.eval(right)
        return self.binop(op, a, at, b, bt, node)

    # -- arithmetic core
    def promote(self, t: CType) -> CType:
        if isinstance(t, IntType) and t.rank < self.INT.rank:
            if t.size < self.INT.size or t.signed:
                return self.INT
            return self.UINT
        if isinstance(t, (PointerType, ArrayType)) or not isinstance(t, (IntType, FloatType)):
            return t if isinstance(t, (IntType, FloatType)) else self.INT
        return t

    def common_type(self, a: CType, b: CType) -> CType:
        if isinstance(a, FloatType) or isinstance(b, FloatType):
            fa = a.size if isinstance(a, FloatType) else 0
            fb = b.size if isinstance(b, FloatType) else 0
            return a if fa >= fb else b
        a, b = self.promote(a), self.promote(b)
        if not isinstance(a, IntType) or not isinstance(b, IntType):
            return self.INT
        if a == b:
            return a
        if a.signed == b.signed:
            return a if a.rank >= b.rank else b
        u, s = (a, b) if not a.signed else (b, a)
        if u.rank >= s.rank:
            return u
        if s.size > u.size:
            return s
        return self.ints["unsigned " + s.name] if ("unsigned " + s.name) in self.ints else u

    def binop(self, op: str, a, at: CType, b, bt: CType, node: Node | None):
        ap, bp = is_pointer_value(a) or isinstance(a, ArrayObj), is_pointer_value(b) or isinstance(b, ArrayObj)
        if ap or bp or isinstance(at, PointerType) or isinstance(bt, PointerType):
            return self._pointer_binop(op, a, at, b, bt, node)
        if isinstance(a, (StructObj,)) or isinstance(b, (StructObj,)):
            raise InterpError(f"operator {op} on a struct (operator overloading is not supported yet)", node)
        if op in ("<<", ">>"):
            lt = self.promote(at)
            av = self.convert(a, at, lt)
            n = int(self.convert(b, bt, self.promote(bt)))
            if n < 0:
                return 0, lt
            if op == "<<":
                return lt.wrap(av << n), lt
            return lt.wrap(av >> n), lt
        ct = self.common_type(at, bt)
        av, bv = self.convert(a, at, ct), self.convert(b, bt, ct)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            r = {"==": av == bv, "!=": av != bv, "<": av < bv, ">": av > bv, "<=": av <= bv, ">=": av >= bv}[op]
            return int(r), self.INT
        if isinstance(ct, FloatType):
            if op == "+":
                return av + bv, ct
            if op == "-":
                return av - bv, ct
            if op == "*":
                return av * bv, ct
            if op == "/":
                if bv == 0:
                    self.warn("Division by zero (floating point); result set to 0.", node)
                    return 0.0, ct
                return av / bv, ct
            raise InterpError(f"operator {op} on floating-point values", node)
        if op == "+":
            r = av + bv
        elif op == "-":
            r = av - bv
        elif op == "*":
            r = av * bv
        elif op in ("/", "%"):
            if bv == 0:
                self.warn("Division by zero; result set to 0.", node)
                return 0, ct
            q = abs(av) // abs(bv)
            if (av < 0) != (bv < 0):
                q = -q
            r = q if op == "/" else av - bv * q
        elif op == "&":
            r = av & bv
        elif op == "|":
            r = av | bv
        elif op == "^":
            r = av ^ bv
        else:
            raise InterpError(f"unknown operator {op}", node)
        return ct.wrap(r), ct

    def _pointer_binop(self, op, a, at, b, bt, node):
        if isinstance(a, ArrayObj):
            a = Ptr(a.items, 0, a.type.elem)
        if isinstance(b, ArrayObj):
            b = Ptr(b.items, 0, b.type.elem)
        a_ptr = is_pointer_value(a) or (isinstance(at, PointerType) and a in (0, None))
        b_ptr = is_pointer_value(b) or (isinstance(bt, PointerType) and b in (0, None))
        if op in ("==", "!="):
            eq = _ptr_eq(a, b)
            return int(eq if op == "==" else not eq), self.INT
        if op in ("<", ">", "<=", ">="):
            d = self.ptr_diff(a, b, node) if a_ptr and b_ptr else M.pointer_address(a) - M.pointer_address(b)
            r = {"<": d < 0, ">": d > 0, "<=": d <= 0, ">=": d >= 0}[op]
            return int(r), self.INT
        if op == "+" and a_ptr and not b_ptr:
            return self.ptr_add(a, int(b), node), at if isinstance(at, PointerType) else PointerType(getattr(a, "elem", self.CHAR))
        if op == "+" and b_ptr and not a_ptr:
            return self.ptr_add(b, int(a), node), bt if isinstance(bt, PointerType) else PointerType(getattr(b, "elem", self.CHAR))
        if op == "-" and a_ptr and not b_ptr:
            return self.ptr_add(a, -int(b), node), at if isinstance(at, PointerType) else PointerType(getattr(a, "elem", self.CHAR))
        if op == "-" and a_ptr and b_ptr:
            return self.ptr_diff(a, b, node), self.ints["long"]
        # Bitwise maths on pointers (alignment tricks): use fake addresses.
        av = M.pointer_address(a) if is_pointer_value(a) else int(a or 0)
        bv = M.pointer_address(b) if is_pointer_value(b) else int(b or 0)
        return self.binop(op, av, self.ints["unsigned long"], bv, self.ints["unsigned long"], node)

    def _e_subscript_expression(self, node: Node):
        return self.rvalue(self.lvalue(node))

    def _e_field_expression(self, node: Node):
        return self.rvalue(self.field_lv(node))

    def _e_initializer_list(self, node: Node):
        # Bare initializer list used as a value (e.g. passed to a function): make an int array.
        items = [c for c in node.named_children if c.type != "comment"]
        arr_t = ArrayType(self.INT, len(items))
        obj = make_object(arr_t)
        self._init_list(obj, arr_t, node)
        return obj, arr_t

    def _e_compound_literal_expression(self, node: Node):
        t = self.type_descriptor(node.child_by_field_name("type"))
        value = node.child_by_field_name("value")
        if isinstance(t, ArrayType) and t.length is None and value is not None:
            t = ArrayType(t.elem, _init_len(value))
        obj = make_object(t)
        if value is not None:
            self.initialize(obj, t, value, record=False)
        if isinstance(obj, Box):
            return obj.value, t
        return obj, t

    def _e_new_expression(self, node: Node):
        tnode = node.child_by_field_name("type")
        t = self.base_type(tnode)
        decl = node.child_by_field_name("declarator")
        if decl is not None and decl.type == "new_declarator":
            length_node = decl.named_children[0] if decl.named_children else None
            n = self.const_eval(length_node) if length_node is not None else 1
            arr = make_object(ArrayType(t, n), "new[]")
            return Ptr(arr.items, 0, t), PointerType(t, self.ptr_size)
        obj = make_object(t, f"new {t}")
        args = node.child_by_field_name("arguments")
        if isinstance(t, StructType):
            self._construct(obj, t, args)
        elif args is not None and args.named_children:
            v, vt = self.eval(args.named_children[0])
            obj.value = self.convert(v, vt, t)
        return Ptr(obj.seq, 0, t), PointerType(t, self.ptr_size)

    def _e_delete_expression(self, node: Node):
        return None, VOID

    def _e_gnu_asm_expression(self, node: Node):
        self.warn("Inline assembly is skipped.", node)
        return 0, self.INT

    def _e_lambda_expression(self, node: Node):
        self.warn("Lambdas are not supported yet; using 0.", node)
        return 0, self.INT

    def _e_template_function(self, node: Node):
        return self._e_identifier_name(node_text(node.child_by_field_name("name")), node)

    # ------------------------------------------------------------------ calls
    def _e_call_expression(self, node: Node):
        fnode = node.child_by_field_name("function")
        anode = node.child_by_field_name("arguments")
        arg_nodes = [a for a in (anode.named_children if anode is not None else []) if a.type != "comment"]
        ft = fnode.type
        while ft == "parenthesized_expression" and fnode.named_children:
            fnode = fnode.named_children[0]
            ft = fnode.type

        if ft == "template_function":
            name = node_text(fnode.child_by_field_name("name"))
            if name in _CASTS:
                targs = fnode.child_by_field_name("arguments")
                td = next((c for c in targs.named_children if c.type == "type_descriptor"), None) if targs is not None else None
                v, vt = self.eval(arg_nodes[0]) if arg_nodes else (0, self.INT)
                if td is None:
                    return v, vt
                t = self.type_descriptor(td)
                return self.convert(v, vt, t), t
            return self.call_named(name, arg_nodes, node)

        if ft in ("primitive_type", "type_identifier", "sized_type_specifier"):
            # functional cast: int(x), uint8_t(x)
            t = self.base_type(fnode)
            v, vt = self.eval(arg_nodes[0]) if arg_nodes else (0, self.INT)
            return self.convert(v, vt, t), t

        if ft == "field_expression":
            return self.call_member(fnode, arg_nodes, node)

        if ft in ("identifier", "qualified_identifier"):
            name = node_text(fnode)
            short = name.rpartition("::")[2]
            # local/global function pointer variable?
            r = self.lookup(short, fnode) if "::" not in name else None
            if isinstance(r, LV):
                v, _ = self.rvalue(r)
                if isinstance(v, FuncPtr):
                    return self.call_pointer(v, arg_nodes, node)
                if isinstance(r.type, StructType):
                    self.warn("Calling an object (operator()) is not supported; using 0.", node)
                    return 0, self.INT
            if isinstance(r, FunctionDef) and r.cls is not None and self.frame.this is not None:
                fn = _overload(r.cls.find_methods(short), len(arg_nodes))
                return self.call(fn, self._eval_args(fn, arg_nodes), self.frame.this, node)
            if "::" in name:
                fn = self.prog.find_function(name, self.unit, len(arg_nodes))
                if fn is not None:
                    this = self.frame.this if fn.cls is not None and self.frame.fn and self.frame.fn.cls is fn.cls else None
                    return self.call(fn, self._eval_args(fn, arg_nodes), this, node)
                st = self.prog.structs.get(name.rpartition("::")[0].rpartition("::")[2])
                if st is not None:
                    ms = st.find_methods(short)
                    if ms:
                        fn = _overload(ms, len(arg_nodes))
                        return self.call(fn, self._eval_args(fn, arg_nodes), self.frame.this, node)
            if short in self.prog.structs:  # temporary object: Foo(1, 2)
                st = self.prog.structs[short]
                obj = make_object(st)
                self._construct(obj, st, node.child_by_field_name("arguments"))
                return obj, st
            return self.call_named(name, arg_nodes, node)

        v, _ = self.eval(fnode)
        if isinstance(v, FuncPtr):
            return self.call_pointer(v, arg_nodes, node)
        raise InterpError("cannot call this expression", node)

    def call_named(self, name: str, arg_nodes: list[Node], node: Node):
        short = name.rpartition("::")[2]
        fn = self.prog.find_function(name, self.unit, len(arg_nodes))
        if fn is not None:
            return self.call(fn, self._eval_args(fn, arg_nodes), None, node)
        args = [self.eval(a) for a in arg_nodes]
        if short in self.inputs.stubs:
            return self.call_stub(short, args, node)
        b = BUILTINS.get(short)
        if b is not None:
            self.cur_node = node
            return b(self, args, node)
        return self.call_stub(short, args, node)

    def call_pointer(self, fp: FuncPtr, arg_nodes: list[Node], node: Node):
        target = fp.func
        if isinstance(target, FunctionDef):
            return self.call(target, self._eval_args(target, arg_nodes), None, node)
        return self.call_named(str(target), arg_nodes, node)

    def call_member(self, fnode: Node, arg_nodes: list[Node], node: Node):
        arg = fnode.child_by_field_name("argument")
        mname = node_text(fnode.child_by_field_name("field")).rpartition("::")[2]
        op = fnode.children[1].type if len(fnode.children) > 1 else "."
        obj_label = node_text(arg)
        try:
            if op == "->":
                v, vt = self.eval(arg)
                base = self.deref(v, node, vt)
            else:
                if arg.type == "identifier" and self.lookup(node_text(arg), arg) is None:
                    # Unknown object such as an Arduino `Serial`: treat the method as a stub.
                    args = [self.eval(a) for a in arg_nodes]
                    return self.call_stub(f"{obj_label}.{mname}", args, node)
                base = self.lvalue(arg)
        except InterpError:
            raise
        st = base.type.target if isinstance(base.type, ReferenceType) else base.type
        if isinstance(st, StructType):
            ms = st.find_methods(mname)
            if isinstance(base, SlotLV) and isinstance(base.slot, StructObj):
                dyn = base.slot.type  # virtual dispatch: most-derived type of the object
                ms = dyn.find_methods(mname) or ms
            if ms:
                fn = _overload(ms, len(arg_nodes))
                this = base.address()
                return self.call(fn, self._eval_args(fn, arg_nodes), this, node)
            ftype = st.field_type(mname)
            if ftype is not None:
                fp, _ = self.rvalue(self.member(base, mname, node))
                if isinstance(fp, FuncPtr):
                    return self.call_pointer(fp, arg_nodes, node)
        args = [self.eval(a) for a in arg_nodes]
        return self.call_stub(f"{obj_label}{op}{mname}", args, node)

    def _eval_args(self, fn: FunctionDef, arg_nodes: list[Node]) -> list:
        out = []
        for i, a in enumerate(arg_nodes):
            ptype = fn.params[i][1] if i < len(fn.params) else None
            if isinstance(ptype, ReferenceType):
                out.append(("ref", self.lvalue(a)))
            else:
                out.append(self.eval(a))
        return out

    def _input_args(self, fn: FunctionDef, raw: list) -> list:
        """Turn user inputs into argument values (allocating buffers for pointer parameters)."""
        out = []
        self.arg_buffers: dict[str, M.Slot] = {}
        for i, (pname, ptype) in enumerate(fn.params):
            value = raw[i] if i < len(raw) else ""
            if isinstance(ptype, ReferenceType):
                obj = make_object(ptype.target, pname)
                if value not in ("", None):
                    self.assign_input(obj, value)
                self.arg_buffers[pname] = obj
                out.append(("ref", SlotLV(self, obj.seq, 0, ptype.target)))
            elif isinstance(ptype, PointerType) and not isinstance(ptype.target, (VoidType, FunctionType)):
                items = parse_list(value, self.parse) if isinstance(value, str) and value.strip() else (value if isinstance(value, list) else [])
                if isinstance(value, str) and value.strip().upper() == "NULL":
                    out.append((0, ptype))
                    continue
                if isinstance(ptype.target, StructType):
                    obj = make_object(ptype.target, f"*{pname}")
                    if isinstance(value, str) and value.strip():
                        self.assign_input(obj, value)
                    self.arg_buffers[pname] = obj
                    out.append((Ptr(obj.seq, 0, ptype.target), ptype))
                    continue
                if isinstance(value, str) and _is_char(ptype.target) and value.strip().startswith('"'):
                    items = [ord(c) for c in _unquote(value.strip())] + [0]
                n = max(len(items), 64)
                arr = make_object(ArrayType(ptype.target, n), pname)
                for box, v in zip(arr.items, items):
                    self.assign_input(box, v)
                self.arg_buffers[pname] = arr
                out.append((Ptr(arr.items, 0, ptype.target), ptype))
            elif isinstance(ptype, (StructType, ArrayType)):
                obj = make_object(ptype, pname)
                if isinstance(value, str) and value.strip():
                    self.assign_input(obj, value)
                out.append((obj, ptype))
            else:
                if value in ("", None):
                    v = 0
                else:
                    v = self.parse(value, ptype) if isinstance(value, str) else value
                out.append((v, _type_of_py(v, self) if not isinstance(ptype, IntType | FloatType) else ptype))
        return out

    def call(self, fn: FunctionDef, args: list, this=None, node: Node | None = None):
        body = fn.body
        if body is None:
            return self.call_stub(fn.name, [a if a[0] != "ref" else self.rvalue(a[1]) for a in args], node)
        self.depth += 1
        if self.depth > self.inputs.max_call_depth:
            self.depth -= 1
            raise InterpError(f"calls nested more than {self.inputs.max_call_depth} deep (runaway recursion?)", node)
        frame = Frame(fn, fn.unit, this)
        scope = frame.scopes[0]
        shown = []
        for i, (pname, ptype) in enumerate(fn.params):
            if i >= len(args):
                obj = make_object(ptype, pname)
                scope[pname] = obj
                shown.append("?")
                continue
            a = args[i]
            if isinstance(ptype, ReferenceType):
                lv = a[1] if a[0] == "ref" else ValueLV(a[0], a[1])
                if isinstance(lv, SlotLV):
                    scope[pname] = lv.slot
                elif isinstance(lv, ValueLV):
                    scope[pname] = lv.box
                else:
                    tmp = Box(ptype.target, lv.get(), pname)
                    scope[pname] = tmp
                shown.append(self.fmt(*self.rvalue(SlotLV(self, scope[pname].seq, 0, ptype.target))))
                continue
            v, vt = (self.rvalue(a[1]) if a[0] == "ref" else a)
            obj = make_object(ptype, pname)
            if isinstance(obj, Box):
                obj.value = self.convert(v, vt, ptype)
            elif isinstance(v, (ArrayObj, StructObj)):
                M.copy_into(obj, v)
            scope[pname] = obj
            shown.append(self.fmt(obj.value if isinstance(obj, Box) else obj, ptype))
        frame.varargs = [a if a[0] != "ref" else self.rvalue(a[1]) for a in args[len(fn.params):]]
        caller_node = self.cur_node
        if node is not None:
            self.cur_node = node
        raw = [scope[p].value if isinstance(scope.get(p), Box) else None for p, _ in fn.params]
        self.emit(CallEvent, callee=fn.qualname, args=shown, kind="function", raw=raw)
        prev_frame, prev_unit = self.frame, self.unit
        self.frame, self.unit = frame, fn.unit
        ret_v, ret_t = None, VOID
        try:
            if this is not None and fn.is_ctor:
                self._ctor_inits(fn, this)
            self.exec(body)
        except _Return as r:
            ret_t = fn.ret
            if isinstance(ret_t, ReferenceType):
                ret_v, ret_t = r.value, r.ctype
            elif isinstance(ret_t, VoidType):
                ret_v = None
            elif isinstance(r.value, (ArrayObj, StructObj)):
                ret_v = M.clone(r.value)
            else:
                ret_v = self.convert(r.value, r.ctype, ret_t)
        finally:
            self.frame, self.unit = prev_frame, prev_unit
            self.depth -= 1
        self.cur_node = node if node is not None else caller_node
        if not isinstance(fn.ret, VoidType):
            self.emit(ReturnEvent, callee=fn.qualname, value=self.fmt(ret_v, ret_t))
        self.cur_node = caller_node
        return ret_v, (fn.ret if not isinstance(fn.ret, ReferenceType) else ret_t)

    def _ctor_inits(self, fn: FunctionDef, this) -> None:
        for c in fn.node.children:
            if c.type != "field_initializer_list":
                continue
            obj_lv = self.deref(this, c)
            for fi in c.named_children:
                if fi.type != "field_initializer":
                    continue
                name_node = fi.named_children[0]
                args = fi.named_children[1] if len(fi.named_children) > 1 else None
                fname = node_text(name_node)
                vals = [a for a in (args.named_children if args is not None else []) if a.type != "comment"]
                try:
                    m = self.member(obj_lv, fname, fi)
                except InterpError:
                    continue  # base-class initialiser etc.
                if isinstance(m.type, StructType) and isinstance(m, SlotLV):
                    self._construct(m.slot, m.type, args)
                elif vals:
                    v, vt = self.eval(vals[0])
                    m.set(v, vt)

    def call_stub(self, name: str, args: list, node: Node | None):
        proto: Prototype | None = self.prog.prototypes.get(name)
        ret_t = proto.ret if proto is not None else self.INT
        seq = self.inputs.stubs.get(name)
        n = self.stub_calls[name]
        self.stub_calls[name] += 1
        if seq:
            raw = seq[min(n, len(seq) - 1)]
            value = self.parse(raw, ret_t) if isinstance(raw, str) else raw
        else:
            value = 0.0 if isinstance(ret_t, FloatType) else 0
        if isinstance(ret_t, VoidType):
            value = None
        else:
            value = self.convert(value, _type_of_py(value, self), ret_t)
        saved = self.cur_node
        if node is not None:
            self.cur_node = node
        self.emit(CallEvent, callee=name, args=[self.fmt(v, t) for v, t in args], kind="stub", raw=[v for v, _ in args])
        if not isinstance(ret_t, VoidType):
            self.emit(ReturnEvent, callee=name, value=self.fmt(value, ret_t))
        self.cur_node = saved
        return value, ret_t

    # ------------------------------------------------------------------ statements
    def tick(self, node: Node) -> None:
        self.steps += 1
        if self.steps > self.inputs.max_steps:
            raise StepLimit()
        self.cur_node = node
        self.coverage[(self.unit.index, node.start_point[0])] += 1

    def exec(self, node: Node) -> None:
        t = node.type
        if t == "compound_statement":
            self.frame.scopes.append({})
            try:
                for c in node.named_children:
                    self.exec(c)
            finally:
                self.frame.scopes.pop()
            return
        if t in ("comment", "preproc_call", "preproc_include", "preproc_def", "preproc_function_def", "empty_statement"):
            return
        self.tick(node)
        if t == "expression_statement":
            for c in node.named_children:
                if c.type != "comment":
                    self.eval(c)
            return
        h = getattr(self, "_s_" + t, None)
        if h is None:
            if t in ("type_definition", "struct_specifier", "enum_specifier", "union_specifier", "class_specifier", "alias_declaration", "using_declaration", "static_assert_declaration"):
                if t == "enum_specifier" or t == "type_definition":
                    _local_types(self.prog, node, self.unit)
                return
            if t == "ERROR":
                self.warn("Code here could not be understood and was skipped.", node)
                return
            self.warn(f"'{t.replace('_', ' ')}' statements are not supported yet; skipped.", node)
            return
        h(node)

    def _s_declaration(self, node: Node) -> None:
        self.exec_declaration(node)

    def _s_return_statement(self, node: Node) -> None:
        vals = [c for c in node.named_children if c.type != "comment"]
        if not vals:
            raise _Return(None, VOID)
        fn = self.frame.fn
        if fn is not None and isinstance(fn.ret, ReferenceType):
            lv = self.lvalue(vals[0])
            raise _Return(lv.get(), lv.type)
        if vals[0].type == "initializer_list" and fn is not None:
            obj = make_object(fn.ret)
            self.initialize(obj, fn.ret, vals[0], record=False)
            raise _Return(obj.value if isinstance(obj, Box) else obj, fn.ret)
        v, vt = self.eval(vals[0])
        raise _Return(v, vt)

    def _cond(self, node: Node | None) -> bool:
        if node is None:
            return True
        if node.type == "condition_clause":
            decl = node.child_by_field_name("initializer")
            if decl is not None:
                self.exec(decl)
            value = node.child_by_field_name("value")
            if value is None:
                return True
            if value.type == "declaration":
                self.exec_declaration(value)
                d = value.child_by_field_name("declarator")
                _, name = self.decl_type(d.child_by_field_name("declarator") if d.type == "init_declarator" else d, self.base_type(value.child_by_field_name("type")))
                v, _ = self.rvalue(self.lookup(name, value))
                return self.truthy(v)
            node = value
        v, _ = self.eval(node)
        return self.truthy(v)

    def _s_if_statement(self, node: Node) -> None:
        if self._cond(node.child_by_field_name("condition")):
            self.exec(node.child_by_field_name("consequence"))
        else:
            alt = node.child_by_field_name("alternative")
            if alt is not None:
                if alt.type == "else_clause":
                    inner = [c for c in alt.named_children if c.type != "comment"]
                    if inner:
                        self.exec(inner[0])
                else:
                    self.exec(alt)

    def _s_while_statement(self, node: Node) -> None:
        cond = node.child_by_field_name("condition")
        body = node.child_by_field_name("body")
        first = True
        while True:
            if not first:
                self.tick(node)  # each re-test of the condition costs a step
            first = False
            if not self._cond(cond):
                break
            try:
                self.exec(body)
            except _Break:
                break
            except _Continue:
                continue

    def _s_do_statement(self, node: Node) -> None:
        cond = node.child_by_field_name("condition")
        body = node.child_by_field_name("body")
        while True:
            try:
                self.exec(body)
            except _Break:
                break
            except _Continue:
                pass
            self.tick(node)
            if not self._cond(cond):
                break

    def _s_for_statement(self, node: Node) -> None:
        self.frame.scopes.append({})
        try:
            init = node.child_by_field_name("initializer")
            if init is not None:
                if init.type == "declaration":
                    self.exec_declaration(init)
                else:
                    self.eval(init)
            cond = node.child_by_field_name("condition")
            update = node.child_by_field_name("update")
            body = node.child_by_field_name("body")
            first = True
            while True:
                if not first:
                    self.tick(node)
                first = False
                if cond is not None and not self._cond(cond):
                    break
                try:
                    self.exec(body)
                except _Break:
                    break
                except _Continue:
                    pass
                if update is not None:
                    self.eval(update)
        finally:
            self.frame.scopes.pop()

    def _s_for_range_loop(self, node: Node) -> None:
        right = node.child_by_field_name("right")
        lv = self.lvalue(right)
        container = lv.slot if isinstance(lv, SlotLV) else None
        if not isinstance(container, ArrayObj):
            self.warn("Range-for is only supported over arrays.", node)
            return
        decl = node.child_by_field_name("declarator")
        base = self.base_type(node.child_by_field_name("type"))
        t, name = self.decl_type(decl, base)
        is_ref = isinstance(t, ReferenceType)
        body = node.child_by_field_name("body")
        for item in container.items:
            self.frame.scopes.append({})
            try:
                if is_ref:
                    self.frame.scopes[-1][name] = item
                else:
                    self.frame.scopes[-1][name] = M.clone(item, name) if not isinstance(item, Box) else Box(item.type, item.value, name)
                self.exec(body)
            except _Break:
                break
            except _Continue:
                pass
            finally:
                self.frame.scopes.pop()
            self.tick(node)

    def _s_switch_statement(self, node: Node) -> None:
        cond = node.child_by_field_name("condition")
        value_node = cond.child_by_field_name("value") if cond.type == "condition_clause" else cond
        v, _ = self.eval(value_node)
        body = node.child_by_field_name("body")
        cases = [c for c in body.named_children if c.type == "case_statement"]
        start = None
        default = None
        for i, c in enumerate(cases):
            cv = c.child_by_field_name("value")
            if cv is None:
                default = i
                continue
            if self.const_eval(cv) == v:
                start = i
                break
        if start is None:
            start = default
        if start is None:
            return
        try:
            for c in cases[start:]:
                cv = c.child_by_field_name("value")
                for stmt in c.named_children:
                    if stmt == cv or stmt.type == "comment":
                        continue
                    self.exec(stmt)
        except _Break:
            pass

    def _s_case_statement(self, node: Node) -> None:
        cv = node.child_by_field_name("value")
        for stmt in node.named_children:
            if stmt != cv:
                self.exec(stmt)

    def _s_break_statement(self, node: Node) -> None:
        raise _Break()

    def _s_continue_statement(self, node: Node) -> None:
        raise _Continue()

    def _s_labeled_statement(self, node: Node) -> None:
        for c in node.named_children:
            if c.type != "statement_identifier":
                self.exec(c)

    def _s_goto_statement(self, node: Node) -> None:
        raise InterpError(f"'goto {node_text(node.child_by_field_name('label'))}' is not supported yet", node)

    def _s_compound_statement(self, node: Node) -> None:  # pragma: no cover - handled in exec
        self.exec(node)

    def _s_try_statement(self, node: Node) -> None:
        body = node.child_by_field_name("body")
        if body is not None:
            self.exec(body)

    def _s_throw_statement(self, node: Node) -> None:
        raise InterpError("C++ exceptions are not supported (throw)", node)

    def _s_function_definition(self, node: Node) -> None:
        return  # nested function definitions (GNU extension) are ignored

    # ------------------------------------------------------------------ formatting
    def fmt(self, v, t: CType | None) -> str:
        if isinstance(v, (ArrayObj, StructObj, Box)):
            return self.fmt_slot(v)
        if v is None:
            return "" if isinstance(t, VoidType) else "NULL"
        if is_pointer_value(v):
            return M.describe_pointer(v)
        if isinstance(t, PointerType):
            return "NULL" if v == 0 else f"0x{int(v):X}"
        if isinstance(v, float):
            return f"{v:.6g}"
        if isinstance(v, int):
            if isinstance(t, IntType) and t.rank == 0:
                return "true" if v else "false"
            if isinstance(t, EnumType):
                name = t.label(v)
                if name is not None:
                    return f"{name} ({v if -9 <= v <= 9 else hex(v)})"
            if v > 9 or v < -9:
                hexv = v & ((1 << (8 * t.size)) - 1) if isinstance(t, IntType) and v < 0 else v
                return f"{v} (0x{hexv:X})"
            return str(v)
        return str(v)

    def fmt_slot(self, slot, limit: int = 24) -> str:
        if isinstance(slot, Box):
            return self.fmt(slot.value, slot.type)
        if isinstance(slot, ArrayObj):
            elem = slot.type.elem
            vals = [b.value if isinstance(b, Box) else None for b in slot.items]
            if _is_char(elem) and vals and all(isinstance(x, int) for x in vals) and 0 in vals:
                end = vals.index(0)
                text = vals[:end]
                if text and all(32 <= (c & 0xFF) < 127 for c in text):
                    return f'"{_printable(text)}"'
            if isinstance(elem, IntType):
                shown = [f"0x{(x & ((1 << (8 * elem.size)) - 1)):0{elem.size * 2}X}" if elem.size == 1 or not elem.signed else str(x) for x in vals[:limit]]
                if elem.size == 1:
                    shown = [f"{x & 0xFF:02X}" for x in vals[:limit]]
                    more = " …" if len(vals) > limit else ""
                    return f"[{' '.join(shown)}{more}]"
            else:
                shown = [self.fmt_slot(b, 4) for b in slot.items[:limit]]
            more = ", …" if len(slot.items) > limit else ""
            return "[" + ", ".join(shown) + more + "]"
        if isinstance(slot, StructObj):
            parts = [f"{k}={self.fmt_slot(m, 8)}" for k, m in list(slot.members.items())[:12]]
            return "{" + ", ".join(parts) + (", …" if len(slot.members) > 12 else "") + "}"
        return str(slot)


# --------------------------------------------------------------------------- helpers


def _compatible(a: CType, b: CType) -> bool:
    if a == b:
        return True
    if isinstance(a, IntType) and isinstance(b, IntType):
        return a.size == b.size
    if isinstance(a, PointerType) and isinstance(b, PointerType):
        return True
    if isinstance(a, StructType) and isinstance(b, StructType):
        return a.tag == b.tag or b in a.bases or a in b.bases
    if isinstance(a, FloatType) and isinstance(b, FloatType):
        return a.size == b.size
    return False


def _ptr_eq(a, b) -> bool:
    if a in (0, None) or b in (0, None):
        return (a in (0, None)) and (b in (0, None))
    if type(a) is type(b):
        return a == b
    return M.pointer_address(a) == M.pointer_address(b)


def _overload(cands: list[FunctionDef], nargs: int) -> FunctionDef:
    for f in cands:
        if len(f.params) == nargs or (f.variadic and nargs >= len(f.params)):
            return f
    return cands[0]


def _init_len(init: Node) -> int | None:
    if init.type == "initializer_list":
        n = 0
        idx = 0
        for item in init.named_children:
            if item.type == "comment":
                continue
            if item.type == "initializer_pair":
                des = item.child_by_field_name("designator")
                if des is not None and des.type == "subscript_designator":
                    from source_tracer.frontend.consteval import evaluate

                    v = evaluate(node_text(des.named_children[0]))
                    if v is not None:
                        idx = v
            idx += 1
            n = max(n, idx)
        return n
    if init.type in ("string_literal", "concatenated_string"):
        return len(string_value(init)) + 1
    return None


def field_offset(st: StructType, fname: str) -> int:
    """Byte offset of a field in the target layout."""
    f = st.field_layout(fname)
    if f is None:
        raise InterpError(f"{st.tag} has no field '{fname}'")
    return f.offset


class BitLV(LV):
    """A bit-field inside a raw-memory unit (hardware register bit definitions)."""

    def __init__(self, unit: LV, shift: int, bits: int, ctype: CType):
        self.unit, self.shift, self.bits, self.type = unit, shift, bits, ctype

    def get(self):
        v = (int(self.unit.get()) >> self.shift) & ((1 << self.bits) - 1)
        if isinstance(self.type, IntType) and self.type.signed and v >> (self.bits - 1):
            v -= 1 << self.bits
        return v

    def set(self, value, vtype):
        mask = ((1 << self.bits) - 1) << self.shift
        cur = int(self.unit.get())
        new = (cur & ~mask) | ((int(value) << self.shift) & mask)
        self.unit.set(new, self.unit.type)

    def address(self):
        raise InterpError("cannot take the address of a bit-field")


def _local_types(prog: Program, node: Node, unit: Unit) -> None:
    from source_tracer.core.program import _typedef

    if node.type == "type_definition":
        _typedef(prog, node, unit)
    elif node.type == "enum_specifier":
        prog.define_enum(node, unit)


def _is_char(t: CType) -> bool:
    return isinstance(t, IntType) and t.size == 1 and t.rank == 1


def _printable(vals: list[int]) -> str:
    out = []
    for c in vals:
        c &= 0xFF
        if c == 10:
            out.append("\\n")
        elif c == 13:
            out.append("\\r")
        elif 32 <= c < 127:
            out.append(chr(c))
        else:
            out.append(f"\\x{c:02X}")
    return "".join(out)


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return bytes(s[1:-1], "utf-8").decode("unicode_escape")
    return s


def _type_of_py(v, it: Interpreter) -> CType:
    if isinstance(v, float):
        return it.DOUBLE
    if is_pointer_value(v):
        return PointerType(getattr(v, "elem", VOID))
    if isinstance(v, int) and not (it.ints["long long"].min <= v <= it.ints["long long"].max):
        return it.ints["unsigned long long"]
    return it.ints["long long"]


# --------------------------------------------------------------------------- input parsing


def parse_input_value(text, ctype: CType | None = None):
    """Parse a user-entered scalar: 42, -3, 0x1F, 0b101, 1.5, 'A', true/false."""
    if not isinstance(text, str):
        return text
    s = text.strip().replace("_", "")
    if not s:
        return 0
    low = s.lower()
    if low in ("true", "on", "yes"):
        return 1
    if low in ("false", "off", "no", "null"):
        return 0
    if len(s) >= 3 and s[0] == s[-1] == "'":
        return ord(_unquote(s)[0])
    neg = s.startswith("-")
    body = s[1:] if neg or s.startswith("+") else s
    try:
        if body.lower().startswith("0x"):
            v = int(body, 16)
        elif body.lower().startswith("0b"):
            v = int(body, 2)
        elif re.fullmatch(r"\d+", body):
            v = int(body)
        else:
            f = float(body)
            return -f if neg else f
    except ValueError:
        raise ValueError(f"'{text}' is not a number") from None
    v = -v if neg else v
    if isinstance(ctype, FloatType):
        return float(v)
    return v


def parse_list(text: str, parse=None) -> list:
    """Parse ``1, 2, 0x3`` / ``[1 2 3]`` / ``"text"`` into a list of values."""
    s = text.strip()
    if not s:
        return []
    if s[0] == '"' and s[-1] == '"':
        return [ord(c) for c in _unquote(s)] + [0]
    s = s.strip("[]{}")
    parts = [p for p in re.split(r"[,\s;]+", s) if p]
    return [(parse or parse_input_value)(p) for p in parts]


def parse_struct(text: str) -> dict:
    """Parse ``x=1, y=0x20`` (or ``{.x = 1, .y = 2}``) into a dict."""
    s = text.strip().strip("{}")
    out = {}
    for part in re.split(r"[,;]", s):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lstrip(".")] = v.strip()
    return out


# --------------------------------------------------------------------------- built-in library


def _bytes_at(it: Interpreter, p, n: int) -> bytes:
    try:
        return _bytes_at_raw(it, p, n)
    except IndexError:
        raise InterpError(f"reading {n} bytes runs past the end of '{_ptr_name(p)}'") from None


def _ptr_name(p) -> str:
    seq = getattr(p, "seq", None)
    return _base_name(seq) if seq else "memory"


def _bytes_at_raw(it: Interpreter, p, n: int) -> bytes:
    if isinstance(p, MmioPtr):
        return bytes(it.bus.read(p.address + i, 1) for i in range(n))
    if isinstance(p, BytePtr):
        return M.read_region(p.seq, p.index, p.offset, n, it.endian)
    if isinstance(p, Ptr):
        if all(isinstance(s, Box) and max(s.type.size, 1) == 1 for s in p.seq[p.index : p.index + n]) and p.index + n <= len(p.seq):
            return bytes(int(s.value) & 0xFF for s in p.seq[p.index : p.index + n])
        return M.read_region(p.seq, p.index, 0, n, it.endian)
    raise InterpError("NULL pointer passed to a memory function")


def _write_bytes(it: Interpreter, p, data: bytes) -> None:
    try:
        _write_bytes_raw(it, p, data)
    except IndexError:
        raise InterpError(f"writing {len(data)} bytes runs past the end of '{_ptr_name(p)}'") from None


def _write_bytes_raw(it: Interpreter, p, data: bytes) -> None:
    if isinstance(p, MmioPtr):
        for i, b in enumerate(data):
            it.bus.write(p.address + i, 1, b)
        return
    if isinstance(p, BytePtr):
        changed = M.write_region(p.seq, p.index, p.offset, data, it.endian)
    elif isinstance(p, Ptr):
        changed = M.write_region(p.seq, p.index, 0, data, it.endian)
    else:
        raise InterpError("NULL pointer passed to a memory function")
    for box in changed:
        it.note_write(box, None)


def _c_string(it: Interpreter, p, limit: int = 4096) -> bytes:
    out = bytearray()
    q = p
    while len(out) < limit:
        b = _bytes_at(it, q, 1)[0] if not isinstance(q, Ptr) or 0 <= q.index < len(q.seq) else 0
        if b == 0:
            break
        out.append(b)
        q = q.moved(1)
    return bytes(out)


def _b_memcpy(it, args, node):
    (dst, _), (src, _), (n, _) = args[:3]
    _write_bytes(it, dst, _bytes_at(it, src, int(n)))
    return dst, PointerType(VOID)


def _b_memset(it, args, node):
    (dst, _), (c, _), (n, _) = args[:3]
    _write_bytes(it, dst, bytes([int(c) & 0xFF]) * int(n))
    return dst, PointerType(VOID)


def _b_memcmp(it, args, node):
    (a, _), (b, _), (n, _) = args[:3]
    x, y = _bytes_at(it, a, int(n)), _bytes_at(it, b, int(n))
    return (x > y) - (x < y), it.INT


def _b_strlen(it, args, node):
    return len(_c_string(it, args[0][0])), it.ints["unsigned long"]


def _b_strcpy(it, args, node):
    s = _c_string(it, args[1][0]) + b"\0"
    _write_bytes(it, args[0][0], s)
    return args[0]


def _b_strncpy(it, args, node):
    n = int(args[2][0])
    s = _c_string(it, args[1][0], n)[:n]
    _write_bytes(it, args[0][0], s.ljust(n, b"\0"))
    return args[0]


def _b_strcat(it, args, node):
    d = _c_string(it, args[0][0])
    s = _c_string(it, args[1][0]) + b"\0"
    _write_bytes(it, it.ptr_add(args[0][0], len(d)), s)
    return args[0]


def _b_strcmp(it, args, node):
    a, b = _c_string(it, args[0][0]), _c_string(it, args[1][0])
    return (a > b) - (a < b), it.INT


def _b_strncmp(it, args, node):
    n = int(args[2][0])
    a, b = _c_string(it, args[0][0], n)[:n], _c_string(it, args[1][0], n)[:n]
    return (a > b) - (a < b), it.INT


def c_format(it: Interpreter, fmt: bytes, args: list) -> str:
    out = []
    ai = 0
    i = 0
    s = fmt.decode("latin-1")
    pat = re.compile(r"%([-+ #0]*)(\*|\d+)?(?:\.(\*|\d+))?(hh|h|ll|l|z|j|t|L)?([diouxXcsfFeEgGp%])")
    pos = 0
    for m in pat.finditer(s):
        out.append(s[pos : m.start()])
        pos = m.end()
        flags, width, prec, _len, conv = m.groups()
        if conv == "%":
            out.append("%")
            continue
        if width == "*":
            width = str(int(args[ai][0])) if ai < len(args) else ""
            ai += 1
        if prec == "*":
            prec = str(int(args[ai][0])) if ai < len(args) else ""
            ai += 1
        v, t = args[ai] if ai < len(args) else (0, it.INT)
        ai += 1
        spec = "%" + (flags or "") + (width or "") + (("." + prec) if prec is not None else "")
        if conv == "s":
            text = _c_string(it, v).decode("latin-1") if is_pointer_value(v) else str(v)
            out.append((spec + "s") % text)
        elif conv == "c":
            out.append((spec + "c") % chr(int(v) & 0xFF))
        elif conv in "fFeEgG":
            out.append((spec + conv) % float(v if not is_pointer_value(v) else 0))
        elif conv == "p":
            out.append(f"0x{M.pointer_address(v):X}")
        else:
            n = M.pointer_address(v) if is_pointer_value(v) else int(v)
            if conv in "uxXo" and n < 0:
                size = t.size if isinstance(t, IntType) and t.size >= it.INT.size else it.INT.size
                n &= (1 << (8 * size)) - 1
            out.append((spec + ("d" if conv in "iu" else conv)) % n)
    out.append(s[pos:])
    _ = i
    return "".join(out)


def _b_printf(it, args, node):
    text = c_format(it, _c_string(it, args[0][0]), args[1:]) if args else ""
    it.emit(OutputText, channel="printf", text=text)
    return len(text), it.INT


def _b_sprintf(it, args, node):
    text = c_format(it, _c_string(it, args[1][0]), args[2:])
    _write_bytes(it, args[0][0], text.encode("latin-1") + b"\0")
    return len(text), it.INT


def _b_snprintf(it, args, node):
    n = int(args[1][0])
    text = c_format(it, _c_string(it, args[2][0]), args[3:])
    if n > 0:
        _write_bytes(it, args[0][0], text.encode("latin-1")[: n - 1] + b"\0")
    return len(text), it.INT


def _b_puts(it, args, node):
    text = _c_string(it, args[0][0]).decode("latin-1")
    it.emit(OutputText, channel="printf", text=text + "\n")
    return 0, it.INT


def _b_putchar(it, args, node):
    it.emit(OutputText, channel="printf", text=chr(int(args[0][0]) & 0xFF))
    return args[0][0], it.INT


def _b_abs(it, args, node):
    v, t = args[0]
    return abs(v), t


def _b_math(fn):
    def impl(it, args, node):
        try:
            return float(fn(*[float(a[0]) for a in args])), it.DOUBLE
        except (ValueError, OverflowError):
            return float("nan"), it.DOUBLE

    return impl


def _b_exit(it, args, node):
    raise ExitProgram(int(args[0][0]) if args else 0)


def _b_malloc(it, args, node):
    n = int(args[0][0]) if args else 0
    arr = make_object(ArrayType(it.ints["unsigned char"], max(n, 1)), "malloc")
    return BytePtr(arr.seq, 0, 0, VOID), PointerType(VOID)


def _b_free(it, args, node):
    return None, VOID


def _b_atoi(it, args, node):
    s = _c_string(it, args[0][0]).decode("latin-1").strip()
    m = re.match(r"[-+]?\d+", s)
    return (int(m.group(0)) if m else 0), it.INT


BUILTINS = {
    "memcpy": _b_memcpy,
    "memmove": _b_memcpy,
    "memset": _b_memset,
    "memcmp": _b_memcmp,
    "strlen": _b_strlen,
    "strcpy": _b_strcpy,
    "strncpy": _b_strncpy,
    "strcat": _b_strcat,
    "strcmp": _b_strcmp,
    "strncmp": _b_strncmp,
    "printf": _b_printf,
    "sprintf": _b_sprintf,
    "snprintf": _b_snprintf,
    "puts": _b_puts,
    "putchar": _b_putchar,
    "abs": _b_abs,
    "labs": _b_abs,
    "fabs": _b_math(abs),
    "sqrt": _b_math(math.sqrt),
    "sin": _b_math(math.sin),
    "cos": _b_math(math.cos),
    "floor": _b_math(math.floor),
    "ceil": _b_math(math.ceil),
    "pow": _b_math(math.pow),
    "exit": _b_exit,
    "abort": _b_exit,
    "malloc": _b_malloc,
    "free": _b_free,
    "atoi": _b_atoi,
}
