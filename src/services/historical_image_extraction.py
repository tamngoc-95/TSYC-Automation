"""
Historical Facebook image extraction (TSYC pipeline stabilization Phase 3).

Generalizes -- does not duplicate -- the existing production image path
(scripts/upload_facebook_images_to_supabase.py). That script already knows
how to turn a local image file plus a JSON metadata sidecar (raw_page_id,
source_url_id, facebook_post_url, facebook_image_url, content_type, sha256,
natural_width, natural_height) into a product_images row. This module's job
is narrower and upstream of that: given a historical candidate's already-
persisted `source_evidence.local_media_paths` (written by
import_historical_facebook_candidates*.py), produce exactly that same
local-file-plus-JSON-sidecar shape on disk so the *same* ingestion script
can pick it up unchanged.

No Supabase writes happen here. This module only:
    1. detects whether the historical media capability is available at all
       (the full Facebook data-export archive, gitignored, must be present
       -- data/raw/facebook_export_probe/ alone is not enough: it holds
       only the classification HTML, not the media archive);
    2. reads image bytes for one historical record out of that archive;
    3. computes the same metadata fields the production JSON sidecar
       already carries (sha256, width/height, MIME type) using small
       dependency-free header parsers -- this repository does not depend
       on Pillow/PIL, so dimensions are read directly from each format's
       own header bytes, not decoded;
    4. writes the image + sidecar into the same
       data/raw/facebook-images/<batch_code>/<raw_page_id>/ layout
       upload_facebook_images_to_supabase.py already scans.

Safety (CLAUDE.md section 11 / .claude/rules/tsyc-safety.md):
    - Never invents image rights. Every image this module hands off still
      gets usage_rights_status=RIGHTS_UNKNOWN by the unchanged downstream
      insert path (build_database_payload in the upload script) -- rights
      classification always remains review_product_images.py's job.
    - Never silently attaches a multi-product post's shared images to one
      candidate. See `evaluate_historical_image_ownership` in
      src.domain.rules.image_rules -- callers must check that decision
      before calling `write_local_image_cache` for a candidate whose
      source Facebook post is shared with other candidates.
    - Video files (and any non-image extension) referenced by
      local_media_paths are never extracted here -- this module ingests
      still images only.
"""
from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Still-image extensions this module will extract. Anything else
# referenced by local_media_paths (video, audio) is skipped -- CLAUDE.md
# never asked for video ingestion, and guessing at video "cover frames"
# would be exactly the kind of invented metadata section 2.2 forbids.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp"}
)

MIME_BY_EXTENSION: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# The full Facebook data-export archive is a large personal file and is
# gitignored (see .gitignore: "facebook-vothitam2810-*") -- it must be
# present on disk (not committed) for historical extraction to run at
# all. Matched by glob rather than one fixed name because the export
# filename itself carries the export's own generated suffix.
EXPORT_ARCHIVE_GLOB = "facebook-*.zip"


@dataclass(frozen=True)
class CapabilityStatus:
    """Whether the historical image extraction capability is usable right
    now in this environment, and why -- surfaced verbatim in
    pipeline_state.py's IMAGE_CAPABILITY_UNAVAILABLE blocked state so an
    operator sees exactly what is missing rather than a generic failure."""

    available: bool
    reason: str
    archive_path: Path | None = None


def find_export_archive(project_root: Path) -> Path | None:
    """Locate the (gitignored) full Facebook export archive, if present."""
    data_raw = project_root / "data" / "raw"

    if not data_raw.is_dir():
        return None

    matches = sorted(data_raw.glob(EXPORT_ARCHIVE_GLOB))
    return matches[0] if matches else None


def check_capability(project_root: Path) -> CapabilityStatus:
    """Read-only capability probe. Never raises -- always returns a
    CapabilityStatus so callers can report a clean blocked state instead
    of an unhandled exception mid-batch."""
    archive_path = find_export_archive(project_root)

    if archive_path is None:
        return CapabilityStatus(
            available=False,
            reason=(
                "Historical image ingestion capability is unavailable: no "
                "Facebook export archive (data/raw/facebook-*.zip) was "
                "found. This archive is gitignored and personal -- it "
                "must be placed in data/raw/ before historical image "
                "extraction can run."
            ),
            archive_path=None,
        )

    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_entry = archive.testzip()
    except (zipfile.BadZipFile, OSError) as error:
        return CapabilityStatus(
            available=False,
            reason=(
                f"Historical image ingestion capability is unavailable: "
                f"{archive_path.name} could not be read as a zip archive "
                f"({type(error).__name__}: {error})."
            ),
            archive_path=archive_path,
        )

    if bad_entry is not None:
        return CapabilityStatus(
            available=False,
            reason=(
                "Historical image ingestion capability is unavailable: "
                f"{archive_path.name} failed integrity check at entry "
                f"{bad_entry!r}."
            ),
            archive_path=archive_path,
        )

    return CapabilityStatus(
        available=True,
        reason=f"Facebook export archive found: {archive_path.name}.",
        archive_path=archive_path,
    )


def filter_image_paths(local_media_paths: list[str]) -> list[str]:
    """Pure: narrow local_media_paths down to still-image entries only."""
    return [
        path
        for path in local_media_paths
        if Path(path).suffix.lower() in IMAGE_EXTENSIONS
    ]


# --- dependency-free image header parsing -----------------------------
#
# This repository has no Pillow/PIL dependency (see requirements.txt).
# Rather than add one for a handful of header bytes, each format's
# dimensions are read directly from its own well-documented header --
# the exact same natural_width/natural_height fields the production
# Facebook collector already writes into its JSON sidecar via a
# browser-side `Image.naturalWidth/Height` read.


def _sniff_png(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _sniff_gif(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _sniff_jpeg(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    offset = 2
    length = len(data)

    while offset + 4 <= length:
        if data[offset] != 0xFF:
            offset += 1
            continue

        marker = data[offset + 1]

        # Start-of-frame markers that carry height/width (baseline,
        # progressive, and their arithmetic/lossless variants) --
        # excludes DHT (0xC4), JPG (0xC8), and DAC (0xCC), which share
        # the 0xC0-0xCF range but are not SOF markers.
        is_sof = 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)

        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:
            offset += 2
            continue

        if offset + 4 > length:
            break

        segment_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]

        if is_sof:
            if offset + 9 > length:
                return None
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height

        offset += 2 + segment_length

    return None


def _sniff_webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None

    chunk_type = data[12:16]

    if chunk_type == b"VP8 " and len(data) >= 30:
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height

    if chunk_type == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height

    if chunk_type == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height

    return None


def sniff_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) for PNG/JPEG/GIF/WEBP bytes, or None if the
    format is unrecognized/truncated. Never raises -- an unreadable image
    is reported as missing dimensions, not a crash."""
    for sniffer in (_sniff_png, _sniff_jpeg, _sniff_gif, _sniff_webp):
        try:
            result = sniffer(data)
        except (struct.error, IndexError):
            result = None

        if result is not None:
            return result

    return None


def mime_type_for_path(relative_path: str) -> str | None:
    return MIME_BY_EXTENSION.get(Path(relative_path).suffix.lower())


def read_archive_image_bytes(archive_path: Path, relative_path: str) -> bytes:
    """Read one image's raw bytes out of the Facebook export archive.

    Raises FileNotFoundError (with the exact relative_path) if the entry
    is not present -- this is a data-integrity problem the caller should
    surface, not silently skip.
    """
    with zipfile.ZipFile(archive_path) as archive:
        try:
            return archive.read(relative_path)
        except KeyError as error:
            raise FileNotFoundError(
                f"Facebook export archive does not contain: {relative_path}"
            ) from error


def build_sidecar_metadata(
    *,
    relative_path: str,
    data: bytes,
    raw_page_id: str,
    source_url_id: str | None,
    facebook_post_url: str | None,
    local_image_path: Path,
) -> dict[str, Any]:
    """Build the exact JSON sidecar shape
    upload_facebook_images_to_supabase.validate_metadata()/
    build_database_payload() already expect from the live Facebook
    collector -- so that script's insert logic runs completely unchanged
    for historical images."""
    dimensions = sniff_dimensions(data)
    content_type = mime_type_for_path(relative_path) or "application/octet-stream"

    return {
        "raw_page_id": raw_page_id,
        "source_url_id": source_url_id,
        "facebook_post_url": facebook_post_url,
        # No live CDN URL exists for a historical export image -- the
        # local archive entry itself is the only truthful locator.
        "facebook_image_url": f"facebook-export-media://{relative_path}",
        "parent_photo_url": None,
        "content_type": content_type,
        "sha256": hashlib.sha256(data).hexdigest(),
        "natural_width": dimensions[0] if dimensions else None,
        "natural_height": dimensions[1] if dimensions else None,
        "local_image_path": str(local_image_path),
        "historical_source_relative_path": relative_path,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def local_cache_dir(project_root: Path, batch_code: str, raw_page_id: str) -> Path:
    return (
        project_root
        / "data"
        / "raw"
        / "facebook-images"
        / batch_code
        / raw_page_id
    )


def write_local_image_cache(
    *,
    project_root: Path,
    batch_code: str,
    raw_page_id: str,
    relative_path: str,
    data: bytes,
    source_url_id: str | None,
    facebook_post_url: str | None,
) -> tuple[Path, dict[str, Any]]:
    """Write one historical image + its JSON sidecar into the same local
    cache layout upload_facebook_images_to_supabase.py already scans.
    Idempotent: re-running with identical bytes overwrites the same
    deterministic (hash-derived) filename with identical content.

    Returns (image_path, metadata_dict).
    """
    directory = local_cache_dir(project_root, batch_code, raw_page_id)
    directory.mkdir(parents=True, exist_ok=True)

    image_hash = hashlib.sha256(data).hexdigest()
    extension = Path(relative_path).suffix.lower() or ".bin"
    image_path = directory / f"image_{image_hash[:16]}{extension}"
    metadata_path = directory / f"image_{image_hash[:16]}.json"

    image_path.write_bytes(data)

    metadata = build_sidecar_metadata(
        relative_path=relative_path,
        data=data,
        raw_page_id=raw_page_id,
        source_url_id=source_url_id,
        facebook_post_url=facebook_post_url,
        local_image_path=image_path,
    )

    import json

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return image_path, metadata
