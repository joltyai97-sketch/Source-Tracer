"""Build an executable program model from preprocessed translation units.

Collects types (typedefs, structs/classes, enums), functions and global
variables across every C/C++ source file, and resolves C declarators into
:mod:`source_tracer.core.types` objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node, Tree

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
    guess_fixed_width,
    make_int_types,
)
from source_tracer.frontend.consteval import evaluate
from source_tracer.frontend.parser import CPP_EXTENSIONS, _parser, node_text
from source_tracer.frontend.preprocess import PreprocessedUnit, preprocess
from source_tracer.profile import Profile

TU_EXTENSIONS = {".c"} | (CPP_EXTENSIONS - {".hpp", ".hh", ".hxx", ".h++"})


@dataclass
class Unit:
    index: int
    pre: PreprocessedUnit
    tree: Tree

    @property
    def path(self) -> Path:
        return self.pre.path

    def origin(self, node: Node) -> tuple[Path, int]:
        return self.pre.origin(node.start_point[0])


@dataclass
class FunctionDef:
    name: str
    qualname: str
    unit: Unit
    node: Node  # function_definition
    ret: CType
    params: list[tuple[str, CType]]
    variadic: bool = False
    cls: StructType | None = None
    static: bool = False
    is_ctor: bool = False
    is_dtor: bool = False

    @property
    def body(self) -> Node | None:
        return self.node.child_by_field_name("body")

    @property
    def location(self) -> tuple[Path, int]:
        return self.unit.origin(self.node)


@dataclass
class GlobalDef:
    name: str
    ctype: CType
    unit: Unit
    node: Node  # the declarator (init_declarator or plain)
    init: Node | None
    static: bool
    ctor_args: Node | None = None  # C++ `Foo obj(1, 2);`

    @property
    def location(self) -> tuple[Path, int]:
        return self.unit.origin(self.node)


@dataclass
class Prototype:
    name: str
    ret: CType
    params: list[tuple[str, CType]]
    variadic: bool


@dataclass
class Program:
    profile: Profile
    ints: dict[str, IntType]
    units: list[Unit] = field(default_factory=list)
    typedefs: dict[str, CType] = field(default_factory=dict)
    structs: dict[str, StructType] = field(default_factory=dict)
    enum_consts: dict[str, int] = field(default_factory=dict)
    enums: dict[str, CType] = field(default_factory=dict)
    macro_consts: dict[str, int] = field(default_factory=dict)  # object-like #defines with integer values
    functions: dict[str, list[FunctionDef]] = field(default_factory=dict)
    statics: dict[int, dict[str, FunctionDef]] = field(default_factory=dict)  # unit index -> static functions
    globals: list[GlobalDef] = field(default_factory=list)
    prototypes: dict[str, Prototype] = field(default_factory=dict)
    warnings: list[tuple[Path | None, int | None, str]] = field(default_factory=list)
    _warned: set = field(default_factory=set)

    # ------------------------------------------------------------------ lookup
    def find_function(self, name: str, unit: Unit | None = None, nargs: int | None = None) -> FunctionDef | None:
        short = name.rpartition("::")[2]
        if unit is not None and short in self.statics.get(unit.index, {}):
            return self.statics[unit.index][short]
        cands = self.functions.get(name) or self.functions.get(short) or []
        if "::" in name:
            scoped = [f for f in cands if f.qualname == name or f.qualname.endswith("::" + name)]
            cands = scoped or cands
        return _pick_overload(cands, nargs)

    def all_functions(self) -> list[FunctionDef]:
        seen, out = set(), []
        for lst in list(self.functions.values()) + [list(d.values()) for d in self.statics.values()]:
            for f in lst:
                if id(f) not in seen:
                    seen.add(id(f))
                    out.append(f)
        return out

    def warn(self, unit: Unit | None, node: Node | None, message: str, key=None) -> None:
        key = key or (message, unit.index if unit else None, node.start_point if node is not None else None)
        if key in self._warned:
            return
        self._warned.add(key)
        if unit is not None and node is not None:
            f, line = unit.origin(node)
            self.warnings.append((f, line, message))
        else:
            self.warnings.append((unit.path if unit else None, None, message))

    # ------------------------------------------------------------------ types
    def resolve_base(self, node: Node | None, unit: Unit) -> CType:
        """Type from a type specifier node (``int``, ``unsigned short``, ``struct x``, ``my_t``...)."""
        if node is None:
            return self.ints["int"]
        t = node.type
        if t in ("primitive_type", "sized_type_specifier"):
            return self._primitive(node_text(node), unit, node)
        if t == "type_identifier":
            return self._named_type(node_text(node), unit, node)
        if t == "qualified_identifier" or t == "nested_namespace_specifier":
            return self._named_type(node_text(node).rpartition("::")[2], unit, node)
        if t in ("struct_specifier", "union_specifier", "class_specifier"):
            return self.define_record(node, unit)
        if t == "enum_specifier":
            return self.define_enum(node, unit)
        if t == "placeholder_type_specifier":  # auto
            return self.ints["int"]
        if t == "type_descriptor":
            return self.type_of_descriptor(node, unit)
        if t == "template_type":
            name = node.child_by_field_name("name")
            self.warn(unit, node, f"Templates are not supported yet ({node_text(node)}); treated as int.")
            return self._named_type(node_text(name) if name else "int", unit, node)
        self.warn(unit, node, f"Unknown type '{node_text(node)}'; treated as int.")
        return self.ints["int"]

    def _primitive(self, text: str, unit: Unit, node: Node) -> CType:
        words = text.replace("\n", " ").split()
        words = [w for w in words if w not in ("const", "volatile", "register", "static", "extern")]
        if not words:
            return self.ints["int"]
        if words == ["void"]:
            return VOID
        if "float" in words:
            return FloatType("float", self.profile.type_sizes.get("float", 4))
        if "double" in words:
            size = self.profile.type_sizes.get("double", 8)
            return FloatType("long double" if "long" in words else "double", size)
        if words in (["bool"], ["_Bool"]):
            return self.ints["bool"]
        unsigned = "unsigned" in words
        if "char" in words:
            base = "char" if not unsigned and "signed" not in words else ("unsigned char" if unsigned else "signed char")
            return self.ints[base]
        longs = words.count("long")
        if "short" in words:
            base = "short"
        elif longs >= 2:
            base = "long long"
        elif longs == 1:
            base = "long"
        elif any(w in ("int", "signed", "unsigned") for w in words):
            base = "int"
        else:
            guessed = guess_fixed_width(words[-1], self.ints)
            if guessed is not None:
                return guessed
            return self._named_type(words[-1], unit, node)
        return self.ints[("unsigned " if unsigned else "") + base]

    def _named_type(self, name: str, unit: Unit, node: Node) -> CType:
        if name in self.typedefs:
            return self.typedefs[name]
        if name in self.structs:
            return self.structs[name]
        guessed = guess_fixed_width(name, self.ints)
        if guessed is not None:
            return guessed
        self.warn(unit, node, f"Unknown type '{name}' (missing header?); treated as int.", key=("type", name))
        return self.ints["int"]

    def type_of_descriptor(self, node: Node, unit: Unit) -> CType:
        """For casts / sizeof: ``type_descriptor`` = type + optional abstract declarator."""
        base = self.resolve_base(node.child_by_field_name("type"), unit)
        decl = node.child_by_field_name("declarator")
        t, _ = self.apply_declarator(decl, base, unit)
        return t

    def apply_declarator(self, decl: Node | None, base: CType, unit: Unit) -> tuple[CType, Node | None]:
        """Apply pointer/array/function declarators to ``base``; return (type, name node)."""
        while decl is not None:
            t = decl.type
            if t in ("identifier", "field_identifier", "type_identifier", "qualified_identifier",
                     "destructor_name", "operator_name", "template_function"):
                return base, decl
            if t in ("pointer_declarator", "abstract_pointer_declarator"):
                base = PointerType(base, self.profile.type_sizes.get("pointer", 4))
            elif t in ("reference_declarator", "abstract_reference_declarator"):
                base = ReferenceType(base)
                inner = decl.child_by_field_name("declarator") or (decl.named_children[0] if decl.named_children else None)
                decl = inner
                continue
            elif t in ("array_declarator", "abstract_array_declarator"):
                size = decl.child_by_field_name("size")
                length = self.const_int(size, unit) if size is not None else None
                base = ArrayType(base, length)
            elif t in ("function_declarator", "abstract_function_declarator"):
                params, variadic = self.parameters(decl, unit)
                base = FunctionType(base, [p for _, p in params], variadic)
            elif t in ("parenthesized_declarator", "abstract_parenthesized_declarator"):
                named = [c for c in decl.named_children if c.type not in ("type_qualifier",)]
                decl = named[0] if named else None
                continue
            elif t == "init_declarator":
                pass
            else:
                return base, None
            decl = decl.child_by_field_name("declarator")
        return base, None

    def parameters(self, fdecl: Node, unit: Unit) -> tuple[list[tuple[str, CType]], bool]:
        plist = fdecl.child_by_field_name("parameters")
        params: list[tuple[str, CType]] = []
        variadic = False
        if plist is None:
            return params, variadic
        for p in plist.named_children:
            if p.type in ("parameter_declaration", "optional_parameter_declaration"):
                base = self.resolve_base(p.child_by_field_name("type"), unit)
                t, name = self.apply_declarator(p.child_by_field_name("declarator"), base, unit)
                if t is VOID and name is None:
                    continue
                if isinstance(t, ArrayType):  # array parameters are pointers
                    t = PointerType(t.elem, self.profile.type_sizes.get("pointer", 4))
                if isinstance(t, FunctionType):
                    t = PointerType(t)
                params.append((node_text(name) if name is not None else f"arg{len(params)}", t))
            elif p.type == "variadic_parameter" or node_text(p) == "...":
                variadic = True
            elif p.type == "identifier":  # K&R list, types filled in later
                params.append((node_text(p), self.ints["int"]))
        return params, variadic

    def const_int(self, node: Node | None, unit: Unit) -> int | None:
        if node is None:
            return None
        text = node_text(node)
        m = re.fullmatch(r"\s*sizeof\s*\(\s*([^()]+?)\s*\)\s*", text)
        if m:
            t = self.typedefs.get(m.group(1)) or self.structs.get(m.group(1).replace("struct ", ""))
            if t is not None:
                return t.size
        return evaluate(text, self.enum_consts)

    # ------------------------------------------------------------------ records & enums
    def define_record(self, node: Node, unit: Unit, name_hint: str | None = None) -> StructType:
        name_node = node.child_by_field_name("name")
        tag = node_text(name_node).rpartition("::")[2] if name_node is not None else (name_hint or f"<anon@{id(node)}>")
        body = node.child_by_field_name("body")
        st = self.structs.get(tag)
        if st is None:
            st = self.new_struct(tag, node.type == "union_specifier")
        if body is None or st.complete:
            return st
        st.complete = True
        for c in node.named_children:
            if c.type == "base_class_clause":
                for b in c.named_children:
                    if b.type in ("type_identifier", "qualified_identifier"):
                        base = self.structs.get(node_text(b).rpartition("::")[2])
                        if base is not None:
                            st.bases.append(base)
        for member in body.named_children:
            if member.type == "field_declaration":
                base = self.resolve_base(member.child_by_field_name("type"), unit)
                decls = member.children_by_field_name("declarator")
                bitclause = next((c for c in member.named_children if c.type == "bitfield_clause" or _is_unnamed_bitfield(c)), None)
                if not decls and bitclause is not None:  # unnamed padding bit-field: `unsigned : 12;`
                    width = self.const_int(bitclause.named_children[0] if bitclause.named_children else None, unit)
                    pad = f"__pad{len(st.fields)}"
                    st.fields.append((pad, base))
                    st.bitfields[pad] = width or 0
                    continue
                if not decls:  # anonymous struct/union member: flatten its fields
                    tnode = member.child_by_field_name("type")
                    if isinstance(base, StructType) and tnode is not None and tnode.child_by_field_name("name") is None:
                        st.fields.extend(base.fields)
                    continue
                width = None
                for c in member.named_children:
                    if c.type == "bitfield_clause":
                        width = self.const_int(c.named_children[0] if c.named_children else None, unit)
                for d in decls:
                    t, name = self.apply_declarator(d, base, unit)
                    if name is None:
                        continue
                    if isinstance(t, FunctionType):
                        self._method_prototype(st, node_text(name), t)
                        continue
                    st.fields.append((node_text(name), t))
                    if width:
                        st.bitfields[node_text(name)] = width
            elif member.type == "function_definition":
                self.add_function(member, unit, cls=st)
            elif member.type == "declaration":  # constructor prototypes etc.
                pass
            elif member.type == "template_declaration":
                self.warn(unit, member, "Template members are not supported yet; skipped.")
        return st

    def new_struct(self, tag: str, is_union: bool = False) -> StructType:
        st = StructType(tag, is_union=is_union, align_limit=self.profile.max_align, big_endian=self.profile.endianness == "big")
        self.structs[tag] = st
        return st

    def _method_prototype(self, st: StructType, name: str, ftype: FunctionType) -> None:
        pass  # body comes from an out-of-line definition; nothing to store yet

    def define_enum(self, node: Node, unit: Unit) -> CType:
        name_node = node.child_by_field_name("name")
        tag = node_text(name_node) if name_node is not None else None
        body = node.child_by_field_name("body")
        if body is None:
            return self.enums.get(tag, self.ints["int"]) if tag else self.ints["int"]
        members: list[tuple[str, int]] = []
        nxt = 0
        for e in body.named_children:
            if e.type != "enumerator":
                continue
            name = node_text(e.child_by_field_name("name"))
            v = e.child_by_field_name("value")
            val = self.const_int(v, unit) if v is not None else nxt
            if val is None:
                self.warn(unit, e, f"Could not work out the value of {name}; using {nxt}.")
                val = nxt
            self.enum_consts[name] = val
            members.append((name, val))
            nxt = val + 1
        base = self.ints["int"]
        et = EnumType(tag or "enum", base.size, base.signed, base.rank, tuple(members))
        if tag:
            self.enums[tag] = et
        return et

    # ------------------------------------------------------------------ functions & globals
    def add_function(self, node: Node, unit: Unit, cls: StructType | None = None, scope: str = "") -> None:
        decl = node.child_by_field_name("declarator")
        type_node = node.child_by_field_name("type")
        base = self.resolve_base(type_node, unit) if type_node is not None else VOID
        ftype, name_node = self.apply_declarator(decl, base, unit)
        if name_node is None or not isinstance(ftype, FunctionType):
            self.warn(unit, node, "Could not understand this function definition; skipped.")
            return
        fdecl = decl
        while fdecl is not None and fdecl.type != "function_declarator":
            fdecl = fdecl.child_by_field_name("declarator")
        params, variadic = self.parameters(fdecl, unit) if fdecl is not None else ([], False)
        full = node_text(name_node)
        name = full.rpartition("::")[2]
        if cls is None and "::" in full:
            cls_name = full.rpartition("::")[0].rpartition("::")[2]
            cls = self.structs.get(cls_name)
            if cls is None:
                cls = self.new_struct(cls_name)
        storage = [node_text(c) for c in node.children if c.type == "storage_class_specifier"]
        fn = FunctionDef(
            name=name,
            qualname=f"{cls.tag}::{name}" if cls else f"{scope}{name}",
            unit=unit,
            node=node,
            ret=ftype.ret if type_node is not None else VOID,
            params=params,
            variadic=variadic,
            cls=cls,
            static="static" in storage and cls is None,
            is_ctor=cls is not None and name == cls.tag,
            is_dtor=name.startswith("~"),
        )
        if cls is not None:
            cls.methods.setdefault(name, []).append(fn)
        if fn.static:
            self.statics.setdefault(unit.index, {})[name] = fn
        else:
            self.functions.setdefault(name, []).append(fn)
            if fn.qualname != name:
                self.functions.setdefault(fn.qualname, []).append(fn)

    def add_declaration(self, node: Node, unit: Unit, scope: str = "") -> None:
        type_node = node.child_by_field_name("type")
        storage = [node_text(c) for c in node.children if c.type == "storage_class_specifier"]
        base = self.resolve_base(type_node, unit) if type_node is not None else self.ints["int"]
        for d in node.children_by_field_name("declarator"):
            init = None
            ctor_args = None
            target = d
            if d.type == "init_declarator":
                v = d.child_by_field_name("value")
                if v is not None and v.type == "argument_list":
                    ctor_args = v
                else:
                    init = v
                target = d.child_by_field_name("declarator")
            t, name = self.apply_declarator(target, base, unit)
            if name is None:
                continue
            n = node_text(name).rpartition("::")[2]
            if isinstance(t, FunctionType):
                fdecl = target
                while fdecl is not None and fdecl.type != "function_declarator":
                    fdecl = fdecl.child_by_field_name("declarator")
                params, variadic = self.parameters(fdecl, unit) if fdecl is not None else ([], False)
                self.prototypes.setdefault(n, Prototype(n, t.ret, params, variadic))
                continue
            if "extern" in storage and init is None:
                # Declaration only; remember the type in case the definition is missing.
                if not any(g.name == n and not g.static for g in self.globals):
                    self._extern_only.setdefault(n, GlobalDef(n, t, unit, d, None, False))
                continue
            if isinstance(t, ArrayType) and t.length is None and init is not None:
                t = ArrayType(t.elem, _initializer_length(init))
            existing = next((g for g in self.globals if g.name == n and not g.static and "static" not in storage), None)
            if existing is not None and "static" not in storage:
                if init is not None and existing.init is None:
                    existing.init, existing.node, existing.ctype = init, d, t
                continue  # tentative definitions in several files share one object
            self.globals.append(GlobalDef(n, t, unit, d, init, "static" in storage, ctor_args))

    def __post_init__(self) -> None:
        self._extern_only: dict[str, GlobalDef] = {}


def _is_unnamed_bitfield(node: Node) -> bool:
    """tree-sitter reports `unsigned : 12;` as an ERROR node holding ': 12'."""
    return node.type == "ERROR" and re.fullmatch(r":\s*\w+", node_text(node).strip()) is not None


def _initializer_length(init: Node) -> int | None:
    if init.type == "initializer_list":
        return len(init.named_children)
    if init.type in ("string_literal", "concatenated_string"):
        from source_tracer.core.literals import string_value

        return len(string_value(init)) + 1
    return None


def _pick_overload(cands: list[FunctionDef], nargs: int | None) -> FunctionDef | None:
    if not cands:
        return None
    if nargs is None or len(cands) == 1:
        return cands[0]
    for f in cands:
        if len(f.params) == nargs or (f.variadic and nargs >= len(f.params)):
            return f
    return cands[0]


# --------------------------------------------------------------------------- building


def _walk_top(prog: Program, nodes: list[Node], unit: Unit, scope: str = "") -> None:
    i = 0
    while i < len(nodes):
        node = nodes[i]
        t = node.type
        # K&R definition: `int f(a, b) int a; int b; { ... }`
        knr = _knr(nodes, i)
        if knr:
            _add_knr(prog, nodes, i, knr, unit)
            i += knr
            continue
        if t == "function_definition":
            prog.add_function(node, unit, scope=scope)
        elif t == "declaration":
            prog.add_declaration(node, unit, scope)
        elif t == "type_definition":
            _typedef(prog, node, unit)
        elif t in ("struct_specifier", "union_specifier", "class_specifier"):
            prog.define_record(node, unit)
        elif t == "enum_specifier":
            prog.define_enum(node, unit)
        elif t in ("linkage_specification", "declaration_list", "preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif"):
            body = node.child_by_field_name("body")
            _walk_top(prog, body.named_children if body is not None and t == "linkage_specification" else node.named_children, unit, scope)
        elif t == "namespace_definition":
            body = node.child_by_field_name("body")
            name = node.child_by_field_name("name")
            if body is not None:
                _walk_top(prog, body.named_children, unit, scope + (node_text(name) + "::" if name else ""))
        elif t == "template_declaration":
            prog.warn(unit, node, "Templates are not supported yet; skipped.")
        elif t == "ERROR":
            prog.warn(unit, node, "Code here could not be understood and will be skipped.")
            _walk_top(prog, [c for c in node.named_children if c.type in ("function_definition", "declaration")], unit, scope)
        elif t == "expression_statement":
            for c in node.named_children:
                if c.type in ("function_definition", "declaration"):
                    _walk_top(prog, [c], unit, scope)
        i += 1


def _typedef(prog: Program, node: Node, unit: Unit) -> None:
    tnode = node.child_by_field_name("type")
    decls = node.children_by_field_name("declarator")
    hint = None
    if tnode is not None and tnode.type in ("struct_specifier", "union_specifier", "class_specifier") and tnode.child_by_field_name("name") is None:
        first = decls[0] if decls else None
        if first is not None and first.type == "type_identifier":
            hint = node_text(first)
    if hint is not None:
        base = prog.define_record(tnode, unit, name_hint=hint)
    else:
        base = prog.resolve_base(tnode, unit)
    for d in decls:
        t, name = prog.apply_declarator(d, base, unit)
        if name is not None:
            prog.typedefs[node_text(name)] = t


def _knr(nodes: list[Node], i: int) -> int:
    node = nodes[i]
    if node.type != "declaration":
        return 0
    init = node.child_by_field_name("declarator")
    if init is None or init.type != "init_declarator":
        return 0
    args = init.child_by_field_name("value")
    if args is None or args.type != "argument_list" or not all(a.type == "identifier" for a in args.named_children):
        return 0
    j = i + 1
    while j < len(nodes) and nodes[j].type == "declaration":
        j += 1
    if j >= len(nodes) or nodes[j].type != "compound_statement":
        return 0
    return j - i + 1


@dataclass
class _KnrNode:
    """Stand-in for a function_definition node assembled from K&R pieces."""

    head: Node
    body: Node

    def child_by_field_name(self, name):
        return self.body if name == "body" else self.head.child_by_field_name(name)

    @property
    def start_point(self):
        return self.head.start_point

    @property
    def children(self):
        return self.head.children


def _add_knr(prog: Program, nodes: list[Node], i: int, count: int, unit: Unit) -> None:
    head = nodes[i]
    init = head.child_by_field_name("declarator")
    ret_base = prog.resolve_base(head.child_by_field_name("type"), unit)
    ret, name_node = prog.apply_declarator(init.child_by_field_name("declarator"), ret_base, unit)
    arg_names = [node_text(a) for a in init.child_by_field_name("value").named_children]
    types: dict[str, CType] = {}
    for d in nodes[i + 1 : i + count - 1]:
        base = prog.resolve_base(d.child_by_field_name("type"), unit)
        for dd in d.children_by_field_name("declarator"):
            t, n = prog.apply_declarator(dd, base, unit)
            if n is not None:
                if isinstance(t, ArrayType):
                    t = PointerType(t.elem)
                types[node_text(n)] = t
    name = node_text(name_node)
    storage = [node_text(c) for c in head.children if c.type == "storage_class_specifier"]
    fn = FunctionDef(
        name=name,
        qualname=name,
        unit=unit,
        node=_KnrNode(head, nodes[i + count - 1]),  # type: ignore[arg-type]
        ret=ret,
        params=[(a, types.get(a, prog.ints["int"])) for a in arg_names],
        static="static" in storage,
    )
    if fn.static:
        prog.statics.setdefault(unit.index, {})[name] = fn
    else:
        prog.functions.setdefault(name, []).append(fn)


def build_program(
    source_files: list[Path],
    profile: Profile,
    include_dirs: list[Path] | None = None,
    defines: dict[str, str] | None = None,
) -> Program:
    """Preprocess + parse every translation unit and collect the program model."""
    prog = Program(profile, make_int_types(profile.type_sizes))
    tus = [p for p in source_files if p.suffix.lower() in TU_EXTENSIONS]
    if not tus:  # headers only: treat each as its own unit
        tus = list(source_files)
    dirs = list(include_dirs or [])
    for p in source_files:
        if p.parent not in dirs:
            dirs.append(p.parent)
    for idx, path in enumerate(tus):
        pre = preprocess(path, profile, dirs, defines)
        for inc in pre.missing_includes:
            prog.warnings.append((path, None, f"Include file not found: {inc} (its declarations will be guessed)"))
        for err in pre.errors:
            prog.warnings.append((path, None, f"Preprocessor: {err}"))
        for name, value in pre.macros.items():
            prog.macro_consts.setdefault(name, value)
        tree = _parser().parse(pre.text.encode("utf-8"))
        unit = Unit(idx, pre, tree)
        prog.units.append(unit)
        _walk_top(prog, tree.root_node.children, unit)
    # extern declarations with no definition anywhere: give them storage anyway
    for name, g in prog._extern_only.items():
        if not any(x.name == name for x in prog.globals):
            prog.globals.append(g)
            prog.warnings.append((g.location[0], g.location[1], f"'{name}' is declared extern but never defined; it starts at 0."))
    return prog
