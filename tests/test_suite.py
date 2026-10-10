"""runner for test_suite.py delegating to tests/unit."""
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()


def load_tests(loader, standard_tests, pattern):
    # return unit tests when run directly; return empty suite during recursive discovery
    if pattern is None or pattern == "test_suite.py":
        unit_dir = Path(__file__).resolve().parent / "unit"
        return loader.discover(start_dir=str(unit_dir), pattern="test_*.py")
    return unittest.TestSuite()


if __name__ == "__main__":
    unittest.main()
