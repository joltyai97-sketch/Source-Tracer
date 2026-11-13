"""Extract a symbol table (functions, types, variables, constants, registers) from parsed files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from source_tracer.frontend.consteval import evaluate
from source_tracer.frontend.parser import ParsedFile, node_text
from source_tracer.profile import Profile, Register

# Symbol kinds
FUNCTION = "function"
ISR = "isr"
METHOD = "method"
PROTOTYPE = "prototype"  # declared but no body seen (may be in assembly / a library)
CLASS = "class"
STRUCT = "struct"
UNION = "union"
ENUM = "enum"
ENUMERATOR = "enumerator"
TYPEDEF = "typedef"
VARIABLE = "variable"
FIELD = "field"
MACRO = "macro"
FUNCTION_MACRO = "function_macro"
REGISTER = "register"
REGISTER_BLOCK = "register_block"

_NAME_NODES = {
    "identifier",
    "field_identifier",
    "type_identifier",
    "qualified_identifier",
    "destructor_name",
    "operator_name",
    "namespace_identifier",
    "template_function",
}
_CONTAINERS = {
    "preproc_if",
    "preproc_ifdef",
    "preproc_else",
    "preproc_elif",
    "preproc_elifdef",
    "linkage_specification",
    "declaration_list",
}
_RECORD_KINDS = {"class_specifier": CLASS, "struct_specifier": STRUCT, "union_specifier": UNION}

_POINTER_CAST = re.compile(
    r"\(\s*(?:const\s+|volatile\s+|struct\s+|union\s+|unsigned\s+|signed\s+)*[A-Za-z_][\w\s]*?\*\s*(?:const\s*|volatile\s*)*\)"
)


@dataclass
class Symbol:
    kind: str
    name: str
    file: Path
    line: int  # 1-based
    end_line: int
    qualified_name: str = ""
    type: str = ""  # variable/field type, function return type
    signature: str = ""  # full declaration text for functions
    value: str | None = None  # initializer / macro body / enumerator expression
    int_value: int | None = None  # evaluated integer value, when known
    storage: list[str] = field(default_factory=list)  # static, extern, volatile, const...
    parent: str | None = None  # enclosing class/enum
    address: int | None = None  # registers
    register: Register | None = None  # matching profile register
    parameters: list[tuple[str, str]] = field(default_factory=list)  # (type, name)
    conditional: bool = False  # inside #if/#ifdef
    children: list[Symbol] = field(default_factory=list)
    node: Node | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.qualified_name:
            self.qualified_name = self.name

    @property
    def is_callable(self) -> bool:
        return self.kind in (FUNCTION, ISR, METHOD, PROTOTYPE)


@dataclass
class SymbolTable:
    symbols: list[Symbol] = field(default_factory=list)

    def of_kind(self, *kinds: str) -> list[Symbol]:
        return [s for s in self.symbols if s.kind in kinds]

    def named(self, name: str) -> list[Symbol]:
        """Symbols whose short or qualified name is ``name`` (indexed; rebuilt if the list changes)."""
        key = (id(self.symbols), len(self.symbols))
        if getattr(self, "_index_key", None) != key:
            index: dict[str, list[Symbol]] = {}
            for s in self.symbols:
                index.setdefault(s.name, []).append(s)
                if s.qualified_name != s.name:
                    index.setdefault(s.qualified_name, []).append(s)
            self._index, self._index_key = index, key
        return list(self._index.get(name, ()))

    def constants(self) -> dict[str, int]:
        """Name -> integer value for every macro / enumerator with a known value."""
        return {
            s.name: s.int_value
            for s in self.symbols
            if s.kind in (MACRO, ENUMERATOR) and s.int_value is not None
        }

    def function_at(self, file: Path, line: int) -> Symbol | None:
        best = None
        for s in self.symbols:
            if s.kind in (FUNCTION, ISR, METHOD) and s.file == file and s.line <= line <= s.end_line:
                if best is None or s.line >= best.line:
                    best = s
        return best


# --------------------------------------------------------------------------- helpers


def declarator_name(node: Node | None) -> Node | None:
    """Follow a declarator chain down to the node holding the declared name."""
    while node is not None:
        if node.type in _NAME_NODES:
            return node
        inner = node.child_by_field_name("declarator")
        if inner is None:
            # parenthesized_declarator and friends have a single named child
            named = [c for c in node.named_children if c.type not in ("type_qualifier", "attribute_specifier")]
            inner = named[0] if node.type == "parenthesized_declarator" and named else None
        node = inner
    return None


def _decl_suffix(node: Node | None) -> tuple[str, str]:
    """Return (prefix, suffix) type decorations (``*``, ``&``, ``[N]``) from a declarator chain."""
    prefix, suffix = "", ""
    while node is not None and node.type not in _NAME_NODES:
        if node.type == "pointer_declarator":
            quals = " ".join(node_text(c) for c in node.children if c.type == "type_qualifier")
            prefix += "*" + (f" {quals} " if quals else "")
        elif node.type == "reference_declarator":
            prefix += "&"
        elif node.type == "array_declarator":
            size = node.child_by_field_name("size")
            suffix += f"[{node_text(size) if size else ''}]"
        elif node.type == "function_declarator":
            break
        node = node.child_by_field_name("declarator") or (
            node.named_children[0] if node.type == "parenthesized_declarator" and node.named_children else None
        )
    return prefix.strip(), suffix


def _function_declarator(node: Node | None) -> Node | None:
    while node is not None:
        if node.type == "function_declarator":
            return node
        node = node.child_by_field_name("declarator")
    return None


def _specifiers(node: Node) -> list[str]:
    out = []
    for c in node.children:
        if c.type in ("storage_class_specifier", "type_qualifier", "virtual", "explicit"):
            out.append(node_text(c))
        elif c.type == "function_specifier" or c.type == "inline":
            out.append(node_text(c))
    return out


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _end_line(node: Node) -> int:
    row, col = node.end_point
    # Preprocessor nodes include their trailing newline; don't count the next line.
    if col == 0 and row > node.start_point[0]:
        return row
    return row + 1


def _parameters(func_decl: Node) -> list[tuple[str, str]]:
    params = func_decl.child_by_field_name("parameters")
    out: list[tuple[str, str]] = []
    if params is None:
        return out
    for p in params.named_children:
        if p.type in ("parameter_declaration", "optional_parameter_declaration"):
            type_node = p.child_by_field_name("type")
            decl = p.child_by_field_name("declarator")
            name_node = declarator_name(decl)
            pre, suf = _decl_suffix(decl)
            quals = " ".join(_specifiers(p))
            t = _collapse(f"{quals} {node_text(type_node) if type_node else ''} {pre}{suf}")
            if t == "void" and name_node is None:
                continue
            out.append((t, node_text(name_node) if name_node else ""))
        elif p.type == "variadic_parameter":
            out.append(("...", ""))
        elif p.type == "identifier":  # K&R identifier list
            out.append(("int", node_text(p)))
    return out


def register_address(text: str, names: dict[str, int], pointer_macros: dict[str, str]) -> tuple[int, bool] | None:
    """If a macro body is a memory-mapped access, return (address, is_dereference).

    Recognises ``(*(volatile unsigned short *)0xFFFC0E)``, ``*(vu16*)(BASE + 0x0E)``,
    ``((qsm_t *)0xFFFC00)`` and wrapper macros such as ``REG16(0xFFFC0E)``.
    """
    body = _collapse(text)
    m = _POINTER_CAST.search(body)
    if m:
        rest = body[m.end():].lstrip()
        expr = _leading_operand(rest)
        addr = evaluate(expr, names) if expr else None
        if addr is not None and addr >= 0x100:
            deref = bool(re.match(r"^\(*\s*\*", body))
            return addr, deref
        return None
    m = re.match(r"^\(*\s*([A-Za-z_]\w*)\s*\((.*)\)\s*\)*$", body)
    if m and m.group(1) in pointer_macros:
        addr = evaluate(m.group(2), names)
        if addr is not None and addr >= 0x100:
            return addr, pointer_macros[m.group(1)].lstrip("( ").startswith("*")
    return None


def _is_include_guard(node: Node) -> bool:
    """``#ifndef X`` immediately followed by ``#define X`` (the usual header guard)."""
    if node.type != "preproc_ifdef" or not node_text(node).lstrip().startswith("#ifndef"):
        return False
    name = node.child_by_field_name("name")
    body = [c for c in node.named_children if c != name and c.type != "comment"]
    if name is None or not body or body[0].type != "preproc_def":
        return False
    return node_text(body[0].child_by_field_name("name")) == node_text(name)


def _leading_operand(text: str) -> str:
    """Return the first operand of ``text``: a balanced (...) group or a single token."""
    if text.startswith("("):
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0:
                return text[: i + 1]
        return ""
    m = re.match(r"[\w]+", text)
    return m.group(0) if m else ""


# --------------------------------------------------------------------------- extractor


class _Extractor:
    def __init__(self, parsed: ParsedFile, profile: Profile, known_constants: dict[str, int]):
        self.parsed = parsed
        self.profile = profile
        self.names = dict(known_constants)
        self.pointer_macros: dict[str, str] = {}
        self.out: list[Symbol] = []

    # -- entry
    def run(self) -> list[Symbol]:
        self._block(self.parsed.root.children, scope="", conditional=False)
        return self.out

    def _sym(self, kind: str, name: str, node: Node, **kw) -> Symbol:
        sym = Symbol(kind, name, self.parsed.path, _line(node), _end_line(node), node=node, **kw)
        self.out.append(sym)
        return sym

    # -- traversal
    def _block(self, children: list[Node], scope: str, conditional: bool) -> None:
        i = 0
        while i < len(children):
            node = children[i]
            consumed = self._knr_function(children, i, scope, conditional)
            if consumed:
                i += consumed
                continue
            self._node(node, scope, conditional)
            i += 1

    def _node(self, node: Node, scope: str, conditional: bool) -> None:
        t = node.type
        if t in _CONTAINERS:
            cond = conditional or (t.startswith("preproc_") and not _is_include_guard(node))
            children = node.named_children
            if t.startswith("preproc_if"):
                # Skip the condition itself (the identifier / expression after #if).
                cond_node = node.child_by_field_name("name") or node.child_by_field_name("condition")
                children = [c for c in children if c != cond_node]
            self._block(children, scope, cond)
        elif t == "namespace_definition":
            name_node = node.child_by_field_name("name")
            ns = node_text(name_node) if name_node else ""
            body = node.child_by_field_name("body")
            if body is not None:
                self._block(body.named_children, f"{scope}{ns}::" if ns else scope, conditional)
        elif t == "template_declaration":
            for c in node.named_children:
                if c.type not in ("template_parameter_list",):
                    self._node(c, scope, conditional)
        elif t == "preproc_def":
            self._macro(node, conditional)
        elif t == "preproc_function_def":
            self._function_macro(node, conditional)
        elif t == "function_definition":
            self._function(node, scope, conditional)
        elif t == "declaration":
            self._declaration(node, scope, conditional)
        elif t == "type_definition":
            self._typedef(node, scope, conditional)
        elif t in _RECORD_KINDS or t == "enum_specifier":
            self._type_specifier(node, scope, conditional)
        elif t == "expression_statement" or t == "ERROR":
            # Parser recovery sometimes wraps declarations; look one level down.
            for c in node.named_children:
                if c.type in ("function_definition", "declaration", "preproc_def"):
                    self._node(c, scope, conditional)

    # -- macros
    def _macro(self, node: Node, conditional: bool) -> None:
        name = node_text(node.child_by_field_name("name"))
        value_node = node.child_by_field_name("value")
        value = _collapse(node_text(value_node)) if value_node else ""
        reg = register_address(value, self.names, self.pointer_macros) if value else None
        if reg is not None:
            address, deref = reg
            profile_reg = self.profile.register_at(address)
            kind = REGISTER if deref else REGISTER_BLOCK
            self._sym(kind, name, node, value=value, address=address, register=profile_reg, conditional=conditional)
            return
        int_value = evaluate(value, self.names) if value else None
        if int_value is not None:
            self.names[name] = int_value
        self._sym(MACRO, name, node, value=value, int_value=int_value, conditional=conditional)

    def _function_macro(self, node: Node, conditional: bool) -> None:
        name = node_text(node.child_by_field_name("name"))
        params = node.child_by_field_name("parameters")
        value_node = node.child_by_field_name("value")
        value = _collapse(node_text(value_node)) if value_node else ""
        if _POINTER_CAST.search(value):
            self.pointer_macros[name] = value
        self._sym(
            FUNCTION_MACRO,
            name,
            node,
            value=value,
            signature=f"#define {name}{node_text(params) if params else '()'}",
            conditional=conditional,
        )

    # -- functions
    def _function(self, node: Node, scope: str, conditional: bool, parent: str | None = None) -> None:
        decl = node.child_by_field_name("declarator")
        fdecl = _function_declarator(decl)
        name_node = declarator_name(fdecl.child_by_field_name("declarator") if fdecl else decl)
        if name_node is None:
            return
        full_name = node_text(name_node)
        type_node = node.child_by_field_name("type")
        ret_pre, _ = _decl_suffix(decl)
        ret_type = _collapse(f"{' '.join(_specifiers(node))} {node_text(type_node) if type_node else ''} {ret_pre}")
        body = node.child_by_field_name("body")
        sig_end = body.start_byte - node.start_byte if body is not None else None
        signature = _collapse(node_text(node)[:sig_end] if sig_end else node_text(node))
        # The original (un-blanked) signature, so the user sees `interrupt void f()`.
        orig_lines = self.parsed.text.splitlines()[_line(node) - 1 : (_line(body) if body else _line(node))]
        if orig_lines:
            orig = "\n".join(orig_lines)
            brace = orig.find("{")
            signature = _collapse(orig[:brace] if brace >= 0 else orig)

        if "::" in full_name:
            cls, _, short = full_name.rpartition("::")
            kind, parent_name, name = METHOD, f"{scope}{cls}", short
        else:
            kind, parent_name, name = (METHOD if parent else FUNCTION), parent, full_name
        keywords = self.parsed.keywords_on_lines(_line(node), _line(body) if body else _line(node))
        if kind == FUNCTION and (keywords & set(self.profile.isr_keywords) or self.profile.is_isr_name(name)):
            kind = ISR
        qualified = f"{parent_name}::{name}" if parent_name else f"{scope}{name}"
        self._sym(
            kind,
            name,
            node,
            qualified_name=qualified,
            type=ret_type,
            signature=signature,
            parent=parent_name,
            parameters=_parameters(fdecl) if fdecl else [],
            storage=_specifiers(node),
            conditional=conditional,
        )

    def _knr_function(self, children: list[Node], i: int, scope: str, conditional: bool) -> int:
        """Detect K&R definitions: ``int f(a, b) int a; int b; { ... }`` (parsed as loose pieces)."""
        node = children[i]
        if node.type != "declaration":
            return 0
        init = node.child_by_field_name("declarator")
        if init is None or init.type != "init_declarator":
            return 0
        args = init.child_by_field_name("value")
        if args is None or args.type != "argument_list":
            return 0
        if not all(a.type == "identifier" for a in args.named_children):
            return 0
        j = i + 1
        while j < len(children) and children[j].type == "declaration":
            j += 1
        if j >= len(children) or children[j].type != "compound_statement":
            return 0
        name = node_text(declarator_name(init.child_by_field_name("declarator")))
        arg_names = [node_text(a) for a in args.named_children]
        arg_types = {}
        for d in children[i + 1 : j]:
            t = node_text(d.child_by_field_name("type"))
            for c in d.children_by_field_name("declarator"):
                pre, suf = _decl_suffix(c)
                arg_types[node_text(declarator_name(c))] = _collapse(f"{t} {pre}{suf}")
        body = children[j]
        type_node = node.child_by_field_name("type")
        ret = _collapse(f"{' '.join(_specifiers(node))} {node_text(type_node) if type_node else 'int'}")
        sym = Symbol(
            ISR if self.profile.is_isr_name(name) else FUNCTION,
            name,
            self.parsed.path,
            _line(node),
            _end_line(body),
            qualified_name=f"{scope}{name}",
            type=ret,
            signature=_collapse(f"{ret} {name}(" + ", ".join(arg_types.get(a, "int") + " " + a for a in arg_names) + ")"),
            parameters=[(arg_types.get(a, "int"), a) for a in arg_names],
            storage=_specifiers(node) + ["K&R"],
            conditional=conditional,
            node=body,
        )
        self.out.append(sym)
        return j - i + 1

    # -- declarations
    def _declaration(self, node: Node, scope: str, conditional: bool, parent: str | None = None) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None and (type_node.type in _RECORD_KINDS or type_node.type == "enum_specifier"):
            self._type_specifier(type_node, scope, conditional)
        storage = _specifiers(node)
        base_type = node_text(type_node) if type_node is not None else ""
        if type_node is not None and type_node.child_by_field_name("body") is not None:
            # struct {..} x;  -> show "struct Name" rather than the whole body
            tag = type_node.child_by_field_name("name")
            base_type = f"{type_node.type.split('_')[0]} {node_text(tag) if tag else '(anonymous)'}"
        for decl in node.children_by_field_name("declarator"):
            value = None
            target = decl
            if decl.type == "init_declarator":
                v = decl.child_by_field_name("value")
                value = _collapse(node_text(v)) if v is not None else None
                target = decl.child_by_field_name("declarator")
            fdecl = _function_declarator(target)
            name_node = declarator_name(fdecl.child_by_field_name("declarator") if fdecl else target)
            if name_node is None:
                continue
            name = node_text(name_node)
            pre, suf = _decl_suffix(target)
            if fdecl is not None:
                ret = _collapse(f"{' '.join(storage)} {base_type} {pre}")
                self._sym(
                    PROTOTYPE,
                    name,
                    node,
                    qualified_name=f"{parent}::{name}" if parent else f"{scope}{name}",
                    type=ret,
                    signature=_collapse(node_text(node).rstrip(";")),
                    parameters=_parameters(fdecl),
                    storage=storage,
                    parent=parent,
                    conditional=conditional,
                )
                continue
            full_type = _collapse(f"{' '.join(q for q in storage if q in ('const', 'volatile'))} {base_type} {pre}{suf}")
            int_value = evaluate(value, self.names) if value else None
            self._sym(
                FIELD if parent else VARIABLE,
                name,
                node,
                qualified_name=f"{parent}::{name}" if parent else f"{scope}{name}",
                type=full_type,
                value=value,
                int_value=int_value,
                storage=storage,
                parent=parent,
                conditional=conditional,
            )

    def _typedef(self, node: Node, scope: str, conditional: bool) -> None:
        type_node = node.child_by_field_name("type")
        names = [declarator_name(d) for d in node.children_by_field_name("declarator")]
        names = [node_text(n) for n in names if n is not None]
        alias = names[0] if names else None
        if type_node is not None and (type_node.type in _RECORD_KINDS or type_node.type == "enum_specifier"):
            self._type_specifier(type_node, scope, conditional, alias=alias)
        quals = " ".join(q for q in _specifiers(node) if q in ("const", "volatile"))
        anonymous_record = (
            type_node is not None
            and type_node.child_by_field_name("body") is not None
            and type_node.child_by_field_name("name") is None
        )
        for idx, decl in enumerate(node.children_by_field_name("declarator")):
            n = declarator_name(decl)
            if n is None:
                continue
            pre, suf = _decl_suffix(decl)
            if anonymous_record and idx == 0 and not pre and not suf:
                continue  # `typedef enum {..} name;` -- the record already carries the name
            base = node_text(type_node) if type_node is not None else ""
            if type_node is not None and type_node.child_by_field_name("body") is not None:
                tag = type_node.child_by_field_name("name")
                base = f"{type_node.type.split('_')[0]} {node_text(tag) if tag else '(anonymous)'}"
            self._sym(
                TYPEDEF,
                node_text(n),
                node,
                qualified_name=f"{scope}{node_text(n)}",
                type=_collapse(f"{quals} {base} {pre}{suf}"),
                conditional=conditional,
            )

    def _type_specifier(self, node: Node, scope: str, conditional: bool, alias: str | None = None) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            return
        name_node = node.child_by_field_name("name")
        name = node_text(name_node) if name_node else (alias or "(anonymous)")
        qualified = f"{scope}{name}"
        if node.type == "enum_specifier":
            sym = self._sym(ENUM, name, node, qualified_name=qualified, conditional=conditional)
            next_value: int | None = 0
            for e in body.named_children:
                if e.type != "enumerator":
                    continue
                ename = node_text(e.child_by_field_name("name"))
                v = e.child_by_field_name("value")
                expr = _collapse(node_text(v)) if v is not None else None
                value = evaluate(expr, self.names) if expr else next_value
                if value is not None:
                    self.names[ename] = value
                next_value = value + 1 if value is not None else None
                child = self._sym(ENUMERATOR, ename, e, qualified_name=ename, parent=name, value=expr, int_value=value, conditional=conditional)
                sym.children.append(child)
            return
        kind = _RECORD_KINDS[node.type]
        sym = self._sym(kind, name, node, qualified_name=qualified, conditional=conditional)
        start = len(self.out)
        for member in body.named_children:
            if member.type in ("field_declaration", "declaration"):
                self._declaration(member, scope, conditional, parent=qualified)
            elif member.type == "function_definition":
                self._function(member, scope, conditional, parent=qualified)
            elif member.type == "template_declaration":
                for c in member.named_children:
                    if c.type == "function_definition":
                        self._function(c, scope, conditional, parent=qualified)
            elif member.type in _RECORD_KINDS or member.type == "enum_specifier":
                self._type_specifier(member, f"{qualified}::", conditional)
        sym.children.extend(s for s in self.out[start:] if s.parent == qualified)


def extract_symbols(files: list[ParsedFile], profile: Profile) -> SymbolTable:
    """Build a symbol table across all files.

    Headers are processed first so constants they define are known when
    evaluating values in source files.
    """
    ordered = sorted(files, key=lambda f: (f.path.suffix.lower() not in (".h", ".hpp", ".hh", ".hxx"), str(f.path)))
    table = SymbolTable()
    constants: dict[str, int] = {}
    pointer_macros: dict[str, str] = {}
    for _ in range(2):  # second pass resolves constants defined in later files
        table = SymbolTable()
        for pf in ordered:
            ex = _Extractor(pf, profile, constants)
            ex.pointer_macros = pointer_macros
            table.symbols.extend(ex.run())
            constants.update(ex.names)
    _mark_prototypes_with_bodies(table)
    return table


def _mark_prototypes_with_bodies(table: SymbolTable) -> None:
    """Drop prototypes / extern declarations whose definition exists; keep the rest (e.g. assembly functions)."""
    defined = {s.qualified_name for s in table.symbols if s.kind in (FUNCTION, ISR, METHOD)}
    defined_vars = {
        s.qualified_name for s in table.symbols if s.kind == VARIABLE and "extern" not in s.storage
    }
    seen: set[str] = set()
    kept = []
    for s in table.symbols:
        if s.kind == VARIABLE and "extern" in s.storage:
            if s.qualified_name in defined_vars or s.qualified_name in seen:
                continue
            seen.add(s.qualified_name)
        if s.kind == PROTOTYPE:
            if s.parent is not None:
                # member function declared in a class; keep only if never defined
                if s.qualified_name in defined:
                    continue
            elif s.qualified_name in defined:
                continue
            if s.qualified_name in seen:
                continue
            seen.add(s.qualified_name)
        kept.append(s)
    table.symbols = kept
    # Re-sync class children so they don't point at removed prototypes.
    ids = {id(s) for s in kept}
    for s in kept:
        s.children = [c for c in s.children if id(c) in ids]
