"""C/C++ type model used by the interpreter.

Sizes come from the target profile so ``int`` can be 16 or 32 bits, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class CType:
    """Base class. Subclasses define ``size`` (bytes) and ``name``."""

    def __str__(self) -> str:
        return self.name

    @property
    def is_integer(self) -> bool:
        return isinstance(self, IntType)

    @property
    def is_float(self) -> bool:
        return isinstance(self, FloatType)

    @property
    def is_arithmetic(self) -> bool:
        return isinstance(self, (IntType, FloatType))

    @property
    def is_pointer(self) -> bool:
        return isinstance(self, PointerType)

    @property
    def is_scalar(self) -> bool:
        return isinstance(self, (IntType, FloatType, PointerType))


@dataclass(frozen=True, eq=True)
class IntType(CType):
    name: str
    size: int
    signed: bool
    rank: int  # conversion rank: bool < char < short < int < long < long long

    def wrap(self, v: int) -> int:
        """Truncate ``v`` to this type's width (two's complement)."""
        if self.rank == 0:  # bool
            return 1 if v else 0
        bits = self.size * 8
        v &= (1 << bits) - 1
        if self.signed and v >> (bits - 1):
            v -= 1 << bits
        return v

    @property
    def min(self) -> int:
        return -(1 << (self.size * 8 - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        return (1 << (self.size * 8 - (1 if self.signed else 0))) - 1

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, eq=True)
class EnumType(IntType):
    """An integer type whose values have names (``enum``)."""

    members: tuple = ()  # ((name, value), ...)

    def label(self, v: int) -> str | None:
        for n, val in self.members:
            if val == v:
                return n
        return None


@dataclass(frozen=True, eq=True)
class FloatType(CType):
    name: str
    size: int

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, eq=True)
class VoidType(CType):
    name: str = "void"
    size: int = 1


@dataclass(eq=False)
class PointerType(CType):
    target: CType
    size: int = 4

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{self.target} *"

    def __eq__(self, other) -> bool:
        return isinstance(other, PointerType) and self.target == other.target

    def __hash__(self) -> int:
        return hash(("ptr", hash(self.target)))


@dataclass(eq=False)
class ReferenceType(CType):
    target: CType
    size: int = 4

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{self.target} &"


@dataclass(eq=False)
class ArrayType(CType):
    elem: CType
    length: int | None  # None = unknown (e.g. `int a[]` before initialisation)

    @property
    def size(self) -> int:  # type: ignore[override]
        return self.elem.size * (self.length or 0)

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{self.elem}[{self.length if self.length is not None else ''}]"

    def __eq__(self, other) -> bool:
        return isinstance(other, ArrayType) and self.elem == other.elem and self.length == other.length

    def __hash__(self) -> int:
        return hash(("arr", hash(self.elem), self.length))


@dataclass(frozen=True)
class FieldLayout:
    name: str
    type: CType
    offset: int  # byte offset of the field (or of its storage unit for bit-fields)
    shift: int | None = None  # bit-fields: bit position of the LSB within the unit
    bits: int | None = None  # bit-fields: width


def align_of(t: CType, limit: int) -> int:
    if isinstance(t, ArrayType):
        return align_of(t.elem, limit)
    if isinstance(t, StructType):
        return t.alignment
    return max(1, min(getattr(t, "size", 1) or 1, limit))


def _round_up(n: int, a: int) -> int:
    return (n + a - 1) // a * a


@dataclass(eq=False)
class StructType(CType):
    """struct / union / class. Fields are filled in when the body is seen.

    Layout follows the target ABI loosely: each field is aligned to
    min(its size, ``align_limit``) and bit-fields share storage units of
    their declared type (allocated from the MSB on big-endian targets).
    """

    tag: str
    is_union: bool = False
    fields: list[tuple[str, CType]] = field(default_factory=list)
    bitfields: dict[str, int] = field(default_factory=dict)  # field -> width in bits
    methods: dict[str, list] = field(default_factory=dict)  # name -> [FunctionDef]
    bases: list[StructType] = field(default_factory=list)
    complete: bool = False
    align_limit: int = 4
    big_endian: bool = False
    _layout_cache: tuple | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{'union' if self.is_union else 'struct'} {self.tag}"

    def layout(self) -> tuple[list[FieldLayout], int, int]:
        """(fields, size, alignment)."""
        key = (len(self.fields), tuple(len(b.fields) for b in self.bases), len(self.bitfields))
        if self._layout_cache is not None and self._layout_cache[0] == key:
            return self._layout_cache[1]
        entries: list[FieldLayout] = []
        offset = 0
        max_al = 1
        unit_off = unit_size = used = None
        for fname, ftype in self.all_fields():
            al = align_of(ftype, self.align_limit)
            max_al = max(max_al, al)
            width = self.bitfields.get(fname)
            if self.is_union:
                fsize = ftype.size if width is None else max(ftype.size, 1)
                shift = None if width is None else (fsize * 8 - width if self.big_endian else 0)
                entries.append(FieldLayout(fname, ftype, 0, shift, width))
                offset = max(offset, fsize)
                continue
            if width is not None:
                usize = max(ftype.size, 1)
                if width == 0:
                    unit_off = None
                    continue
                if unit_off is None or unit_size != usize or used + width > usize * 8:
                    offset = _round_up(offset, al)
                    unit_off, unit_size, used = offset, usize, 0
                    offset += usize
                shift = usize * 8 - used - width if self.big_endian else used
                entries.append(FieldLayout(fname, ftype, unit_off, shift, width))
                used += width
                continue
            unit_off = None
            offset = _round_up(offset, al)
            entries.append(FieldLayout(fname, ftype, offset))
            offset += ftype.size
        size = _round_up(max(offset, 1), max_al)
        result = (entries, size, max_al)
        self._layout_cache = (key, result)
        return result

    @property
    def size(self) -> int:  # type: ignore[override]
        return self.layout()[1]

    @property
    def alignment(self) -> int:
        return self.layout()[2]

    def field_layout(self, name: str) -> FieldLayout | None:
        return next((f for f in self.layout()[0] if f.name == name), None)

    def all_fields(self) -> list[tuple[str, CType]]:
        out: list[tuple[str, CType]] = []
        for b in self.bases:
            out.extend(b.all_fields())
        out.extend(self.fields)
        return out

    def field_type(self, name: str) -> CType | None:
        for n, t in self.all_fields():
            if n == name:
                return t
        return None

    def find_methods(self, name: str) -> list:
        if name in self.methods:
            return self.methods[name]
        for b in self.bases:
            m = b.find_methods(name)
            if m:
                return m
        return []

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other) -> bool:
        return self is other


@dataclass(eq=False)
class FunctionType(CType):
    ret: CType
    params: list[CType]
    variadic: bool = False
    size: int = 4

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{self.ret} (*)({', '.join(map(str, self.params))})"


VOID = VoidType()


def make_int_types(sizes: dict[str, int]) -> dict[str, IntType]:
    """Standard integer types with sizes from the profile."""
    s = {k: int(v) for k, v in sizes.items()}
    c, sh, i, lo, ll = s.get("char", 1), s.get("short", 2), s.get("int", 4), s.get("long", 4), s.get("long long", 8)
    t = {
        "bool": IntType("bool", 1, False, 0),
        "char": IntType("char", c, True, 1),
        "signed char": IntType("signed char", c, True, 1),
        "unsigned char": IntType("unsigned char", c, False, 1),
        "short": IntType("short", sh, True, 2),
        "unsigned short": IntType("unsigned short", sh, False, 2),
        "int": IntType("int", i, True, 3),
        "unsigned int": IntType("unsigned int", i, False, 3),
        "long": IntType("long", lo, True, 4),
        "unsigned long": IntType("unsigned long", lo, False, 4),
        "long long": IntType("long long", ll, True, 5),
        "unsigned long long": IntType("unsigned long long", ll, False, 5),
    }
    return t


# Common fixed-width / vendor names recognised even without a header.
def guess_fixed_width(name: str, ints: dict[str, IntType]) -> IntType | None:
    import re

    m = re.fullmatch(r"(?:__)?(u|U|uint|UINT|s|S|int|INT|sint|SINT)(8|16|32|64)(?:_t|_T)?", name)
    if m:
        unsigned = m.group(1).lower() in ("u", "uint")
        bits = int(m.group(2))
        for t in ints.values():
            if t.size * 8 == bits and t.signed != unsigned and t.rank > 0:
                return t
    aliases = {
        "BYTE": "unsigned char", "UBYTE": "unsigned char", "UCHAR": "unsigned char", "uchar": "unsigned char",
        "WORD": "unsigned short", "UWORD": "unsigned short", "USHORT": "unsigned short", "ushort": "unsigned short",
        "DWORD": "unsigned long", "ULONG": "unsigned long", "ulong": "unsigned long", "uint": "unsigned int",
        "BOOL": "unsigned char", "BOOLEAN": "unsigned char", "boolean": "bool", "_Bool": "bool",
        "size_t": "unsigned long", "ssize_t": "long", "ptrdiff_t": "long", "uintptr_t": "unsigned long",
        "intptr_t": "long", "wchar_t": "int", "char16_t": "unsigned short", "char32_t": "unsigned long",
    }
    if name in aliases:
        return ints[aliases[name]]
    return None
