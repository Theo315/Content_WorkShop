"""Create a source-only ZIP for a new Git repository, without touching Git state.

This is a conservative allowlist and a limited credential-pattern check, not a
comprehensive secret scanner. No upload, commit, cloud resource or deploy occurs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT_FILES = frozenset({
    ".env.example", ".gitignore", ".gitattributes", ".python-version",
    ".vercelignore", "README.md", "requirements.txt", "requirements-dev.txt",
    "package.json", "pytest.ini", "vercel.json",
})
ROOT_DIRECTORIES = (
    "api", "backend", "frontend", "assets", "tests", "tools", "docs",
    "vendor/hexph",
)
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vercel", "tmp", "output",
    "originals", "real", "selfcheck-artifacts", ".aws", ".ssh", "secrets",
})
EXCLUDED_FILES = frozenset({
    "inspect_sources.py", "disassemble_sds.py", ".ds_store", "thumbs.db",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials",
    "credentials.json", "secrets.json", "service-account.json",
})
EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".log", ".tsbuildinfo", ".pem", ".pfx", ".p12",
    ".key", ".jks", ".keystore", ".db", ".sqlite", ".sqlite3",
})
DOC_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
})
BINARY_SUFFIXES = DOC_IMAGE_SUFFIXES | frozenset({
    ".pdf", ".ttf", ".otf", ".woff", ".woff2", ".ico", ".zip", ".gz",
})
MAX_FILE_BYTES = 100 * 1024 * 1024
MANIFEST_NAME = "GIT-PACKAGE-MANIFEST.json"
PATTERNS = (
    ("GitHub token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{50,255})\b")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)
SECRET_ENV_NAME = re.compile(r"(?:PASSWORD|SECRET|TOKEN|PRIVATE_KEY|ACCESS_KEY(?:_ID)?)", re.I)
PLACEHOLDER_VALUE = re.compile(
    r"^(?:<[^>]+>|\$\{[^}]+\}|(?:YOUR|REPLACE|CHANGE|EXAMPLE|PLACEHOLDER)[_ -].*)$",
    re.I,
)


class PackageError(ValueError):
    """A safe, user-facing failure that never includes a credential value."""


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    sha256: str


def _check_local_path(path: Path, root: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise PackageError(f"Links and junctions are not allowed: {path.relative_to(root).as_posix()}")
    if not path.resolve().is_relative_to(root):
        raise PackageError("A source path resolves outside the project root")
    return info


def _excluded(relative: Path, *, directory: bool) -> bool:
    name = relative.name.lower()
    if directory:
        return name in EXCLUDED_DIRECTORIES
    if name in EXCLUDED_FILES or relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if (name.startswith(".env") and name != ".env.example") or relative.suffix.lower() == ".env":
        return True
    if relative.parts[0].lower() == "docs":
        return (
            relative.suffix.lower() in DOC_IMAGE_SUFFIXES
            or any("screenshot" in part.lower() for part in relative.parts)
        )
    return False


def _walk(directory: Path, root: Path):
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = path.relative_to(root)
        # Excluded dependencies/data are never traversed, even if junctions.
        if path.name.lower() in EXCLUDED_DIRECTORIES:
            continue
        info = _check_local_path(path, root)
        if stat.S_ISDIR(info.st_mode):
            if not _excluded(relative, directory=True):
                yield from _walk(path, root)
        elif stat.S_ISREG(info.st_mode):
            if not _excluded(relative, directory=False):
                yield path
        else:
            raise PackageError(f"Only regular files are allowed: {relative.as_posix()}")


def _check_credentials(relative: str, data: bytes) -> None:
    if Path(relative).suffix.lower() not in BINARY_SUFFIXES:
        for label, pattern in PATTERNS:
            if pattern.search(data):
                raise PackageError(f"Possible {label} in {relative}; remove it before packaging")
    if Path(relative).name.lower() != ".env.example":
        return
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise PackageError(f"Example environment file must be UTF-8: {relative}") from exc
    for line in lines:
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        name, separator, value = clean.removeprefix("export ").partition("=")
        if not separator:
            raise PackageError(f"Invalid example environment assignment in {relative}")
        if not SECRET_ENV_NAME.search(name):
            continue
        value = value.split(" #", 1)[0].strip().strip("\"'")
        if value and not PLACEHOLDER_VALUE.fullmatch(value):
            raise PackageError(f"Non-placeholder credential in {relative}; leave secrets empty")


def collect_sources(root: Path) -> list[SourceFile]:
    """Validate allowed project sources and return their immutable inventory."""
    root = root.absolute()
    if not root.is_dir():
        raise PackageError("Project root must be a real directory")
    _check_local_path(root, root)
    root = root.resolve()
    paths: list[Path] = []
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.exists() or path.is_symlink():
            info = _check_local_path(path, root)
            if not stat.S_ISREG(info.st_mode):
                raise PackageError(f"Expected a regular root file: {name}")
            paths.append(path)
    for name in ROOT_DIRECTORIES:
        path = root / name
        # Validate each parent too: vendor itself could otherwise be a junction.
        for parent in (*reversed(path.relative_to(root).parents), path.relative_to(root)):
            if parent == Path("."):
                continue
            target = root / parent
            if target.exists() or target.is_symlink():
                info = _check_local_path(target, root)
                if not stat.S_ISDIR(info.st_mode):
                    raise PackageError(f"Expected a source directory: {parent.as_posix()}")
        if path.is_dir():
            paths.extend(_walk(path, root))
    sources = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise PackageError(f"File exceeds the 100 MiB Git limit: {relative}")
        data = path.read_bytes()
        _check_credentials(relative, data)
        sources.append(SourceFile(relative, len(data), hashlib.sha256(data).hexdigest()))
    if not sources:
        raise PackageError("No allowed project sources were found")
    return sources


def manifest_for(sources: list[SourceFile]) -> dict:
    return {
        "schema_version": 1,
        "warning": "no cloud deploy performed",
        "security_note": "Source allowlist and limited credential-pattern checks only; not a comprehensive secret scan.",
        "source_file_count": len(sources),
        "source_total_bytes": sum(item.size for item in sources),
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in sources
        ],
    }


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def create_archive(root: Path, output: Path, sources: list[SourceFile]) -> Path:
    """Create a new ZIP exclusively; never replace an existing output."""
    root = root.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise PackageError(f"Output already exists; choose a new --output path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as destination:
            created = True
            with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
                for item in sources:
                    path = root / item.path
                    _check_local_path(path, root)
                    data = path.read_bytes()
                    if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
                        raise PackageError(f"Source changed while packaging; retry: {item.path}")
                    archive.writestr(_zip_info(item.path), data)
                archive.writestr(
                    _zip_info(MANIFEST_NAME),
                    json.dumps(manifest_for(sources), ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                )
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Project root (default: this script's project)")
    parser.add_argument("--output", type=Path, help="New ZIP path; existing files are never overwritten")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Validate and report counts without writing")
    modes.add_argument("--list", action="store_true", help="Validate and list source paths without writing")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        sources = collect_sources(args.root)
        if args.list:
            for source in sources:
                print(source.path)
        if not (args.check or args.list):
            output = args.output or root / "tmp" / "releases" / "content-workshop-git.zip"
            print(f"Archive: {create_archive(root, output, sources)}")
        print(f"Validated {len(sources)} source files; {sum(item.size for item in sources)} bytes")
        print("No cloud deploy performed. Limited credential checks are not a comprehensive secret scan.")
        return 0
    except (PackageError, OSError) as exc:
        print(f"Cannot prepare Git package: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
