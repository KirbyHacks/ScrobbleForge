"""unit test package with zero network calls."""
import sys
from pathlib import Path

# add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()
