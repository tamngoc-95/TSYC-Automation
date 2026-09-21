"""
Offline tests for upload_facebook_images_to_supabase.py's per-candidate
image resolution on a raw page shared by more than one candidate
(historical multi-image ownership resolver, Phases 1-4).

No live Supabase/Storage dependency -- FakeSupabaseRepository backs every
Supabase read/write; Storage calls are never reached by these tests.
"""

from __future__ import annotations

import pytest

import upload_facebook_images_to_supabase as uploader

from support.fake_supabase import FakeSupabaseRepository


RAW_PAGE_ID = "raw-page-1"


def _candidate_row(candidate_id: str, candidate_code: str, paths: list[str]) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code,
        "raw_page_id": RAW_PAGE_ID,
        "extracted_title": f"Title for {candidate_code}",
        "workflow_status": "EXTRACTED",
        "source_evidence": {
            "extraction_source": "MANUAL_VISUAL_REVIEW",
            "local_media_paths": paths,
        },
    }


def _item(relative_path: str, image_hash: str) -> dict:
    """A metadata item shaped like build_metadata_item()'s output, plus
    the full "metadata" dict select_raw_page_group()/filter_items_to_
    candidate_provenance() actually reads historical_source_relative_
    path from."""
    return {
        "metadata_path": None,
        "metadata": {
            "raw_page_id": RAW_PAGE_ID,
            "historical_source_relative_path": relative_path,
        },
        "raw_page_id": RAW_PAGE_ID,
        "source_url_id": "source-1",
        "facebook_post_url": "https://facebook.com/x",
        "image_hash": image_hash,
        "width": 800,
        "height": 800,
    }


# --- filter_items_to_candidate_provenance ---------------------------------


def test_filter_keeps_only_items_matching_candidates_own_paths():
    candidate = _candidate_row("cand-a", "FB-HIST-2026-IMG-001-CAN-0001", ["a.jpg"])
    items = [_item("a.jpg", "hash-a"), _item("b.jpg", "hash-b")]

    filtered = uploader.filter_items_to_candidate_provenance(items, candidate)

    assert [item["metadata"]["historical_source_relative_path"] for item in filtered] == [
        "a.jpg"
    ]


def test_filter_returns_empty_when_candidate_has_no_own_paths():
    candidate = _candidate_row("cand-a", "FB-HIST-2026-IMG-001-CAN-0001", [])
    items = [_item("a.jpg", "hash-a")]

    assert uploader.filter_items_to_candidate_provenance(items, candidate) == []


def test_filter_keeps_shared_path_when_candidate_also_names_it():
    """Legitimate multi-product photo: this candidate's own local_media_
    paths also names the shared file, so it is kept."""
    candidate = _candidate_row(
        "cand-a", "FB-HIST-2026-IMG-006-CAN-0001", ["flatlay.jpg"]
    )
    items = [_item("flatlay.jpg", "hash-shared")]

    filtered = uploader.filter_items_to_candidate_provenance(items, candidate)

    assert len(filtered) == 1


# --- get_candidate_by_selector / select_raw_page_group ---------------------


def _repository_with_candidates(candidates: list[dict]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(tables={"product_candidates": list(candidates)})


def test_get_candidate_by_selector_resolves_exact_candidate_code():
    repository = _repository_with_candidates(
        [
            _candidate_row("cand-a", "FB-HIST-2026-IMG-001-CAN-0001", ["a.jpg"]),
            _candidate_row("cand-b", "FB-HIST-2026-IMG-001-CAN-0002", ["b.jpg"]),
        ]
    )

    candidate = uploader.get_candidate_by_selector(
        repository, candidate_code="FB-HIST-2026-IMG-001-CAN-0002"
    )

    assert candidate is not None
    assert candidate["candidate_id"] == "cand-b"


def test_select_raw_page_group_resolves_exact_candidate_not_newest(
    monkeypatch: pytest.MonkeyPatch,
):
    """The historical bug this fixes: get_candidate_for_raw_page() always
    returns whichever candidate is newest for a shared raw page,
    regardless of which one was explicitly requested. An explicit
    --candidate-code selector must always resolve to that exact
    candidate."""

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError(
            "get_candidate_for_raw_page must not be used when an explicit "
            "candidate selector is given"
        )

    monkeypatch.setattr(uploader, "get_candidate_for_raw_page", _fail_if_called)

    candidate_a = _candidate_row("cand-a", "FB-HIST-2026-IMG-001-CAN-0001", ["a.jpg"])
    candidate_b = _candidate_row("cand-b", "FB-HIST-2026-IMG-001-CAN-0002", ["b.jpg"])
    repository = _repository_with_candidates([candidate_a, candidate_b])

    groups = {RAW_PAGE_ID: [_item("a.jpg", "hash-a"), _item("b.jpg", "hash-b")]}

    raw_page_id, items, candidate = uploader.select_raw_page_group(
        repository,
        groups,
        candidate_code="FB-HIST-2026-IMG-001-CAN-0001",
        non_interactive=True,
    )

    assert candidate["candidate_id"] == "cand-a"
    # Narrowed to only this candidate's own path -- b.jpg (the sibling's
    # own image) must never be attached to candidate A.
    assert [item["metadata"]["historical_source_relative_path"] for item in items] == [
        "a.jpg"
    ]


def test_select_raw_page_group_single_candidate_post_keeps_every_item():
    """A raw page with exactly one candidate is never filtered -- this
    change must be a pure narrowing for shared posts, never a behavior
    change for the overwhelming common case (including every live-
    pipeline post, whose metadata never carries historical_source_
    relative_path at all)."""
    candidate = _candidate_row("cand-a", "FB-HIST-2026-001-CAN-0001", ["a.jpg"])
    repository = _repository_with_candidates([candidate])

    items_without_historical_metadata = [
        {
            "metadata_path": None,
            "metadata": {"raw_page_id": RAW_PAGE_ID},
            "raw_page_id": RAW_PAGE_ID,
            "source_url_id": "source-1",
            "facebook_post_url": "https://facebook.com/x",
            "image_hash": "hash-1",
            "width": 800,
            "height": 800,
        }
    ]
    groups = {RAW_PAGE_ID: items_without_historical_metadata}

    raw_page_id, items, resolved_candidate = uploader.select_raw_page_group(
        repository,
        groups,
        candidate_code="FB-HIST-2026-001-CAN-0001",
        non_interactive=True,
    )

    assert items == items_without_historical_metadata


def test_select_raw_page_group_raises_when_no_item_matches_shared_candidate():
    """A raw page shared by more than one candidate, where this
    candidate's own local_media_paths matches none of the locally
    cached metadata -- refuse rather than silently uploading nothing or
    guessing."""
    candidate_a = _candidate_row("cand-a", "FB-HIST-2026-IMG-001-CAN-0001", ["z.jpg"])
    candidate_b = _candidate_row("cand-b", "FB-HIST-2026-IMG-001-CAN-0002", ["b.jpg"])
    repository = _repository_with_candidates([candidate_a, candidate_b])

    groups = {RAW_PAGE_ID: [_item("b.jpg", "hash-b")]}

    with pytest.raises(RuntimeError, match="refusing to guess"):
        uploader.select_raw_page_group(
            repository,
            groups,
            candidate_code="FB-HIST-2026-IMG-001-CAN-0001",
            non_interactive=True,
        )


def test_select_raw_page_group_raises_for_unknown_candidate_code():
    repository = _repository_with_candidates([])

    with pytest.raises(RuntimeError, match="No local Facebook image group"):
        uploader.select_raw_page_group(
            repository,
            {RAW_PAGE_ID: [_item("a.jpg", "hash-a")]},
            candidate_code="FB-HIST-2026-IMG-001-CAN-9999",
            non_interactive=True,
        )


# --- find_existing_database_record / filter_database_duplicates -----------


def _image_row(candidate_id: str, image_hash: str) -> dict:
    return {
        "image_id": f"img-for-{candidate_id}",
        "candidate_id": candidate_id,
        "raw_page_id": RAW_PAGE_ID,
        "image_hash": image_hash,
        "storage_bucket": "product-images",
        "storage_path": f"facebook/x/{RAW_PAGE_ID}/{image_hash}.jpg",
        "image_status": "PENDING",
    }


def test_find_existing_prefers_this_candidates_own_row():
    repository = FakeSupabaseRepository(
        tables={
            "product_images": [
                _image_row("cand-a", "hash-shared"),
                _image_row("cand-b", "hash-shared"),
            ]
        }
    )

    record = uploader.find_existing_database_record(
        repository, RAW_PAGE_ID, "hash-shared", candidate_id="cand-b"
    )

    assert record["candidate_id"] == "cand-b"


def test_find_existing_falls_back_to_other_candidate_when_none_of_own():
    repository = FakeSupabaseRepository(
        tables={"product_images": [_image_row("cand-a", "hash-shared")]}
    )

    record = uploader.find_existing_database_record(
        repository, RAW_PAGE_ID, "hash-shared", candidate_id="cand-b"
    )

    assert record["candidate_id"] == "cand-a"


def test_filter_database_duplicates_raises_without_cross_candidate_share():
    """Unchanged default behavior: a genuine accidental collision (not a
    confirmed multi-product share) still raises."""
    repository = FakeSupabaseRepository(
        tables={"product_images": [_image_row("cand-a", "hash-shared")]}
    )

    with pytest.raises(RuntimeError, match="already.*linked to a different candidate"):
        uploader.filter_database_duplicates(
            repository,
            [_item("shared.jpg", "hash-shared")],
            candidate_id="cand-b",
        )


def test_filter_database_duplicates_allows_confirmed_multi_product_share():
    repository = FakeSupabaseRepository(
        tables={"product_images": [_image_row("cand-a", "hash-shared")]}
    )

    uploadable, duplicates = uploader.filter_database_duplicates(
        repository,
        [_item("shared.jpg", "hash-shared")],
        candidate_id="cand-b",
        allow_cross_candidate_share=True,
    )

    assert len(uploadable) == 1
    assert duplicates == []
