"""Hardware model: memory-mapped registers and absolute memory.

Register behaviour comes from the target profile:
  * ``tx``     writes are captured as transmitted data
  * ``rx``     reads take the next value from a scripted queue (default 0)
  * ``status`` reads return the profile default unless overridden
  * ``plain``  ordinary storage
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from source_tracer.profile import Profile, Register


@dataclass
class HardwareInputs:
    """Scenario-provided register behaviour."""

    values: dict[str, int] = field(default_factory=dict)  # register name -> fixed read value
    queues: dict[str, list[int]] = field(default_factory=dict)  # register or channel -> values read in order


class Bus:
    def __init__(self, profile: Profile, inputs: HardwareInputs | None = None, on_access=None):
        self.profile = profile
        self.endian = profile.endianness
        self.mask = (1 << profile.address_bits) - 1
        self.memory: dict[int, int] = {}
        self.inputs = inputs or HardwareInputs()
        self.queues = {k: deque(v) for k, v in self.inputs.queues.items()}
        self.on_access = on_access  # callback(kind, reg, address, value, size)

    # -- raw bytes
    def _load(self, address: int, size: int) -> int:
        data = bytes(self.memory.get((address + i) & self.mask, 0) for i in range(size))
        return int.from_bytes(data, "big" if self.endian == "big" else "little")

    def _store(self, address: int, size: int, value: int) -> None:
        data = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "big" if self.endian == "big" else "little")
        for i, b in enumerate(data):
            self.memory[(address + i) & self.mask] = b

    # -- accesses
    def read(self, address: int, size: int) -> int:
        address &= self.mask
        reg = self.profile.register_at(address)
        value = self._read_register(reg, address, size) if reg is not None else self._load(address, size)
        if self.on_access is not None:
            self.on_access("read", reg, address, value, size)
        return value

    def write(self, address: int, size: int, value: int) -> None:
        address &= self.mask
        self._store(address, size, value)
        if self.on_access is not None:
            self.on_access("write", self.profile.register_at(address), address, value, size)

    def _read_register(self, reg: Register, address: int, size: int) -> int:
        if reg.name in self.inputs.values:
            return self._slice(reg, self.inputs.values[reg.name], address, size)
        if reg.role == "rx":
            for key in (reg.name, reg.rx_channel or reg.channel):
                q = self.queues.get(key)
                if q:
                    return q.popleft() & ((1 << (8 * size)) - 1)
            return self._load(address, size)
        if reg.role == "tx" and reg.rx_channel:
            q = self.queues.get(reg.name) or self.queues.get(reg.rx_channel)
            if q:
                return q.popleft() & ((1 << (8 * size)) - 1)
            return 0
        if reg.role == "status":
            return self._slice(reg, reg.default, address, size)
        return self._load(address, size)

    def _slice(self, reg: Register, full_value: int, address: int, size: int) -> int:
        """Extract ``size`` bytes at ``address`` from a register whose whole value is ``full_value``."""
        if address == reg.address and size == reg.size:
            return full_value & ((1 << (8 * size)) - 1)
        data = (full_value & ((1 << (8 * reg.size)) - 1)).to_bytes(reg.size, "big" if self.endian == "big" else "little")
        off = (address - reg.address) % reg.size
        chunk = data[off : off + size].ljust(size, b"\0")
        return int.from_bytes(chunk, "big" if self.endian == "big" else "little")
