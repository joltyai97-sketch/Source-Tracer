"""Target profiles: chip-specific facts (register map, type sizes, compiler dialect).

Profiles are YAML files so new microcontrollers can be added without code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).parent / "profiles"


@dataclass(frozen=True)
class Register:
    name: str
    address: int
    size: int = 1  # bytes per element
    count: int = 1  # >1 for register arrays / RAM blocks
    module: str = ""
    description: str = ""
    role: str = "plain"  # plain | tx | rx | status
    channel: str = ""
    rx_channel: str = ""
    default: int = 0

    @property
    def end(self) -> int:
        """One past the last byte covered by this register."""
        return self.address + self.size * self.count

    def contains(self, address: int) -> bool:
        return self.address <= address < self.end


@dataclass
class Profile:
    name: str
    description: str = ""
    endianness: str = "little"
    address_bits: int = 32
    max_align: int = 4  # largest alignment the compiler uses for struct fields
    type_sizes: dict[str, int] = field(default_factory=dict)
    compiler_keywords: list[str] = field(default_factory=list)
    isr_keywords: list[str] = field(default_factory=list)
    isr_name_patterns: list[re.Pattern] = field(default_factory=list)
    registers: list[Register] = field(default_factory=list)
    source: Path | None = None

    def register_at(self, address: int) -> Register | None:
        """Return the register covering ``address`` (after masking to the address width)."""
        address &= (1 << self.address_bits) - 1
        for reg in self.registers:
            if reg.contains(address):
                return reg
        return None

    def register_named(self, name: str) -> Register | None:
        for reg in self.registers:
            if reg.name == name:
                return reg
        return None

    def is_isr_name(self, name: str) -> bool:
        return any(p.search(name) for p in self.isr_name_patterns)


def _int(value) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def load_profile(path_or_name: str | Path) -> Profile:
    """Load a profile by file path or by bundled name (e.g. ``"mc68332"``)."""
    path = Path(path_or_name)
    if not path.suffix:
        path = PROFILE_DIR / f"{path_or_name.lower()}.yaml"
    data = yaml.safe_load(path.read_text())
    isr = data.get("isr_markers") or {}
    registers = [
        Register(
            name=r["name"],
            address=_int(r["address"]),
            size=int(r.get("size", 1)),
            count=int(r.get("count", 1)),
            module=r.get("module", ""),
            description=r.get("description", ""),
            role=r.get("role", "plain"),
            channel=r.get("channel", ""),
            rx_channel=r.get("rx_channel", ""),
            default=_int(r.get("default", 0)),
        )
        for r in data.get("registers") or []
    ]
    return Profile(
        name=data["name"],
        description=data.get("description", ""),
        endianness=data.get("endianness", "little"),
        address_bits=int(data.get("address_bits", 32)),
        max_align=int(data.get("max_align", 4)),
        type_sizes=dict(data.get("type_sizes") or {}),
        compiler_keywords=list(data.get("compiler_keywords") or []),
        isr_keywords=list(isr.get("keywords") or []),
        isr_name_patterns=[re.compile(p) for p in isr.get("name_patterns") or []],
        registers=registers,
        source=path,
    )


def available_profiles() -> list[str]:
    """Names of bundled profiles, e.g. ``["generic", "mc68332"]``."""
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))
