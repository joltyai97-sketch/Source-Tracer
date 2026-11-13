"""Parse C / C++ source with tree-sitter (error tolerant)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser, Tree

from source_tracer.frontend.dialect import BlankedKeyword, blank_keywords

C_EXTENSIONS = {".c", ".h"}
CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ino", ".pde"}
SOURCE_EXTENSIONS = C_EXTENSIONS | CPP_EXTENSIONS
ASM_EXTENSIONS = {".s", ".asm", ".a68", ".sa", ".src", ".inc"}


@lru_cache(maxsize=1)
def _parser() -> Parser:
    # The C++ grammar is a superset of C that is good enough for both, and
    # lets mixed C/C++ projects share one symbol model.
    return Parser(Language(tree_sitter_cpp.language()))


@dataclass
class ParseIssue:
    line: int  # 1-based
    column: int
    message: str


@dataclass
class ParsedFile:
    path: Path
    text: str  # original text, shown to the user
    parsed_text: str  # text after dialect blanking, what the parser saw
    tree: Tree
    blanked: list[BlankedKeyword] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def root(self) -> Node:
        return self.tree.root_node

    def keywords_on_lines(self, first: int, last: int) -> set[str]:
        """Blanked compiler keywords found between 1-based lines ``first``..``last``."""
        return {b.keyword for b in self.blanked if first <= b.line <= last}


def read_source(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Old firmware is frequently Latin-1 / Windows-1252.
        return data.decode("latin-1")


def node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def _collect_issues(root: Node, lines: list[str]) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if not node.has_error:
            continue
        if node.is_missing:
            row, col = node.start_point
            issues.append(ParseIssue(row + 1, col, f"Expected '{node.type}' here"))
            continue
        if node.type == "ERROR":
            row, col = node.start_point
            snippet = lines[row].strip() if row < len(lines) else ""
            issues.append(
                ParseIssue(row + 1, col, f"Could not fully understand this code: {snippet[:80]}")
            )
            continue  # don't report nested errors inside the same region
        stack.extend(reversed(node.children))
    issues.sort(key=lambda i: (i.line, i.column))
    return issues


def parse_text(text: str, path: Path | str = "<memory>", compiler_keywords: list[str] | None = None) -> ParsedFile:
    parsed_text, blanked = blank_keywords(text, compiler_keywords or [])
    tree = _parser().parse(parsed_text.encode("utf-8"))
    issues = _collect_issues(tree.root_node, text.splitlines())
    return ParsedFile(Path(path), text, parsed_text, tree, blanked, issues)


def parse_file(path: Path, compiler_keywords: list[str] | None = None) -> ParsedFile:
    return parse_text(read_source(path), path, compiler_keywords)
