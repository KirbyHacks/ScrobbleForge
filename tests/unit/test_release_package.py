"""check that release archives contain project files only."""
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts import build_release


class TestReleasePackage(unittest.TestCase):
    def test_archive_includes_only_project_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in build_release.FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("project file\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src/__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
            hidden = (
                "docs/notes.md", "src/settings.json", "src/instructions.md",
                "src/.local/settings.json", "src/.private/config.yaml",
                "tests/.cache/data.py", "scripts/.hidden/tool.py",
                ".github/workflows/settings.json",
            )
            for name in hidden:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private local settings\n", encoding="utf-8")
            with patch.object(build_release, "ROOT", root), redirect_stdout(StringIO()):
                build_release.build_release()
                archive = root / "dist/scrobbleforge-1.1.0-source.zip"
                first = archive.read_bytes()
                build_release.build_release()
                self.assertEqual(first, archive.read_bytes())
            with ZipFile(archive) as contents:
                names = contents.namelist()
                self.assertIn("scrobbleforge-1.1.0/src/__init__.py", names)
                self.assertIn("scrobbleforge-1.1.0/README.md", names)
                for name in hidden:
                    self.assertNotIn("scrobbleforge-1.1.0/" + name, names)
                self.assertFalse(any(b"private local settings" in contents.read(name) for name in names))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertIn(checksum, archive.with_suffix(".zip.sha256").read_text(encoding="utf-8"))
