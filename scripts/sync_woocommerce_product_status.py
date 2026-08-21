import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


SYNC_NAME = "woocommerce_product_status_sync"
SYNC_VERSION = "1.1.0"


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize WooCommerce product statuses into Supabase."
        )
    )

    parser.add_argument(
        "--product-code",
        help=(
            "Synchronize one exact internal product code instead of all "
            "WooCommerce-linked products."
        ),
    )

    parser.add_argument(
        "--confirm-sync",
        action="store_true",
        help=(
            "Confirm synchronization without an interactive prompt."
        ),
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable prompts. Requires --confirm-sync and --product-code."
        ),
    )

    return parser.parse_args()


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def get_required_environment_variable(
    variable_name: str,
) -> str:
    """Return one required environment variable."""
    value = os.getenv(
        variable_name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {variable_name}"
        )

    return value


def get_timeout_seconds() -> int:
    """Return the configured HTTP timeout."""
    raw_value = os.getenv(
        "WOOCOMMERCE_TIMEOUT_SECONDS",
        "30",
    ).strip()

    try:
        timeout_seconds = int(raw_value)

    except ValueError as error:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be an integer."
        ) from error

    if timeout_seconds <= 0:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be greater than zero."
        )

    return timeout_seconds


def build_api_url(
    store_url: str,
    api_version: str,
    endpoint: str,
) -> str:
    """Build a WooCommerce REST API URL."""
    return (
        f"{store_url.rstrip('/')}/wp-json/"
        f"{api_version.strip('/')}/"
        f"{endpoint.strip('/')}"
    )


def safe_json_response(
    response: requests.Response,
) -> Any:
    """Return JSON or a readable raw HTTP response."""
    try:
        return response.json()

    except ValueError:
        return {
            "raw_response": response.text[:5000],
        }


def get_sync_records(
    repository: SupabaseRepository,
    product_code: str | None = None,
) -> list[dict[str, Any]]:
    """Return local WooCommerce records that have a product ID."""
    query = (
        repository.client
        .table("woocommerce_product_syncs")
        .select(
            "sync_id,"
            "internal_product_id,"
            "woocommerce_product_id,"
            "woocommerce_status,"
            "product_sku,"
            "product_name,"
            "product_permalink,"
            "request_payload,"
            "response_payload,"
            "sync_attempt_count,"
            "last_attempt_at,"
            "synced_at,"
            "created_at,"
            "updated_at"
        )
        .not_.is_(
            "woocommerce_product_id",
            "null",
        )
    )

    if product_code:
        product_response = (
            repository.client
            .table("internal_products")
            .select(
                "internal_product_id,"
                "product_code"
            )
            .eq(
                "product_code",
                product_code,
            )
            .limit(2)
            .execute()
        )

        product_rows = product_response.data or []

        if len(product_rows) != 1:
            raise RuntimeError(
                "Product code did not resolve to exactly one internal product."
            )

        query = query.eq(
            "internal_product_id",
            product_rows[0]["internal_product_id"],
        )

    response = (
        query
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    rows = response.data or []

    if product_code and len(rows) != 1:
        raise RuntimeError(
            "The selected internal product does not have exactly one "
            "WooCommerce synchronization record with a remote product ID."
        )

    return rows



def get_internal_product(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return one linked internal product."""
    response = (
        repository.client
        .table("internal_products")
        .select(
            "internal_product_id,"
            "candidate_id,"
            "product_code,"
            "title,"
            "pricing_status,"
            "content_status,"
            "image_status,"
            "woocommerce_status,"
            "review_required,"
            "review_reason,"
            "product_metadata"
        )
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def get_woocommerce_product(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    woocommerce_product_id: int,
) -> tuple[int, Any]:
    """Read one product from WooCommerce."""
    api_url = build_api_url(
        store_url=store_url,
        api_version=api_version,
        endpoint=f"products/{woocommerce_product_id}",
    )

    response = requests.get(
        api_url,
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret,
        ),
        headers={
            "Accept": "application/json",
            "User-Agent": (
                f"{SYNC_NAME}/"
                f"{SYNC_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    return (
        response.status_code,
        safe_json_response(response),
    )


def map_remote_status(
    remote_status: str | None,
) -> tuple[str, str]:
    """
    Map WooCommerce status to local synchronization and workflow statuses.

    The first returned value belongs to woocommerce_product_syncs.
    The second returned value belongs to internal_products.
    """
    mapping = {
        "draft": (
            "DRAFT_CREATED",
            "DRAFT_CREATED",
        ),
        "pending": (
            "PENDING_REVIEW",
            "READY_TO_PUBLISH",
        ),
        "private": (
            "PRIVATE",
            "READY_TO_PUBLISH",
        ),
        "publish": (
            "PUBLISHED",
            "PUBLISHED",
        ),
        "trash": (
            "TRASHED",
            "FAILED",
        ),
    }

    return mapping.get(
        remote_status or "",
        (
            "REVIEW_REQUIRED",
            "REVIEW_REQUIRED",
        ),
    )


def build_updated_metadata(
    internal_product: dict[str, Any],
    remote_product: dict[str, Any],
) -> dict[str, Any]:
    """Add the latest WooCommerce status to product metadata."""
    existing_metadata = internal_product.get(
        "product_metadata"
    )

    if isinstance(
        existing_metadata,
        dict,
    ):
        product_metadata = dict(
            existing_metadata
        )

    else:
        product_metadata = {}

    product_metadata[
        "woocommerce_status_sync"
    ] = {
        "sync_name": SYNC_NAME,
        "sync_version": SYNC_VERSION,
        "checked_at": utc_now(),
        "woocommerce_product_id": remote_product.get(
            "id"
        ),
        "remote_status": remote_product.get(
            "status"
        ),
        "remote_name": remote_product.get(
            "name"
        ),
        "remote_sku": remote_product.get(
            "sku"
        ),
        "remote_permalink": remote_product.get(
            "permalink"
        ),
        "remote_regular_price": remote_product.get(
            "regular_price"
        ),
        "remote_sale_price": remote_product.get(
            "sale_price"
        ),
        "remote_stock_status": remote_product.get(
            "stock_status"
        ),
        "remote_modified_at": remote_product.get(
            "date_modified_gmt"
        ),
    }

    return product_metadata


def clear_recovery_marker(
    response_payload: Any,
    remote_product: dict[str, Any],
) -> dict[str, Any]:
    """Clear create-draft recovery flags after successful remote reconciliation."""
    if isinstance(
        response_payload,
        dict,
    ):
        normalized = dict(
            response_payload
        )
    else:
        normalized = {}

    if normalized.get(
        "recovery_required"
    ) is True:
        normalized[
            "recovery_required"
        ] = False
        normalized[
            "recovered_at"
        ] = utc_now()
        normalized[
            "recovery_resolution"
        ] = (
            "WooCommerce product was found remotely and local state "
            "was reconciled by the status synchronization workflow."
        )

    normalized[
        "latest_status_check"
    ] = {
        "sync_name": SYNC_NAME,
        "sync_version": SYNC_VERSION,
        "checked_at": utc_now(),
        "woocommerce_product": remote_product,
    }

    return normalized


def update_sync_record(
    repository: SupabaseRepository,
    sync_record: dict[str, Any],
    remote_product: dict[str, Any],
    local_sync_status: str,
) -> None:
    """Update the local WooCommerce synchronization record."""
    response_payload = clear_recovery_marker(
        response_payload=sync_record.get(
            "response_payload"
        ),
        remote_product=remote_product,
    )

    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": local_sync_status,
                "product_name": (
                    remote_product.get("name")
                    or sync_record.get("product_name")
                ),
                "product_permalink": (
                    remote_product.get("permalink")
                    or sync_record.get("product_permalink")
                ),
                "response_payload": response_payload,
                "error_code": None,
                "error_message": None,
                "last_attempt_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_record["sync_id"],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "WooCommerce synchronization record update returned no data."
        )


def update_internal_product(
    repository: SupabaseRepository,
    internal_product: dict[str, Any],
    remote_product: dict[str, Any],
    internal_status: str,
) -> None:
    """Update the internal product with the actual website status."""
    remote_status = remote_product.get(
        "status"
    )

    remote_price = (
        remote_product.get("regular_price")
        or remote_product.get("price")
    )

    review_required = False
    review_reason = None

    if remote_status == "trash":
        review_required = True
        review_reason = (
            "The linked WooCommerce product is currently in Trash."
        )

    elif internal_status in {
        "FAILED",
        "REVIEW_REQUIRED",
    }:
        review_required = True
        review_reason = (
            f"Unsupported or review-required WooCommerce product status: "
            f"{remote_status}"
        )

    update_payload: dict[str, Any] = {
        "woocommerce_status": internal_status,
        "review_required": review_required,
        "review_reason": review_reason,
        "product_metadata": build_updated_metadata(
            internal_product=internal_product,
            remote_product=remote_product,
        ),
        "updated_at": utc_now(),
    }

    # WooCommerce prices are observed for audit metadata only.
    # They must never change internal pricing workflow status automatically.
    # Purchase price and approved pricing remain controlled by TSYC sources
    # and the internal pricing workflow.

    response = (
        repository.client
        .table("internal_products")
        .update(update_payload)
        .eq(
            "internal_product_id",
            internal_product[
                "internal_product_id"
            ],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Internal product status update returned no data."
        )


def mark_product_missing(
    repository: SupabaseRepository,
    sync_record: dict[str, Any],
    internal_product: dict[str, Any] | None,
    response_payload: Any,
) -> None:
    """Mark a local product when WooCommerce returns 404."""
    existing_response_payload = sync_record.get(
        "response_payload"
    )

    if isinstance(
        existing_response_payload,
        dict,
    ):
        stored_payload = dict(
            existing_response_payload
        )

    else:
        stored_payload = {}

    stored_payload[
        "latest_status_check"
    ] = {
        "sync_name": SYNC_NAME,
        "sync_version": SYNC_VERSION,
        "checked_at": utc_now(),
        "http_status": 404,
        "response": response_payload,
    }

    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": "MISSING",
                "response_payload": stored_payload,
                "error_code": "WOOCOMMERCE_PRODUCT_NOT_FOUND",
                "error_message": (
                    "The WooCommerce product ID was not found."
                ),
                "last_attempt_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_record[
                "sync_id"
            ],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Could not mark the WooCommerce product as missing."
        )

    if internal_product:
        (
            repository.client
            .table("internal_products")
            .update(
                {
                    "woocommerce_status": "FAILED",
                    "review_required": True,
                    "review_reason": (
                        "The linked WooCommerce product could not be found."
                    ),
                    "updated_at": utc_now(),
                }
            )
            .eq(
                "internal_product_id",
                internal_product[
                    "internal_product_id"
                ],
            )
            .execute()
        )


def mark_sync_error(
    repository: SupabaseRepository,
    sync_record: dict[str, Any],
    error_code: str,
    error_message: str,
    response_payload: Any,
) -> None:
    """Save a non-404 synchronization error."""
    if isinstance(
        response_payload,
        dict,
    ):
        normalized_payload = response_payload

    else:
        normalized_payload = {
            "raw_response": str(
                response_payload
            )[:5000],
        }

    existing_response_payload = sync_record.get(
        "response_payload"
    )

    if isinstance(
        existing_response_payload,
        dict,
    ):
        stored_payload = dict(
            existing_response_payload
        )

    else:
        stored_payload = {}

    stored_payload[
        "latest_status_check"
    ] = {
        "sync_name": SYNC_NAME,
        "sync_version": SYNC_VERSION,
        "checked_at": utc_now(),
        "error": normalized_payload,
    }

    (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "error_code": error_code,
                "error_message": error_message,
                "response_payload": stored_payload,
                "last_attempt_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_record[
                "sync_id"
            ],
        )
        .execute()
    )


def print_product_result(
    sync_record: dict[str, Any],
    remote_product: dict[str, Any],
    local_sync_status: str,
    internal_status: str,
) -> None:
    """Print one synchronization result."""
    print()
    print("-" * 72)

    print(
        "WooCommerce product ID: "
        f"{remote_product.get('id')}"
    )

    print(
        "Product SKU: "
        f"{remote_product.get('sku') or sync_record.get('product_sku')}"
    )

    print(
        "Product name: "
        f"{remote_product.get('name')}"
    )

    print(
        "Remote status: "
        f"{remote_product.get('status')}"
    )

    print(
        "Local sync status: "
        f"{local_sync_status}"
    )

    print(
        "Internal product status: "
        f"{internal_status}"
    )

    print(
        "Regular price: "
        f"{remote_product.get('regular_price') or '[not set]'}"
    )

    print(
        "Stock status: "
        f"{remote_product.get('stock_status') or '[not returned]'}"
    )


def synchronize_one_product(
    repository: SupabaseRepository,
    sync_record: dict[str, Any],
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
) -> bool:
    """Synchronize one WooCommerce product status."""
    internal_product = get_internal_product(
        repository=repository,
        internal_product_id=sync_record[
            "internal_product_id"
        ],
    )

    if internal_product is None:
        mark_sync_error(
            repository=repository,
            sync_record=sync_record,
            error_code="INTERNAL_PRODUCT_NOT_FOUND",
            error_message=(
                "The linked internal product was not found."
            ),
            response_payload={
                "internal_product_id": sync_record[
                    "internal_product_id"
                ],
            },
        )

        print()
        print(
            "Skipped because the linked internal product was not found:"
        )

        print(
            sync_record.get(
                "product_sku"
            )
        )

        return False

    woocommerce_product_id = int(
        sync_record[
            "woocommerce_product_id"
        ]
    )

    http_status, response_payload = get_woocommerce_product(
        store_url=store_url,
        api_version=api_version,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        timeout_seconds=timeout_seconds,
        woocommerce_product_id=woocommerce_product_id,
    )

    if http_status == 404:
        mark_product_missing(
            repository=repository,
            sync_record=sync_record,
            internal_product=internal_product,
            response_payload=response_payload,
        )

        print()
        print(
            "WooCommerce product was not found:"
        )

        print(
            f"Product ID: {woocommerce_product_id}"
        )

        return False

    if http_status != 200:
        mark_sync_error(
            repository=repository,
            sync_record=sync_record,
            error_code=(
                f"WOOCOMMERCE_HTTP_{http_status}"
            ),
            error_message=(
                "WooCommerce product status request failed."
            ),
            response_payload=response_payload,
        )

        print()
        print(
            "WooCommerce status request failed:"
        )

        print(
            f"Product ID: {woocommerce_product_id}"
        )

        print(
            f"HTTP status: {http_status}"
        )

        return False

    if not isinstance(
        response_payload,
        dict,
    ):
        mark_sync_error(
            repository=repository,
            sync_record=sync_record,
            error_code="INVALID_WOOCOMMERCE_RESPONSE",
            error_message=(
                "WooCommerce returned an unexpected response."
            ),
            response_payload=response_payload,
        )

        return False

    remote_sku = str(
        response_payload.get("sku")
        or ""
    ).strip()

    expected_sku = str(
        sync_record.get("product_sku")
        or internal_product.get("product_code")
        or ""
    ).strip()

    if (
        remote_sku
        and expected_sku
        and remote_sku != expected_sku
    ):
        mark_sync_error(
            repository=repository,
            sync_record=sync_record,
            error_code="SKU_MISMATCH",
            error_message=(
                "WooCommerce product SKU does not match the linked "
                "internal product."
            ),
            response_payload=response_payload,
        )

        print()
        print(
            "WooCommerce SKU mismatch detected."
        )
        print(
            f"Expected SKU: {expected_sku}"
        )
        print(
            f"Remote SKU: {remote_sku}"
        )

        return False

    remote_status = response_payload.get(
        "status"
    )

    (
        local_sync_status,
        internal_status,
    ) = map_remote_status(
        remote_status
    )

    update_sync_record(
        repository=repository,
        sync_record=sync_record,
        remote_product=response_payload,
        local_sync_status=local_sync_status,
    )

    update_internal_product(
        repository=repository,
        internal_product=internal_product,
        remote_product=response_payload,
        internal_status=internal_status,
    )

    print_product_result(
        sync_record=sync_record,
        remote_product=response_payload,
        local_sync_status=local_sync_status,
        internal_status=internal_status,
    )

    return True


def main() -> None:
    """Synchronize WooCommerce product statuses into Supabase."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )
    args = parse_arguments()

    if args.non_interactive:
        if not args.product_code:
            raise RuntimeError(
                "--non-interactive requires --product-code."
            )

        if not args.confirm_sync:
            raise RuntimeError(
                "--non-interactive requires --confirm-sync."
            )

    print("=" * 72)
    print("WOOCOMMERCE PRODUCT STATUS SYNC")
    print("=" * 72)

    print(
        f"Version: {SYNC_VERSION}"
    )

    store_url = get_required_environment_variable(
        "WOOCOMMERCE_URL"
    )

    consumer_key = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_KEY"
    )

    consumer_secret = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_SECRET"
    )

    api_version = os.getenv(
        "WOOCOMMERCE_API_VERSION",
        "wc/v3",
    ).strip()

    timeout_seconds = get_timeout_seconds()

    repository = SupabaseRepository()

    sync_records = get_sync_records(
        repository=repository,
        product_code=args.product_code,
    )

    print()
    print(
        "WooCommerce products found for status check: "
        f"{len(sync_records)}"
    )

    if not sync_records:
        print(
            "No synchronized WooCommerce products were found."
        )
        return

    if args.confirm_sync:
        confirmation = "SYNC_STATUS"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = input(
            "Type SYNC_STATUS to check and update the selected "
            "WooCommerce product statuses, or press Enter to cancel: "
        ).strip().upper()

    if confirmation != "SYNC_STATUS":
        print(
            "WooCommerce product status synchronization was cancelled."
        )
        return

    successful_count = 0
    failed_count = 0

    for sync_record in sync_records:
        try:
            was_successful = synchronize_one_product(
                repository=repository,
                sync_record=sync_record,
                store_url=store_url,
                api_version=api_version,
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                timeout_seconds=timeout_seconds,
            )

            if was_successful:
                successful_count += 1
            else:
                failed_count += 1

        except requests.Timeout:
            failed_count += 1

            mark_sync_error(
                repository=repository,
                sync_record=sync_record,
                error_code="TIMEOUT",
                error_message=(
                    "WooCommerce did not respond before the timeout."
                ),
                response_payload={},
            )

            print()
            print(
                "Timeout while checking SKU: "
                f"{sync_record.get('product_sku')}"
            )

        except requests.ConnectionError as error:
            failed_count += 1

            mark_sync_error(
                repository=repository,
                sync_record=sync_record,
                error_code="CONNECTION_ERROR",
                error_message=str(error),
                response_payload={},
            )

            print()
            print(
                "Connection error while checking SKU: "
                f"{sync_record.get('product_sku')}"
            )

        except Exception as error:
            failed_count += 1

            mark_sync_error(
                repository=repository,
                sync_record=sync_record,
                error_code=type(error).__name__,
                error_message=str(error),
                response_payload={
                    "exception_type": type(
                        error
                    ).__name__,
                    "exception_message": str(
                        error
                    ),
                },
            )

            print()
            print(
                "Unexpected error while checking SKU: "
                f"{sync_record.get('product_sku')}"
            )

            print(
                f"Error details: {error}"
            )

    print()
    print("=" * 72)
    print("STATUS SYNCHRONIZATION RESULT")
    print("=" * 72)

    print(
        f"Successful products: {successful_count}"
    )

    print(
        f"Failed products: {failed_count}"
    )

    print(
        f"Total products checked: {len(sync_records)}"
    )




if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "WooCommerce product status synchronization was cancelled."
        )

        sys.exit(130)

    except Exception as error:
        print()
        print(
            "WooCommerce product status synchronization failed."
        )

        print(
            f"Error type: {type(error).__name__}"
        )

        print(
            f"Error details: {error}"
        )

        sys.exit(1)