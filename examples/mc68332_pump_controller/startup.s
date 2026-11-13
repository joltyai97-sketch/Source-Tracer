* startup.s - reset vector and low-level helpers (not interpreted)
        XDEF    _cpu_idle
_cpu_idle:
        STOP    #$2000
        RTS
