import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.content_status import ContentStatus
from src.domain.decisions import Outcome
from src.domain.rules import readiness_rules
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


SCRIPT_VERSION = "1.1.2"
VALID_CONFIRMATIONS = {"READY", "READY_FOR_DRAFT"}


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_confirmation(value: str | None) -> str:
    """Normalize confirmation values across case, spaces, and hyphens."""
    if not value:
        return ""

    return (
        value.strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Check and update WooCommerce draft readiness."
    )
    parser.add_argument(
        "--product-code",
        help="Check one exact internal product code.",
    )
    parser.add_argument(
        "--confirm-ready",
        action="store_true",
        help="Mark a ready product as READY_FOR_DRAFT without prompting.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable all input prompts.",
    )
    return parser.parse_args()


def get_product(
    repository: SupabaseRepository,
    product_code: str | None,
) -> dict[str, Any] | None:
    """Return one active product that has not created a WooCommerce draft."""
    query = (
        repository.client
        .table("internal_products")
        .select("*")
        .eq("is_active", True)
        .in_(
            "woocommerce_status",
            [WooCommerceStatus.NOT_CREATED, WooCommerceStatus.READY_FOR_DRAFT],
        )
    )

    if product_code:
        query = query.eq("product_code", product_code)

    response = query.order("created_at", desc=False).limit(1).execute()
    rows = response.data or []

    if product_code and not rows:
        raise RuntimeError(
            "No eligible internal product was found for product code: "
            f"{product_code}"
        )

    return rows[0] if rows else None


def get_candidate(
    repository: SupabaseRepository,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    """Return candidate identity status."""
    if not candidate_id:
        return None

    response = (
        repository.client
        .table("product_candidates")
        .select("candidate_id,identity_status")
        .eq("candidate_id", candidate_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def get_approved_content(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return approved Vietnamese product content."""
    response = (
        repository.client
        .table("product_contents")
        .select("product_content_id,content_status,review_required,approved_at")
        .eq("internal_product_id", internal_product_id)
        .eq("content_language", "vi")
        .eq("content_status", ContentStatus.APPROVED)
        .eq("review_required", False)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def get_selected_main_images(
    repository: SupabaseRepository,
    candidate_id: str | None,
) -> list[dict[str, Any]]:
    """Return selected main images linked to the product candidate."""
    if not candidate_id:
        return []

    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id,candidate_id,image_status,is_publish_eligible,"
            "is_selected_main_image,image_role,usage_rights_status"
        )
        .eq("candidate_id", candidate_id)
        .eq("is_selected_main_image", True)
        .execute()
    )
    return response.data or []


def get_woo_sync_state(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> tuple[bool, bool]:
    """
    Return (recovery_required, has_created_woo_sync) for one internal
    product, read from its woocommerce_product_syncs row if any.

    recovery_required mirrors sync_woocommerce_product_status.py's own
    recovery marker (response_payload.recovery_required). A sync row
    with a populated woocommerce_product_id means a WooCommerce product
    may already exist -- READY_FOR_DRAFT must not be re-granted for it.
    """
    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .select("woocommerce_product_id,woocommerce_status,response_payload")
        .eq("internal_product_id", internal_product_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []

    if not rows:
        return False, False

    sync = rows[0]
    response_payload = sync.get("response_payload")
    recovery_required = bool(
        isinstance(response_payload, dict)
        and response_payload.get("recovery_required") is True
    )
    has_created_woo_sync = bool(
        sync.get("woocommerce_product_id")
        or sync.get("woocommerce_status") == WooCommerceSyncStatus.DRAFT_CREATED
    )

    return recovery_required, has_created_woo_sync


def evaluate_readiness(
    product: dict[str, Any],
    candidate: dict[str, Any] | None,
    approved_content: dict[str, Any] | None,
    selected_images: list[dict[str, Any]],
    recovery_required: bool = False,
    has_created_woo_sync: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Return (blockers, warnings) via the shared readiness rule engine
    (src.domain.rules.readiness_rules) -- kept as this exact tuple shape
    so print_result()/main() below are unaffected by the extraction.
    """
    result = readiness_rules.evaluate_readiness(
        product=product,
        candidate=candidate,
        approved_content=approved_content,
        selected_images=selected_images,
        recovery_required=recovery_required,
        has_created_woo_sync=has_created_woo_sync,
    )

    if result.outcome == Outcome.AUTO_PASS:
        blockers: list[str] = []
    else:
        blockers = list(result.evidence.get("blockers") or [result.reason])

    return blockers, list(result.warnings)


def print_result(
    product: dict[str, Any],
    candidate: dict[str, Any] | None,
    approved_content: dict[str, Any] | None,
    selected_images: list[dict[str, Any]],
    blockers: list[str],
    warnings: list[str],
) -> None:
    """Print readiness details."""
    print()
    print("=" * 72)
    print("WOOCOMMERCE DRAFT READINESS")
    print("=" * 72)
    print(f"Product code: {product.get('product_code')}")
    print(f"Title: {product.get('title')}")
    print(
        "Identity status: "
        f"{candidate.get('identity_status') if candidate else '[missing]'}"
    )
    print(f"Content status: {product.get('content_status')}")
    print(f"Approved content: {'YES' if approved_content else 'NO'}")
    print(f"Image status: {product.get('image_status')}")
    print(
        "Selected main image: "
        f"{selected_images[0].get('image_id') if len(selected_images) == 1 else '[invalid count]'}"
    )
    print(f"Pricing status: {product.get('pricing_status')}")

    print()
    if blockers:
        print("Blocking issues:")
        for item in blockers:
            print(f"- {item}")
    else:
        print("Blocking issues: none")

    print()
    if warnings:
        print("Warnings:")
        for item in warnings:
            print(f"- {item}")
    else:
        print("Warnings: none")

    print()
    print(f"Draft readiness: {'READY' if not blockers else 'NOT READY'}")


def mark_ready(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> None:
    """Mark an internal product as ready for WooCommerce draft creation."""
    response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "woocommerce_status": WooCommerceStatus.READY_FOR_DRAFT,
                "updated_at": utc_now(),
            }
        )
        .eq("internal_product_id", internal_product_id)
        .execute()
    )

    if not response.data:
        raise RuntimeError("Draft readiness update returned no data.")


def main() -> None:
    """Check one product and optionally mark it ready for draft creation."""
    load_dotenv()
    args = parse_arguments()

    print("=" * 72)
    print("WOOCOMMERCE DRAFT READINESS CHECKER")
    print("=" * 72)
    print(f"Version: {SCRIPT_VERSION}")

    if args.non_interactive and not args.product_code:
        raise RuntimeError(
            "--non-interactive requires --product-code."
        )

    repository = SupabaseRepository()
    product = get_product(repository, args.product_code)

    if product is None:
        print("No product requires a draft readiness check.")
        return

    candidate = get_candidate(
        repository,
        product.get("candidate_id"),
    )
    approved_content = get_approved_content(
        repository,
        product["internal_product_id"],
    )
    selected_images = get_selected_main_images(
        repository,
        product.get("candidate_id"),
    )
    recovery_required, has_created_woo_sync = get_woo_sync_state(
        repository,
        product["internal_product_id"],
    )

    blockers, warnings = evaluate_readiness(
        product,
        candidate,
        approved_content,
        selected_images,
        recovery_required,
        has_created_woo_sync,
    )

    print_result(
        product,
        candidate,
        approved_content,
        selected_images,
        blockers,
        warnings,
    )

    if blockers:
        print()
        print("The product was not updated because blocking issues remain.")
        return

    if product.get("woocommerce_status") == WooCommerceStatus.READY_FOR_DRAFT:
        print()
        print("Product is already marked as READY_FOR_DRAFT.")
        return

    if args.confirm_ready:
        confirmation = "READY"
    elif args.non_interactive:
        print()
        print(
            "The product is ready, but --confirm-ready was not supplied. "
            "No database changes were made."
        )
        return
    else:
        confirmation = normalize_confirmation(
            input(
                "Type READY or READY_FOR_DRAFT to mark this product as "
                "READY_FOR_DRAFT, or press Enter to cancel: "
            )
        )

    if confirmation not in VALID_CONFIRMATIONS:
        print()
        print(
            "Invalid confirmation. Enter READY or READY_FOR_DRAFT."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
        print("Draft readiness update was cancelled.")
        return

    mark_ready(
        repository,
        product["internal_product_id"],
    )

    print()
    print("Product marked as READY_FOR_DRAFT.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("Draft readiness check was cancelled by the user.")
        sys.exit(130)
    except Exception as error:
        print()
        print("Draft readiness check failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
