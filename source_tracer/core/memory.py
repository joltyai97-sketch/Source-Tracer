"""Object-based memory model.

Every scalar lives in a :class:`Box`; arrays and structs are containers of
boxes. Pointers refer to (container, index) instead of raw addresses, which
keeps values readable ("g_tx_buf[3]") while still supporting pointer
arithmetic. Byte-level reinterpretation (``(uint8_t *)&msg``) is supported
by serialising the underlying objects with the target's endianness.
"""

from __future__ import annotations

import struct as _struct
from dataclasses import dataclass

from source_tracer.core.types import (
    ArrayType,
    CType,
    FloatType,
    FunctionType,
    IntType,
    PointerType,
    StructType,
)

# --------------------------------------------------------------------------- storage


class Box:
    """Storage for one scalar value."""

    __slots__ = ("type", "value", "name", "bits", "union", "_seq")

    def __init__(self, ctype: CType, value=0, name: str = "", bits: int | None = None):
        self.type = ctype
        self.value = value
        self.name = name  # human-readable path, e.g. "g_status.temp" or "buf[2]"
        self.bits = bits  # bit-field width, if any
        self.union: tuple[StructObj, str] | None = None
        self._seq: list | None = None

    @property
    def seq(self) -> list:
        """A one-element list holding this box, so ``&x`` can be a normal Ptr."""
        if self._seq is None:
            self._seq = [self]
        return self._seq

    def __repr__(self) -> str:
        return f"Box({self.name or '?'}={self.value!r}:{self.type})"


class ArrayObj:
    __slots__ = ("type", "items", "name", "_seq")

    def __init__(self, ctype: ArrayType, items: list, name: str = ""):
        self.type = ctype
        self.items = items
        self.name = name
        self._seq: list | None = None

    @property
    def seq(self) -> list:
        if self._seq is None:
            self._seq = [self]
        return self._seq


class StructObj:
    __slots__ = ("type", "members", "name", "_seq")

    def __init__(self, ctype: StructType, members: dict, name: str = ""):
        self.type = ctype
        self.members = members
        self.name = name
        self._seq: list | None = None

    @property
    def seq(self) -> list:
        if self._seq is None:
            self._seq = [self]
        return self._seq


Slot = Box | ArrayObj | StructObj


def make_object(ctype: CType, name: str = "") -> Slot:
    """Create zero-initialised storage for ``ctype``."""
    if isinstance(ctype, ArrayType):
        n = ctype.length or 0
        return ArrayObj(ctype, [make_object(ctype.elem, f"{name}[{i}]") for i in range(n)], name)
    if isinstance(ctype, StructType):
        members = {}
        for fname, ftype in ctype.all_fields():
            obj = make_object(ftype, f"{name}.{fname}" if name else fname)
            if isinstance(obj, Box) and fname in ctype.bitfields:
                obj.bits = ctype.bitfields[fname]
            members[fname] = obj
        so = StructObj(ctype, members, name)
        if ctype.is_union:
            for fname, obj in members.items():
                for leaf in iter_boxes(obj):
                    leaf.union = (so, fname)
        return so
    return Box(ctype, 0.0 if isinstance(ctype, FloatType) else 0, name)


def iter_boxes(slot: Slot):
    if isinstance(slot, Box):
        yield slot
    elif isinstance(slot, ArrayObj):
        for it in slot.items:
            yield from iter_boxes(it)
    else:
        for it in slot.members.values():
            yield from iter_boxes(it)


def slot_size(slot: Slot) -> int:
    if isinstance(slot, Box):
        return max(slot.type.size, 1)
    if isinstance(slot, ArrayObj):
        return sum(slot_size(i) for i in slot.items)
    return slot.type.size


def copy_into(dst: Slot, src: Slot) -> None:
    """Struct/array assignment: copy values recursively (types assumed compatible)."""
    if isinstance(dst, Box) and isinstance(src, Box):
        dst.value = src.value
    elif isinstance(dst, ArrayObj) and isinstance(src, ArrayObj):
        for d, s in zip(dst.items, src.items):
            copy_into(d, s)
    elif isinstance(dst, StructObj) and isinstance(src, StructObj):
        for k, d in dst.members.items():
            if k in src.members:
                copy_into(d, src.members[k])


def clone(slot: Slot, name: str = "") -> Slot:
    new = make_object(slot.type, name or slot.name)
    copy_into(new, slot)
    return new


# --------------------------------------------------------------------------- byte images


def scalar_to_bytes(ctype: CType, value, endian: str) -> bytes:
    size = max(ctype.size, 1)
    order = "big" if endian == "big" else "little"
    if isinstance(ctype, FloatType):
        fmt = (">" if endian == "big" else "<") + ("f" if size == 4 else "d")
        return _struct.pack(fmt, float(value))
    if isinstance(value, (Ptr, FuncPtr, MmioPtr)):
        value = pointer_address(value)
    return (int(value) & ((1 << (8 * size)) - 1)).to_bytes(size, order)


def scalar_from_bytes(ctype: CType, data: bytes, endian: str):
    if isinstance(ctype, FloatType):
        fmt = (">" if endian == "big" else "<") + ("f" if len(data) == 4 else "d")
        return _struct.unpack(fmt, data)[0]
    v = int.from_bytes(data, "big" if endian == "big" else "little")
    if isinstance(ctype, IntType):
        return ctype.wrap(v)
    return v


def _order(endian: str) -> str:
    return "big" if endian == "big" else "little"


def to_bytes(slot: Slot, endian: str) -> bytes:
    """Byte image of an object as it would sit in target memory (padding = 0)."""
    if isinstance(slot, Box):
        return scalar_to_bytes(slot.type, slot.value, endian)
    if isinstance(slot, ArrayObj):
        return b"".join(to_bytes(i, endian) for i in slot.items)
    fields, size, _ = slot.type.layout()
    buf = bytearray(size)
    if slot.type.is_union:
        # All members alias; the largest one carries the most information.
        order = sorted(fields, key=lambda f: slot_size(slot.members[f.name]) if f.name in slot.members else 0)
        fields = order
    for f in fields:
        member = slot.members.get(f.name)
        if member is None:
            continue
        if f.bits is not None and isinstance(member, Box):
            usize = max(f.type.size, 1)
            unit = int.from_bytes(buf[f.offset : f.offset + usize], _order(endian))
            mask = (1 << f.bits) - 1
            unit = (unit & ~(mask << f.shift)) | ((int(member.value) & mask) << f.shift)
            buf[f.offset : f.offset + usize] = unit.to_bytes(usize, _order(endian))
            continue
        data = to_bytes(member, endian)
        buf[f.offset : f.offset + len(data)] = data
    return bytes(buf)


def from_bytes(slot: Slot, data: bytes, endian: str) -> None:
    if isinstance(slot, Box):
        n = slot_size(slot)
        slot.value = scalar_from_bytes(slot.type, data[:n].ljust(n, b"\0"), endian)
        return
    if isinstance(slot, ArrayObj):
        off = 0
        for c in slot.items:
            n = slot_size(c)
            from_bytes(c, data[off : off + n], endian)
            off += n
        return
    fields, size, _ = slot.type.layout()
    data = data.ljust(size, b"\0")
    for f in fields:
        member = slot.members.get(f.name)
        if member is None:
            continue
        if f.bits is not None and isinstance(member, Box):
            usize = max(f.type.size, 1)
            unit = int.from_bytes(data[f.offset : f.offset + usize], _order(endian))
            v = (unit >> f.shift) & ((1 << f.bits) - 1)
            if isinstance(member.type, IntType) and member.type.signed and v >> (f.bits - 1):
                v -= 1 << f.bits
            member.value = v
            continue
        n = slot_size(member)
        from_bytes(member, data[f.offset : f.offset + n], endian)


def sync_union(box: Box, endian: str) -> None:
    """After writing a union member, re-derive the other members from its bytes."""
    if box.union is None:
        return
    union, member = box.union
    data = to_bytes(union.members[member], endian).ljust(slot_size(union), b"\0")
    for name, m in union.members.items():
        if name != member:
            from_bytes(m, data, endian)


# --------------------------------------------------------------------------- pointers


@dataclass(frozen=True)
class Ptr:
    """Pointer into a list of slots (an array's items, or a single object's ``seq``)."""

    seq: list
    index: int
    elem: CType

    def __eq__(self, other) -> bool:
        return isinstance(other, Ptr) and other.seq is self.seq and other.index == self.index

    def __hash__(self) -> int:
        return hash((id(self.seq), self.index))

    def moved(self, n: int) -> Ptr:
        return Ptr(self.seq, self.index + n, self.elem)

    def label(self) -> str:
        if 0 <= self.index < len(self.seq):
            name = getattr(self.seq[self.index], "name", "")
            if name:
                return f"&{name}"
        if self.seq:
            base = getattr(self.seq[0], "name", "?").rsplit("[", 1)[0]
            return f"&{base}[{self.index}]"
        return "pointer"


@dataclass(frozen=True)
class BytePtr:
    """Pointer that reinterprets a region of slots as raw bytes of type ``elem``."""

    seq: list
    index: int  # slot where the byte region starts
    offset: int  # byte offset from the start of seq[index]
    elem: CType

    def moved(self, n: int) -> BytePtr:
        return BytePtr(self.seq, self.index, self.offset + n * max(self.elem.size, 1), self.elem)

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, BytePtr)
            and other.seq is self.seq
            and other.index == self.index
            and other.offset == self.offset
        )

    def __hash__(self) -> int:
        return hash((id(self.seq), self.index, self.offset))

    def label(self) -> str:
        base = getattr(self.seq[self.index], "name", "?") if self.index < len(self.seq) else "?"
        return f"(bytes of {base})+{self.offset}"


@dataclass(frozen=True)
class MmioPtr:
    """Pointer to a raw address (hardware registers / absolute memory)."""

    address: int
    elem: CType

    def moved(self, n: int) -> MmioPtr:
        return MmioPtr(self.address + n * max(self.elem.size, 1), self.elem)

    def label(self) -> str:
        return f"0x{self.address:X}"


@dataclass(frozen=True)
class FuncPtr:
    func: object  # FunctionDef or stub name (str)
    ftype: CType | None = None

    def label(self) -> str:
        return f"&{getattr(self.func, 'name', self.func)}"


PointerValue = Ptr | BytePtr | MmioPtr | FuncPtr


def is_pointer_value(v) -> bool:
    return isinstance(v, (Ptr, BytePtr, MmioPtr, FuncPtr))


_fake_addresses: dict[int, int] = {}


def pointer_address(v) -> int:
    """A stable, fake numeric address for object pointers (used for printing / int casts)."""
    if isinstance(v, MmioPtr):
        return v.address
    if v is None or v == 0:
        return 0
    if isinstance(v, (Ptr, BytePtr)):
        base = _fake_addresses.setdefault(id(v.seq), 0x10000000 + 0x10000 * (len(_fake_addresses) + 1))
        if isinstance(v, BytePtr):
            return base + sum(slot_size(s) for s in v.seq[: v.index]) + v.offset
        return base + v.index * max(v.elem.size, 1)
    if isinstance(v, FuncPtr):
        return 0x00F00000 + (id(v.func) & 0xFFFF)
    return int(v)


def region_bytes(seq: list, index: int, endian: str) -> tuple[bytes, list[tuple[int, int]]]:
    """Serialise seq[index:] and return (bytes, [(slot_index, start_offset)])."""
    data = bytearray()
    starts = []
    for i in range(index, len(seq)):
        starts.append((i, len(data)))
        data += to_bytes(seq[i], endian)
    return bytes(data), starts


def read_region(seq: list, index: int, offset: int, size: int, endian: str) -> bytes:
    data, _ = region_bytes(seq, index, endian)
    if offset < 0 or offset + size > len(data):
        raise IndexError("byte access outside the object")
    return data[offset : offset + size]


def write_region(seq: list, index: int, offset: int, chunk: bytes, endian: str) -> list[Box]:
    """Write raw bytes into the slots of seq[index:]; returns the boxes that changed."""
    data, starts = region_bytes(seq, index, endian)
    if offset < 0 or offset + len(chunk) > len(data):
        raise IndexError("byte access outside the object")
    new = bytearray(data)
    new[offset : offset + len(chunk)] = chunk
    changed: list[Box] = []
    for (i, start), nxt in zip(starts, [s for _, s in starts[1:]] + [len(data)]):
        if start >= offset + len(chunk) or nxt <= offset:
            continue
        before = [(b, b.value) for b in iter_boxes(seq[i])]
        from_bytes(seq[i], bytes(new[start:nxt]), endian)
        changed.extend(b for b, old in before if b.value != old)
    return changed


def first_box(slot: Slot) -> Box:
    return next(iter_boxes(slot))


def describe_pointer(v) -> str:
    if v is None or v == 0:
        return "NULL"
    if hasattr(v, "label"):
        return v.label()
    return str(v)


def is_function_type(t: CType) -> bool:
    return isinstance(t, FunctionType) or (isinstance(t, PointerType) and isinstance(t.target, FunctionType))
