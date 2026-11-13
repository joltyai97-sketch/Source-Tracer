"""Entry point: ``python -m source_tracer [files or folders...]``."""

from __future__ import annotations

import sys


def main() -> int:
    from source_tracer.gui.app import run

    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
