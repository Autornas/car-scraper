import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# The scripts are run as `python scripts/x.py`, so they import each other as
# top-level modules; tests do the same.
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def page():
    """page("autoplius/results_bmw_320.html") -> that saved page's HTML."""
    def read(name):
        return (FIXTURES / name).read_text(encoding="utf-8")
    return read
