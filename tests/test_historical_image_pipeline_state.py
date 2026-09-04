"""Offline tests for pipeline_state.py's FB-HIST image gates and the four
named stage preflights (TSYC pipeline stabilization Phases 3-4).

No live Supabase/filesystem dependency: FakeSupabaseRepository backs every
Supabase read, and historical_image_extraction.check_capability is
monkeypatched so the (gitignored, personal) Facebook export archive is
never actually touched.
"""
from __future__ import annotations

from typing import Any

import pytest

import pipeline_state
from pipeline_state import (
    derive_candidate_state,
    evaluate_stage_preflights,
    load_candidate_bundle,
    READY_FOR_CONTENT,
    READY_FOR_DRAFT_PREFLIGHT,
    READY_FOR_IDENTITY,
    READY_FOR_IMAGE,
)
from src.domain.decisions import Outcome
from src.services.historical_image_extraction import CapabilityStatus

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "22222222-2222-2222-2222-222222222222"
SIBLING_CANDIDATE_ID = "22222222-2222-2222-2222-222222222299"
RAW_PAGE_ID = "raw-page-1"
INTERNAL_PRODUCT_ID = "44444444-4444-4444-4444-444444444444"
HISTORICAL_CANDIDATE_CODE = "FB-HIST-2026-AUTOIMPORT-CAN-0001"
SIBLING_CANDIDATE_CODE = "FB-HIST-2026-AUTOIMPORT-CAN-0002"


def _historical_candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": HISTORICAL_CANDIDATE_CODE,
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_VERIFIED",
        "source_evidence": {
            "local_media_paths": [
                "your_facebook_activity/posts/media/x/1.jpg",
                "your_facebook_activity/posts/media/x/2.mp4",
            ],
        },
    }
    row.update(overrides)
    return row


def _internal_product(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": "TSYC-" + HISTORICAL_CANDIDATE_CODE,
        "isbn": "9786041234567",
        "weight_grams": 250,
        "content_status": "APPROVED",
        "image_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
    }
    row.update(overrides)
    return row


def _repository(
    candidate: dict[str, Any],
    internal_product: dict[str, Any] | None = None,
    siblings: list[dict[str, Any]] | None = None,
) -> FakeSupabaseRepository:
    candidates = [candidate] + list(siblings or [])
    tables: dict[str, list[dict[str, Any]]] = {"product_candidates": candidates}

    if internal_product is not None:
        tables["internal_products"] = [internal_product]

    return FakeSupabaseRepository(tables=tables)


def _patch_capability(monkeypatch: pytest.MonkeyPatch, status: CapabilityStatus) -> None:
    monkeypatch.setattr(
        pipeline_state, "check_historical_image_capability", lambda _root: status
    )


# --- IMAGE_CAPABILITY_UNAVAILABLE ------------------------------------------


def test_historical_candidate_blocked_when_capability_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=False, reason="No Facebook export archive found."),
    )

    repository = _repository(_historical_candidate(), _internal_product())
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_CAPABILITY_UNAVAILABLE"
    assert state.blocked is True
    assert state.human_gate is False
    assert state.outcome == Outcome.BLOCKED
    assert "No Facebook export archive found." in state.blocked_reason


def test_historical_candidate_pending_review_when_capability_available(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=True, reason="Facebook export archive found."),
    )

    repository = _repository(_historical_candidate(), _internal_product())
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_PENDING"
    assert state.human_gate is True
    assert "extract_historical_facebook_images.py" in state.human_gate_reason


# --- IMAGE_GROUP_OWNERSHIP_AMBIGUOUS ---------------------------------------


def test_historical_candidate_ambiguous_when_sibling_shares_raw_page(
    monkeypatch: pytest.MonkeyPatch,
):
    # Even with capability available, a shared multi-product post must
    # never auto-associate images to one candidate (CLAUDE.md section 11).
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=True, reason="Facebook export archive found."),
    )

    sibling = {
        "candidate_id": SIBLING_CANDIDATE_ID,
        "candidate_code": SIBLING_CANDIDATE_CODE,
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_PENDING",
    }
    repository = _repository(
        _historical_candidate(), _internal_product(), siblings=[sibling]
    )
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS"
    assert state.human_gate is True
    assert state.outcome == Outcome.REVIEW_REQUIRED
    assert SIBLING_CANDIDATE_CODE in state.human_gate_reason


def test_ambiguity_takes_priority_over_capability_check(
    monkeypatch: pytest.MonkeyPatch,
):
    """Ownership ambiguity is a safety judgment independent of whether the
    export archive happens to be present -- it must be reported even when
    the capability is also unavailable, not silently masked by it."""
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=False, reason="No archive."),
    )

    sibling = {
        "candidate_id": SIBLING_CANDIDATE_ID,
        "candidate_code": SIBLING_CANDIDATE_CODE,
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_PENDING",
    }
    repository = _repository(
        _historical_candidate(), _internal_product(), siblings=[sibling]
    )
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS"


# --- non-historical candidates are unaffected ------------------------------


def test_non_historical_candidate_keeps_original_image_pending_message(
    monkeypatch: pytest.MonkeyPatch,
):
    """A production (live-crawl) candidate with no local_media_paths must
    keep the exact original IMAGE_PENDING message -- this generalization
    must not change behavior for the non-historical path."""

    def _fail_if_called(_root):
        raise AssertionError(
            "check_historical_image_capability must not be called for a "
            "non-historical candidate"
        )

    monkeypatch.setattr(
        pipeline_state, "check_historical_image_capability", _fail_if_called
    )

    candidate = _historical_candidate(source_evidence={})
    repository = _repository(candidate, _internal_product())
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_PENDING"
    assert "upload_facebook_images_to_supabase.py" in state.human_gate_reason
    assert "extract_historical_facebook_images.py" not in state.human_gate_reason


# --- stage preflights -------------------------------------------------------


def test_stage_preflight_image_blocked_for_capability_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=False, reason="No archive."),
    )
    repository = _repository(_historical_candidate(), _internal_product())
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)

    result = pipeline_state.stage_preflight_image(bundle)

    assert result.outcome == Outcome.BLOCKED
    assert result.rule_code == READY_FOR_IMAGE


def test_stage_preflight_content_blocked_without_internal_product():
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": "FB-2026-001-CAN-0001",
        "identity_status": "IDENTITY_PENDING",
    }
    repository = FakeSupabaseRepository(tables={"product_candidates": [candidate]})
    bundle = load_candidate_bundle(repository, "FB-2026-001-CAN-0001")

    result = pipeline_state.stage_preflight_content(bundle)

    assert result.outcome in (Outcome.BLOCKED, Outcome.REVIEW_REQUIRED)
    assert result.rule_code == READY_FOR_CONTENT


def test_stage_preflight_identity_auto_pass_once_reference_collected():
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": "FB-2026-001-CAN-0001",
        "identity_status": "IDENTITY_PENDING",
    }
    reference = {
        "reference_id": "ref-1",
        "candidate_id": CANDIDATE_ID,
        "match_decision": None,
    }
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "product_references": [reference],
        }
    )
    bundle = load_candidate_bundle(repository, "FB-2026-001-CAN-0001")

    result = pipeline_state.stage_preflight_identity(bundle)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == READY_FOR_IDENTITY


def test_stage_preflight_draft_blocked_without_internal_product():
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": "FB-2026-001-CAN-0001",
        "identity_status": "IDENTITY_PENDING",
    }
    repository = FakeSupabaseRepository(tables={"product_candidates": [candidate]})
    bundle = load_candidate_bundle(repository, "FB-2026-001-CAN-0001")

    result = pipeline_state.stage_preflight_draft(bundle)

    assert result.outcome == Outcome.BLOCKED
    assert result.rule_code == READY_FOR_DRAFT_PREFLIGHT


def test_evaluate_stage_preflights_returns_all_four_named_results(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_capability(
        monkeypatch,
        CapabilityStatus(available=True, reason="Facebook export archive found."),
    )
    repository = _repository(_historical_candidate(), _internal_product())
    bundle = load_candidate_bundle(repository, HISTORICAL_CANDIDATE_CODE)

    results = evaluate_stage_preflights(bundle)

    assert set(results.keys()) == {
        READY_FOR_IDENTITY,
        READY_FOR_CONTENT,
        READY_FOR_IMAGE,
        READY_FOR_DRAFT_PREFLIGHT,
    }
    for decision in results.values():
        assert decision.outcome in (
            Outcome.AUTO_PASS,
            Outcome.AUTO_REJECT,
            Outcome.REVIEW_REQUIRED,
            Outcome.BLOCKED,
        )
