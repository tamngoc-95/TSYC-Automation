"""Automated tests for the internal-product creation gate.

Covers scripts/create_internal_product.py: the mandatory gate that must hold
before an internal_products row is ever created from a candidate (CLAUDE.md
"Mandatory gates: Before create_internal_product.py").

Pure/offline: no live Supabase, no WooCommerce, no Facebook, no browser, no
human input. Repository access is a FakeSupabaseRepository (in-memory dict
rows) so the real gate functions run against realistic query chains without
touching a live project.
"""

from __future__ import annotations

import copy

import pytest

import create_internal_product as cip

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "22222222-2222-2222-2222-222222222222"
OTHER_CANDIDATE_ID = "99999999-9999-9999-9999-999999999999"
REFERENCE_ID = "33333333-3333-3333-3333-333333333333"
SOURCE_URL_ID = "44444444-4444-4444-4444-444444444444"


def make_verified_candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": "FB-2026-001-CAN-0001",
        "candidate_type": "SINGLE_BOOK",
        "extracted_title": "Original Extracted Title",
        "extracted_author": None,
        "possible_isbn": None,
        "verified_title": "Verified Book Title",
        "verified_isbn": None,
        "verified_author": None,
        "verified_publisher": None,
        "verified_page_count": None,
        "verified_weight_grams": None,
        "verified_length_cm": None,
        "verified_width_cm": None,
        "verified_height_cm": None,
        "identity_status": "IDENTITY_VERIFIED",
        "workflow_status": "IDENTITY_VERIFIED",
        "identity_confidence": 0.95,
        "source_evidence": {},
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    candidate.update(overrides)
    return candidate


def make_match_reference(**overrides) -> dict:
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "source_url_id": SOURCE_URL_ID,
        "source_type": "PUBLISHER",
        "source_name": "NXB Kim Đồng",
        "source_url": "https://kimdong.com.vn/example-book",
        "reference_title": "Verified Book Title",
        "reference_isbn": "9786041234567",
        "reference_author": "Author Name",
        "reference_publisher": "NXB Kim Đồng",
        "reference_page_count": 120,
        "reference_weight_grams": 250,
        "reference_length_cm": 20,
        "reference_width_cm": 14,
        "reference_height_cm": 1,
        "reference_cover_price_vnd": 85000,
        "reference_description": "A verified reference description.",
        "reference_image_url": "https://kimdong.com.vn/example-book.jpg",
        "match_decision": "MATCH",
        "match_confidence": 0.98,
        "source_priority": 1,
        "raw_metadata": {},
    }
    reference.update(overrides)
    return reference


# --------------------------------------------------------------------------
# 1. creation is refused when candidate identity_status != IDENTITY_VERIFIED
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identity_status",
    ["IDENTITY_PENDING", "IDENTITY_REJECTED", "IDENTITY_NEEDS_REVIEW", None],
)
def test_creation_refused_when_identity_not_verified(identity_status):
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate(identity_status=identity_status)
    reference = make_match_reference()

    with pytest.raises(RuntimeError, match="IDENTITY_VERIFIED"):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=reference,
            images=[],
        )

    # No internal_products row must be written when the gate refuses.
    assert repository.client.tables.get("internal_products", []) == []


def test_get_verified_candidate_excludes_non_verified_identity():
    """The query-level gate must never even surface a non-verified candidate."""
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [
                make_verified_candidate(identity_status="IDENTITY_PENDING"),
            ],
        }
    )

    result = cip.get_verified_candidate(
        repository=repository,
        candidate_code=None,
        candidate_id=None,
    )

    assert result is None


# --------------------------------------------------------------------------
# 2. creation is refused when no MATCH product_reference exists
# --------------------------------------------------------------------------


def test_creation_refused_when_reference_is_none():
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate()

    with pytest.raises(RuntimeError, match="No matched product reference"):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=None,
            images=[],
        )

    assert repository.client.tables.get("internal_products", []) == []


def test_creation_refused_when_reference_is_not_a_match_decision():
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate()
    reference = make_match_reference(match_decision="REJECTED")

    with pytest.raises(RuntimeError, match="match_decision = MATCH"):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=reference,
            images=[],
        )


def test_get_best_matched_reference_returns_none_when_no_match_exists():
    repository = FakeSupabaseRepository(
        tables={
            "product_references": [
                make_match_reference(match_decision="REJECTED"),
                make_match_reference(
                    reference_id="other-ref",
                    match_decision="AMBIGUOUS",
                ),
            ],
        }
    )

    result = cip.get_best_matched_reference(
        repository=repository,
        candidate_id=CANDIDATE_ID,
    )

    assert result is None


# --------------------------------------------------------------------------
# 3. creation is refused when MATCH reference belongs to another candidate
# --------------------------------------------------------------------------


def test_creation_refused_when_reference_belongs_to_another_candidate():
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate()
    reference = make_match_reference(candidate_id=OTHER_CANDIDATE_ID)

    with pytest.raises(RuntimeError, match="not linked to the selected candidate"):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=reference,
            images=[],
        )

    assert repository.client.tables.get("internal_products", []) == []


def test_get_best_matched_reference_only_returns_rows_for_requested_candidate():
    """Query-level gate: a MATCH belonging to another candidate must not surface."""
    repository = FakeSupabaseRepository(
        tables={
            "product_references": [
                make_match_reference(candidate_id=OTHER_CANDIDATE_ID),
            ],
        }
    )

    result = cip.get_best_matched_reference(
        repository=repository,
        candidate_id=CANDIDATE_ID,
    )

    assert result is None


# --------------------------------------------------------------------------
# 4. creation is refused when source_url_id is NULL
# --------------------------------------------------------------------------


def test_creation_refused_when_source_url_id_is_null():
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate()
    reference = make_match_reference(source_url_id=None)

    with pytest.raises(RuntimeError, match="source_url_id"):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=reference,
            images=[],
        )

    assert repository.client.tables.get("internal_products", []) == []


def test_get_best_matched_reference_excludes_null_source_url_id():
    """Query-level gate: not_.is_('source_url_id','null') must filter these out."""
    repository = FakeSupabaseRepository(
        tables={
            "product_references": [
                make_match_reference(source_url_id=None),
            ],
        }
    )

    result = cip.get_best_matched_reference(
        repository=repository,
        candidate_id=CANDIDATE_ID,
    )

    assert result is None


# --------------------------------------------------------------------------
# 5. creation succeeds structurally under all satisfied gates
# --------------------------------------------------------------------------


def test_creation_succeeds_structurally_when_all_gates_satisfied():
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate()
    reference = make_match_reference()

    images = [
        {
            "image_id": "image-1",
            "candidate_id": CANDIDATE_ID,
            "image_status": "VALIDATED",
            "is_selected_main_image": True,
            "is_publish_eligible": True,
        },
    ]

    created = cip.create_internal_product(
        repository=repository,
        candidate=candidate,
        reference=reference,
        images=images,
    )

    assert created["candidate_id"] == CANDIDATE_ID
    assert created["primary_reference_id"] == REFERENCE_ID
    assert created["product_code"] == f"TSYC-{candidate['candidate_code']}"
    assert created["title"] == "Verified Book Title"
    assert created["woocommerce_status"] == "NOT_CREATED"
    assert created["image_status"] == "APPROVED"

    stored_rows = repository.client.tables["internal_products"]
    assert len(stored_rows) == 1
    assert stored_rows[0]["internal_product_id"] == created["internal_product_id"]


def test_get_verified_candidate_returns_candidate_when_all_gates_satisfied():
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [make_verified_candidate()],
        }
    )

    result = cip.get_verified_candidate(
        repository=repository,
        candidate_code=candidate_code_of(repository),
        candidate_id=None,
    )

    assert result is not None
    assert result["candidate_id"] == CANDIDATE_ID


def candidate_code_of(repository: FakeSupabaseRepository) -> str:
    return repository.client.tables["product_candidates"][0]["candidate_code"]


# --------------------------------------------------------------------------
# 6. duplicate internal-product creation is refused
# --------------------------------------------------------------------------


def test_get_verified_candidate_refuses_when_internal_product_already_exists():
    candidate = make_verified_candidate()

    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "internal_products": [
                {
                    "internal_product_id": "existing-product-id",
                    "candidate_id": CANDIDATE_ID,
                    "product_code": f"TSYC-{candidate['candidate_code']}",
                },
            ],
        }
    )

    with pytest.raises(RuntimeError, match="internal product already exists"):
        cip.get_verified_candidate(
            repository=repository,
            candidate_code=candidate["candidate_code"],
            candidate_id=None,
        )


def test_get_verified_candidate_skips_but_does_not_raise_in_batch_mode():
    """
    Without an explicit selector, an existing internal product is skipped
    (not raised) so a batch scan can move on to the next candidate -- but it
    must never be returned for re-creation.
    """
    candidate = make_verified_candidate()
    other_candidate = make_verified_candidate(
        candidate_id=OTHER_CANDIDATE_ID,
        candidate_code="FB-2026-001-CAN-0002",
    )

    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate, other_candidate],
            "internal_products": [
                {
                    "internal_product_id": "existing-product-id",
                    "candidate_id": CANDIDATE_ID,
                    "product_code": f"TSYC-{candidate['candidate_code']}",
                },
            ],
        }
    )

    result = cip.get_verified_candidate(
        repository=repository,
        candidate_code=None,
        candidate_id=None,
    )

    assert result is not None
    assert result["candidate_id"] == OTHER_CANDIDATE_ID


def test_reference_and_candidate_dicts_are_not_mutated_by_a_refused_creation():
    """A refused gate must not corrupt caller-owned data structures."""
    repository = FakeSupabaseRepository()
    candidate = make_verified_candidate(identity_status="IDENTITY_PENDING")
    reference = make_match_reference()

    candidate_before = copy.deepcopy(candidate)
    reference_before = copy.deepcopy(reference)

    with pytest.raises(RuntimeError):
        cip.create_internal_product(
            repository=repository,
            candidate=candidate,
            reference=reference,
            images=[],
        )

    assert candidate == candidate_before
    assert reference == reference_before
