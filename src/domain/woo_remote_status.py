"""Local-versus-remote WooCommerce state of TSYC-managed products (no I/O).

Background (2026-10-26 project audit): 67 sync rows were DRAFT_CREATED
locally while the shop owner had already published (26), permanently
deleted (38) or trashed (3) the product, and one product recorded as
removed (AUTOIMPORT-CAN-0010 #3772) was live again. Neither the pipeline
audit nor preflight read remote status, so nothing noticed.

This module classifies ONE local record against ONE remote observation and
decides what may happen next. It never guesses:

  * an HTTP error, timeout or unexpected body is API_ACCESS_ERROR, never a
    deletion;
  * a 404 is a deletion only when woo_remote_lifecycle.evaluate_remote_removal
    confirms it (exact-SKU searches with status=any AND status=trash both
    empty) -- a missing or empty SKU therefore never proves removal;
  * a product whose SKU also lives on EN/DE copies (Polylang translations
    keep the VI SKU, docs/POLYLANG_EXISTING_ARCHITECTURE.md) is a
    TRANSLATION_LINKED_RECORD, never an unrelated duplicate and never a
    confirmed removal;
  * a title is never identity: only the stored Woo ID plus the exact SKU.

Only three remote facts have an existing, policy-backed LOCAL repair
(scripts/sync_woocommerce_product_status.py, owner instruction 2026-10-02
"removal is terminal, never recreate"): published -> PUBLISHED, confirmed
trash -> REMOTE_TRASHED, confirmed deletion -> REMOTE_DELETED; plus a
local internal_products status that lags its own sync row while the
remote draft is exactly as expected. Everything else is a proposal for a
human and is never written. Nothing here ever writes to WooCommerce.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from src.domain import woo_remote_lifecycle as lifecycle

# Classifications.
CONSISTENT = "CONSISTENT"
LOCAL_STATUS_STALE = "LOCAL_STATUS_STALE"
REMOTE_PUBLISHED = "REMOTE_PUBLISHED"
REMOTE_TRASHED = "REMOTE_TRASHED"
REMOTE_REMOVED = "REMOTE_REMOVED"
OWNER_MODIFIED = "OWNER_MODIFIED"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
TRANSLATION_LINKED_RECORD = "TRANSLATION_LINKED_RECORD"
UNCERTAIN_CREATE = "UNCERTAIN_CREATE"
API_ACCESS_ERROR = "API_ACCESS_ERROR"
UNRESOLVED = "UNRESOLVED"

ALL_CLASSIFICATIONS = (
    CONSISTENT, LOCAL_STATUS_STALE, REMOTE_PUBLISHED, REMOTE_TRASHED, REMOTE_REMOVED,
    OWNER_MODIFIED, IDENTITY_MISMATCH, TRANSLATION_LINKED_RECORD, UNCERTAIN_CREATE,
    API_ACCESS_ERROR, UNRESOLVED,
)

# Next action.
ACTION_NONE = "NONE"
ACTION_LOCAL_RECONCILE = "LOCAL_RECONCILE"      # existing official writer, local rows only
ACTION_PROPOSE = "PROPOSE_ONLY"                 # human decision; nothing written
ACTION_REREAD = "REREAD_LATER"                  # remote could not be read reliably

# Classifications whose local-only repair is already authorized by policy
# and implemented by sync_woocommerce_product_status.synchronize_one_product.
LOCAL_RECONCILABLE = frozenset({REMOTE_PUBLISHED, REMOTE_TRASHED, REMOTE_REMOVED, LOCAL_STATUS_STALE})

# Local woocommerce_status values that expect a live remote product.
_EXPECT_DRAFT = "draft"
_EXPECT_PUBLISH = "publish"

# A remote modification later than our own last write by more than this
# is owner activity (WordPress re-saves within seconds of our own PUT).
OWNER_EDIT_TOLERANCE_SECONDS = 120

# Systemic outage: at least this many unreadable records, or this share.
SYSTEMIC_MIN_ERRORS = 3
SYSTEMIC_ERROR_SHARE = 0.10

# Write-gate reasons.
GATE_OK = "ELIGIBLE"
GATE_NO_REMOTE_IDENTITY = "NO_REMOTE_IDENTITY"
GATE_OWNER_REMOVED = "OWNER_REMOVED"
GATE_UNCERTAIN_CREATE = "UNCERTAIN_CREATE"
GATE_LOCAL_STATUS = "LOCAL_STATUS_NOT_WRITABLE"
GATE_REMOTE_CONFLICT = "REMOTE_STATE_CONFLICT"
GATE_PUBLISHED_NOT_AUTHORIZED = "PUBLISHED_NOT_AUTHORIZED"


@dataclass(frozen=True)
class LocalRecord:
    sync_id: str
    internal_product_id: str | None
    product_code: str | None
    candidate_id: str | None
    sync_status: str | None
    product_status: str | None
    woocommerce_product_id: int | None
    sku: str
    removal_state: str | None = None
    recovery_required: bool = False
    create_outcome_resolved: bool = False
    last_local_write_at: str | None = None
    identity_status: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class RemoteObservation:
    """What the read-only checker saw for one Woo ID.

    http_status None means the request itself failed (timeout, connection,
    unreadable body). sku_any / sku_trash None mean that lookup was not
    made or was uncertain."""

    http_status: int | None
    product: Mapping[str, Any] | None = None
    error: str | None = None
    sku_any: Sequence[Mapping[str, Any]] | None = None
    sku_trash: Sequence[Mapping[str, Any]] | None = None
    source: str = "DIRECT"


@dataclass
class StatusResult:
    sync_id: str
    product_code: str | None
    candidate_id: str | None
    woocommerce_product_id: int | None
    sku: str
    local_sync_status: str | None
    local_product_status: str | None
    identity_status: str | None
    remote_status: str | None
    classification: str
    action: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    proposed_correction: str | None = None
    owner_modified_after_sync: bool | None = None

    @property
    def mismatch(self) -> bool:
        return self.classification != CONSISTENT

    def as_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id, "product_code": self.product_code, "candidate_id": self.candidate_id,
            "woocommerce_product_id": self.woocommerce_product_id, "sku": self.sku,
            "local_sync_status": self.local_sync_status, "local_product_status": self.local_product_status,
            "identity_status": self.identity_status, "remote_status": self.remote_status,
            "classification": self.classification, "action": self.action, "reason": self.reason,
            "evidence": self.evidence, "proposed_correction": self.proposed_correction,
            "owner_modified_after_sync": self.owner_modified_after_sync,
        }


def local_record_from_rows(
    sync: Mapping[str, Any],
    product: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None = None,
) -> LocalRecord:
    """Build the comparable local view from one sync row and its product."""
    payload = sync.get("response_payload") if isinstance(sync.get("response_payload"), Mapping) else {}
    woo_id = sync.get("woocommerce_product_id")
    product = product or {}
    candidate = candidate or {}
    return LocalRecord(
        sync_id=str(sync.get("sync_id")),
        internal_product_id=sync.get("internal_product_id"),
        product_code=product.get("product_code"),
        candidate_id=product.get("candidate_id"),
        sync_status=sync.get("woocommerce_status"),
        product_status=product.get("woocommerce_status"),
        woocommerce_product_id=int(woo_id) if woo_id not in (None, "") else None,
        sku=str(sync.get("product_sku") or "").strip(),
        removal_state=lifecycle.confirmed_remote_removal(sync),
        recovery_required=payload.get("recovery_required") is True,
        create_outcome_resolved=isinstance(payload.get("create_outcome_resolution"), Mapping),
        last_local_write_at=sync.get("synced_at") or sync.get("last_attempt_at"),
        identity_status=candidate.get("identity_status"),
        title=product.get("title"),
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # Woo *_gmt fields carry no offset
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def owner_modified_after(local_write_at: Any, remote_modified_gmt: Any) -> bool | None:
    """True when the remote product changed after TSYC's own last write.
    None when either timestamp is unknown (no conclusion drawn)."""
    local = _parse_time(local_write_at)
    remote = _parse_time(remote_modified_gmt)
    if local is None or remote is None:
        return None
    return (remote - local).total_seconds() > OWNER_EDIT_TOLERANCE_SECONDS


def _language_of(permalink: Any) -> str:
    link = str(permalink or "")
    if "/en/" in link:
        return "en"
    if "/de/" in link:
        return "de"
    return "vi-or-unknown"


def _sku_others(woo_id: int | None, matches: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return [m for m in (matches or []) if str(m.get("id")) != str(woo_id)]


def _translation_copies_only(others: Sequence[Mapping[str, Any]]) -> bool:
    """Every other product carrying the SKU is an EN/DE copy (by its
    language URL prefix). A VI-or-unknown copy is not a translation."""
    return bool(others) and all(_language_of(o.get("permalink")) in ("en", "de") for o in others)


def _expected_remote(record: LocalRecord) -> str | None:
    if record.product_status == "PUBLISHED":
        return _EXPECT_PUBLISH
    if record.sync_status == "DRAFT_CREATED":
        return _EXPECT_DRAFT
    return None


def _result(record: LocalRecord, observation: RemoteObservation | None, classification: str,
            action: str, reason: str, **extra: Any) -> StatusResult:
    remote = (observation.product if observation else None) or {}
    owner_modified = owner_modified_after(record.last_local_write_at, remote.get("date_modified_gmt")) if remote else None
    evidence = {
        "http_status": observation.http_status if observation else None,
        "observation_source": observation.source if observation else None,
        "remote_id": remote.get("id"),
        "remote_sku": remote.get("sku"),
        "remote_status": remote.get("status"),
        "remote_modified_gmt": remote.get("date_modified_gmt"),
        "local_last_write_at": record.last_local_write_at,
        "local_removal_marker": record.removal_state,
        "recovery_required": record.recovery_required,
    }
    if observation and observation.sku_any is not None:
        evidence["sku_any_ids"] = [m.get("id") for m in observation.sku_any]
        evidence["sku_any_languages"] = {str(m.get("id")): _language_of(m.get("permalink")) for m in observation.sku_any}
    if observation and observation.sku_trash is not None:
        evidence["sku_trash_ids"] = [m.get("id") for m in observation.sku_trash]
    if observation and observation.error:
        evidence["error"] = observation.error
    evidence.update(extra.pop("evidence", {}))
    return StatusResult(
        sync_id=record.sync_id, product_code=record.product_code, candidate_id=record.candidate_id,
        woocommerce_product_id=record.woocommerce_product_id, sku=record.sku,
        local_sync_status=record.sync_status, local_product_status=record.product_status,
        identity_status=record.identity_status, remote_status=remote.get("status") if remote else (
            "NOT_FOUND" if observation and observation.http_status == 404 else None),
        classification=classification, action=action, reason=reason,
        evidence=evidence, owner_modified_after_sync=owner_modified, **extra,
    )


def classify(record: LocalRecord, observation: RemoteObservation | None) -> StatusResult:
    """Classify one record. `observation` may be None only for a record
    without a Woo ID (nothing to read)."""
    # 1. No remote identity recorded.
    if record.woocommerce_product_id is None:
        if record.create_outcome_resolved:
            return _result(record, None, CONSISTENT, ACTION_NONE,
                           "No Woo ID; the interrupted create was already resolved (create_outcome_resolution).")
        return _result(
            record, None, UNCERTAIN_CREATE, ACTION_PROPOSE,
            "No Woo ID is recorded and no create-outcome resolution exists.",
            proposed_correction=("Run scripts/recover_historical_woo_create.py for this product "
                                 "(never clear_woocommerce_sync_recovery.py, never re-create blindly)."),
        )

    if observation is None:
        return _result(record, None, API_ACCESS_ERROR, ACTION_REREAD, "The remote product was not read.")

    # 2. An unfinished create is a recovery case whatever the remote says.
    if record.recovery_required:
        return _result(
            record, observation, UNCERTAIN_CREATE, ACTION_PROPOSE,
            "The sync row still carries recovery_required=true.",
            proposed_correction="Resolve through the sanctioned recovery scripts before any other step.",
        )

    status_code = observation.http_status
    remote = observation.product

    # 3. Unreadable remote: never a deletion.
    if status_code is None or status_code not in (200, 404) or (status_code == 200 and not isinstance(remote, Mapping)):
        return _result(
            record, observation, API_ACCESS_ERROR, ACTION_REREAD,
            f"Remote read is not conclusive (HTTP {status_code}).",
        )

    # 4. Locally recorded as owner-removed.
    if record.removal_state:
        if status_code == 404 or (remote or {}).get("status") == "trash":
            return _result(record, observation, CONSISTENT, ACTION_NONE,
                           f"Recorded {record.removal_state}; remote is still removed.")
        return _result(
            record, observation, OWNER_MODIFIED, ACTION_PROPOSE,
            (f"Recorded {record.removal_state}, but the same Woo ID is live again "
             f"(status {(remote or {}).get('status')!r})."),
            proposed_correction=(
                "Ask the owner whether the restore was intentional. Only then clear the removal marker "
                "through a sanctioned, logged step; until then every TSYC write to this product is refused."),
        )

    # 5. 404: deletion only when the lifecycle rule confirms it.
    if status_code == 404:
        if observation.sku_any is None or (observation.sku_trash is None and observation.sku_any == []):
            return _result(record, observation, API_ACCESS_ERROR, ACTION_REREAD,
                           "GET-by-id is 404 but the exact-SKU searches were not completed.")
        state, why = lifecycle.evaluate_remote_removal(
            woocommerce_product_id=record.woocommerce_product_id, expected_sku=record.sku,
            get_http_status=404, get_body=None,
            sku_any_matches=observation.sku_any, sku_trash_matches=observation.sku_trash,
        )
        if state == lifecycle.REMOTE_DELETED:
            return _result(record, observation, REMOTE_REMOVED, ACTION_LOCAL_RECONCILE, why)
        others = _sku_others(record.woocommerce_product_id, observation.sku_any)
        if _translation_copies_only(others):
            return _result(
                record, observation, TRANSLATION_LINKED_RECORD, ACTION_PROPOSE,
                "The VI product is gone, but its EN/DE translations still carry the SKU.",
                proposed_correction=(
                    "Owner decision: the EN/DE copies stay sellable without their VI source. TSYC never "
                    "re-creates the VI product and never treats the copies as TSYC's product."),
            )
        return _result(
            record, observation, UNRESOLVED, ACTION_PROPOSE, why,
            proposed_correction=("Inspect the products carrying this SKU by hand; never reconcile onto "
                                 "another product ID automatically."),
        )

    # 6. 200: identity first (stored ID + exact SKU), never the title.
    assert remote is not None
    if str(remote.get("id")) != str(record.woocommerce_product_id):
        return _result(record, observation, IDENTITY_MISMATCH, ACTION_PROPOSE,
                       f"GET #{record.woocommerce_product_id} returned product #{remote.get('id')}.",
                       proposed_correction="Investigate by hand; never reconcile onto another product ID.")
    remote_sku = str(remote.get("sku") or "").strip()
    if not record.sku or remote_sku != record.sku:
        return _result(
            record, observation, IDENTITY_MISMATCH, ACTION_PROPOSE,
            f"Remote SKU {remote_sku!r} does not equal the stored SKU {record.sku!r}.",
            proposed_correction=("Owner or developer: check whether the SKU was edited; TSYC does not "
                                 "write to this product until identity is re-established."),
        )

    remote_status = remote.get("status")
    expected = _expected_remote(record)

    if remote_status == "trash":
        state, why = lifecycle.evaluate_remote_removal(
            woocommerce_product_id=record.woocommerce_product_id, expected_sku=record.sku,
            get_http_status=200, get_body=remote,
            sku_any_matches=observation.sku_any, sku_trash_matches=observation.sku_trash,
        )
        if state == lifecycle.REMOTE_TRASHED:
            return _result(record, observation, REMOTE_TRASHED, ACTION_LOCAL_RECONCILE, why)
        if observation.sku_any is None:
            return _result(record, observation, API_ACCESS_ERROR, ACTION_REREAD,
                           "Trashed, but the exact-SKU search was not completed.")
        if _translation_copies_only(_sku_others(record.woocommerce_product_id, observation.sku_any)):
            return _result(
                record, observation, TRANSLATION_LINKED_RECORD, ACTION_PROPOSE,
                "The VI product is trashed while its EN/DE translations still carry the SKU.",
                proposed_correction="Owner decision about the live EN/DE copies; TSYC changes nothing.",
            )
        return _result(record, observation, UNRESOLVED, ACTION_PROPOSE, why,
                       proposed_correction="Inspect the live products carrying this SKU by hand.")

    if remote_status == _EXPECT_PUBLISH:
        if expected == _EXPECT_PUBLISH:
            return _result(record, observation, CONSISTENT, ACTION_NONE, "Published locally and remotely.")
        if expected == _EXPECT_DRAFT:
            return _result(record, observation, REMOTE_PUBLISHED, ACTION_LOCAL_RECONCILE,
                           "The owner published the draft; the local status still says DRAFT_CREATED.")
        return _result(record, observation, UNRESOLVED, ACTION_PROPOSE,
                       f"Remote is published; local statuses {record.sync_status}/{record.product_status} have no mapping.",
                       proposed_correction="Review the local rows by hand.")

    if remote_status == _EXPECT_DRAFT:
        if expected == _EXPECT_DRAFT:
            if record.product_status == "DRAFT_CREATED":
                return _result(record, observation, CONSISTENT, ACTION_NONE, "Draft locally and remotely.")
            return _result(
                record, observation, LOCAL_STATUS_STALE, ACTION_LOCAL_RECONCILE,
                (f"Sync row and remote agree on a draft, but internal_products says "
                 f"{record.product_status!r}."),
            )
        if expected == _EXPECT_PUBLISH:
            return _result(
                record, observation, OWNER_MODIFIED, ACTION_PROPOSE,
                "Recorded PUBLISHED, but the owner moved it back to draft.",
                proposed_correction=("Owner-maintained product: confirm the intent before TSYC treats it as "
                                     "an automation-owned draft again; no automatic status change."),
            )
        return _result(record, observation, UNRESOLVED, ACTION_PROPOSE,
                       f"Remote is a draft; local statuses {record.sync_status}/{record.product_status} have no mapping.",
                       proposed_correction="Review the local rows by hand.")

    # pending / private / anything else: an owner workflow state TSYC has no mapping for.
    return _result(
        record, observation, OWNER_MODIFIED, ACTION_PROPOSE,
        f"Remote status {remote_status!r} has no local mapping (owner workflow state).",
        proposed_correction="Leave unchanged; owner decides. TSYC never writes to this product meanwhile.",
    )


def assess_systemic(results: Sequence[StatusResult], bulk_read_ok: bool) -> tuple[bool, str]:
    """(systemic_failure, message). A failed bulk read, or unreadable
    records at or above the outage threshold, stops ALL production writes."""
    if not bulk_read_ok:
        return True, "The bulk remote read failed; remote state is unknown for every product."
    checked = [r for r in results if r.woocommerce_product_id is not None]
    errors = [r for r in checked if r.classification == API_ACCESS_ERROR]
    threshold = max(SYSTEMIC_MIN_ERRORS, math.ceil(SYSTEMIC_ERROR_SHARE * len(checked)))
    if errors and len(errors) >= threshold:
        return True, f"{len(errors)} of {len(checked)} remote reads were inconclusive (threshold {threshold})."
    return False, f"{len(errors)} inconclusive remote read(s) of {len(checked)}."


def local_write_block_reason(
    product: Mapping[str, Any],
    sync: Mapping[str, Any],
    *,
    allow_published: bool = False,
) -> str | None:
    """Pre-write gate on the LOCAL rows (None = may proceed to the fresh
    remote re-read every writer already performs). Fails closed."""
    if not sync.get("woocommerce_product_id") or not str(sync.get("product_sku") or "").strip():
        return GATE_NO_REMOTE_IDENTITY
    if lifecycle.confirmed_remote_removal(sync) is not None:
        return GATE_OWNER_REMOVED
    payload = sync.get("response_payload")
    if isinstance(payload, Mapping) and payload.get(lifecycle.REMOVAL_MARKER_KEY):
        # A marker that no longer matches the row is a conflict, not a pass.
        return GATE_OWNER_REMOVED
    if isinstance(payload, Mapping) and payload.get("recovery_required") is True:
        return GATE_UNCERTAIN_CREATE
    if "woocommerce_status" in sync and sync.get("woocommerce_status") != "DRAFT_CREATED":
        return GATE_LOCAL_STATUS
    product_status = product.get("woocommerce_status")
    if product_status == "PUBLISHED":
        return None if allow_published else GATE_PUBLISHED_NOT_AUTHORIZED
    if product_status != "DRAFT_CREATED":
        return GATE_LOCAL_STATUS
    return None


def write_eligibility(result: StatusResult, *, allow_published: bool = False) -> tuple[bool, str]:
    """Remote-aware gate from a fresh classification (never a cached one)."""
    if result.classification != CONSISTENT:
        return False, f"{GATE_REMOTE_CONFLICT}: {result.classification}"
    if result.evidence.get("local_removal_marker"):
        return False, GATE_OWNER_REMOVED
    if result.remote_status == _EXPECT_DRAFT:
        return True, GATE_OK
    if result.remote_status == _EXPECT_PUBLISH and allow_published:
        return True, GATE_OK
    if result.remote_status == _EXPECT_PUBLISH:
        return False, GATE_PUBLISHED_NOT_AUTHORIZED
    return False, f"{GATE_REMOTE_CONFLICT}: remote status {result.remote_status!r}"
