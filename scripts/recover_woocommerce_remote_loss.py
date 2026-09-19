"""
Bounded recovery tool for exactly one WooCommerce synchronization record
whose remote product is CONFIRMED ABSENT (deleted/lost on the remote
store after a successful local DRAFT_CREATED), distinct from the FAILED/
uncertain-create condition clear_woocommerce_sync_recovery.py already
handles.

Scope (CLAUDE.md section 2.6/18 "never blindly retry an uncertain remote
Woo operation" -- this tool is the sanctioned, narrowly-bounded exception
that turns an *uncertain* remote-loss suspicion into a *confirmed* one
before anything is allowed to retry):

This tool NEVER creates, publishes, or prices a WooCommerce product
itself, and it never reuses the stale WooCommerce product ID. It only
transitions exactly one named candidate's local state out of the way so
the normal, already-sanctioned scripts/create_woocommerce_draft.py can
run its own full pre-create flow (including its own fresh SKU re-check
immediately before creating) and mint a brand-new remote product.

The recovery condition this tool handles, and only this condition:

    LOCAL DRAFT_CREATED (both internal_products.woocommerce_status and
    woocommerce_product_syncs.woocommerce_status)
    + a stored woocommerce_product_id
    + a FRESH GET-by-ID on that exact ID returns 404
    + a FRESH exact-SKU search (status=any: draft/publish/private/
      pending) returns zero matches
    + a FRESH exact-SKU search (status=trash) returns zero matches
    => REMOTE_PRODUCT_CONFIRMED_ABSENT

Refuses (leaves both rows completely untouched) when:
  - no internal product resolves to the given --product-code
  - no synchronization record exists for that product
  - more than one synchronization record exists for that product
    (ambiguous target this tool never guesses at)
  - the synchronization record has no stored woocommerce_product_id
    (nothing to recover)
  - the synchronization record's woocommerce_status is not exactly
    DRAFT_CREATED (this tool only handles the specific "was created,
    now gone" case -- a FAILED sync belongs to
    clear_woocommerce_sync_recovery.py instead)
  - the internal product's woocommerce_status is not exactly
    DRAFT_CREATED (e.g. already PUBLISHED -- that is a status-drift
    case for sync_woocommerce_product_status.py, never this tool)
  - the fresh GET-by-ID does not return a definite 404 (any 200, any
    other status, or a network error is treated as uncertain -- the
    remote product may still exist)
  - the fresh exact-SKU search (any status) is uncertain, or finds one
    or more matches (a remote product already exists under this SKU --
    reconcile it, never recreate)
    - the fresh exact-SKU search (trash) is uncertain, or finds one or
    more matches (the product is in trash, which is a distinct
    "restore or permanently delete" business decision this tool never
    makes silently)

When safe, every existing field on the sync row -- including the
now-stale woocommerce_product_id, the original response_payload
(uploaded_media, latest_status_check, everything else) -- is preserved
untouched inside a new, additive "remote_loss_confirmed" evidence
record. Only then are woocommerce_product_syncs.woocommerce_status
moved DRAFT_CREATED -> PENDING (a normal, resumable pre-attempt state,
exactly mirroring clear_woocommerce_sync_recovery.py's FAILED -> PENDING
pattern) and woocommerce_product_syncs.woocommerce_product_id cleared
to NULL (never the old ID -- create_woocommerce_draft.py's
has_existing_remote_draft() check refuses to create when a product_id
is present, and the old ID is never reused). internal_products.
woocommerce_status moves DRAFT_CREATED -> READY_FOR_DRAFT so the normal
create_woocommerce_draft.py picks the candidate back up through its own
fully-gated flow (content/image re-validation, fresh pre-create SKU
check, draft-only payload, no price fields) exactly as if it were any
other READY_FOR_DRAFT candidate. content_status, image_status,
pricing_status and review_required are never touched by this tool.

Idempotent: once a sync row's woocommerce_product_id has been cleared,
a rerun finds "no stored woocommerce_product_id" and refuses -- there is
nothing left to recover, and no duplicate evidence record is appended.

No raw SQL: every read and write goes through SupabaseRepository's query
builder, exactly targeted by product_code / sync_id / internal_product_id.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.decisions import DecisionResult, Outcome  # noqa: E402
from src.domain.woocommerce_status import (  # noqa: E402
    WooCommerceStatus,
    WooCommerceSyncStatus,
)
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

configure_utf8_console()


TOOL_NAME = "recover_woocommerce_remote_loss"
TOOL_VERSION = "1.0.0"

REMOTE_LOSS_RULE = "REMOTE_PRODUCT_CONFIRMED_ABSENT"

VALID_CONFIRMATIONS = {"CONFIRM_REMOTE_LOSS_RECOVERY"}


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def get_required_environment_variable(variable_name: str) -> str:
    """Return one required environment variable."""
    value = os.getenv(variable_name, "").strip()

    if not value:
        raise RuntimeError(f"Missing required environment variable: {variable_name}")

    return value


def get_timeout_seconds() -> int:
    """Return the configured HTTP timeout."""
    raw_value = os.getenv("WOOCOMMERCE_TIMEOUT_SECONDS", "30").strip()

    try:
        timeout_seconds = int(raw_value)
    except ValueError as error:
        raise RuntimeError("WOOCOMMERCE_TIMEOUT_SECONDS must be an integer.") from error

    if timeout_seconds <= 0:
        raise RuntimeError("WOOCOMMERCE_TIMEOUT_SECONDS must be greater than zero.")

    return timeout_seconds


def build_api_url(store_url: str, api_version: str, endpoint: str) -> str:
    """Build a WooCommerce REST API URL."""
    return (
        f"{store_url.rstrip('/')}/wp-json/"
        f"{api_version.strip('/')}/"
        f"{endpoint.strip('/')}"
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Recover exactly one internal product's local state after a "
            "fresh, three-part live check confirms its previously-"
            "created WooCommerce product is now conclusively absent."
        )
    )

    parser.add_argument(
        "--product-code",
        required=True,
        help="Exact internal_products.product_code (WooCommerce SKU) to recover.",
    )

    parser.add_argument(
        "--confirm-recover",
        action="store_true",
        help="Confirm the recovery transition without an interactive prompt.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable prompts. Requires --confirm-recover.",
    )

    parser.add_argument(
        "--reason",
        default=None,
        help=(
            "Optional additional context recorded alongside the standard "
            "remote-loss recovery reason."
        ),
    )

    return parser.parse_args()


def get_internal_product_by_code(
    repository: SupabaseRepository,
    product_code: str,
) -> dict[str, Any] | None:
    """Return exactly one internal_products row by its exact product_code."""
    rows = (
        repository.client
        .table("internal_products")
        .select("internal_product_id, candidate_id, product_code, woocommerce_status")
        .eq("product_code", product_code)
        .limit(2)
        .execute()
        .data
        or []
    )

    if len(rows) > 1:
        raise RuntimeError(
            f"product_code={product_code!r} resolved to more than one "
            "internal product; refusing (this should never happen -- "
            "product_code is expected to be unique)."
        )

    return rows[0] if rows else None


def get_sole_sync_for_product(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return the sole woocommerce_product_syncs row for one internal
    product, or None if none exists. Refuses (raises) if more than one
    exists -- an ambiguous target this tool never guesses at."""
    rows = (
        repository.client
        .table("woocommerce_product_syncs")
        .select("*")
        .eq("internal_product_id", internal_product_id)
        .execute()
        .data
        or []
    )

    if len(rows) > 1:
        raise RuntimeError(
            f"internal_product_id={internal_product_id!r} has "
            f"{len(rows)} WooCommerce synchronization records; refusing "
            "-- this tool only recovers an unambiguous single sync record."
        )

    return rows[0] if rows else None


class RemoteLookupResult:
    """The outcome of one read-only remote WooCommerce lookup.

    Exactly one of (definite result) or (error) is set -- a lookup that
    raised, timed out, or returned a non-2xx/non-404 status is always
    `error`-carrying and must never be treated as a confirmed result by
    the caller.
    """

    def __init__(
        self,
        *,
        error: str | None = None,
        exists: bool | None = None,
        matches: list[dict[str, Any]] | None = None,
    ) -> None:
        self.error = error
        self.exists = exists
        self.matches = matches


def check_remote_product_by_id(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    woocommerce_product_id: int,
    http_get: Callable[..., Any] = requests.get,
) -> RemoteLookupResult:
    """READ-ONLY GET of one exact WooCommerce product by ID.

    A definite 404 is the only "confirmed absent" outcome. A 200 means
    the product still exists (exists=True). Any other status, or a
    raised exception, is an uncertain lookup (error set) -- the caller
    must never treat it as confirmed absence.
    """
    api_url = build_api_url(
        store_url=store_url,
        api_version=api_version,
        endpoint=f"products/{woocommerce_product_id}",
    )

    try:
        response = http_get(
            api_url,
            auth=HTTPBasicAuth(consumer_key, consumer_secret),
            headers={
                "Accept": "application/json",
                "User-Agent": f"{TOOL_NAME}/{TOOL_VERSION}",
            },
            timeout=timeout_seconds,
        )
    except Exception as error:  # noqa: BLE001
        return RemoteLookupResult(error=f"{type(error).__name__}: {error}")

    if response.status_code == 404:
        return RemoteLookupResult(exists=False)

    if response.status_code == 200:
        return RemoteLookupResult(exists=True)

    return RemoteLookupResult(
        error=f"Unexpected HTTP status: {response.status_code}"
    )


def find_remote_products_by_sku(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    sku: str,
    status: str,
    http_get: Callable[..., Any] = requests.get,
) -> RemoteLookupResult:
    """READ-ONLY GET of every remote WooCommerce product matching an
    exact SKU under one status filter ("any" or "trash"). A raised
    exception or non-200 response is an uncertain lookup (error set),
    never a confirmed-absent result."""
    api_url = build_api_url(store_url=store_url, api_version=api_version, endpoint="products")

    try:
        response = http_get(
            api_url,
            auth=HTTPBasicAuth(consumer_key, consumer_secret),
            params={"sku": sku, "status": status, "per_page": 10},
            headers={
                "Accept": "application/json",
                "User-Agent": f"{TOOL_NAME}/{TOOL_VERSION}",
            },
            timeout=timeout_seconds,
        )
    except Exception as error:  # noqa: BLE001
        return RemoteLookupResult(error=f"{type(error).__name__}: {error}")

    if response.status_code != 200:
        return RemoteLookupResult(
            error=f"Unexpected HTTP status: {response.status_code}"
        )

    payload = response.json()

    if not isinstance(payload, list):
        return RemoteLookupResult(error="Unexpected (non-list) response body.")

    return RemoteLookupResult(matches=payload)


def evaluate_remote_loss_recovery(
    internal_product: dict[str, Any] | None,
    sync: dict[str, Any] | None,
    get_by_id_result: RemoteLookupResult | None,
    sku_any_result: RemoteLookupResult | None,
    sku_trash_result: RemoteLookupResult | None,
) -> DecisionResult:
    """
    Pure decision: is it safe to recover this candidate from a confirmed
    remote-loss condition?

    AUTO_PASS requires all of:
      - a sync record exists, with a stored woocommerce_product_id
      - the sync record's woocommerce_status is exactly DRAFT_CREATED
      - the internal product's woocommerce_status is exactly DRAFT_CREATED
      - a fresh GET-by-ID on the stored ID returned a definite 404
      - a fresh exact-SKU search (status=any) returned zero matches
      - a fresh exact-SKU search (status=trash) returned zero matches

    Any other combination is BLOCKED -- a structural precondition this
    tool must never proceed past, and never a business judgment call.
    """
    if sync is None:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "No WooCommerce synchronization record exists for this "
                "product; there is nothing to recover."
            ),
        )

    if not sync.get("woocommerce_product_id"):
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The synchronization record has no stored WooCommerce "
                "product ID; there is nothing to recover (it may "
                "already have been recovered, or never had one)."
            ),
        )

    if sync.get("woocommerce_status") != WooCommerceSyncStatus.DRAFT_CREATED:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The synchronization record's status is "
                f"{sync.get('woocommerce_status')!r}, not DRAFT_CREATED; "
                "this tool only recovers a confirmed remote-loss on a "
                "previously successful draft. A FAILED sync belongs to "
                "clear_woocommerce_sync_recovery.py instead."
            ),
        )

    if internal_product is None:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason="No internal product was found for this candidate.",
        )

    if internal_product.get("woocommerce_status") != WooCommerceStatus.DRAFT_CREATED:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "internal_products.woocommerce_status is "
                f"{internal_product.get('woocommerce_status')!r}, not "
                "DRAFT_CREATED; this tool never touches a candidate "
                "whose local status has already moved on (e.g. "
                "PUBLISHED) -- reconcile with "
                "sync_woocommerce_product_status.py instead."
            ),
        )

    if get_by_id_result is None or get_by_id_result.error:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The fresh GET-by-ID lookup was uncertain "
                f"({get_by_id_result.error if get_by_id_result else 'not run'}); "
                "refusing without a confirmed 404."
            ),
        )

    if get_by_id_result.exists:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The stored WooCommerce product ID still exists "
                "remotely; this is not a remote-loss condition -- "
                "reconcile with sync_woocommerce_product_status.py "
                "instead."
            ),
        )

    if sku_any_result is None or sku_any_result.error:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The fresh exact-SKU lookup (any status) was uncertain "
                f"({sku_any_result.error if sku_any_result else 'not run'}); "
                "refusing without confirmed remote absence."
            ),
        )

    any_matches = sku_any_result.matches or []

    if len(any_matches) > 1:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                f"{len(any_matches)} remote products share this SKU; "
                "stop for manual review -- this is an ambiguous remote "
                "match, never silently resolved."
            ),
        )

    if len(any_matches) == 1:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "A remote product already exists under this exact SKU "
                f"(id={any_matches[0].get('id')!r}); this tool never "
                "recreates when a remote product is confirmed to exist "
                "-- reconcile it instead."
            ),
        )

    if sku_trash_result is None or sku_trash_result.error:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                "The fresh exact-SKU lookup (trash) was uncertain "
                f"({sku_trash_result.error if sku_trash_result else 'not run'}); "
                "refusing without confirmed remote absence."
            ),
        )

    trash_matches = sku_trash_result.matches or []

    if trash_matches:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=REMOTE_LOSS_RULE,
            reason=(
                f"{len(trash_matches)} product(s) with this exact SKU "
                "are in the WooCommerce trash; restoring or permanently "
                "deleting a trashed product is a separate business "
                "decision this tool never makes silently."
            ),
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=REMOTE_LOSS_RULE,
        reason=(
            "A fresh GET-by-ID returned 404, a fresh exact-SKU search "
            "(any status) found zero matches, and a fresh exact-SKU "
            "search (trash) found zero matches: the remote product is "
            "confirmed absent. Safe to recover for a fresh draft "
            "creation attempt."
        ),
        evidence={
            "previous_woocommerce_product_id": sync.get("woocommerce_product_id"),
            "sku_any_match_count": 0,
            "sku_trash_match_count": 0,
        },
    )


def build_remote_loss_response_payload(
    existing_payload: Any,
    reason: str,
    sku: str,
    previous_woocommerce_product_id: Any,
    previous_sync_status: Any,
) -> dict[str, Any]:
    """Return response_payload with every prior key -- uploaded_media,
    latest_status_check, everything -- fully preserved, plus one new
    "remote_loss_confirmed" record. Never deletes or overwrites any
    existing key; this is the forensic evidence trail CLAUDE.md section
    2.7/18 requires be kept, not erased."""
    normalized = dict(existing_payload) if isinstance(existing_payload, dict) else {}

    normalized["remote_loss_confirmed"] = {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "confirmed_at": utc_now(),
        "sku_checked": sku,
        "previous_woocommerce_product_id": previous_woocommerce_product_id,
        "previous_woocommerce_status": previous_sync_status,
        "reason": reason,
    }

    return normalized


def recover_sync_and_product(
    repository: SupabaseRepository,
    sync: dict[str, Any],
    internal_product: dict[str, Any],
    reason: str,
    sku: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Perform the two bounded writes: move the sync row from
    DRAFT_CREATED to PENDING with its stale product ID cleared (old ID
    preserved only inside response_payload evidence), and move the
    internal product's woocommerce_status back to READY_FOR_DRAFT."""
    updated_payload = build_remote_loss_response_payload(
        existing_payload=sync.get("response_payload"),
        reason=reason,
        sku=sku,
        previous_woocommerce_product_id=sync.get("woocommerce_product_id"),
        previous_sync_status=sync.get("woocommerce_status"),
    )

    sync_response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": WooCommerceSyncStatus.PENDING,
                "woocommerce_product_id": None,
                "product_permalink": None,
                "error_code": None,
                "error_message": None,
                "response_payload": updated_payload,
                "updated_at": utc_now(),
            }
        )
        .eq("sync_id", sync["sync_id"])
        .execute()
    )

    if not sync_response.data:
        raise RuntimeError("Remote-loss recovery sync update returned no data.")

    product_response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "woocommerce_status": WooCommerceStatus.READY_FOR_DRAFT,
                "updated_at": utc_now(),
            }
        )
        .eq("internal_product_id", internal_product["internal_product_id"])
        .execute()
    )

    if not product_response.data:
        raise RuntimeError("Remote-loss recovery product update returned no data.")

    return sync_response.data[0], product_response.data[0]


def main() -> int:
    """Recover exactly one product's local state after confirming
    remote loss with three fresh, read-only checks. No write happens
    unless every precondition holds and the operation is explicitly
    confirmed."""
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    if args.non_interactive and not args.confirm_recover:
        raise RuntimeError("--non-interactive requires --confirm-recover.")

    print("=" * 72)
    print("WOOCOMMERCE REMOTE-LOSS RECOVERY")
    print("=" * 72)
    print(f"Version: {TOOL_VERSION}")
    print(f"Target product_code: {args.product_code}")

    store_url = get_required_environment_variable("WOOCOMMERCE_URL")
    consumer_key = get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY")
    consumer_secret = get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET")

    api_version = os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    timeout_seconds = get_timeout_seconds()

    repository = SupabaseRepository()

    internal_product = get_internal_product_by_code(repository, args.product_code)

    if internal_product is None:
        print()
        print(f"No internal product found for product_code={args.product_code!r}.")
        return 1

    sync = get_sole_sync_for_product(
        repository, internal_product["internal_product_id"]
    )

    print()
    print(f"internal_product_id: {internal_product['internal_product_id']}")
    print(
        "internal_products.woocommerce_status: "
        f"{internal_product.get('woocommerce_status')}"
    )

    if sync is None:
        print("No WooCommerce synchronization record exists. Nothing to recover.")
        return 1

    print(f"sync_id: {sync['sync_id']}")
    print(
        "woocommerce_product_syncs.woocommerce_status (before): "
        f"{sync.get('woocommerce_status')}"
    )
    print(
        "woocommerce_product_syncs.woocommerce_product_id (before): "
        f"{sync.get('woocommerce_product_id')}"
    )

    get_by_id_result: RemoteLookupResult | None = None
    sku_any_result: RemoteLookupResult | None = None
    sku_trash_result: RemoteLookupResult | None = None

    stored_id = sync.get("woocommerce_product_id")

    if stored_id:
        print()
        print(f"Fresh GET-by-ID on {stored_id} (read-only, no retry)...")

        get_by_id_result = check_remote_product_by_id(
            store_url=store_url,
            api_version=api_version,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            timeout_seconds=timeout_seconds,
            woocommerce_product_id=int(stored_id),
            http_get=requests.get,
        )

        print(
            "GET-by-ID result: "
            + (
                f"error ({get_by_id_result.error})"
                if get_by_id_result.error
                else ("exists (200)" if get_by_id_result.exists else "confirmed absent (404)")
            )
        )

        print()
        print("Fresh exact-SKU search, status=any (read-only)...")

        sku_any_result = find_remote_products_by_sku(
            store_url=store_url,
            api_version=api_version,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            timeout_seconds=timeout_seconds,
            sku=args.product_code,
            status="any",
            http_get=requests.get,
        )

        print(
            "SKU search (any) result: "
            + (
                f"error ({sku_any_result.error})"
                if sku_any_result.error
                else f"{len(sku_any_result.matches or [])} match(es)"
            )
        )

        print()
        print("Fresh exact-SKU search, status=trash (read-only)...")

        sku_trash_result = find_remote_products_by_sku(
            store_url=store_url,
            api_version=api_version,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            timeout_seconds=timeout_seconds,
            sku=args.product_code,
            status="trash",
            http_get=requests.get,
        )

        print(
            "SKU search (trash) result: "
            + (
                f"error ({sku_trash_result.error})"
                if sku_trash_result.error
                else f"{len(sku_trash_result.matches or [])} match(es)"
            )
        )

    decision = evaluate_remote_loss_recovery(
        internal_product=internal_product,
        sync=sync,
        get_by_id_result=get_by_id_result,
        sku_any_result=sku_any_result,
        sku_trash_result=sku_trash_result,
    )

    print()
    print(f"Decision: {decision.outcome}")
    print(f"Reason: {decision.reason}")

    if decision.outcome != Outcome.AUTO_PASS:
        print()
        print("Refusing to recover. No write was made.")
        return 1

    reason = (
        "Confirmed remote loss by a fresh GET-by-ID (404) plus fresh "
        "exact-SKU searches (any status: 0 matches; trash: 0 matches), "
        f"performed by {TOOL_NAME}/{TOOL_VERSION}. Recovered for a "
        "fresh draft creation attempt; the old WooCommerce product ID "
        "is never reused."
    )

    if args.reason:
        reason = f"{reason} Operator note: {args.reason}"

    print()

    if args.confirm_recover:
        confirmation = "CONFIRM_REMOTE_LOSS_RECOVERY"
    elif args.non_interactive:
        confirmation = ""
    else:
        confirmation = input(
            "Type CONFIRM_REMOTE_LOSS_RECOVERY to recover this "
            "candidate for a fresh draft attempt, or press Enter to "
            "cancel: "
        ).strip().upper()

    if confirmation not in VALID_CONFIRMATIONS:
        print("Remote-loss recovery was cancelled.")
        return 1

    updated_sync, updated_product = recover_sync_and_product(
        repository=repository,
        sync=sync,
        internal_product=internal_product,
        reason=reason,
        sku=args.product_code,
    )

    print()
    print("Remote-loss recovery complete.")
    print(
        "woocommerce_product_syncs.woocommerce_status (after): "
        f"{updated_sync.get('woocommerce_status')}"
    )
    print(
        "woocommerce_product_syncs.woocommerce_product_id (after): "
        f"{updated_sync.get('woocommerce_product_id')}"
    )
    print(
        "internal_products.woocommerce_status (after): "
        f"{updated_product.get('woocommerce_status')}"
    )
    print(
        "Old WooCommerce product ID preserved in "
        "response_payload.remote_loss_confirmed.previous_woocommerce_product_id."
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print()
        print("Remote-loss recovery was cancelled.")
        sys.exit(130)

    except Exception as error:
        print()
        print("Remote-loss recovery failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
