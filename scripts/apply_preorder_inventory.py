"""
Apply the TSYC Fahasa pre-order inventory policy to existing WooCommerce
DRAFTS (src/domain/rules/preorder_inventory_rules.py).

For each EXACT --product-code (bounded by --max-products):

  1. local: the internal product's candidate is a Fahasa pre-order
     candidate (FAHASA-..., supply PREORDER_PENDING_SUPPLY_CONFIRMATION);
     exactly one woocommerce_product_syncs row with a remote id and SKU.
  2. remote: GET the exact stored id. It must be the same id and SKU, a
     DRAFT, created by TSYC (_tsyc_internal_product_id meta), unpriced, and
     its inventory still TSYC's creation default. Published/trashed/
     pending/private, priced, owner-restocked or foreign products are
     isolated with a stop code and never written.
  3. PUT only {"manage_stock": true, "stock_quantity": 0, "backorders":
     "yes"}. Never status, never a price, never stock_status (WooCommerce
     derives "onbackorder"), never description or images.
  4. re-GET and verify: inventory applied, stock_status "onbackorder",
     status/prices/text/images unchanged.

An uncertain PUT (timeout/connection error) is never retried: the product
is re-read once to reconcile; if the policy is not provably applied the
result is UPDATE_RESULT_UNCERTAIN. Idempotent: a product already carrying
the policy is NO_OP. Dry run unless --confirm-update.

Usage:
  .venv/Scripts/python.exe scripts/apply_preorder_inventory.py \
      --product-code TSYC-FAHASA-2026-002-CAN-0001 --max-products 1
  ... --non-interactive --confirm-update
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_woocommerce_draft as cwd  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_remote_status  # noqa: E402
from src.domain.rules import preorder_inventory_rules as rules  # noqa: E402

configure_utf8_console()

APPLIER_NAME = "woocommerce_preorder_inventory"
APPLIER_VERSION = "1.0.0"
MAX_PRODUCTS_HARD_CAP = 20

UPDATED = "UPDATED"
DRY_RUN = "DRY_RUN"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
UNCERTAIN = "UPDATE_RESULT_UNCERTAIN"
VERIFY_FAILED = "POST_UPDATE_VERIFICATION_FAILED"


class WooInventoryClient:
    """Read one product / write exactly the inventory fields."""

    def __init__(self, store_url: str, api_version: str, key: str, secret: str, timeout: int) -> None:
        self.store_url = store_url
        self.api_version = api_version
        self.auth = HTTPBasicAuth(key, secret)
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": f"{APPLIER_NAME}/{APPLIER_VERSION}"}

    def _url(self, product_id: int | str) -> str:
        return cwd.build_api_url(self.store_url, self.api_version, f"products/{product_id}")

    def get(self, product_id: int | str) -> dict[str, Any] | None:
        response = requests.get(
            self._url(product_id), auth=self.auth, params={"context": "edit"}, headers=self.headers, timeout=self.timeout
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def update_inventory(self, product_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != set(rules.INVENTORY_FIELDS):
            raise RuntimeError(f"Refusing to send fields other than {rules.INVENTORY_FIELDS}.")
        response = requests.put(self._url(product_id), auth=self.auth, json=payload, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


def load_local_target(repository: Any, product_code: str) -> dict[str, Any]:
    products = (
        repository.client.table("internal_products")
        .select("internal_product_id, product_code, candidate_id, woocommerce_status")
        .eq("product_code", product_code).execute().data
        or []
    )
    if len(products) != 1:
        raise RuntimeError(f"product_code did not resolve to exactly one product: {product_code}")
    product = products[0]

    candidate = cwd.get_candidate_origin(repository=repository, candidate_id=product["candidate_id"])
    if not rules.is_fahasa_preorder_candidate(candidate):
        raise RuntimeError(f"{rules.NOT_PREORDER_PRODUCT}: candidate is not a Fahasa pre-order candidate.")

    syncs = (
        repository.client.table("woocommerce_product_syncs")
        .select("sync_id, woocommerce_product_id, product_sku, woocommerce_status, request_payload, response_payload")
        .eq("internal_product_id", product["internal_product_id"]).execute().data
        or []
    )
    if len(syncs) != 1:
        raise RuntimeError(f"Expected exactly one woocommerce_product_syncs row, found {len(syncs)}.")
    sync = syncs[0]
    if not sync.get("woocommerce_product_id") or not sync.get("product_sku"):
        raise RuntimeError("Sync row has no remote product id / SKU; remote identity is not certain.")
    blocked = woo_remote_status.local_write_block_reason(product, sync)
    if blocked is not None:
        raise RuntimeError(f"Local write gate: {blocked}")
    return {"product": product, "candidate": candidate, "sync": sync}


def run_apply(
    repository: Any,
    client: Any,
    product_code: str,
    dry_run: bool,
    log: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"product_code": product_code, "result": None, "detail": None}
    try:
        local = load_local_target(repository, product_code)
    except RuntimeError as error:
        report.update(result=LOCAL_NOT_READY, detail=str(error))
        return report

    sync = local["sync"]
    product_id = sync["woocommerce_product_id"]
    sku = str(sync["product_sku"])
    report["woocommerce_product_id"] = product_id

    before = client.get(product_id)
    classification = rules.classify_inventory_target(
        before, product_id, sku, local["product"]["internal_product_id"], sync.get("request_payload") or {}
    )
    report["remote_status"] = (before or {}).get("status")
    if classification != rules.ELIGIBLE:
        report.update(result=classification)
        return report
    if dry_run:
        report.update(result=DRY_RUN)
        return report

    payload = rules.build_inventory_payload()
    try:
        client.update_inventory(product_id, payload)
    except (requests.Timeout, requests.ConnectionError) as error:
        # Never retried: re-read once to reconcile.
        problems = rules.verify_inventory_after(before, client.get(product_id))
        if problems:
            report.update(result=UNCERTAIN, detail=f"{type(error).__name__}; {problems}")
            return report
        report["detail"] = f"{type(error).__name__} during PUT; policy verified present on re-read"

    after = client.get(product_id)
    problems = rules.verify_inventory_after(before, after)
    if problems:
        report.update(result=VERIFY_FAILED, detail="; ".join(problems))
        return report
    report.update(result=UPDATED, stock_status=after.get("stock_status"))

    if log is not None:
        try:
            log(
                message=(
                    f"WooCommerce draft {product_id} ({sku}) pre-order inventory applied: manage_stock=true, "
                    "stock_quantity=0, backorders=yes -> stock_status onbackorder (status, prices, text and images unchanged)."
                ),
                process_name=APPLIER_NAME,
                candidate_id=local["product"].get("candidate_id"),
                process_step="APPLY_PREORDER_INVENTORY",
                log_level="INFO",
                status=UPDATED,
                error_details={
                    "woocommerce_product_id": product_id,
                    "sku": sku,
                    "previous_inventory": {
                        field: before.get(field) for field in rules.INVENTORY_FIELDS + ("stock_status",)
                    },
                },
            )
        except Exception as error:  # audit failure must not mask the update
            print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")
    return report


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply the Fahasa pre-order inventory policy to exact Woo drafts.")
    parser.add_argument("--product-code", action="append", required=True, help="Exact internal product code. Repeat.")
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-update", action="store_true", help="Required to write; otherwise a dry run.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_products <= MAX_PRODUCTS_HARD_CAP:
        parser.error(f"--max-products must be between 1 and {MAX_PRODUCTS_HARD_CAP}.")
    if len(set(args.product_code)) != len(args.product_code):
        parser.error("--product-code values must be unique.")
    if len(args.product_code) > args.max_products:
        parser.error("More --product-code values than --max-products.")
    return args


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    if not store_url.startswith("https://"):
        raise RuntimeError("WOOCOMMERCE_URL must use HTTPS.")

    from src.repositories.supabase_repository import SupabaseRepository

    client = WooInventoryClient(
        store_url=store_url,
        api_version=cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip(),
        key=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        secret=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
        timeout=cwd.get_timeout_seconds(),
    )
    repository = SupabaseRepository()
    for product_code in args.product_code:  # serial: one product at a time
        report = run_apply(repository, client, product_code, dry_run=not args.confirm_update, log=repository.write_process_log)
        print(f"PREORDER_INVENTORY_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
