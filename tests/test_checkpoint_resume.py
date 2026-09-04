"""Checkpoint/resume confirmation (TSYC pipeline stabilization Phase 7).

pipeline_state.derive_candidate_state holds no process-local state of its
own -- it is a pure function of whatever load_candidate_bundle() reads
fresh from Supabase. "Resume" is therefore not a separate feature to add:
any new process that re-reads the same candidate_code gets the exact same
derived state and dispatch decision a still-running process would have
computed next. These tests make that property explicit and regression-
protected, complementing test_batch_orchestrator.py's
test_completed_stage_is_not_repeated.
"""
from __future__ import annotations

from typing import Any

import run_batch
from pipeline_state import derive_candidate_state, load_candidate_bundle

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "77777777-7777-7777-7777-777777777777"
INTERNAL_PRODUCT_ID = "88888888-8888-8888-8888-888888888888"
CANDIDATE_CODE = "FB-2026-001-CAN-0099"


def _repository_after_identity_verified() -> FakeSupabaseRepository:
    """A candidate stopped right after IDENTITY_VERIFIED, before
    create_internal_product.py ever ran -- simulates a process that
    exited (crash, batch stop, machine restart) between those two
    stages."""
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "candidate_code": CANDIDATE_CODE,
                    "identity_status": "IDENTITY_VERIFIED",
                }
            ],
            "product_references": [
                {
                    "reference_id": "ref-1",
                    "candidate_id": CANDIDATE_ID,
                    "match_decision": "MATCH",
                    "source_url_id": "source-url-1",
                }
            ],
        }
    )


def test_resuming_after_identity_verified_never_redoes_identity_work():
    """A fresh 'process restart' (a brand-new load_candidate_bundle call,
    with no shared in-memory state) must dispatch create_internal_
    product.py next -- never re-run reference collection or identity
    matching, both of which already completed before the stop."""
    repository = _repository_after_identity_verified()

    # Simulate a completely independent process: a new bundle load, a
    # new CandidateState derivation, nothing carried over in memory.
    bundle = load_candidate_bundle(repository, CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IDENTITY_VERIFIED"

    kind, dispatch, _description = run_batch.decide_action(state, False)

    assert kind == "invoke"
    assert dispatch.script == "create_internal_product.py"


def test_repeated_bundle_loads_are_referentially_stable():
    """Two independent reads of the same unchanged candidate must derive
    byte-identical state -- the precondition for safe resume: nothing
    about *when* or *how many times* a candidate is re-read changes the
    decision, only the persisted row data does."""
    repository = _repository_after_identity_verified()

    first_state = derive_candidate_state(
        load_candidate_bundle(repository, CANDIDATE_CODE)
    )
    second_state = derive_candidate_state(
        load_candidate_bundle(repository, CANDIDATE_CODE)
    )

    assert first_state.derived_state == second_state.derived_state
    assert first_state.human_gate == second_state.human_gate
    assert first_state.blocked == second_state.blocked
    assert first_state.warnings == second_state.warnings


def test_resuming_after_internal_product_created_never_redoes_earlier_stages():
    """Once create_internal_product.py has already run, a fresh resume
    must dispatch prepare_product_content.py -- never re-create the
    internal product, and never fall back to identity/reference stages."""
    repository = _repository_after_identity_verified()
    repository.client.table("internal_products").insert(
        {
            "internal_product_id": INTERNAL_PRODUCT_ID,
            "candidate_id": CANDIDATE_ID,
            "product_code": f"TSYC-{CANDIDATE_CODE}",
            "content_status": "PENDING",
            # Images already approved -- CLAUDE.md's required pipeline
            # order gates on image_status before content_status, so
            # INTERNAL_PRODUCT_CREATED (content not yet drafted) is only
            # reachable once the image gate is already satisfied.
            "image_status": "APPROVED",
            "woocommerce_status": "NOT_CREATED",
        }
    ).execute()

    bundle = load_candidate_bundle(repository, CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "INTERNAL_PRODUCT_CREATED"

    kind, dispatch, _description = run_batch.decide_action(state, False)

    assert kind == "invoke"
    assert dispatch.script == "prepare_product_content.py"
