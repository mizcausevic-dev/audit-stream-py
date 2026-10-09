"""Fail a release when the built wheel or source archive has unexpected contents."""

from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    dist_path: Path = args.dist
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    wheel_path = dist_path / f"audit_stream-{version}-py3-none-any.whl"
    sdist_path = dist_path / f"audit_stream-{version}.tar.gz"
    actual = {path.name for path in dist_path.iterdir() if path.is_file()}
    expected = {wheel_path.name, sdist_path.name}
    if actual != expected:
        raise SystemExit(f"expected exactly wheel + sdist {sorted(expected)}, got {sorted(actual)}")

    info = f"audit_stream-{version}.dist-info"
    package_files = {
        f"audit_stream/{name}"
        for name in (
            "__init__.py",
            "__main__.py",
            "app.py",
            "boundary.py",
            "models.py",
            "sqlite_store.py",
            "store.py",
        )
    }
    metadata_files = {
        f"{info}/METADATA",
        f"{info}/WHEEL",
        f"{info}/entry_points.txt",
        f"{info}/licenses/LICENSE",
        f"{info}/RECORD",
    }
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        if names != package_files | metadata_files:
            raise SystemExit(f"wheel content mismatch: {sorted(names ^ (package_files | metadata_files))}")
        metadata = Parser().parsestr(wheel.read(f"{info}/METADATA").decode("utf-8"))
        if metadata["Name"] != project["name"] or metadata["Version"] != version:
            raise SystemExit("wheel package name/version does not match pyproject.toml")

    root = f"audit_stream-{version}/"
    source_files = {root + f"src/{name}" for name in package_files}
    test_files = {
        root + f"tests/{name}"
        for name in (
            "__init__.py",
            "test_app.py",
            "test_main.py",
            "test_sqlite_store.py",
            "test_store.py",
        )
    }
    expected_sdist = (
        source_files
        | test_files
        | {
            root + "examples/producer.py",
            root + "scripts/check_dist.py",
            root + "pyproject.toml",
            root + "README.md",
            root + "LICENSE",
            root + ".gitignore",
            root + "PKG-INFO",
        }
    )
    with tarfile.open(sdist_path, "r:gz") as sdist:
        members = sdist.getmembers()
        names = {member.name for member in members}
        if names != expected_sdist:
            raise SystemExit(f"sdist content mismatch: {sorted(names ^ expected_sdist)}")
        for member in members:
            parts = Path(member.name).parts
            if (
                not member.isfile()
                or not member.name.startswith(root)
                or ".." in parts
                or any(part in {".git", ".venv", "__pycache__", ".env"} for part in parts)
                or member.name.endswith((".pyc", ".sqlite3", ".db", ".pem", ".key"))
            ):
                raise SystemExit(f"unexpected sdist member: {member.name}")

    print(f"verified {wheel_path.name} and {sdist_path.name}")


if __name__ == "__main__":
    main()
