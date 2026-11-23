"""CLAUDE.md 6.3 standing Fahasa pre-order draft authorization (2026-11-23).

Eligible new Fahasa pre-order books reach create_woocommerce_draft.py
without a per-batch --allow-woo-draft. Every identity, readiness,
duplicate, owner-removal and recovery gate still applies; any other
candidate keeps the READY_FOR_DRAFT human gate. Fully offline.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

import create_woocommerce_draft
import pipeline_state
import preflight_pipeline
import run_batch
from src.domain.rules import lane_rules, preorder_inventory_rules
from src.services import writer_lock
from support.fake_supabase import FakeSupabaseRepository

CANDIDATE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
INTERNAL_PRODUCT_ID = "aaaaaaaa-0000-0000-0000-000000000002"
CODE = "FAHASA-2026-006-CAN-0001"
PRODUCT_CODE = f"TSYC-{CODE}"

PREORDER_EVIDENCE = {
    "origin": "FAHASA_DISCOVERY",
    "supply_status": "PREORDER_PENDING_SUPPLY_CONFIRMATION",
}


def candidate_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CODE,
        "batch_id": "batch-1",
        "identity_status": "IDENTITY_VERIFIED",
        "workflow_status": "EXTRACTED",
        "source_evidence": dict(PREORDER_EVIDENCE),
    }
    row.update(overrides)
    return row


def product_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": PRODUCT_CODE,
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "woocommerce_status": "READY_FOR_DRAFT",
        "review_required": False,
    }
    row.update(overrides)
    return row


def contents(languages: tuple[str, ...] = ("vi", "en", "de")) -> list[dict[str, Any]]:
    return [
        {
            "product_content_id": f"content-{language}",
            "internal_product_id": INTERNAL_PRODUCT_ID,
            "content_language": language,
            "content_status": "APPROVED",
            "review_required": False,
        }
        for language in languages
    ]


def main_image(**overrides: Any) -> dict[str, Any]:
    row = {
        "image_id": "image-1",
        "candidate_id": CANDIDATE_ID,
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "is_selected_main_image": True,
        "image_status": "VALIDATED",
        "is_publish_eligible": True,
        "usage_rights_status": "SUPPLIER_APPROVED",
        "source_type": "FAHASA",
    }
    row.update(overrides)
    return row


def repository(
    *,
    candidate: dict[str, Any] | None = None,
    product: dict[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
    syncs: list[dict[str, Any]] | None = None,
    extra_candidates: list[dict[str, Any]] | None = None,
    languages: tuple[str, ...] = ("vi", "en", "de"),
) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate or candidate_row()] + list(extra_candidates or []),
            "product_references": [
                {
                    "reference_id": "ref-1",
                    "candidate_id": CANDIDATE_ID,
                    "match_decision": "MATCH",
                    "source_url_id": "source-1",
                    "source_type": "FAHASA",
                }
            ],
            "internal_products": [product or product_row()],
            "product_contents": contents(languages),
            "product_images": [main_image()] if images is None else images,
            "woocommerce_product_syncs": list(syncs or []),
        }
    )


def args(**overrides: Any) -> types.SimpleNamespace:
    values = dict(dry_run=False, non_interactive=True, allow_woo_draft=False, batch_code=None, verbose=False)
    values.update(overrides)
    return types.SimpleNamespace(**values)


def recording_runner():
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    return runner, calls


def derive(repo: FakeSupabaseRepository, code: str = CODE) -> pipeline_state.CandidateState:
    return pipeline_state.derive_candidate_state(pipeline_state.load_candidate_bundle(repo, code))


def run_one(repo: FakeSupabaseRepository, code: str = CODE, **arg_overrides: Any):
    runner, calls = recording_runner()
    report = run_batch.process_one_candidate(code, args(**arg_overrides), repo, runner, lambda _p: True, None)
    return report, calls


def creator_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[1].endswith("create_woocommerce_draft.py")]


# -- Standing authorization: eligible product, no repeated approval --------


def test_eligible_fahasa_preorder_derives_standing_state_without_human_gate():
    state = derive(repository())
    assert state.derived_state == "READY_FOR_DRAFT_FAHASA"
    assert state.human_gate is False
    assert state.recovery_state is None


def test_eligible_fahasa_preorder_dispatches_creator_without_allow_flag():
    report, calls = run_one(repository(), allow_woo_draft=False)
    created = creator_calls(calls)
    assert len(created) == 1
    assert created[0][-4:] == ["--product-code", PRODUCT_CODE, "--non-interactive", "--confirm-create"]
    assert report.result != "HUMAN_GATE"


def test_standing_dispatch_never_carries_publish_or_price_arguments():
    entry = run_batch.AUTOMATABLE_DISPATCH["READY_FOR_DRAFT_FAHASA"]
    built = " ".join(entry.build_args(pipeline_state.CandidateState(
        candidate_code=CODE, candidate_id=CANDIDATE_ID, product_code=PRODUCT_CODE,
        derived_state="READY_FOR_DRAFT_FAHASA",
    ))).lower()
    for forbidden in ("publish", "price", "status"):
        assert forbidden not in built


def test_only_the_two_standing_states_reach_the_creator_without_the_flag():
    unflagged = {
        state for state, entry in run_batch.AUTOMATABLE_DISPATCH.items()
        if entry.script == "create_woocommerce_draft.py"
    }
    assert unflagged == {"READY_FOR_DRAFT_HISTORICAL", "READY_FOR_DRAFT_FAHASA"}
    assert unflagged == set(run_batch.STANDING_WOO_DRAFT_STATES)


def test_orchestrator_safety_preflight_passes_with_standing_state():
    check = preflight_pipeline.check_orchestrator_safety()
    assert check.status == preflight_pipeline.CheckStatus.PASS, check.message


def test_ready_for_draft_fahasa_is_a_ready_lane_state_for_multilingual_guard():
    assert lane_rules.is_ready_for_draft_state("READY_FOR_DRAFT_FAHASA")


def test_standing_state_without_en_de_never_reaches_creator():
    repo = repository(languages=("vi",))
    _report, calls = run_one(repo)
    assert creator_calls(calls) == []


# -- Out of scope: keep the human gate --------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        candidate_row(candidate_code="FB-2026-009-CAN-0001"),  # live Facebook
        candidate_row(source_evidence={"origin": "FAHASA_DISCOVERY", "supply_status": "IN_STOCK"}),
        candidate_row(source_evidence={}),  # FAHASA- prefix but no pre-order evidence
        candidate_row(identity_status="IDENTITY_PENDING"),
    ],
    ids=["fb-live", "not-preorder", "no-evidence", "identity-pending"],
)
def test_ineligible_candidate_keeps_ready_for_draft_human_gate(candidate):
    repo = repository(candidate=candidate)
    state = derive(repo, candidate["candidate_code"])
    assert state.derived_state != "READY_FOR_DRAFT_FAHASA"
    _report, calls = run_one(repo, candidate["candidate_code"], allow_woo_draft=False)
    assert creator_calls(calls) == []


@pytest.mark.parametrize(
    "images",
    [
        [],  # no main image
        [main_image(), main_image(image_id="image-2")],  # two main images
        [main_image(image_status="PENDING")],
        [main_image(usage_rights_status="RIGHTS_UNKNOWN")],
        [main_image(is_publish_eligible=False)],
    ],
    ids=["none", "two", "not-validated", "rights-unknown", "not-eligible"],
)
def test_failing_full_readiness_keeps_human_gate(images):
    repo = repository(images=images)
    assert derive(repo).derived_state != "READY_FOR_DRAFT_FAHASA"
    _report, calls = run_one(repo)
    assert creator_calls(calls) == []


# -- Owner removal, duplicates, recovery, idempotent resume -----------------


def test_owner_removed_product_is_terminal_and_never_recreated():
    repo = repository(
        product=product_row(woocommerce_status="DRAFT_CREATED"),
        syncs=[{
            "sync_id": "sync-1", "internal_product_id": INTERNAL_PRODUCT_ID,
            "woocommerce_status": "DRAFT_CREATED", "woocommerce_product_id": 4930,
            "response_payload": {pipeline_state.woo_remote_lifecycle.REMOVAL_MARKER_KEY: {
                "state": "REMOTE_TRASHED", "woocommerce_product_id": 4930,
            }},
        }],
    )
    state = derive(repo)
    assert state.derived_state == "REMOTE_REMOVED"
    assert state.terminal is True
    _report, calls = run_one(repo)
    assert creator_calls(calls) == []


def test_catalog_duplicate_marker_blocks_standing_dispatch():
    marker_key = pipeline_state.woo_catalog_identity.MARKER_KEY
    repo = repository(product=product_row(product_metadata={
        marker_key: {"classification": "POSSIBLE_DUPLICATE", "reason": "same title", "terminal": False, "product_code": PRODUCT_CODE},
    }))
    state = derive(repo)
    assert state.derived_state == "CATALOG_DUPLICATE_REVIEW"
    _report, calls = run_one(repo)
    assert creator_calls(calls) == []


def test_existing_sync_blocks_standing_dispatch():
    repo = repository(syncs=[{
        "sync_id": "sync-1", "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_status": "FAILED", "woocommerce_product_id": 5500, "response_payload": {},
    }])
    _report, calls = run_one(repo)
    assert creator_calls(calls) == []


def test_uncertain_create_recovery_is_never_retried():
    repo = repository(syncs=[{
        "sync_id": "sync-1", "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_status": "FAILED", "woocommerce_product_id": None,
        "response_payload": {"recovery_required": True},
    }])
    state = derive(repo)
    assert state.derived_state != "READY_FOR_DRAFT_FAHASA"
    assert state.human_gate is True
    _report, calls = run_one(repo, allow_woo_draft=True)
    assert creator_calls(calls) == []


def test_resumed_batch_is_idempotent_for_already_created_drafts():
    repo = repository(
        product=product_row(woocommerce_status="DRAFT_CREATED"),
        syncs=[{
            "sync_id": "sync-1", "internal_product_id": INTERNAL_PRODUCT_ID,
            "woocommerce_status": "DRAFT_CREATED", "woocommerce_product_id": 5394,
            "product_sku": PRODUCT_CODE, "response_payload": {},
        }],
    )
    _report, calls = run_one(repo)
    # Only read-only reconciliation, never a second create.
    assert creator_calls(calls) == []


def test_failed_candidate_read_is_isolated(monkeypatch):
    other = candidate_row(candidate_id="bbbbbbbb-0000-0000-0000-000000000001", candidate_code="FAHASA-2026-006-CAN-0002")
    repo = repository(extra_candidates=[other])
    real = run_batch.load_candidate_bundle

    def flaky(repo_, code):
        if code == CODE:
            raise ConnectionError("TLS handshake timed out")
        return real(repo_, code)

    monkeypatch.setattr(run_batch, "load_candidate_bundle", flaky)
    runner, _calls = recording_runner()
    report = run_batch.process_one_candidate(CODE, args(), repo, runner, lambda _p: True, None)
    assert report.result == run_batch.READ_FAILED
    assert _calls == []


# -- Pre-order settings and draft-only payload ------------------------------


def test_preorder_inventory_settings_are_exact():
    assert preorder_inventory_rules.is_fahasa_preorder_candidate(candidate_row())
    assert preorder_inventory_rules.build_inventory_payload() == {
        "manage_stock": True, "stock_quantity": 0, "backorders": "yes",
    }


def test_creator_payload_is_draft_only_with_no_price_field():
    source = Path(create_woocommerce_draft.__file__).read_text(encoding="utf-8")
    assert '"status": "draft"' in source
    assert '"status": "publish"' not in source
    for field in ('"regular_price":', '"sale_price":'):
        assert field not in source


# -- Tests never touch the production writer lock ---------------------------


def test_writer_lock_default_path_is_redirected_during_tests():
    from conftest import PRODUCTION_WRITER_LOCK_PATH

    lock = writer_lock.ProductionWriterLock("pytest")
    assert lock.path != PRODUCTION_WRITER_LOCK_PATH
    assert writer_lock.inspect_lock().path != PRODUCTION_WRITER_LOCK_PATH
