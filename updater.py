"""Apply a BlindBridge release zip to an install folder.

Used by the server (a zip uploaded on the Settings page) and by the bridge (an
update passed on by the server). Standard library only.

A release zip holds one top folder with the program files in it. Only the
folders and files named below are installed. Anything else in the zip is
ignored, and settings, keys, the database, captures and the Python environment
are never written, whatever the zip contains.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

TOP = "blindbridge"
ALLOWED_ROOTS = {"server", "bridge", "deploy", "docs", "tools", "images"}
ALLOWED_FILES = {"README.md", "CHANGELOG.md", "LICENSE.md", "SECURITY.md", "VERSION", "updater.py",
                 ".gitattributes", ".gitignore"}
REQUIRED = {"server/server.py", "bridge/bridge.py", "VERSION"}
PROTECTED_NAMES = {"config.json", "blinds.json", "bridge.json", "pending.json", "stuck.txt", "wifi_off"}
PROTECTED_SUFFIXES = (".db", ".db-journal", ".db-wal", ".db-shm", ".tmp")
DATA_DIRS = {"captures"}
SKIP_DIRS = {"__pycache__", "venv", ".git"} | DATA_DIRS
PACK_ORDER = ["README.md", "CHANGELOG.md", "LICENSE.md", "SECURITY.md", "VERSION", "updater.py",
              ".gitattributes", ".gitignore", "server", "bridge", "deploy", "docs", "tools", "images"]
MAX_FILES = 400
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


class UpdateError(Exception):
    pass


def release_files(install_dir: Path) -> list[Path]:
    """The program files of an install: no settings, keys, database, captures or environment."""
    install_dir = Path(install_dir)
    files = []
    for entry in PACK_ORDER:
        path = install_dir / entry
        if path.is_symlink():
            continue
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            continue
        for item in sorted(path.rglob("*")):
            rel = item.relative_to(install_dir)
            if not item.is_file() or item.is_symlink() or SKIP_DIRS & set(rel.parts):
                continue
            if item.name in PROTECTED_NAMES or item.name.endswith(PROTECTED_SUFFIXES + (".pyc", ".new")):
                continue
            files.append(item)
    return files


def build_zip(install_dir: Path) -> bytes:
    """Pack an install into a release zip. The result is a valid update."""
    install_dir = Path(install_dir)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in release_files(install_dir):
            archive.write(item, TOP + "/" + item.relative_to(install_dir).as_posix())
    return buffer.getvalue()


def _parts(name: str) -> tuple[str, ...]:
    """The path parts of a zip member, refusing anything that could leave the install folder."""
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise UpdateError(f"unsafe path in zip: {name}")
    parts = tuple(name.rstrip("/").split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateError(f"unsafe path in zip: {name}")
    return parts


def _clean_member(rel: tuple[str, ...], name: str) -> str | None:
    """Path inside the install folder for a zip member, or None when the member is not installed."""
    if not rel or "__pycache__" in rel:
        return None
    if len(rel) == 1:
        if rel[0] not in ALLOWED_FILES:
            return None
    elif rel[0] not in ALLOWED_ROOTS:
        return None
    if DATA_DIRS & set(rel[:-1]):
        raise UpdateError(f"zip tries to write into a data folder: {name}")
    leaf = rel[-1]
    if leaf in PROTECTED_NAMES or leaf.endswith(PROTECTED_SUFFIXES):
        raise UpdateError(f"zip tries to replace a settings or data file: {name}")
    return "/".join(rel)


def read_update(data: bytes) -> dict[str, bytes]:
    """Validate a release zip and return {relative path: contents} of what it installs."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("that file is not a zip") from None
    files: dict[str, bytes] = {}
    total = 0
    tops = set()
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise UpdateError("too many files in zip")
        for info in infos:
            parts = _parts(info.filename)
            tops.add(parts[0])
            if len(tops) > 1:
                raise UpdateError("zip must contain a single top folder")
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"zip contains a link: {info.filename}")
            rel = _clean_member(parts[1:], info.filename)
            if rel is None:
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise UpdateError(f"file too large: {info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise UpdateError("zip is too large")
            content = archive.read(info)
            if len(content) != info.file_size:
                raise UpdateError(f"size mismatch: {info.filename}")
            files[rel] = content
    missing = REQUIRED - set(files)
    if missing:
        raise UpdateError("not a BlindBridge release: missing " + ", ".join(sorted(missing)))
    for rel, content in files.items():
        if rel.endswith(".py"):
            try:
                compile(content, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                detail = f"{exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError) else str(exc)
                raise UpdateError(f"{rel} does not compile: {detail}") from None
    version = files["VERSION"].decode("utf-8", "replace").strip()
    if not version or len(version) > 20 or not all(c.isascii() and (c.isalnum() or c in ".-") for c in version):
        raise UpdateError("bad VERSION file")
    return files


def installed_version(install_dir: Path) -> str:
    try:
        return (Path(install_dir) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _inside(root: Path, folder: Path) -> bool:
    """Is this folder, as far as it exists yet, really inside the install folder?"""
    while not folder.exists():
        folder = folder.parent
    real = folder.resolve()
    return real == root or root in real.parents


def apply_update(data: bytes, install_dir: Path) -> dict:
    """Validate and install. Returns {"from", "to", "changed": [paths]}.

    Only files whose contents differ are written, each through a temporary file
    and an atomic rename, so a second apply of the same zip changes nothing.
    """
    install_dir = Path(install_dir)
    files = read_update(data)
    before = installed_version(install_dir)
    root = install_dir.resolve()
    changed = []
    for rel in sorted(files):
        target = install_dir / rel
        content = files[rel]
        try:
            if target.is_file() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(content).digest():
                continue
        except OSError:
            pass
        if target.is_symlink() or not _inside(root, target.parent):
            raise UpdateError(f"refusing to write through a link: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        if tmp.is_symlink():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        changed.append(rel)
    return {"from": before, "to": files["VERSION"].decode().strip(), "changed": changed}
