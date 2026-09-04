"""Offline tests for upload_facebook_images_to_supabase.py's --batch-code
generalization (TSYC pipeline stabilization Phase 3).

Covers only the pure/local-filesystem pieces (local_image_root_for,
find_metadata_files, build_storage_path) -- the Supabase/Storage upload
path itself is unchanged and already exercised by hand in production; no
live Supabase dependency is introduced here.
"""
from __future__ import annotations

from pathlib import Path

import upload_facebook_images_to_supabase as uploader


def test_local_image_root_for_uses_batch_code_subdirectory():
    root = uploader.local_image_root_for("FB-HIST-TEST")

    assert root.name == "FB-HIST-TEST"
    assert root.parent.name == "facebook-images"


def test_default_batch_code_is_unchanged_for_backward_compatibility():
    assert uploader.BATCH_CODE == "FB-2026-001"
    assert uploader.LOCAL_IMAGE_ROOT == uploader.local_image_root_for("FB-2026-001")


def test_find_metadata_files_scans_the_given_root(tmp_path: Path):
    root = tmp_path / "facebook-images" / "FB-HIST-TEST"
    (root / "raw-page-1").mkdir(parents=True)
    (root / "raw-page-1" / "image_abc.json").write_text("{}", encoding="utf-8")
    (root / "raw-page-1" / "download_summary.json").write_text("{}", encoding="utf-8")

    found = uploader.find_metadata_files(root)

    assert len(found) == 1
    assert found[0].name == "image_abc.json"


def test_find_metadata_files_raises_for_missing_root(tmp_path: Path):
    missing_root = tmp_path / "does-not-exist"

    try:
        uploader.find_metadata_files(missing_root)
        assert False, "expected RuntimeError"
    except RuntimeError as error:
        assert str(missing_root) in str(error)


def test_build_storage_path_uses_the_given_batch_code():
    metadata = {"raw_page_id": "raw-page-1", "sha256": "a" * 64}
    image_path = Path("image_abc.jpg")

    path = uploader.build_storage_path(metadata, image_path, batch_code="FB-HIST-TEST")

    assert path == f"facebook/FB-HIST-TEST/raw-page-1/{'a' * 64}.jpg"


def test_build_storage_path_defaults_to_module_batch_code():
    metadata = {"raw_page_id": "raw-page-1", "sha256": "b" * 64}
    image_path = Path("image_abc.png")

    path = uploader.build_storage_path(metadata, image_path)

    assert path == f"facebook/{uploader.BATCH_CODE}/raw-page-1/{'b' * 64}.png"
