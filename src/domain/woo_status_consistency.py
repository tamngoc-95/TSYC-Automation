"""Stored internal_products.woocommerce_status versus the local sync evidence.

Background (2026-11-07 audit): 27 internal products still said
DRAFT_CREATED days after the shop owner had published or trashed them, and
two READY_FOR_DRAFT rows were already terminal (catalog check proved an
existing product). The stored column is a cache of a lifecycle that the
sync row, the removal marker and the catalog marker actually decide. This
module says, for one internal product, where that cache contradicts the
trusted local records.

Trusted local records (all written only by official TSYC writers):
  - woocommerce_product_syncs: woocommerce_product_id, woocommerce_status,
    response_payload[remote_removal] (src.domain.woo_remote_lifecycle),
    response_payload[create_outcome_resolution]
  - internal_products.product_metadata[remote_removal] (mirror of the sync
    marker) and [catalog_identity_check] (src.domain.woo_catalog_identity)

Remote WooCommerce state is NOT read here: owner publish/trash after the
last reconciliation is the job of the live check
(scripts/audit_woo_remote_status.py, preflight WOO_REMOTE_STATE). This
module catches what the local rows alone can prove.

Severity:
  ERROR    the stored value could send a writer down the wrong path
           (update a removed product, treat a failed create as live,
           or carry a half-written reconciliation).
  WARNING  stale but harmless: the derived state is terminal and no
           writer acts on the stored value (accepted, see
           pipeline_state.ACCEPTED_WARNING_CODES).

Combinations the existing audit_woocommerce() checks already report
(DRAFT_CREATED without exactly one Woo ID; a Woo ID with a pre-draft
status) are not repeated here.

Pure functions only: no I/O, inputs are never mutated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.domain import woo_catalog_identity, woo_remote_lifecycle
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus

ERROR = "ERROR"
WARNING = "WARNING"

# A confirmed owner removal is recorded, but the stored status says the
# product is live, ready or still a draft.
LIVE_AFTER_CONFIRMED_REMOVAL = "WOO_STATUS_LIVE_AFTER_CONFIRMED_REMOVAL"
# The removal evidence exists on one row but not (or differently) on the
# other: an interrupted reconciliation.
REMOVAL_MARKER_MISMATCH = "WOO_STATUS_REMOVAL_MARKER_MISMATCH"
# The stored status claims a live Woo product, but no sync row holds a
# Woo ID.
LIVE_WITHOUT_REMOTE_ID = "WOO_STATUS_LIVE_WITHOUT_REMOTE_ID"
# The stored status claims a live Woo product, but the only ID-bearing sync
# row is FAILED without a confirmed removal.
LIVE_WITH_FAILED_SYNC = "WOO_STATUS_LIVE_WITH_FAILED_SYNC"
# A create attempt started (sync IN_PROGRESS/FAILED, no Woo ID) and was
# never resolved, while the stored status still invites a create.
CREATE_OUTCOME_UNRESOLVED = "WOO_STATUS_CREATE_OUTCOME_UNRESOLVED"
# Stored READY_FOR_DRAFT, but the catalog check is terminal (existing or
# owner-removed product): never dispatched again.
STALE_TERMINAL = "WOO_STATUS_STALE_TERMINAL"

ALL_CODES = frozenset({
    LIVE_AFTER_CONFIRMED_REMOVAL,
    REMOVAL_MARKER_MISMATCH,
    LIVE_WITHOUT_REMOTE_ID,
    LIVE_WITH_FAILED_SYNC,
    CREATE_OUTCOME_UNRESOLVED,
    STALE_TERMINAL,
})

# Stored values that say "a Woo product exists and is ours to track".
_LIVE_STATUSES = frozenset({
    WooCommerceStatus.DRAFT_CREATED,
    WooCommerceStatus.READY_TO_PUBLISH,
    WooCommerceStatus.PUBLISHED,
})
# Stored values that still invite a create.
_PRE_CREATE_STATUSES = frozenset({
    WooCommerceStatus.NOT_CREATED,
    WooCommerceStatus.READY_FOR_DRAFT,
})
_STARTED_SYNC_STATUSES = frozenset({
    WooCommerceSyncStatus.IN_PROGRESS,
    WooCommerceSyncStatus.FAILED,
})


@dataclass(frozen=True)
class StatusFinding:
    severity: str
    code: str
    message: str


def _product_removal_marker(product: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = product.get("product_metadata")
    marker = metadata.get(woo_remote_lifecycle.REMOVAL_MARKER_KEY) if isinstance(metadata, Mapping) else None
    return marker if isinstance(marker, Mapping) else None


def evaluate_stored_woo_status(
    product: Mapping[str, Any],
    syncs: Sequence[Mapping[str, Any]],
) -> list[StatusFinding]:
    """Contradictions between one product's stored woocommerce_status and
    its own sync rows / markers. Empty list means consistent."""
    stored = product.get("woocommerce_status")
    findings: list[StatusFinding] = []

    with_id = [s for s in syncs if s.get("woocommerce_product_id")]
    without_id = [s for s in syncs if not s.get("woocommerce_product_id")]

    removals = {
        str(s.get("woocommerce_product_id")): woo_remote_lifecycle.confirmed_remote_removal(s)
        for s in with_id
    }
    confirmed = {woo_id: state for woo_id, state in removals.items() if state}
    product_marker = _product_removal_marker(product)

    # 1. Removal recorded, stored status not FAILED.
    if confirmed and stored != WooCommerceStatus.FAILED:
        ids = ", ".join(f"#{i} {s}" for i, s in sorted(confirmed.items()))
        findings.append(StatusFinding(
            ERROR, LIVE_AFTER_CONFIRMED_REMOVAL,
            f"Stored status is {stored}, but the sync row records a confirmed owner removal ({ids}). "
            "A removed product is terminal and must never be updated or recreated; "
            "re-run the remote status check and reconcile with the official writer.",
        ))

    # 2. Removal evidence on only one of the two rows, or on different IDs.
    if confirmed or product_marker is not None:
        marker_id = str(product_marker.get("woocommerce_product_id")) if product_marker else None
        if product_marker is None:
            findings.append(StatusFinding(
                ERROR, REMOVAL_MARKER_MISMATCH,
                "The sync row records a confirmed removal but the internal product carries no "
                "removal marker: the reconciliation stopped half-way.",
            ))
        elif marker_id not in confirmed:
            findings.append(StatusFinding(
                ERROR, REMOVAL_MARKER_MISMATCH,
                f"The internal product records a removal of #{marker_id}, but no sync row confirms "
                "a removal of that Woo ID: the reconciliation stopped half-way or the rows diverged.",
            ))

    # 3./4. Stored live status without a live remote ID.
    if stored in _LIVE_STATUSES and not confirmed:
        if not with_id:
            # DRAFT_CREATED without an ID is already reported by
            # audit_woocommerce (DRAFT_CREATED_WITHOUT_SINGLE_REMOTE_ID).
            if stored != WooCommerceStatus.DRAFT_CREATED:
                findings.append(StatusFinding(
                    ERROR, LIVE_WITHOUT_REMOTE_ID,
                    f"Stored status is {stored}, but no sync row holds a WooCommerce product ID.",
                ))
        elif all(s.get("woocommerce_status") == WooCommerceSyncStatus.FAILED for s in with_id):
            findings.append(StatusFinding(
                ERROR, LIVE_WITH_FAILED_SYNC,
                f"Stored status is {stored}, but the sync row for "
                f"{', '.join('#' + str(s.get('woocommerce_product_id')) for s in with_id)} is FAILED "
                "without a confirmed removal. The remote state is unproven; reconcile before any write.",
            ))

    catalog_marker = woo_catalog_identity.blocking_marker(product)
    terminal_catalog = bool(catalog_marker and catalog_marker.get("terminal"))

    # 5. A started create without an ID, never resolved, while a create is
    #    still invited.
    if stored in _PRE_CREATE_STATUSES and not with_id:
        for sync in without_id:
            if sync.get("woocommerce_status") not in _STARTED_SYNC_STATUSES:
                continue
            if woo_catalog_identity.create_outcome_resolved(sync, product):
                continue
            findings.append(StatusFinding(
                ERROR, CREATE_OUTCOME_UNRESOLVED,
                f"Stored status is {stored}, but a create attempt (sync {sync.get('woocommerce_status')}, "
                "no Woo ID) was never resolved. Never retry blindly: run "
                "recover_historical_woo_create.py / the recovery workflow first.",
            ))
            break

    # 6. Stale READY_FOR_DRAFT on a terminal catalog result.
    if stored == WooCommerceStatus.READY_FOR_DRAFT and terminal_catalog and not any(
        f.code == CREATE_OUTCOME_UNRESOLVED for f in findings
    ):
        findings.append(StatusFinding(
            WARNING, STALE_TERMINAL,
            f"Stored status is READY_FOR_DRAFT, but the catalog check is terminal "
            f"({catalog_marker.get('classification')}): the candidate is never dispatched and no "
            "draft will be created. The stored value is stale, not a queue entry.",
        ))

    return findings
