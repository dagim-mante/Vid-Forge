"""Reference images: find, check and fingerprint the files in input/references/.

The filename is the metadata:

    character_<name>.png   identity of a recurring character; <name> must be a
                           key in style.characters
    scene_<name>.jpg       a place or an object
    style_<name>.webp      the overall look; applies to every shot

<name> is lowercase letters, digits and underscores, starts with a letter, and
is 2 to 32 characters long. Formats: png, jpg/jpeg, webp.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

REFERENCES_SUBDIR = Path("input") / "references"
KINDS = ("character", "scene", "style")
MAX_REFERENCES = 8
MAX_FILE_BYTES = 4 * 1024 * 1024  # per image
MAX_TOTAL_BYTES = 12 * 1024 * 1024  # all images; they are sent inline to the model

NAME_PATTERN = re.compile(r"^(character|scene|style)_([a-z][a-z0-9_]{1,31})\.(?i:png|jpe?g|webp)$")
SUPPORTED_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
UNSUPPORTED_IMAGE_SUFFIXES = {".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".svg", ".avif"}
NAME_HELP = (
    "name must look like character_<name>.png, scene_<name>.jpg or style_<name>.webp "
    "(lowercase letters, digits and underscores; <name> starts with a letter and is 2 to 32 characters)"
)


@dataclass(frozen=True)
class ReferenceFile:
    """One validated reference image on disk."""

    id: str  # e.g. character_girl
    kind: str  # character | scene | style
    name: str  # e.g. girl
    path: Path  # absolute
    rel_path: str  # relative to the project folder, posix style
    mime_type: str
    size: int
    sha256: str

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_mime(data: bytes) -> str | None:
    """Identify the real format from the file's first bytes, not its extension."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def scan_references(project_dir: Path) -> tuple[list[ReferenceFile], list[str]]:
    """Scan projects/<name>/input/references/. Returns (references, errors).

    A missing folder is fine and means no references. Files that are not images
    (notes, .gitkeep) are ignored. Image files with a bad name, an unsupported
    format, a wrong signature or a size over the limit are reported as errors.
    """
    folder = project_dir / REFERENCES_SUBDIR
    if not folder.is_dir():
        return [], []

    references: list[ReferenceFile] = []
    errors: list[str] = []
    seen: dict[str, str] = {}
    total_bytes = 0

    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        label = f"{REFERENCES_SUBDIR.as_posix()}/{path.name}"
        suffix = path.suffix.lower()

        if suffix in UNSUPPORTED_IMAGE_SUFFIXES:
            errors.append(f"{label}: unsupported format {suffix} (use png, jpg or webp)")
            continue
        if suffix not in SUPPORTED_SUFFIXES:
            continue

        match = NAME_PATTERN.fullmatch(path.name)
        if match is None:
            errors.append(f"{label}: {NAME_HELP}")
            continue

        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            errors.append(
                f"{label}: {size / 1024 / 1024:.1f} MB is over the {MAX_FILE_BYTES // 1024 // 1024} MB "
                "limit per image; resize it (about 1024 px on the long side is plenty)"
            )
            continue

        data = path.read_bytes()
        actual = _detect_mime(data)
        if actual is None or actual != SUPPORTED_SUFFIXES[suffix]:
            found = actual or "an unrecognised format"
            errors.append(f"{label}: extension says {SUPPORTED_SUFFIXES[suffix]} but the file is {found}")
            continue

        kind, name = match.group(1), match.group(2)
        ref_id = f"{kind}_{name}"
        if ref_id in seen:
            errors.append(f"{label}: duplicate id {ref_id!r} (also {seen[ref_id]})")
            continue
        seen[ref_id] = label

        total_bytes += size
        references.append(
            ReferenceFile(
                id=ref_id,
                kind=kind,
                name=name,
                path=path.resolve(),
                rel_path=f"{REFERENCES_SUBDIR.as_posix()}/{path.name}",
                mime_type=actual,
                size=size,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )

    if len(references) > MAX_REFERENCES:
        errors.append(f"{len(references)} reference images found; the limit is {MAX_REFERENCES}")
    if total_bytes > MAX_TOTAL_BYTES:
        errors.append(
            f"reference images total {total_bytes / 1024 / 1024:.1f} MB; "
            f"the limit is {MAX_TOTAL_BYTES // 1024 // 1024} MB"
        )
    return references, errors