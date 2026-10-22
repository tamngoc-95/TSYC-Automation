"""
Bounded recovery for FAILED WooCommerce creates whose sync row has NO
WooCommerce product ID -- the record shapes the existing tools refuse:

  sync_woocommerce_product_status.py   needs a stored woocommerce_product_id
  clear_woocommerce_sync_recovery.py   would move the row to PENDING, i.e.
                                       toward a new create

Two legacy shapes exist on production (2026-10-22):

  A. LEGACY_REMOTE_LOSS -- recover_woocommerce_remote_loss.py (2026-09-19)
     cleared the Woo ID of a draft the owner had permanently deleted, kept
     it as response_payload.remote_loss_confirmed.previous_woocommerce_product_id,
     and a later create was rejected (400, SKU still in the lookup table).
     Since 2026-10-02 an owner deletion is intentional and terminal. With
     fresh proof (GET by the previous ID = 404, exact-SKU any = 0, trash = 0;
     woo_remote_lifecycle.evaluate_remote_removal) the historical ID is
     restored and the standard REMOTE_DELETED marker is recorded on both
     rows -- exactly what sync_woocommerce_product_status.py records when
     the ID was never cleared. Never recreated.

  B. UNCERTAIN_CREATE -- the POST was interrupted (e.g. RemoteProtocolError)
     and no Woo ID was ever stored. The outcome is resolved only when it no
     longer matters: fresh exact-SKU any = 0 and trash = 0, AND the
     mandatory catalog identity check (src.domain.woo_catalog_identity)
     proves the book is an EXISTING_PRODUCT. Then the product gets the
     terminal catalog marker and the sync row a
     create_outcome_resolution record; the original failure evidence is
     kept and the row stays FAILED. Nothing claims the create succeeded,
     and the residual question (did the POST create a product that was
     later removed?) is recorded, not answered. Any other classification
     leaves the recovery condition untouched (a non-terminal catalog block
     is recorded for human review).

Writes only internal_products and woocommerce_product_syncs, exact rows,
dry run unless --confirm-apply. Never creates, restores, deletes or edits
a WooCommerce product; all remote calls are GET.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import catalog_identity_loader  # noqa: E402
import create_woocommerce_draft as cwd  # noqa: E402
import sync_woocommerce_product_status as woo_sync  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_catalog_identity as wci  # noqa: E402
from src.domain import woo_remote_lifecycle  # noqa: E402
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus  # noqa: E402

configure_utf8_console()

TOOL_NAME = "recover_historical_woo_create"
TOOL_VERSION = "1.0.0"

LEGACY_REMOTE_LOSS = "LEGACY_REMOTE_LOSS"
UNCERTAIN_CREATE = "UNCERTAIN_CREATE"

ACTION_RECORD_OWNER_REMOVAL = "RECORD_OWNER_REMOVAL"
ACTION_RESOLVE_EXISTING_PRODUCT = "RESOLVE_EXISTING_PRODUCT"
ACTION_RECORD_CATALOG_REVIEW = "RECORD_CATALOG_REVIEW"
ACTION_NONE = "NONE"


@dataclass
class Plan:
    product_code: str
    shape: str | None = None
    action: str = ACTION_NONE
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    removal_state: str | None = None
    previous_id: int | None = None
    catalog_check: wci.CreateCheck | None = None


def record_shape(sync: dict[str, Any]) -> str | None:
    if sync.get("woocommerce_status") != WooCommerceSyncStatus.FAILED or sync.get("woocommerce_product_id"):
        return None
    payload = sync.get("response_payload") if isinstance(sync.get("response_payload"), dict) else {}
    loss = payload.get("remote_loss_confirmed")
    if isinstance(loss, dict) and loss.get("previous_woocommerce_product_id"):
        return LEGACY_REMOTE_LOSS
    return UNCERTAIN_CREATE


def plan_recovery(
    *,
    product: dict[str, Any],
    sync: dict[str, Any] | None,
    sku_any: list[dict[str, Any]] | None,
    sku_trash: list[dict[str, Any]] | None,
    get_previous: Callable[[int], tuple[int | None, Any]],
    run_catalog_check: Callable[[], wci.CreateCheck],
) -> Plan:
    """Pure decision over freshly read evidence (callables do the GETs)."""
    code = product["product_code"]
    plan = Plan(product_code=code)
    if sync is None:
        plan.reason = "No sync record; nothing to recover."
        return plan
    shape = record_shape(sync)
    plan.shape = shape
    if shape is None:
        plan.reason = "Sync is not a FAILED record without a WooCommerce product ID; use the standard tools."
        return plan
    if sku_any is None or sku_trash is None:
        plan.reason = "A fresh exact-SKU search (any/trash) was uncertain; nothing is decided."
        return plan
    plan.evidence["sku_any_ids"] = [p.get("id") for p in sku_any]
    plan.evidence["sku_trash_ids"] = [p.get("id") for p in sku_trash]

    if shape == LEGACY_REMOTE_LOSS:
        previous = int(sync["response_payload"]["remote_loss_confirmed"]["previous_woocommerce_product_id"])
        status_code, body = get_previous(previous)
        state, reason = woo_remote_lifecycle.evaluate_remote_removal(
            woocommerce_product_id=previous, expected_sku=code, get_http_status=status_code,
            get_body=body if isinstance(body, dict) else None, sku_any_matches=sku_any, sku_trash_matches=sku_trash)
        plan.evidence["previous_woocommerce_product_id"] = previous
        plan.evidence["previous_get_status"] = status_code
        if state is None:
            plan.reason = f"Owner removal not confirmed: {reason}"
            return plan
        plan.action = ACTION_RECORD_OWNER_REMOVAL
        plan.removal_state = state
        plan.previous_id = previous
        plan.reason = (f"{reason} The earlier TSYC draft #{previous} was removed in WooCommerce; "
                       "an owner removal is intentional (2026-10-02) and is never recreated.")
        return plan

    if sku_any or sku_trash:
        plan.reason = (f"Exact-SKU search finds {plan.evidence['sku_any_ids'] + plan.evidence['sku_trash_ids']}; "
                       "reconcile with sync_woocommerce_product_status.py instead.")
        return plan
    check = run_catalog_check()
    plan.catalog_check = check
    plan.evidence["catalog_classification"] = check.classification
    if check.classification == wci.EXISTING_PRODUCT:
        plan.action = ACTION_RESOLVE_EXISTING_PRODUCT
        plan.reason = (f"Exact SKU absent in every status, and the book is an existing product ({check.reason}). "
                       "It is never created, so the interrupted create's outcome no longer decides anything.")
    elif check.classification in (wci.CATALOG_UNAVAILABLE, wci.NEW_CONFIRMED):
        plan.reason = (f"Catalog check {check.classification}: the uncertain create stays in recovery review "
                       "(no evidence makes it moot).")
    else:
        plan.action = ACTION_RECORD_CATALOG_REVIEW
        plan.reason = f"Catalog check {check.classification}: {check.reason} Recorded for review; recovery stays."
    return plan


def apply_owner_removal(repository: Any, product: dict[str, Any], sync: dict[str, Any], plan: Plan) -> None:
    checked_at = cwd.utc_now()
    marker = woo_remote_lifecycle.build_removal_marker(
        state=plan.removal_state, woocommerce_product_id=plan.previous_id, sku=product["product_code"],
        reason=plan.reason, checked_at=checked_at, checked_by=f"{TOOL_NAME}/{TOOL_VERSION}")
    payload = dict(sync.get("response_payload") or {})
    payload[woo_remote_lifecycle.REMOVAL_MARKER_KEY] = marker
    payload["legacy_woo_id_restored"] = {
        "woocommerce_product_id": plan.previous_id,
        "source": "response_payload.remote_loss_confirmed.previous_woocommerce_product_id",
        "restored_at": checked_at,
        "restored_by": f"{TOOL_NAME}/{TOOL_VERSION}",
        "evidence": plan.evidence,
    }
    rows = (repository.client.table("woocommerce_product_syncs")
            .update({"woocommerce_product_id": plan.previous_id,
                     "woocommerce_status": WooCommerceSyncStatus.FAILED,
                     "error_code": woo_remote_lifecycle.REMOVAL_ERROR_CODES[plan.removal_state],
                     "error_message": plan.reason, "response_payload": payload,
                     "last_attempt_at": checked_at, "updated_at": checked_at})
            .eq("sync_id", sync["sync_id"]).is_("woocommerce_product_id", "null")
            .eq("woocommerce_status", WooCommerceSyncStatus.FAILED).execute().data or [])
    if not rows:
        raise RuntimeError("Sync row changed since it was read; nothing written.")
    metadata = dict(product.get("product_metadata") or {})
    metadata[woo_remote_lifecycle.REMOVAL_MARKER_KEY] = marker
    rows = (repository.client.table("internal_products")
            .update({"woocommerce_status": WooCommerceStatus.FAILED, "product_metadata": metadata,
                     "updated_at": checked_at})
            .eq("internal_product_id", product["internal_product_id"]).execute().data or [])
    if not rows:
        raise RuntimeError("Internal product update returned no data after the sync row was written; "
                           "re-run to verify (the sync marker alone keeps the product terminal).")


def apply_catalog_result(repository: Any, product: dict[str, Any], sync: dict[str, Any], plan: Plan) -> None:
    checked_at = cwd.utc_now()
    check = plan.catalog_check
    metadata = dict(product.get("product_metadata") or {})
    metadata[wci.MARKER_KEY] = check.marker(product_code=product["product_code"], checked_at=checked_at,
                                            checked_by=f"{TOOL_NAME}/{TOOL_VERSION}")
    rows = (repository.client.table("internal_products")
            .update({"product_metadata": metadata, "updated_at": checked_at})
            .eq("internal_product_id", product["internal_product_id"]).execute().data or [])
    if not rows:
        raise RuntimeError("Internal product update returned no data.")
    if plan.action != ACTION_RESOLVE_EXISTING_PRODUCT:
        return
    payload = dict(sync.get("response_payload") or {})
    payload[wci.RESOLUTION_KEY] = {
        "state": wci.RESOLVED_NOT_RECREATED_EXISTING,
        "sku": product["product_code"],
        "reason": plan.reason,
        "evidence": plan.evidence,
        "existing_products": [list(ids) for ids in check.logical_products],
        "residual_uncertainty": (
            "Whether the interrupted POST created a product that was later removed cannot be proven "
            "from the remote state; the candidate is never created either way."),
        "original_error_code": sync.get("error_code"),
        "resolved_at": checked_at,
        "resolved_by": f"{TOOL_NAME}/{TOOL_VERSION}",
    }
    rows = (repository.client.table("woocommerce_product_syncs")
            .update({"response_payload": payload, "updated_at": checked_at})
            .eq("sync_id", sync["sync_id"]).is_("woocommerce_product_id", "null")
            .eq("woocommerce_status", WooCommerceSyncStatus.FAILED).execute().data or [])
    if not rows:
        raise RuntimeError("Sync row changed since it was read; resolution not written (the catalog marker "
                           "alone keeps the candidate from being created).")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover FAILED, ID-less historical WooCommerce creates (GET only remotely).")
    parser.add_argument("--product-code", action="append", required=True, help="Exact product code (repeatable).")
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--confirm-apply", action="store_true", help="Write the decided local records.")
    parser.add_argument("--non-interactive", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    codes = list(dict.fromkeys(args.product_code))
    if len(codes) > args.max_products:
        raise RuntimeError(f"{len(codes)} product codes exceed --max-products {args.max_products}.")

    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    api_version = cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    consumer_key = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY")
    consumer_secret = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET")
    timeout = cwd.get_timeout_seconds()
    lookup = dict(store_url=store_url, api_version=api_version, consumer_key=consumer_key,
                  consumer_secret=consumer_secret, timeout_seconds=timeout)
    repository = cwd.SupabaseRepository()

    print("=" * 72)
    print(f"HISTORICAL WOO CREATE RECOVERY {TOOL_VERSION} ({'APPLY' if args.confirm_apply else 'DRY RUN'})")
    print("=" * 72)
    failures = 0
    for code in codes:
        products = (repository.client.table("internal_products").select("*").eq("product_code", code)
                    .limit(2).execute().data or [])
        if len(products) != 1:
            print(f"\n{code}: does not resolve to exactly one internal product -- skipped.")
            failures += 1
            continue
        product = products[0]
        syncs = (repository.client.table("woocommerce_product_syncs").select("*")
                 .eq("internal_product_id", product["internal_product_id"]).execute().data or [])
        if len(syncs) > 1:
            print(f"\n{code}: {len(syncs)} sync records -- ambiguous, skipped.")
            failures += 1
            continue
        sync = syncs[0] if syncs else None

        def get_previous(woo_id: int) -> tuple[int | None, Any]:
            try:
                return woo_sync.get_woocommerce_product(
                    store_url=store_url, api_version=api_version, consumer_key=consumer_key,
                    consumer_secret=consumer_secret, timeout_seconds=timeout, woocommerce_product_id=woo_id)
            except requests.RequestException:
                return None, None

        plan = plan_recovery(
            product=product, sync=sync,
            sku_any=woo_sync.search_remote_products_by_sku(sku=code, status="any", **{k: v for k, v in lookup.items()}),
            sku_trash=woo_sync.search_remote_products_by_sku(sku=code, status="trash", **{k: v for k, v in lookup.items()}),
            get_previous=get_previous,
            run_catalog_check=lambda: cwd.run_catalog_identity_check(
                repository=repository, product=product, store_url=store_url, api_version=api_version,
                consumer_key=consumer_key, consumer_secret=consumer_secret, timeout_seconds=timeout),
        )
        print(f"\n{code}\n  shape: {plan.shape}\n  action: {plan.action}\n  reason: {plan.reason}")
        print(f"  evidence: {plan.evidence}")
        if plan.catalog_check is not None:
            for item in plan.catalog_check.evidence[:6]:
                print(f"    - {item.get('origin')} {item.get('ref')}: {item.get('reason')} ({item.get('title')!r})")
        if not args.confirm_apply or plan.action == ACTION_NONE:
            continue
        try:
            if plan.action == ACTION_RECORD_OWNER_REMOVAL:
                apply_owner_removal(repository, product, sync, plan)
            else:
                apply_catalog_result(repository, product, sync, plan)
            print("  written.")
        except RuntimeError as error:
            print(f"  NOT written: {error}")
            failures += 1
    print()
    print("No WooCommerce product was created, restored, deleted or modified.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"Recovery failed: {error}")
        sys.exit(1)
