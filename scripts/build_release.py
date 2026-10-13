"""build a source zip without credentials or local state."""
import ast
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md", "LICENSE", "Dockerfile", "docker-compose.yml", ".dockerignore",
    ".gitignore", ".gitattributes",
    ".env.example", "requirements.txt", "requirements-dev.txt", "auth_helper.py",
)
DIRECTORIES = ("src", "tests", "scripts", ".github/workflows")


def is_release_file(path):
    """include project files and leave out local settings."""
    relative = path.relative_to(ROOT)
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
        return False
    if not path.is_file():
        return False
    if relative.as_posix() in FILES:
        return True
    workflow = relative.parts[:2] == (".github", "workflows")
    parts = relative.parts[1:] if workflow else relative.parts
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return False
    if workflow:
        return path.suffix in (".yml", ".yaml")
    return relative.parts[0] in ("src", "tests", "scripts") and path.suffix == ".py"


def build_release():
    version = None
    for statement in ast.parse((ROOT / "src/__init__.py").read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            version = ast.literal_eval(statement.value)
    if not isinstance(version, str) or not version:
        raise ValueError("Release version missing")

    paths = [ROOT / name for name in FILES]
    for directory in DIRECTORIES:
        paths.extend(
            path for path in (ROOT / directory).rglob("*")
            if is_release_file(path)
        )
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / f"scrobbleforge-{version}-source.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
        for path in sorted(set(paths)):
            if not is_release_file(path):
                continue
            name = f"scrobbleforge-{version}/{path.relative_to(ROOT).as_posix()}"
            entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            output.writestr(entry, path.read_bytes())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8"
    )
    print(archive)
    print(f"SHA-256: {checksum}")


if __name__ == "__main__":
    build_release()
