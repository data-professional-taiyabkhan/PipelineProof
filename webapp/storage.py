"""Submission archives: validation, safe extraction, and packing folders into zips."""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 500
MAX_UNCOMPRESSED = 60 * 1024 * 1024
SKIP_PARTS = {"__pycache__", ".ipynb_checkpoints", ".git", "__MACOSX", ".DS_Store", ".venv", "venv"}


class BadArchive(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    out = []
    for info in z.infolist():
        if info.is_dir():
            continue
        p = PurePosixPath(info.filename.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            raise BadArchive(f"unsafe path in zip: {info.filename}")
        if any(part in SKIP_PARTS for part in p.parts) or p.name.startswith("._"):
            continue
        out.append(info)
    return out


def inspect(data: bytes) -> list[str]:
    """Validate an uploaded zip; return the list of file paths inside (after stripping one top folder)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BadArchive("the file is not a valid zip archive") from e
    with z:
        members = _members(z)
        if not members:
            raise BadArchive("the zip is empty")
        if len(members) > MAX_FILES:
            raise BadArchive(f"the zip contains too many files ({len(members)} > {MAX_FILES})")
        total = sum(m.file_size for m in members)
        if total > MAX_UNCOMPRESSED:
            raise BadArchive(f"the zip expands to {total // 1_000_000} MB, more than the "
                             f"{MAX_UNCOMPRESSED // 1_000_000} MB allowed")
        names = [m.filename.replace("\\", "/") for m in members]
    return _strip_common_root(names)


def _strip_common_root(names: list[str]) -> list[str]:
    parts = [PurePosixPath(n).parts for n in names]
    if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1:
        return ["/".join(p[1:]) for p in parts]
    return names


def extract(data: bytes, dest: Path) -> Path:
    """Extract safely into dest (created). A single top-level folder is flattened away."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = _members(z)
        names = [m.filename.replace("\\", "/") for m in members]
        stripped = _strip_common_root(names)
        written = 0
        for info, rel in zip(members, stripped):
            target = (dest / rel).resolve()
            if dest.resolve() not in target.parents:
                raise BadArchive(f"unsafe path in zip: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 16):
                    written += len(chunk)
                    if written > MAX_UNCOMPRESSED:
                        raise BadArchive("archive expands beyond the size limit")
                    out.write(chunk)
    return dest


def zip_folder(folder: Path, top: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not any(part in SKIP_PARTS for part in p.relative_to(folder).parts):
                arc = p.relative_to(folder).as_posix()
                z.write(p, f"{top}/{arc}" if top else arc)
    return buf.getvalue()
