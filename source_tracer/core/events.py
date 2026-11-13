"""Things that happen during a run, recorded for the GUI and analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Event:
    step: int
    file: Path | None
    line: int | None
    function: str


@dataclass
class VarWrite(Event):
    name: str
    old: object
    new: object
    text: str = ""  # display form of the new value


@dataclass
class CallEvent(Event):
    callee: str
    args: list[str]
    kind: str = "function"  # function | stub | builtin | isr
    raw: list = field(default_factory=list)  # argument values (ints/floats/pointers)


@dataclass
class ReturnEvent(Event):
    callee: str
    value: str


@dataclass
class RegisterAccess(Event):
    kind: str  # "read" | "write"
    register: str
    address: int
    value: int
    size: int


@dataclass
class Transmit(Event):
    channel: str
    register: str
    address: int
    value: int
    size: int  # bytes


@dataclass
class OutputText(Event):
    channel: str
    text: str


@dataclass
class RunWarning(Event):
    message: str


@dataclass
class RunResult:
    function: str
    stop_reason: str  # "returned" | "step_limit" | "error" | "exit"
    message: str = ""
    return_value: object = None
    return_text: str = ""
    steps: int = 0
    events: list[Event] = field(default_factory=list)
    globals_before: dict[str, str] = field(default_factory=dict)
    globals_after: dict[str, str] = field(default_factory=dict)
    coverage: dict[tuple[Path, int], int] = field(default_factory=dict)  # (file, line) -> times executed
    last_location: tuple[Path, int] | None = None
    raw_globals_after: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.stop_reason in ("returned", "exit")

    def of(self, cls) -> list:
        return [e for e in self.events if isinstance(e, cls)]

    @property
    def transmitted(self) -> list[Transmit]:
        return self.of(Transmit)

    @property
    def changed_globals(self) -> dict[str, tuple[str, str]]:
        return {
            k: (self.globals_before.get(k, ""), v)
            for k, v in self.globals_after.items()
            if self.globals_before.get(k) != v
        }
