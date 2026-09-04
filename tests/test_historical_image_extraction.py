"""Offline tests for src/services/historical_image_extraction.py.

No live Facebook/Supabase dependency, and no dependency on the real
(gitignored, personal) Facebook export archive -- every zip archive and
image byte string used here is constructed in-memory or under tmp_path.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from src.services.historical_image_extraction import (
    build_sidecar_metadata,
    check_capability,
    filter_image_paths,
    find_export_archive,
    read_archive_image_bytes,
    sniff_dimensions,
    write_local_image_cache,
)


# --- filter_image_paths --------------------------------------------------


def test_filter_image_paths_keeps_only_still_images():
    paths = [
        "your_facebook_activity/posts/media/x/1.jpg",
        "your_facebook_activity/posts/media/x/2.mp4",
        "your_facebook_activity/posts/media/x/3.PNG",
        "your_facebook_activity/posts/media/x/4.mov",
        "your_facebook_activity/posts/media/x/5.webp",
    ]

    result = filter_image_paths(paths)

    assert result == [
        "your_facebook_activity/posts/media/x/1.jpg",
        "your_facebook_activity/posts/media/x/3.PNG",
        "your_facebook_activity/posts/media/x/5.webp",
    ]


def test_filter_image_paths_empty_input():
    assert filter_image_paths([]) == []


# --- sniff_dimensions -----------------------------------------------------


def _build_png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 4
    )


def _build_gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 10


def _build_jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + bytes([8])
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )


def _build_webp(width: int, height: int) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 100)
        + b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def test_sniff_dimensions_png():
    assert sniff_dimensions(_build_png(120, 80)) == (120, 80)


def test_sniff_dimensions_gif():
    assert sniff_dimensions(_build_gif(64, 48)) == (64, 48)


def test_sniff_dimensions_jpeg():
    assert sniff_dimensions(_build_jpeg(200, 300)) == (200, 300)


def test_sniff_dimensions_webp():
    assert sniff_dimensions(_build_webp(640, 480)) == (640, 480)


def test_sniff_dimensions_unrecognized_returns_none():
    assert sniff_dimensions(b"not an image") is None


def test_sniff_dimensions_never_raises_on_truncated_bytes():
    # Truncated/garbage input that starts with a real magic number but
    # cuts off before the fields sniff_dimensions() reads.
    assert sniff_dimensions(b"\x89PNG\r\n\x1a\n\x00\x00") is None
    assert sniff_dimensions(b"\xff\xd8\xff") is None


# --- check_capability -----------------------------------------------------


def test_capability_unavailable_when_no_archive_present(tmp_path: Path):
    (tmp_path / "data" / "raw").mkdir(parents=True)

    status = check_capability(tmp_path)

    assert status.available is False
    assert status.archive_path is None
    assert "no Facebook export archive" in status.reason


def test_capability_available_when_valid_archive_present(tmp_path: Path):
    data_raw = tmp_path / "data" / "raw"
    data_raw.mkdir(parents=True)
    archive_path = data_raw / "facebook-someone-01_01_2026-XYZ.zip"

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("your_facebook_activity/posts/media/x/1.jpg", b"fake-bytes")

    status = check_capability(tmp_path)

    assert status.available is True
    assert status.archive_path == archive_path


def test_capability_unavailable_for_corrupt_archive(tmp_path: Path):
    data_raw = tmp_path / "data" / "raw"
    data_raw.mkdir(parents=True)
    archive_path = data_raw / "facebook-broken.zip"
    archive_path.write_bytes(b"not actually a zip file")

    status = check_capability(tmp_path)

    assert status.available is False
    assert archive_path.name in status.reason


def test_find_export_archive_picks_first_match_deterministically(tmp_path: Path):
    data_raw = tmp_path / "data" / "raw"
    data_raw.mkdir(parents=True)

    with zipfile.ZipFile(data_raw / "facebook-a.zip", "w"):
        pass
    with zipfile.ZipFile(data_raw / "facebook-b.zip", "w"):
        pass

    found = find_export_archive(tmp_path)

    assert found is not None
    assert found.name == "facebook-a.zip"


# --- read_archive_image_bytes ----------------------------------------------


def test_read_archive_image_bytes_returns_exact_content(tmp_path: Path):
    archive_path = tmp_path / "export.zip"
    relative_path = "your_facebook_activity/posts/media/x/1.jpg"

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(relative_path, b"raw-image-bytes")

    assert read_archive_image_bytes(archive_path, relative_path) == b"raw-image-bytes"


def test_read_archive_image_bytes_missing_entry_raises_file_not_found(tmp_path: Path):
    archive_path = tmp_path / "export.zip"

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("some/other/file.jpg", b"x")

    try:
        read_archive_image_bytes(archive_path, "does/not/exist.jpg")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as error:
        assert "does/not/exist.jpg" in str(error)


# --- build_sidecar_metadata / write_local_image_cache -----------------------


def test_build_sidecar_metadata_matches_production_shape():
    data = _build_jpeg(200, 300)

    metadata = build_sidecar_metadata(
        relative_path="your_facebook_activity/posts/media/x/1.jpg",
        data=data,
        raw_page_id="raw-page-1",
        source_url_id="source-url-1",
        facebook_post_url="facebook-export://record=1",
        local_image_path=Path("/tmp/x/image_abc.jpg"),
    )

    # Same keys upload_facebook_images_to_supabase.validate_metadata()
    # requires, plus local_image_path for resolve_local_image_path().
    for required_field in (
        "raw_page_id",
        "source_url_id",
        "facebook_post_url",
        "facebook_image_url",
        "content_type",
        "sha256",
        "natural_width",
        "natural_height",
    ):
        assert required_field in metadata

    assert metadata["content_type"] == "image/jpeg"
    assert metadata["natural_width"] == 200
    assert metadata["natural_height"] == 300
    assert len(metadata["sha256"]) == 64


def test_write_local_image_cache_writes_image_and_sidecar(tmp_path: Path):
    data = _build_png(10, 20)

    image_path, metadata = write_local_image_cache(
        project_root=tmp_path,
        batch_code="FB-HIST-TEST",
        raw_page_id="raw-page-1",
        relative_path="your_facebook_activity/posts/media/x/1.png",
        data=data,
        source_url_id="source-url-1",
        facebook_post_url="facebook-export://record=1",
    )

    assert image_path.exists()
    assert image_path.read_bytes() == data

    sidecar_path = image_path.with_suffix(".json")
    assert sidecar_path.exists()

    import json

    on_disk_metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert on_disk_metadata["raw_page_id"] == "raw-page-1"
    assert on_disk_metadata["natural_width"] == 10
    assert on_disk_metadata["natural_height"] == 20
    assert on_disk_metadata == metadata

    expected_dir = (
        tmp_path / "data" / "raw" / "facebook-images" / "FB-HIST-TEST" / "raw-page-1"
    )
    assert image_path.parent == expected_dir


def test_write_local_image_cache_is_idempotent(tmp_path: Path):
    data = _build_png(10, 20)

    first_path, _ = write_local_image_cache(
        project_root=tmp_path,
        batch_code="FB-HIST-TEST",
        raw_page_id="raw-page-1",
        relative_path="a.png",
        data=data,
        source_url_id=None,
        facebook_post_url=None,
    )
    second_path, _ = write_local_image_cache(
        project_root=tmp_path,
        batch_code="FB-HIST-TEST",
        raw_page_id="raw-page-1",
        relative_path="a.png",
        data=data,
        source_url_id=None,
        facebook_post_url=None,
    )

    assert first_path == second_path
    assert first_path.read_bytes() == data
