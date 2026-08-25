"""Offline tests for scripts/create_candidates_from_cleaned_posts.py's
create_candidates_from_page() automatic-extraction contract, focused on
the CANDIDATE_CODES machine-readable output contract:

    - newly created candidates -> returned/reported codes are correct
    - idempotent repeat (existing candidates, nothing new created) ->
      returned/reported codes are still correct, and no duplicate row
      is inserted
    - the output line itself uses one neutral label for both cases
      (not "CREATED_..." for codes that were not newly created)

Fully offline: FakeSupabaseRepository only, no live Supabase/WooCommerce/
Facebook/Playwright access anywhere.
"""
from __future__ import annotations

import create_candidates_from_cleaned_posts as ccfp
from support.fake_supabase import FakeSupabaseRepository


BATCH_ID = "11111111-1111-1111-1111-111111111111"
RAW_PAGE_ID = "22222222-2222-2222-2222-222222222222"
SOURCE_URL_ID = "33333333-3333-3333-3333-333333333333"

ONE_BOOK_TEXT = "“Doraemon Tap 1” của Fujiko F. Fujio, s\xe1ch c\xf2n mới 100%."


def make_batch() -> dict:
    return {"batch_id": BATCH_ID, "batch_code": ccfp.BATCH_CODE}


def make_raw_page(cleaned_text: str) -> dict:
    return {
        "raw_page_id": RAW_PAGE_ID,
        "source_url_id": SOURCE_URL_ID,
        "cleaned_text": cleaned_text,
        "page_type": "FACEBOOK_POST",
        "page_url": "https://www.facebook.com/example",
        "cleaning_status": "CLEANED",
    }


# --- format_candidate_codes_line: the output-line contract itself ------


def test_format_candidate_codes_line_uses_neutral_label():
    assert (
        ccfp.format_candidate_codes_line(["FB-2026-001-CAN-0001"])
        == "CANDIDATE_CODES: FB-2026-001-CAN-0001"
    )


def test_format_candidate_codes_line_joins_multiple_codes_with_commas():
    line = ccfp.format_candidate_codes_line(
        ["FB-2026-001-CAN-0001", "FB-2026-001-CAN-0002"]
    )

    assert line == "CANDIDATE_CODES: FB-2026-001-CAN-0001,FB-2026-001-CAN-0002"


def test_format_candidate_codes_line_handles_empty_list():
    assert ccfp.format_candidate_codes_line([]) == "CANDIDATE_CODES: "


# --- create_candidates_from_page: newly created candidates -------------


def test_automatic_one_book_extraction_returns_newly_created_code():
    repository = FakeSupabaseRepository(
        tables={
            "batches": [make_batch()],
            "product_candidates": [],
            "product_images": [],
        }
    )
    raw_page = make_raw_page(ONE_BOOK_TEXT)

    results, codes = ccfp.create_candidates_from_page(
        repository=repository,
        batch=make_batch(),
        raw_page=raw_page,
        candidate_type=ccfp.DEFAULT_CANDIDATE_TYPE,
        explicit_extractions=[],
        confirm_create=True,
        non_interactive=True,
        max_candidates=5,
    )

    assert results["CREATED"] == 1
    assert results["DUPLICATE_CANDIDATE"] == 0
    assert len(codes) == 1
    assert codes[0].startswith(f"{ccfp.BATCH_CODE}-CAN-")

    stored = repository.client.tables["product_candidates"]
    assert len(stored) == 1
    assert stored[0]["candidate_code"] == codes[0]
    assert stored[0]["extracted_title"] == "Doraemon Tap 1"


# --- create_candidates_from_page: idempotent repeat ---------------------


def test_idempotent_repeat_returns_existing_code_without_creating_new_row():
    repository = FakeSupabaseRepository(
        tables={
            "batches": [make_batch()],
            "product_candidates": [
                {
                    "candidate_id": "existing-1",
                    "batch_id": BATCH_ID,
                    "raw_page_id": RAW_PAGE_ID,
                    "candidate_code": f"{ccfp.BATCH_CODE}-CAN-0001",
                    "extracted_title": "Doraemon Tap 1",
                    "extracted_author": "Fujiko F",
                    "candidate_type": "SINGLE_BOOK",
                    "workflow_status": "EXTRACTED",
                }
            ],
            "product_images": [],
        }
    )
    raw_page = make_raw_page(ONE_BOOK_TEXT)

    results, codes = ccfp.create_candidates_from_page(
        repository=repository,
        batch=make_batch(),
        raw_page=raw_page,
        candidate_type=ccfp.DEFAULT_CANDIDATE_TYPE,
        explicit_extractions=[],
        confirm_create=True,
        non_interactive=True,
        max_candidates=5,
    )

    assert results["CREATED"] == 0
    assert results["DUPLICATE_CANDIDATE"] == 1
    assert codes == [f"{ccfp.BATCH_CODE}-CAN-0001"]
    # No second row was inserted -- still exactly the one pre-existing row.
    assert len(repository.client.tables["product_candidates"]) == 1


def test_idempotent_repeat_and_fresh_creation_produce_the_same_line_shape():
    """Both cases must be expressible through the same neutral
    format_candidate_codes_line() -- downstream (watch_facebook_
    clipboard.py) must not need to distinguish them."""
    created_line = ccfp.format_candidate_codes_line(["FB-2026-001-CAN-0002"])
    existing_line = ccfp.format_candidate_codes_line(["FB-2026-001-CAN-0001"])

    assert created_line.startswith(ccfp.CANDIDATE_CODES_PREFIX)
    assert existing_line.startswith(ccfp.CANDIDATE_CODES_PREFIX)
