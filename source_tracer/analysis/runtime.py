"""Interpret a run's events in terms of fault codes and transmitted data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from source_tracer.analysis.faults import FaultCode, FaultReport
from source_tracer.core.events import CallEvent, RunResult, Transmit, VarWrite


@dataclass
class FaultEvent:
    step: int
    file: Path | None
    line: int | None
    function: str
    action: str  # "raised" | "cleared"
    code: str  # fault code name, or a number when no name matches
    value: int | None
    via: str  # variable or function that carried it
    detail: str = ""


@dataclass
class FaultRunSummary:
    events: list[FaultEvent] = field(default_factory=list)
    final_state: dict[str, str] = field(default_factory=dict)  # store variable -> decoded value
    active: list[str] = field(default_factory=list)  # fault codes active at the end

    @property
    def raised(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.events:
            if e.action == "raised":
                seen.setdefault(e.code)
        return list(seen)


@dataclass
class ChannelData:
    channel: str
    values: list[int]
    sizes: list[int]
    events: list[Transmit]

    @property
    def hex(self) -> str:
        return " ".join(f"{v:0{max(s, 1) * 2}X}" for v, s in zip(self.values, self.sizes))

    @property
    def ascii(self) -> str:
        return "".join(chr(v) if 32 <= v < 127 else "." for v in self.values)


def _root(name: str) -> str:
    return re.split(r"[.\[]", name, maxsplit=1)[0]


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _is_none(label: str) -> bool:
    return re.search(r"(?i)(none|ok|clear|no_?fault|no_?error)$", label) is not None


def summarize_faults(result: RunResult, report: FaultReport) -> FaultRunSummary:
    codes = [c for c in report.codes if c.value is not None]
    stores = {s.symbol.name: s for s in report.sinks if s.role == "store"}
    funcs = {s.symbol.name: s for s in report.sinks if s.role in ("reporter", "clearer")}

    def codes_for(target: str, kind: str | None = None) -> list[FaultCode]:
        used = [c for c in codes if any(u.target == target for u in c.usages)]
        pool = used or codes
        return [c for c in pool if kind is None or c.kind == kind]

    def name_of(value: int, target: str, kind: str | None = None) -> str | None:
        for pool in (codes_for(target, kind), codes):
            for c in pool:
                if c.value == value and (kind is None or c.kind == kind):
                    return c.name
        return None

    def bit_names(mask: int, target: str) -> list[tuple[str, int]]:
        out = []
        used = [c for c in codes_for(target, "bit") if c.value]
        bit_codes = used + [c for c in codes if c.kind == "bit" and c.value and c not in used]
        remaining = mask
        for c in sorted(bit_codes, key=lambda c: -bin(c.value).count("1")):
            if c.value and remaining & c.value == c.value:
                out.append((c.name, c.value))
                remaining &= ~c.value
        b = 0
        while remaining:
            if remaining & 1:
                out.append((f"bit {b}", 1 << b))
            remaining >>= 1
            b += 1
        return out

    summary = FaultRunSummary()
    bit_stores = {name for name in stores if codes_for(name, "bit") and any(c.kind == "bit" for c in codes_for(name))}
    for ev in result.events:
        if isinstance(ev, VarWrite):
            root = _root(ev.name)
            if root not in stores:
                continue
            new, old = _int(ev.new), _int(ev.old)
            if new is None or new == old:
                continue
            if root in bit_stores and old is not None:
                for name, val in bit_names(new & ~old, root):
                    summary.events.append(FaultEvent(ev.step, ev.file, ev.line, ev.function, "raised", name, val, ev.name, f"bit set in {ev.name}"))
                for name, val in bit_names(old & ~new, root):
                    summary.events.append(FaultEvent(ev.step, ev.file, ev.line, ev.function, "cleared", name, val, ev.name, f"bit cleared in {ev.name}"))
                continue
            label = name_of(new, root, "code") or name_of(new, root)
            none = new == 0 or (label is not None and _is_none(label))
            if none:
                prev = name_of(old, root, "code") or name_of(old, root) if old else None
                if old is not None and (old == 0 or (prev and _is_none(prev))):
                    continue
                summary.events.append(
                    FaultEvent(ev.step, ev.file, ev.line, ev.function, "cleared", prev or (f"0x{old:X}" if old else "?"), old, ev.name, f"{ev.name} = {ev.text}")
                )
            else:
                summary.events.append(
                    FaultEvent(ev.step, ev.file, ev.line, ev.function, "raised", label or f"0x{new:X}", new, ev.name, f"{ev.name} = {ev.text}")
                )
        elif isinstance(ev, CallEvent):
            short = ev.callee.rpartition("::")[2]
            sink = funcs.get(short)
            if sink is None:
                continue
            action = "cleared" if sink.role == "clearer" else "raised"
            if action == "cleared" and stores:
                continue  # clears are reported from the fault variables, which show if anything changed
            named = False
            for v in ev.raw:
                iv = _int(v)
                if iv is None:
                    continue
                label = name_of(iv, short, "code") or name_of(iv, short)
                if label is None:
                    continue
                named = True
                summary.events.append(FaultEvent(ev.step, ev.file, ev.line, ev.function, action, label, iv, f"{short}()", f"{short}({', '.join(ev.args)})"))
            if not named and ev.raw:
                iv = _int(ev.raw[0])
                summary.events.append(
                    FaultEvent(ev.step, ev.file, ev.line, ev.function, action, f"0x{iv:X}" if iv is not None else "?", iv, f"{short}()", f"{short}({', '.join(ev.args)})")
                )

    for name in stores:
        text = result.globals_after.get(name)
        if text is None:
            continue
        summary.final_state[name] = text
        raw = result.raw_globals_after.get(name)
        value = getattr(raw, "value", None)
        if not isinstance(value, int):
            continue
        if name in bit_stores:
            summary.active.extend(n for n, _ in bit_names(value, name))
        elif value:
            label = name_of(value, name, "code") or name_of(value, name)
            if label is None or not _is_none(label):
                summary.active.append(label or f"{name}=0x{value:X}")
    return summary


def transmitted_by_channel(result: RunResult) -> list[ChannelData]:
    channels: dict[str, ChannelData] = {}
    for t in result.transmitted:
        ch = channels.setdefault(t.channel, ChannelData(t.channel, [], [], []))
        ch.values.append(t.value)
        ch.sizes.append(t.size if t.value > 0xFF else 1)
        ch.events.append(t)
    return list(channels.values())
