"""Golden tests G and K for the historical-migration draft-safe policy
(explicit shop-owner business authorization, 2026-09-04).

G: WooCommerce DRAFT creation can be dispatched automatically for an
FB-HIST candidate at READY_FOR_DRAFT, with no --allow-woo-draft flag --
while a non-historical (live pipeline) candidate at the exact same
woocommerce_status keeps the original human-gated behavior completely
unchanged.

K: rerunning the historical internal-product creation gate for an
already-created candidate is refused/skipped rather than duplicated --
the same dedup check the live pipeline already relies on, now proven
for the historical IDENTITY_PENDING (never IDENTITY_VERIFIED) path too.

Offline: pipeline_state.derive_candidate_state() is a pure function over
a plain bundle dict (no repository needed); run_batch.decide_action() is
pure over a CandidateState; create_internal_product.get_verified_candidate
uses FakeSupabaseRepository. No live Supabase/WooCommerce/network.
"""
from __future__ import annotations

import pytest

import create_internal_product as cip
import run_batch
from pipeline_state import derive_candidate_state
from support.fake_supabase import FakeSupabaseRepository


HISTORICAL_CANDIDATE_CODE = "FB-HIST-2026-002-CAN-0099"
LIVE_CANDIDATE_CODE = "FB-2026-001-CAN-0099"
CANDIDATE_ID = "cand-hist-1"
INTERNAL_PRODUCT_ID = "product-hist-1"


def _ready_internal_product(**overrides) -> dict:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": "TSYC-CAN-0099",
        "woocommerce_status": "READY_FOR_DRAFT",
        "isbn": None,
        "weight_grams": None,
    }
    row.update(overrides)
    return row


def _bundle(candidate_code: str, **product_overrides) -> dict:
    return {
        "candidate": {"candidate_id": CANDIDATE_ID, "candidate_code": candidate_code},
        "references": [],
        "discovery_sources": [],
        "images": [],
        "internal_product": _ready_internal_product(**product_overrides),
        "contents": [],
        "sync": None,
        "historical_local_media_paths": [],
        "historical_capability_available": None,
        "historical_capability_reason": None,
        "sibling_candidate_codes": [],
    }


# --- G. WooCommerce draft can be created automatically for historical ------


def test_g_historical_candidate_at_ready_for_draft_is_not_human_gated():
    state = derive_candidate_state(_bundle(HISTORICAL_CANDIDATE_CODE))

    assert state.derived_state == "READY_FOR_DRAFT_HISTORICAL"
    assert state.human_gate is False


def test_g_historical_ready_for_draft_dispatches_without_allow_woo_draft():
    state = derive_candidate_state(_bundle(HISTORICAL_CANDIDATE_CODE))

    kind, dispatch, _description = run_batch.decide_action(state, allow_woo_draft=False)

    assert kind == "invoke"
    assert dispatch is not None
    assert dispatch.script == "create_woocommerce_draft.py"


def test_g_live_candidate_at_ready_for_draft_still_human_gated():
    """Non-historical candidates keep the original human gate, completely
    unaffected by the historical policy."""
    state = derive_candidate_state(_bundle(LIVE_CANDIDATE_CODE))

    assert state.derived_state == "READY_FOR_DRAFT"
    assert state.human_gate is True

    kind, dispatch, _description = run_batch.decide_action(state, allow_woo_draft=False)

    assert kind == "human_gate"
    assert dispatch is None


def test_g_live_candidate_still_requires_explicit_allow_woo_draft_flag():
    state = derive_candidate_state(_bundle(LIVE_CANDIDATE_CODE))

    kind, dispatch, _description = run_batch.decide_action(state, allow_woo_draft=True)

    assert kind == "invoke"
    assert dispatch is run_batch.WOO_DRAFT_DISPATCH


def _approved_translations() -> list[dict]:
    return [
        {
            "internal_product_id": INTERNAL_PRODUCT_ID,
            "content_language": language,
            "content_status": "APPROVED",
            "review_required": False,
        }
        for language in ("en", "de")
    ]


def test_g_historical_ready_for_draft_requires_multilingual_content():
    """The historical Woo auto-dispatch is still guarded by the
    multilingual precondition (CLAUDE_AUTOMATION.md section 5)."""
    bundle = _bundle(HISTORICAL_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert run_batch.multilingual_content_missing(state, bundle) is True

    bundle["contents"] = _approved_translations()

    assert run_batch.multilingual_content_missing(state, bundle) is False


def test_g_multilingual_guard_ignores_non_ready_states():
    bundle = _bundle(HISTORICAL_CANDIDATE_CODE, woocommerce_status="DRAFT_CREATED")
    state = derive_candidate_state(bundle)

    assert run_batch.multilingual_content_missing(state, bundle) is False


# --- K. rerun is idempotent -------------------------------------------------


def make_historical_pending_candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": HISTORICAL_CANDIDATE_CODE,
        "candidate_type": "SINGLE_BOOK",
        "extracted_title": "Combo 4 cuốn truyện của Thomas Harris",
        "extracted_author": None,
        "possible_isbn": None,
        "verified_title": None,
        "verified_isbn": None,
        "verified_author": None,
        "verified_publisher": None,
        "verified_page_count": None,
        "verified_weight_grams": None,
        "verified_length_cm": None,
        "verified_width_cm": None,
        "verified_height_cm": None,
        "identity_status": "IDENTITY_PENDING",
        "workflow_status": "IDENTITY_PENDING",
        "identity_confidence": None,
        "source_evidence": {},
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    candidate.update(overrides)
    return candidate


def test_k_first_run_returns_the_pending_historical_candidate():
    candidate = make_historical_pending_candidate()
    repository = FakeSupabaseRepository(
        tables={"product_candidates": [candidate], "internal_products": []}
    )

    result = cip.get_verified_candidate(
        repository=repository,
        candidate_code=HISTORICAL_CANDIDATE_CODE,
    )

    assert result is not None
    assert result["candidate_id"] == CANDIDATE_ID


def test_k_rerun_after_creation_is_refused_not_duplicated():
    """Once an internal_products row exists for this historical
    candidate, an explicit rerun with the same --candidate-code must
    refuse (never silently create a second row)."""
    candidate = make_historical_pending_candidate()
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "internal_products": [
                {
                    "internal_product_id": INTERNAL_PRODUCT_ID,
                    "candidate_id": CANDIDATE_ID,
                    "product_code": f"TSYC-{HISTORICAL_CANDIDATE_CODE}",
                }
            ],
        }
    )

    with pytest.raises(RuntimeError, match="internal product already exists"):
        cip.get_verified_candidate(
            repository=repository,
            candidate_code=HISTORICAL_CANDIDATE_CODE,
        )

    # Exactly one internal_products row exists -- rerun did not duplicate it.
    assert len(repository.client.tables["internal_products"]) == 1


def test_k_bare_batch_scan_does_not_surface_historical_pending_candidates():
    """The no-selector auto-scan path stays scoped to IDENTITY_VERIFIED
    only, exactly as before this policy -- a historical draft-safe
    (IDENTITY_PENDING) candidate is only ever created via run_batch.py's
    explicit --candidate-code dispatch (see AUTOMATABLE_DISPATCH in
    run_batch.py), never picked up by a bare scan. This keeps the
    already-conservative default batch behavior unchanged; it is not a
    duplicate-creation risk since the explicit-code path (tested above)
    is the only one run_batch.py actually calls."""
    candidate = make_historical_pending_candidate()
    repository = FakeSupabaseRepository(
        tables={"product_candidates": [candidate], "internal_products": []}
    )

    result = cip.get_verified_candidate(repository=repository, candidate_code=None)

    assert result is None
