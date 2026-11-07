"""Regression tests for src/domain/woo_status_consistency.py and its wiring
into scripts/audit_pipeline_state.py (audit 1.1.0).

Incident (2026-11-07 audit): 27 internal products still said DRAFT_CREATED
after the owner published (25) or trashed (2) them, and two READY_FOR_DRAFT
rows (FB-HIST-IMG-002-CAN-0019, IMG-004-CAN-0031) were already terminal.
The live remote drift is the job of audit_woo_remote_status.py; these
tests pin what the local rows alone must reveal.

Offline: plain dicts only, no Supabase/WooCommerce access.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

import audit_pipeline_state as audit
from pipeline_state import ACCEPTED_WARNING_CODES
from src.domain import woo_catalog_identity as wci
from src.domain import woo_remote_lifecycle as wrl
from src.domain import woo_status_consistency as wsc
from src.domain.woocommerce_status import WooCommerceStatus as S
from src.domain.woocommerce_status import WooCommerceSyncStatus as SS

PID = "ip-1"
CODE = "TSYC-FB-HIST-2026-TEST-CAN-0001"
WC_ID = 4930


def _marker(state: str = wrl.REMOTE_TRASHED, woo_id: int = WC_ID) -> dict[str, Any]:
    return wrl.build_removal_marker(state, woo_id, CODE, "test", "2026-11-07T00:00:00Z", "test")


def _product(stored: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "internal_product_id": PID,
        "product_code": CODE,
        "woocommerce_status": stored,
        "product_metadata": metadata or {},
    }


def _sync(
    sync_status: str = SS.DRAFT_CREATED,
    woo_id: int | None = WC_ID,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "internal_product_id": PID,
        "woocommerce_product_id": woo_id,
        "woocommerce_status": sync_status,
        "product_sku": CODE,
        "response_payload": payload or {},
    }


def _removed(state: str = wrl.REMOTE_TRASHED) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both rows as the official writer leaves a confirmed removal."""
    marker = _marker(state)
    return (
        _product(S.FAILED, {wrl.REMOVAL_MARKER_KEY: marker}),
        _sync(SS.FAILED, payload={wrl.REMOVAL_MARKER_KEY: marker}),
    )


def _catalog(classification: str = wci.EXISTING_PRODUCT) -> dict[str, Any]:
    return {
        wci.MARKER_KEY: {
            "product_code": CODE,
            "classification": classification,
            "terminal": classification in wci.TERMINAL_CLASSIFICATIONS,
            "reason": "test",
        }
    }


def _codes(findings) -> dict[str, str]:
    return {f.code: f.severity for f in findings}


# --- consistent shapes produce nothing -----------------------------------------


@pytest.mark.parametrize("stored", [S.DRAFT_CREATED, S.PUBLISHED])
def test_draft_and_published_with_a_live_sync_are_consistent(stored):
    """Remote 'draft' and 'publish' both map to sync DRAFT_CREATED."""
    assert wsc.evaluate_stored_woo_status(_product(stored), [_sync()]) == []


@pytest.mark.parametrize("stored", [S.NOT_CREATED, S.READY_FOR_DRAFT])
def test_pre_create_without_any_sync_is_consistent(stored):
    assert wsc.evaluate_stored_woo_status(_product(stored), []) == []


@pytest.mark.parametrize("state", sorted(wrl.REMOTE_REMOVAL_STATES))
def test_confirmed_removal_on_both_rows_is_consistent(state):
    product, sync = _removed(state)
    assert wsc.evaluate_stored_woo_status(product, [sync]) == []


def test_pending_sync_without_id_is_not_an_unresolved_create():
    """PENDING means no remote call was made yet."""
    findings = wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT), [_sync(SS.PENDING, woo_id=None)])
    assert findings == []


# --- published products ---------------------------------------------------------


def test_published_without_any_remote_id_is_an_error():
    findings = wsc.evaluate_stored_woo_status(_product(S.PUBLISHED), [])
    assert _codes(findings) == {wsc.LIVE_WITHOUT_REMOTE_ID: "ERROR"}


@pytest.mark.parametrize("stored", [S.PUBLISHED, S.DRAFT_CREATED])
def test_live_status_with_only_a_failed_unconfirmed_sync_is_an_error(stored):
    findings = wsc.evaluate_stored_woo_status(_product(stored), [_sync(SS.FAILED)])
    assert _codes(findings) == {wsc.LIVE_WITH_FAILED_SYNC: "ERROR"}


# --- trashed / removed products ------------------------------------------------


@pytest.mark.parametrize("stored", [S.DRAFT_CREATED, S.PUBLISHED, S.READY_FOR_DRAFT, S.NOT_CREATED])
@pytest.mark.parametrize("state", sorted(wrl.REMOTE_REMOVAL_STATES))
def test_removed_product_with_a_live_or_pre_create_status_is_an_error(stored, state):
    product, sync = _removed(state)
    product["woocommerce_status"] = stored
    findings = wsc.evaluate_stored_woo_status(product, [sync])
    assert _codes(findings) == {wsc.LIVE_AFTER_CONFIRMED_REMOVAL: "ERROR"}


# --- interrupted reconciliation --------------------------------------------------


def test_sync_marked_removed_but_product_untouched_is_caught():
    """Writer stopped after the sync row: stored still DRAFT_CREATED, no product marker."""
    marker = _marker()
    findings = wsc.evaluate_stored_woo_status(
        _product(S.DRAFT_CREATED), [_sync(SS.FAILED, payload={wrl.REMOVAL_MARKER_KEY: marker})]
    )
    assert _codes(findings) == {
        wsc.LIVE_AFTER_CONFIRMED_REMOVAL: "ERROR",
        wsc.REMOVAL_MARKER_MISMATCH: "ERROR",
    }


def test_product_marked_removed_but_sync_untouched_is_caught():
    """Writer stopped after the product row: sync still live, no sync marker."""
    product = _product(S.FAILED, {wrl.REMOVAL_MARKER_KEY: _marker()})
    findings = wsc.evaluate_stored_woo_status(product, [_sync(SS.DRAFT_CREATED)])
    assert _codes(findings) == {wsc.REMOVAL_MARKER_MISMATCH: "ERROR"}


def test_markers_for_different_woo_ids_are_caught():
    product, sync = _removed()
    product["product_metadata"][wrl.REMOVAL_MARKER_KEY] = _marker(woo_id=9999)
    findings = wsc.evaluate_stored_woo_status(product, [sync])
    assert _codes(findings) == {wsc.REMOVAL_MARKER_MISMATCH: "ERROR"}


def test_a_removal_marker_under_recovery_required_is_not_trusted():
    """recovery_required voids the marker (woo_remote_lifecycle), so the row
    is in recovery, not removed; the product-side marker is then unconfirmed."""
    product, sync = _removed()
    sync["response_payload"]["recovery_required"] = True
    findings = wsc.evaluate_stored_woo_status(product, [sync])
    assert _codes(findings) == {wsc.REMOVAL_MARKER_MISMATCH: "ERROR"}


# --- owner-modified products (#3772 shape) ---------------------------------------


def test_owner_restored_product_stays_locally_consistent_and_untouched():
    """#3772: recorded REMOTE_TRASHED, the owner later restored it. Only the
    live remote check can see that (OWNER_MODIFIED); locally the rows are a
    consistent terminal removal, so this check reports nothing and must not
    suggest any write that would clear the marker."""
    product, sync = _removed(wrl.REMOTE_TRASHED)
    assert wsc.evaluate_stored_woo_status(product, [sync]) == []


# --- terminal candidates and missing Woo IDs ------------------------------------


def test_ready_for_draft_with_terminal_existing_product_is_a_stale_warning():
    """CAN-0019 shape: catalog check proved an existing product, no sync."""
    findings = wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT, _catalog()), [])
    assert _codes(findings) == {wsc.STALE_TERMINAL: "WARNING"}
    assert wsc.STALE_TERMINAL in ACCEPTED_WARNING_CODES


def test_ready_for_draft_with_resolved_interrupted_create_is_only_stale():
    """CAN-0031 shape: FAILED sync without Woo ID, resolved as NOT_RECREATED_EXISTING."""
    sync = _sync(SS.FAILED, woo_id=None, payload={
        wci.RESOLUTION_KEY: {"state": wci.RESOLVED_NOT_RECREATED_EXISTING, "sku": CODE},
    })
    findings = wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT, _catalog()), [sync])
    assert _codes(findings) == {wsc.STALE_TERMINAL: "WARNING"}


@pytest.mark.parametrize("sync_status", [SS.FAILED, SS.IN_PROGRESS])
@pytest.mark.parametrize("stored", [S.READY_FOR_DRAFT, S.NOT_CREATED])
def test_unresolved_create_without_woo_id_is_an_error(stored, sync_status):
    findings = wsc.evaluate_stored_woo_status(_product(stored), [_sync(sync_status, woo_id=None)])
    assert _codes(findings) == {wsc.CREATE_OUTCOME_UNRESOLVED: "ERROR"}


def test_unresolved_create_is_not_downgraded_by_a_terminal_marker_alone():
    """A terminal catalog marker without the resolution record is not proof
    that the failed create left nothing behind: the ERROR stays and no
    WARNING hides it."""
    findings = wsc.evaluate_stored_woo_status(
        _product(S.READY_FOR_DRAFT, _catalog()), [_sync(SS.FAILED, woo_id=None)]
    )
    assert _codes(findings) == {wsc.CREATE_OUTCOME_UNRESOLVED: "ERROR"}


def test_resolution_for_a_different_sku_does_not_count():
    sync = _sync(SS.FAILED, woo_id=None, payload={
        wci.RESOLUTION_KEY: {"state": wci.RESOLVED_NOT_RECREATED_EXISTING, "sku": "OTHER"},
    })
    findings = wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT, _catalog()), [sync])
    assert wsc.CREATE_OUTCOME_UNRESOLVED in _codes(findings)


def test_non_terminal_catalog_marker_is_not_stale():
    findings = wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT, _catalog(wci.POSSIBLE_DUPLICATE)), [])
    assert findings == []


def test_marker_for_another_product_code_is_ignored():
    metadata = _catalog()
    metadata[wci.MARKER_KEY]["product_code"] = "TSYC-OTHER"
    assert wsc.evaluate_stored_woo_status(_product(S.READY_FOR_DRAFT, metadata), []) == []


# --- idempotence -------------------------------------------------------------------


def test_repeated_runs_are_identical_and_never_mutate_inputs():
    product, sync = _removed()
    product["woocommerce_status"] = S.DRAFT_CREATED
    before = (copy.deepcopy(product), copy.deepcopy(sync))
    first = wsc.evaluate_stored_woo_status(product, [sync])
    second = wsc.evaluate_stored_woo_status(product, [sync])
    assert first == second and first
    assert (product, sync) == before


# --- audit wiring -------------------------------------------------------------------


def test_audit_reports_findings_with_severity_and_entity():
    product, sync = _removed()
    product["woocommerce_status"] = S.PUBLISHED
    issues: list[dict[str, str]] = []
    audit.audit_woocommerce([product], [sync], issues)
    matching = [i for i in issues if i["code"] == wsc.LIVE_AFTER_CONFIRMED_REMOVAL]
    assert len(matching) == 1
    assert matching[0]["severity"] == "ERROR"
    assert matching[0]["entity"] == CODE


def test_audit_keeps_existing_removed_warning_and_adds_nothing_when_consistent():
    product, sync = _removed()
    issues: list[dict[str, str]] = []
    audit.audit_woocommerce([product], [sync], issues)
    assert [i["code"] for i in issues] == ["REMOTE_WOO_PRODUCT_REMOVED"]


def test_audit_does_not_duplicate_existing_draft_created_check():
    issues: list[dict[str, str]] = []
    audit.audit_woocommerce([_product(S.DRAFT_CREATED)], [], issues)
    assert [i["code"] for i in issues] == ["DRAFT_CREATED_WITHOUT_SINGLE_REMOTE_ID"]


def test_audit_running_twice_gives_the_same_issues():
    product = _product(S.READY_FOR_DRAFT, _catalog())
    first: list[dict[str, str]] = []
    second: list[dict[str, str]] = []
    audit.audit_woocommerce([product], [], first)
    audit.audit_woocommerce([product], [], second)
    assert first == second == [
        {"severity": "WARNING", "entity": CODE, "code": wsc.STALE_TERMINAL, "message": first[0]["message"]}
    ]


def test_every_error_code_stays_unaccepted():
    """No dangerous contradiction may be silently accepted by preflight/run_batch."""
    assert (wsc.ALL_CODES - {wsc.STALE_TERMINAL}).isdisjoint(ACCEPTED_WARNING_CODES)
