"""
Push repaired, APPROVED Vietnamese content to ONE existing WooCommerce DRAFT.

Bounded follow-up to the FT-BATCH-9-2026-10-01 storefront-content repair
(prepare_product_content.py --action REPAIR). For exactly one product code:

  1. local: the product has a woocommerce_product_syncs row with a remote
     product id and SKU; its APPROVED vi content passes storefront
     validation (create_woocommerce_draft.require_storefront_valid_content)
  2. remote: GET the exact stored product id; it must exist, carry the
     exact same id and SKU, and be status == "draft" -- or, only with
     --allow-published (2026-10-02 shop-owner authorization) and a local
     PUBLISHED status, "publish". Anything else (missing, SKU mismatch,
     private/pending/trash) stops this product with no write. Status is
     never sent; it is verified unchanged after the PUT
  3. manual-edit protection: the remote description/short_description must
     still be what TSYC originally sent (sync request_payload) or already
     the new content -- a shop-owner edit made in WooCommerce is never
     overwritten
  4. PUT only {"description", "short_description"}. Never status, never
     regular_price/sale_price/price, never any other field.
  5. re-GET and verify: content applied, status unchanged, prices and
     other manually maintained store fields (PRESERVED_FIELDS) unchanged.

An uncertain PUT (timeout/connection error) is never retried: the product
is re-read once to reconcile; if the new content is not provably present
the result is UNCERTAIN for human review.

Never creates a product, never publishes, never sets a price.

Usage:
  .venv/Scripts/python.exe scripts/update_woocommerce_draft_content.py \
      --product-code TSYC-FB-HIST-2026-002-CAN-0011 --dry-run
  .venv/Scripts/python.exe scripts/update_woocommerce_draft_content.py \
      --product-code TSYC-FB-HIST-2026-002-CAN-0011 --non-interactive --confirm-update
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
from src.domain.rules import storefront_text  # noqa: E402
from src.domain.woocommerce_status import WooCommerceStatus  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

configure_utf8_console()

UPDATER_NAME = "woocommerce_draft_content_updater"
UPDATER_VERSION = "1.0.0"

# Result codes.
UPDATED = "UPDATED"
NO_OP = "NO_OP"
DRY_RUN = "DRY_RUN"
NOT_DRAFT = "REMOTE_NOT_DRAFT"
REMOTE_MISSING = "REMOTE_MISSING"
SKU_MISMATCH = "REMOTE_SKU_MISMATCH"
MANUAL_EDIT = "REMOTE_MANUALLY_EDITED"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
UNCERTAIN = "UPDATE_RESULT_UNCERTAIN"
VERIFY_FAILED = "POST_UPDATE_VERIFICATION_FAILED"

PRICE_FIELDS = ("regular_price", "sale_price", "price")
UPDATABLE_FIELDS = ("description", "short_description")
# Verified unchanged after every PUT (prices plus manually maintained
# store fields that a description update must never touch).
PRESERVED_FIELDS = PRICE_FIELDS + ("name", "slug", "stock_status", "stock_quantity", "catalog_visibility")


def comparable_text(value: str | None) -> str:
    """HTML/entity/whitespace-insensitive text for comparing what TSYC sent
    with what WooCommerce stored (WordPress may re-serialize markup)."""
    return " ".join(storefront_text.clean_source_text(value).split())


def allowed_remote_statuses(allow_published: bool, local_woocommerce_status: str | None) -> set[str]:
    """
    Remote statuses this run may update. "draft" always. "publish" only
    with the explicit --allow-published authorization (2026-10-02 shop-
    owner authorization to repair defective storefront descriptions of
    already-PUBLISHED products) AND when the local state already records
    the product as PUBLISHED -- the update never changes status either way.
    """
    statuses = {"draft"}
    if allow_published and local_woocommerce_status == WooCommerceStatus.PUBLISHED:
        statuses.add("publish")
    return statuses


def classify_remote(
    remote: dict[str, Any] | None,
    expected_sku: str,
    original_payload: dict[str, Any],
    new_payload: dict[str, str],
    allowed_statuses: set[str] | frozenset[str] = frozenset({"draft"}),
    expected_product_id: int | str | None = None,
) -> str | None:
    """
    Pure: None when the remote product may be updated; otherwise the stop
    code. NO_OP when the remote already carries the new content.
    """
    if not remote or not remote.get("id"):
        return REMOTE_MISSING

    if expected_product_id is not None and str(remote.get("id")) != str(expected_product_id):
        return REMOTE_MISSING

    if str(remote.get("sku") or "") != expected_sku:
        return SKU_MISMATCH

    if remote.get("status") not in allowed_statuses:
        return NOT_DRAFT

    if all(
        comparable_text(remote.get(field)) == comparable_text(new_payload[field])
        for field in UPDATABLE_FIELDS
    ):
        return NO_OP

    for field in UPDATABLE_FIELDS:
        remote_text = comparable_text(remote.get(field))
        if remote_text in {
            comparable_text(original_payload.get(field)),
            comparable_text(new_payload[field]),
        }:
            continue
        return MANUAL_EDIT

    return None


def build_update_payload(content: dict[str, Any]) -> dict[str, str]:
    """Exactly the two customer-facing content fields -- nothing else."""
    payload = {
        "description": cwd.build_description(content),
        "short_description": cwd.build_short_description(content),
    }
    assert set(payload) == set(UPDATABLE_FIELDS)
    return payload


def verify_after_update(
    before: dict[str, Any],
    after: dict[str, Any] | None,
    new_payload: dict[str, str],
    expected_sku: str,
) -> list[str]:
    """Pure: problems found after the PUT (empty list = verified)."""
    problems: list[str] = []
    if not after:
        return ["remote product could not be re-read"]
    if after.get("status") != before.get("status"):
        problems.append(
            f"status is {after.get('status')!r}, expected unchanged {before.get('status')!r}"
        )
    if str(after.get("sku") or "") != expected_sku:
        problems.append("SKU changed")
    for field in PRESERVED_FIELDS:
        if (before.get(field) or "") != (after.get(field) or ""):
            problems.append(f"{field} changed")
    for field in UPDATABLE_FIELDS:
        if comparable_text(after.get(field)) != comparable_text(new_payload[field]):
            problems.append(f"{field} not applied")
    return problems


class WooClient:
    """Minimal read/update client for one product id."""

    def __init__(self, store_url: str, api_version: str, key: str, secret: str, timeout: int) -> None:
        self.store_url = store_url
        self.api_version = api_version
        self.auth = HTTPBasicAuth(key, secret)
        self.timeout = timeout
        self.headers = {
            "Accept": "application/json",
            "User-Agent": f"{UPDATER_NAME}/{UPDATER_VERSION}",
        }

    def _url(self, product_id: int | str) -> str:
        return cwd.build_api_url(self.store_url, self.api_version, f"products/{product_id}")

    def get(self, product_id: int | str) -> dict[str, Any] | None:
        response = requests.get(
            self._url(product_id),
            auth=self.auth,
            params={"context": "edit"},
            headers=self.headers,
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def update(self, product_id: int | str, payload: dict[str, str]) -> dict[str, Any]:
        if set(payload) != set(UPDATABLE_FIELDS):
            raise RuntimeError(f"Refusing to send fields other than {UPDATABLE_FIELDS}.")
        response = requests.put(
            self._url(product_id),
            auth=self.auth,
            json=payload,
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def load_local_target(repository: SupabaseRepository, product_code: str) -> dict[str, Any]:
    products = (
        repository.client.table("internal_products")
        .select("internal_product_id, product_code, candidate_id, woocommerce_status")
        .eq("product_code", product_code)
        .execute()
        .data
        or []
    )
    if len(products) != 1:
        raise RuntimeError(f"product_code did not resolve to exactly one product: {product_code}")
    product = products[0]

    syncs = (
        repository.client.table("woocommerce_product_syncs")
        .select("sync_id, woocommerce_product_id, product_sku, woocommerce_status, request_payload")
        .eq("internal_product_id", product["internal_product_id"])
        .execute()
        .data
        or []
    )
    if len(syncs) != 1:
        raise RuntimeError(f"Expected exactly one woocommerce_product_syncs row, found {len(syncs)}.")
    sync = syncs[0]
    if not sync.get("woocommerce_product_id") or not sync.get("product_sku"):
        raise RuntimeError("Sync row has no remote product id / SKU; remote identity is not certain.")

    content = cwd.get_approved_content(repository=repository, internal_product_id=product["internal_product_id"])
    if content is None:
        raise RuntimeError("No APPROVED Vietnamese content.")
    cwd.require_storefront_valid_content(content)

    return {"product": product, "sync": sync, "content": content}


def run_update(
    repository: SupabaseRepository,
    client: WooClient,
    product_code: str,
    dry_run: bool,
    log: Callable[..., Any] | None = None,
    allow_published: bool = False,
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
    new_payload = build_update_payload(local["content"])
    original_payload = sync.get("request_payload") or {}

    allowed = allowed_remote_statuses(allow_published, local["product"].get("woocommerce_status"))
    before = client.get(product_id)
    stop = classify_remote(
        before, sku, original_payload, new_payload,
        allowed_statuses=allowed, expected_product_id=product_id,
    )
    report["remote_status"] = (before or {}).get("status")

    if stop is not None:
        report.update(result=stop)
        return report

    if dry_run:
        report.update(result=DRY_RUN)
        return report

    try:
        client.update(product_id, new_payload)
    except (requests.Timeout, requests.ConnectionError) as error:
        # Never retried: re-read once to reconcile.
        after = client.get(product_id)
        problems = verify_after_update(before, after, new_payload, sku)
        if problems:
            report.update(result=UNCERTAIN, detail=f"{type(error).__name__}; {problems}")
            return report
        report["detail"] = f"{type(error).__name__} during PUT; content verified present on re-read"

    after = client.get(product_id)
    problems = verify_after_update(before, after, new_payload, sku)
    if problems:
        report.update(result=VERIFY_FAILED, detail="; ".join(problems))
        return report

    report.update(result=UPDATED)

    if log is not None:
        try:
            log(
                message=(
                    f"WooCommerce product {product_id} ({sku}, status {before.get('status')}) "
                    "description/short_description updated from repaired APPROVED content "
                    "(status, prices and other store fields unchanged)."
                ),
                process_name=UPDATER_NAME,
                candidate_id=local["product"].get("candidate_id"),
                process_step="UPDATE_DRAFT_CONTENT",
                log_level="INFO",
                status=UPDATED,
                error_details={
                    "woocommerce_product_id": product_id,
                    "sku": sku,
                    "previous_description": before.get("description"),
                    "previous_short_description": before.get("short_description"),
                },
            )
        except Exception as error:  # audit failure must not mask the update
            print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")

    return report


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update one WooCommerce DRAFT's description from repaired APPROVED content.")
    parser.add_argument("--product-code", required=True, help="Exact internal product code.")
    parser.add_argument("--dry-run", action="store_true", help="Read-only: check local and remote, write nothing.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-update", action="store_true", help="Required to write.")
    parser.add_argument(
        "--allow-published",
        action="store_true",
        help=(
            "Also update a product that is already published remotely AND "
            "PUBLISHED locally (explicit shop-owner authorization). Status is "
            "never sent and is verified unchanged."
        ),
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    if not args.dry_run and not args.confirm_update:
        raise RuntimeError("Writing requires --confirm-update (or use --dry-run).")

    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    if not store_url.startswith("https://"):
        raise RuntimeError("WOOCOMMERCE_URL must use HTTPS.")

    client = WooClient(
        store_url=store_url,
        api_version=(cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()),
        key=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        secret=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
        timeout=cwd.get_timeout_seconds(),
    )
    repository = SupabaseRepository()
    report = run_update(
        repository=repository,
        client=client,
        product_code=args.product_code,
        dry_run=args.dry_run,
        log=repository.write_process_log,
        allow_published=args.allow_published,
    )
    print(f"WOO_CONTENT_UPDATE_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
