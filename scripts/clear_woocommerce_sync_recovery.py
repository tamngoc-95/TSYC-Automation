"""
Bounded recovery-clear tool for exactly one WooCommerce synchronization
record: a FAILED local sync with confirmed remote absence.

Scope (deliberately narrow -- CLAUDE.md section 2.6/18 "never blindly
retry an uncertain remote Woo operation"):

This tool NEVER creates, retries, publishes, or prices a WooCommerce
product itself. It only removes the "stop for recovery review" signal
(pipeline_state.derive_sync_recovery_state()'s RECOVERY_REVIEW_REQUIRED)
from exactly one named internal product's synchronization record --
and only after a *fresh* remote SKU lookup this tool performs itself
(never a cached/prior result) confirms zero matching remote products.

Refuses (leaves the row completely untouched) when:
  - no internal product resolves to the given --product-code
  - no synchronization record exists for that product (nothing to clear)
  - the synchronization record already has a woocommerce_product_id
    (a remote product may already be linked -- use
    sync_woocommerce_product_status.py to reconcile instead)
  - the synchronization record's woocommerce_status is not FAILED
    (nothing to clear)
  - the fresh remote SKU lookup errors, times out, or is otherwise
    uncertain
  - the fresh remote SKU lookup finds one or more matching products
    (a remote product may already exist -- this tool never clears in
    that case; reconcile it instead)

When safe, the sync row's own failure evidence (the original
response_payload -- error details, first_bytes, uploaded_media, etc.)
is preserved untouched. Only woocommerce_status moves FAILED -> PENDING
(a normal, resumable pre-attempt state, exactly mirroring
scripts/collect_reference_metadata.py's reset_stuck_in_progress_source()
IN_PROGRESS -> PENDING pattern for source_urls), error_code/error_message
are cleared (they described the now-resolved failure, not the row's
history), and a new "recovery_cleared" record is appended to
response_payload recording why, when, by which tool/version, and the
confirmed-absent remote lookup evidence.

internal_products is never written by this tool -- its own
woocommerce_status is not part of the recovery signal this tool clears
(see pipeline_state.derive_sync_recovery_state()) and is left exactly as
the pipeline already set it.

After a successful clear, the next normal orchestrator pass
(scripts/run_batch.py) re-derives this candidate's state from scratch
and, if every other readiness gate already holds, dispatches
create_woocommerce_draft.py exactly as it would for a candidate that
never failed -- that script performs its own fresh SKU check and
existing-sync-record reuse before ever creating a remote product, so
this tool never has to (and must never try to) create anything itself.

No raw SQL: every read and write goes through SupabaseRepository's
query builder, exactly targeted by product_code / sync_id.
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
from src.domain.woocommerce_status import WooCommerceSyncStatus  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

configure_utf8_console()


TOOL_NAME = "clear_woocommerce_sync_recovery"
TOOL_VERSION = "1.0.0"

RECOVERY_CLEAR = "WOO_SYNC_RECOVERY_CLEAR"

VALID_CONFIRMATIONS = {"CLEAR_RECOVERY"}


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
            "Clear the RECOVERY_REVIEW_REQUIRED signal for exactly one "
            "internal product's FAILED WooCommerce synchronization "
            "record, after confirming (via a fresh remote SKU lookup) "
            "that no remote product exists."
        )
    )

    parser.add_argument(
        "--product-code",
        required=True,
        help="Exact internal_products.product_code (WooCommerce SKU) to recover.",
    )

    parser.add_argument(
        "--confirm-clear",
        action="store_true",
        help="Confirm the recovery clear without an interactive prompt.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable prompts. Requires --confirm-clear.",
    )

    parser.add_argument(
        "--reason",
        default=None,
        help=(
            "Optional additional context recorded alongside the standard "
            "recovery-clear reason."
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
            "-- this tool only clears an unambiguous single sync record."
        )

    return rows[0] if rows else None


def find_remote_products_by_sku(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    sku: str,
    http_get: Callable[..., Any] = requests.get,
) -> list[dict[str, Any]]:
    """READ-ONLY GET of every remote WooCommerce product matching an
    exact SKU. Raises on any non-200 response or malformed payload --
    the caller must treat a raised exception as an uncertain lookup and
    refuse to clear, never as a confirmed-absent result."""
    api_url = build_api_url(store_url=store_url, api_version=api_version, endpoint="products")

    response = http_get(
        api_url,
        auth=HTTPBasicAuth(consumer_key, consumer_secret),
        params={"sku": sku, "status": "any", "per_page": 10},
        headers={
            "Accept": "application/json",
            "User-Agent": f"{TOOL_NAME}/{TOOL_VERSION}",
        },
        timeout=timeout_seconds,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"WooCommerce SKU lookup failed. HTTP status: {response.status_code}."
        )

    payload = response.json()

    if not isinstance(payload, list):
        raise RuntimeError("WooCommerce SKU lookup returned an unexpected response.")

    return payload


def evaluate_recovery_clear(
    sync: dict[str, Any] | None,
    remote_products: list[dict[str, Any]] | None,
    lookup_error: str | None,
) -> DecisionResult:
    """
    Pure decision: is it safe to clear this sync record's FAILED
    recovery signal?

    AUTO_PASS requires all of:
      - a sync record exists
      - it has no woocommerce_product_id
      - its woocommerce_status is exactly FAILED
      - the fresh remote SKU lookup succeeded (no lookup_error) and
        found exactly zero matching remote products

    Any other combination is BLOCKED -- a structural precondition this
    tool must never proceed past, not a business judgment call.
    """
    if sync is None:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "No WooCommerce synchronization record exists for this "
                "product; there is nothing to clear."
            ),
        )

    if sync.get("woocommerce_product_id"):
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "The synchronization record already has a WooCommerce "
                "product ID "
                f"({sync.get('woocommerce_product_id')!r}); a remote "
                "product may already be linked. This tool never clears "
                "in that case -- reconcile with "
                "sync_woocommerce_product_status.py instead."
            ),
        )

    if sync.get("woocommerce_status") != WooCommerceSyncStatus.FAILED:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "The synchronization record's status is "
                f"{sync.get('woocommerce_status')!r}, not FAILED; "
                "there is nothing to clear."
            ),
        )

    if lookup_error:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "The fresh remote SKU lookup was uncertain "
                f"({lookup_error}); refusing to clear without confirmed "
                "remote absence."
            ),
        )

    if remote_products is None:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "The fresh remote SKU lookup did not run; refusing to "
                "clear without confirmed remote absence."
            ),
        )

    if len(remote_products) > 1:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                f"{len(remote_products)} remote products share this "
                "SKU; stop for manual review."
            ),
        )

    if len(remote_products) == 1:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=RECOVERY_CLEAR,
            reason=(
                "A remote product already exists (id="
                f"{remote_products[0].get('id')!r}); this tool never "
                "clears when a remote product is confirmed to exist -- "
                "reconcile it instead."
            ),
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=RECOVERY_CLEAR,
        reason=(
            "A fresh remote SKU lookup confirmed zero matching "
            "WooCommerce products; safe to clear the FAILED recovery "
            "signal."
        ),
        evidence={"remote_product_count": 0},
    )


def build_cleared_response_payload(
    existing_payload: Any,
    reason: str,
    sku: str,
) -> dict[str, Any]:
    """Return response_payload with the original failure evidence fully
    preserved, plus one new "recovery_cleared" record. Never deletes or
    overwrites any existing key."""
    normalized = dict(existing_payload) if isinstance(existing_payload, dict) else {}

    normalized["recovery_cleared"] = {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "cleared_at": utc_now(),
        "sku_checked": sku,
        "reason": reason,
    }

    return normalized


def clear_sync_recovery(
    repository: SupabaseRepository,
    sync: dict[str, Any],
    reason: str,
    sku: str,
) -> dict[str, Any]:
    """Perform the one bounded write: move this exact sync row from
    FAILED to PENDING, preserving all prior history."""
    updated_payload = build_cleared_response_payload(
        existing_payload=sync.get("response_payload"),
        reason=reason,
        sku=sku,
    )

    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": WooCommerceSyncStatus.PENDING,
                "error_code": None,
                "error_message": None,
                "response_payload": updated_payload,
                "updated_at": utc_now(),
            }
        )
        .eq("sync_id", sync["sync_id"])
        .execute()
    )

    if not response.data:
        raise RuntimeError("Recovery-clear update returned no data.")

    return response.data[0]


def main() -> int:
    """Clear the recovery signal for exactly one product, after a fresh
    remote-absence confirmation. No production write happens unless
    every precondition holds and the operation is explicitly confirmed."""
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    if args.non_interactive and not args.confirm_clear:
        raise RuntimeError("--non-interactive requires --confirm-clear.")

    print("=" * 72)
    print("WOOCOMMERCE SYNC RECOVERY CLEAR")
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
    print(f"internal_products.woocommerce_status: {internal_product.get('woocommerce_status')}")

    if sync is None:
        print("No WooCommerce synchronization record exists. Nothing to clear.")
        return 1

    print(f"sync_id: {sync['sync_id']}")
    print(f"woocommerce_product_syncs.woocommerce_status (before): {sync.get('woocommerce_status')}")
    print(f"woocommerce_product_syncs.woocommerce_product_id (before): {sync.get('woocommerce_product_id')}")

    print()
    print("Performing a fresh remote SKU lookup (read-only GET, no create/retry)...")

    remote_products: list[dict[str, Any]] | None = None
    lookup_error: str | None = None

    try:
        remote_products = find_remote_products_by_sku(
            store_url=store_url,
            api_version=api_version,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            timeout_seconds=timeout_seconds,
            sku=args.product_code,
            # A fresh module-attribute lookup at call time (not the
            # function's bound default) -- so tests can monkeypatch
            # requests.get without needing a second code path.
            http_get=requests.get,
        )
    except Exception as error:  # noqa: BLE001
        lookup_error = f"{type(error).__name__}: {error}"

    print(f"Remote SKU lookup result: {'error' if lookup_error else f'{len(remote_products)} product(s) found'}")

    decision = evaluate_recovery_clear(
        sync=sync,
        remote_products=remote_products,
        lookup_error=lookup_error,
    )

    print()
    print(f"Decision: {decision.outcome}")
    print(f"Reason: {decision.reason}")

    if decision.outcome != Outcome.AUTO_PASS:
        print()
        print("Refusing to clear. No write was made.")
        return 1

    reason = (
        "Confirmed remote absence by a fresh SKU lookup performed by "
        f"{TOOL_NAME}/{TOOL_VERSION}; the FAILED sync attempt uploaded "
        "no media and created no remote product. Cleared to PENDING "
        "for a normal orchestrator retry."
    )

    if args.reason:
        reason = f"{reason} Operator note: {args.reason}"

    print()

    if args.confirm_clear:
        confirmation = "CLEAR_RECOVERY"
    elif args.non_interactive:
        confirmation = ""
    else:
        confirmation = input(
            "Type CLEAR_RECOVERY to clear this sync record's FAILED "
            "recovery signal, or press Enter to cancel: "
        ).strip().upper()

    if confirmation not in VALID_CONFIRMATIONS:
        print("Recovery clear was cancelled.")
        return 1

    updated_sync = clear_sync_recovery(
        repository=repository,
        sync=sync,
        reason=reason,
        sku=args.product_code,
    )

    print()
    print("Recovery signal cleared.")
    print(f"woocommerce_product_syncs.woocommerce_status (after): {updated_sync.get('woocommerce_status')}")
    print("internal_products row was not modified.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print()
        print("Recovery clear was cancelled.")
        sys.exit(130)

    except Exception as error:
        print()
        print("Recovery clear failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
