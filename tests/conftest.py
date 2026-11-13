import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "mc68332_pump_controller"


@pytest.fixture(scope="session")
def example_project():
    from source_tracer.project import load_project

    return load_project([EXAMPLE], "mc68332")
