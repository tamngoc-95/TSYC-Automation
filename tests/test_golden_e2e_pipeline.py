"""Golden end-to-end regression cases (TSYC pipeline stabilization Phase 5).

Each test below exercises the composed decision chain a real candidate
would pass through -- identity_rules / image_rules / readiness_rules
producing a DecisionResult, a bundle reflecting what that decision would
persist to the database, and pipeline_state.derive_candidate_state (the
one place that turns a bundle into "what stage is this candidate at, and
does an operator need to do anything") reading it back -- rather than
testing each rule module in isolation.

Fully offline: FakeSupabaseRepository backs every Supabase read, and
historical_image_extraction.check_capability is monkeypatched where a
scenario needs it. No production DB writes, no WooCommerce calls, no live
Facebook/export-archive access.
"""
from __future__ import annotations

from typing import Any

import pytest

import pipeline_state
from pipeline_state import derive_candidate_state, load_candidate_bundle
from src.domain.decisions import Outcome
from src.domain.identity_status import MatchDecision
from src.domain.rules import identity_rules, image_rules, readiness_rules
from src.services.historical_image_extraction import CapabilityStatus

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
INTERNAL_PRODUCT_ID = "bbbbbbbb-0000-0000-0000-000000000001"
REFERENCE_ID = "cccccccc-0000-0000-0000-000000000001"
REFERENCE_ID_2 = "cccccccc-0000-0000-0000-000000000002"
RAW_PAGE_ID = "raw-page-golden-1"


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": "FB-2026-001-CAN-0001",
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Doraemon Tap 1",
        "extracted_author": None,
        "possible_isbn": None,
        "source_evidence": {},
    }
    row.update(overrides)
    return row


def _internal_product(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": "TSYC-FB-2026-001-CAN-0001",
        "isbn": "9786041234567",
        "weight_grams": 250,
        "content_status": "PENDING",
        "image_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
    }
    row.update(overrides)
    return row


def _bundle_repository(
    candidate: dict[str, Any],
    *,
    references: list[dict[str, Any]] | None = None,
    internal_product: dict[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
    contents: list[dict[str, Any]] | None = None,
    siblings: list[dict[str, Any]] | None = None,
) -> FakeSupabaseRepository:
    tables: dict[str, list[dict[str, Any]]] = {
        "product_candidates": [candidate, *(siblings or [])],
        "product_references": references or [],
        "product_images": images or [],
    }
    if internal_product is not None:
        tables["internal_products"] = [internal_product]
        tables["product_contents"] = contents or []

    return FakeSupabaseRepository(tables=tables)


# ---------------------------------------------------------------------
# 1. Valid historical candidate -> READY_FOR_DRAFT
# ---------------------------------------------------------------------


def test_golden_valid_historical_candidate_reaches_ready_for_draft():
    candidate = _candidate(
        candidate_code="FB-HIST-2026-AUTOIMPORT-CAN-0010",
        identity_status="IDENTITY_VERIFIED",
    )
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": MatchDecision.MATCH,
        "source_url_id": "source-url-1",
    }
    internal_product = _internal_product(
        content_status="APPROVED",
        image_status="APPROVED",
    )
    contents = [
        {
            "internal_product_id": INTERNAL_PRODUCT_ID,
            "content_language": "vi",
            "content_status": "APPROVED",
            "review_required": False,
        }
    ]
    images = [
        {
            "image_id": "image-1",
            "candidate_id": CANDIDATE_ID,
            "image_status": "VALIDATED",
            "is_selected_main_image": True,
            "is_publish_eligible": True,
            "usage_rights_status": "STORE_OWNED",
        }
    ]

    repository = _bundle_repository(
        candidate,
        references=[reference],
        internal_product=internal_product,
        images=images,
        contents=contents,
    )
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])

    # Stage preflight: every readiness gate is satisfied given this bundle.
    draft_preflight = pipeline_state.stage_preflight_draft(bundle)
    assert draft_preflight.outcome == Outcome.AUTO_PASS

    # And the pipeline-state milestone one stage earlier confirms the
    # image/content sub-machine reached the pre-draft milestone.
    state = derive_candidate_state(bundle)
    assert state.derived_state == "IMAGE_VALIDATED"

    # Once check_draft_readiness.py has written READY_FOR_DRAFT, the
    # candidate becomes the single required human (Woo) gate.
    ready_internal_product = dict(internal_product, woocommerce_status="READY_FOR_DRAFT")
    ready_bundle = load_candidate_bundle(
        _bundle_repository(
            candidate,
            references=[reference],
            internal_product=ready_internal_product,
            images=images,
            contents=contents,
        ),
        candidate["candidate_code"],
    )
    ready_state = derive_candidate_state(ready_bundle)
    assert ready_state.derived_state == "READY_FOR_DRAFT"
    assert ready_state.human_gate is True
    assert ready_state.outcome == Outcome.REVIEW_REQUIRED


# ---------------------------------------------------------------------
# 2. Valid identity, no image
# ---------------------------------------------------------------------


def test_golden_valid_identity_no_image():
    candidate = _candidate(identity_status="IDENTITY_VERIFIED")
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": MatchDecision.MATCH,
        "source_url_id": "source-url-1",
    }
    internal_product = _internal_product()

    repository = _bundle_repository(
        candidate, references=[reference], internal_product=internal_product
    )
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_PENDING"
    assert state.human_gate is True
    assert "upload_facebook_images_to_supabase.py" in state.human_gate_reason


# ---------------------------------------------------------------------
# 3. Ambiguous group image (multi-product historical post)
# ---------------------------------------------------------------------


def test_golden_ambiguous_group_image(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        pipeline_state,
        "check_historical_image_capability",
        lambda _root: CapabilityStatus(available=True, reason="Archive present."),
    )

    candidate = _candidate(
        candidate_code="FB-HIST-2026-AUTOIMPORT-CAN-0020",
        identity_status="IDENTITY_VERIFIED",
        source_evidence={
            "local_media_paths": ["your_facebook_activity/posts/media/x/1.jpg"]
        },
    )
    sibling = {
        "candidate_id": "aaaaaaaa-0000-0000-0000-000000000099",
        "candidate_code": "FB-HIST-2026-AUTOIMPORT-CAN-0021",
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_PENDING",
    }
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": MatchDecision.MATCH,
        "source_url_id": "source-url-1",
    }
    internal_product = _internal_product()

    repository = _bundle_repository(
        candidate,
        references=[reference],
        internal_product=internal_product,
        siblings=[sibling],
    )
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS"
    assert state.outcome == Outcome.REVIEW_REQUIRED
    assert "FB-HIST-2026-AUTOIMPORT-CAN-0021" in state.human_gate_reason


# ---------------------------------------------------------------------
# 4. Rights unknown
# ---------------------------------------------------------------------


def test_golden_rights_unknown():
    rights_decision = image_rules.evaluate_rights_classification(
        rights_status="RIGHTS_UNKNOWN",
        policy_established=False,
    )
    assert rights_decision.outcome == Outcome.REVIEW_REQUIRED

    candidate = _candidate(identity_status="IDENTITY_VERIFIED")
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": MatchDecision.MATCH,
        "source_url_id": "source-url-1",
    }
    internal_product = _internal_product()
    images = [
        {
            "image_id": "image-1",
            "candidate_id": CANDIDATE_ID,
            "image_status": "PENDING",
            "is_selected_main_image": False,
            "is_publish_eligible": False,
            "usage_rights_status": "RIGHTS_UNKNOWN",
        }
    ]

    repository = _bundle_repository(
        candidate, references=[reference], internal_product=internal_product, images=images
    )
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    state = derive_candidate_state(bundle)

    assert state.derived_state == "RIGHTS_REVIEW_REQUIRED"
    assert state.human_gate is True


# ---------------------------------------------------------------------
# 5. Valid STORE_OWNED image
# ---------------------------------------------------------------------


def test_golden_valid_store_owned_image():
    rights_decision = image_rules.evaluate_rights_classification(
        rights_status="STORE_OWNED",
        policy_established=True,
    )
    assert rights_decision.outcome == Outcome.AUTO_PASS
    assert rights_decision.rule_code == image_rules.IMAGE_STORE_OWNED_EXACT

    main_image_decision = image_rules.evaluate_main_image_selection(
        [
            {
                "image_id": "image-1",
                "image_status": "VALIDATED",
                "usage_rights_status": "STORE_OWNED",
            }
        ]
    )
    assert main_image_decision.outcome == Outcome.AUTO_PASS
    assert main_image_decision.evidence["selected_image_id"] == "image-1"


# ---------------------------------------------------------------------
# 6. Identity ambiguity (conflicting credible sources)
# ---------------------------------------------------------------------


def test_golden_identity_ambiguity():
    candidate = _candidate(
        extracted_title="Doraemon Tap 1", extracted_author="Fujiko F. Fujio"
    )
    matching_reference = {
        "reference_id": REFERENCE_ID,
        "reference_title": "Doraemon Tap 1",
        "reference_author": "Fujiko F. Fujio",
    }
    conflicting_reference = {
        "reference_id": REFERENCE_ID_2,
        "reference_title": "Nhat Ky Chu Nhoc Nghich Ngom",
        "reference_author": "Jeff Kinney",
    }

    decision = identity_rules.evaluate_candidate_identity(
        candidate, [matching_reference, conflicting_reference]
    )

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == identity_rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert decision.evidence["has_genuine_conflict"] is True

    # What a script applying this decision would persist, then read back.
    candidate_persisted = dict(candidate, identity_status="IDENTITY_CONFLICT")
    repository = _bundle_repository(candidate_persisted)
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IDENTITY_CONFLICT"
    assert state.human_gate is True


# ---------------------------------------------------------------------
# 7. CAN-0039-style unusable reference (empty crawl)
# ---------------------------------------------------------------------


def test_golden_unusable_reference_never_fabricates_a_decision():
    """Regression companion to
    tests/test_match_candidate_identity_hardening.py's CAN-0039 fix: an
    empty-crawl reference must be excluded from evaluation entirely, not
    scored as a confirmed rejection, and must never receive a fabricated
    non-null match_decision."""
    candidate = _candidate()
    unusable_reference = {
        "reference_id": REFERENCE_ID,
        "reference_title": "",  # empty crawl -- is_reference_evaluable() == False
        "reference_author": None,
    }

    assert identity_rules.is_reference_evaluable(unusable_reference) is False

    decision = identity_rules.evaluate_candidate_identity(candidate, [unusable_reference])

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == identity_rules.IDENTITY_NO_USABLE_EVIDENCE
    assert decision.evidence["has_genuine_conflict"] is False
    assert decision.evidence["match_decision"] == MatchDecision.MANUAL_REVIEW

    # The unusable reference itself keeps match_decision=None (cleared,
    # never fabricated) -- pipeline_state must then treat it as still
    # unresolved, eligible for another automated matching attempt, not
    # as a human gate.
    persisted_reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": None,
        "source_url_id": "source-url-1",
    }
    repository = _bundle_repository(candidate, references=[persisted_reference])
    bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_COLLECTED"
    assert state.human_gate is False


# ---------------------------------------------------------------------
# 8. Publisher conflict
# ---------------------------------------------------------------------


def test_golden_publisher_conflict():
    candidate = _candidate(extracted_title="Doraemon Tap 1", extracted_author=None)
    reference_a = {
        "reference_id": REFERENCE_ID,
        "reference_title": "Doraemon Tap 1",
        "reference_author": None,
        "reference_publisher": "NXB Kim Dong",
    }
    reference_b = {
        "reference_id": REFERENCE_ID_2,
        "reference_title": "Doraemon Tap 1",
        "reference_author": None,
        "reference_publisher": "NXB Tre",
    }

    decision = identity_rules.evaluate_candidate_identity(
        candidate, [reference_a, reference_b]
    )

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == identity_rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert decision.evidence.get("publisher_conflict") is True


# ---------------------------------------------------------------------
# 9. Invalid ISBN (893-prefixed barcode, never treated as ISBN)
# ---------------------------------------------------------------------


def test_golden_invalid_isbn_barcode_never_treated_as_isbn():
    """CLAUDE.md section 2.3: an 893-prefixed identifier is a barcode,
    never an ISBN, even when it is superficially 13 digits."""
    assert identity_rules.looks_like_valid_isbn("8931234567890") is False
    assert identity_rules.looks_like_valid_isbn("9786041234567") is True

    candidate = _candidate(
        extracted_title="Doraemon Tap 1",
        extracted_author="Fujiko F. Fujio",
        possible_isbn="8931234567890",
    )
    reference = {
        "reference_id": REFERENCE_ID,
        "reference_title": "Doraemon Tap 1",
        "reference_author": "Fujiko F. Fujio",
        "reference_isbn": "9786041234567",
    }

    result = identity_rules.evaluate_single_reference_identity(candidate, reference)

    # The barcode must never register as an ISBN match OR an ISBN
    # conflict -- it falls through to the title/author comparison.
    assert result.evidence["isbn_match"] is False
    assert result.evidence["isbn_conflict"] is False
    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == identity_rules.IDENTITY_EXACT_TITLE_AUTHOR


# ---------------------------------------------------------------------
# 10. Idempotent rerun
# ---------------------------------------------------------------------


def test_golden_idempotent_rerun():
    candidate = _candidate(identity_status="IDENTITY_VERIFIED")
    reference = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": MatchDecision.MATCH,
        "source_url_id": "source-url-1",
    }
    internal_product = _internal_product()

    repository = _bundle_repository(
        candidate, references=[reference], internal_product=internal_product
    )

    first_bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    first_state = derive_candidate_state(first_bundle)

    second_bundle = load_candidate_bundle(repository, candidate["candidate_code"])
    second_state = derive_candidate_state(second_bundle)

    assert first_state.derived_state == second_state.derived_state
    assert first_state.human_gate == second_state.human_gate
    assert first_state.human_gate_reason == second_state.human_gate_reason

    # The identity rule engine itself is equally idempotent given an
    # unchanged reference set.
    identity_candidate = _candidate(
        extracted_title="Doraemon Tap 1", extracted_author="Fujiko F. Fujio"
    )
    identity_reference = {
        "reference_id": REFERENCE_ID,
        "reference_title": "Doraemon Tap 1",
        "reference_author": "Fujiko F. Fujio",
    }
    first_decision = identity_rules.evaluate_candidate_identity(
        identity_candidate, [identity_reference]
    )
    second_decision = identity_rules.evaluate_candidate_identity(
        identity_candidate, [identity_reference]
    )

    assert first_decision.outcome == second_decision.outcome
    assert first_decision.rule_code == second_decision.rule_code
    assert first_decision.evidence == second_decision.evidence
