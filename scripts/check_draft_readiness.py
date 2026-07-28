import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


CHECKER_NAME = "woocommerce_draft_readiness_checker"
CHECKER_VERSION = "1.0.0"


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


def get_product_to_check(
    repository: SupabaseRepository,
) -> dict[str, Any] | None:
    """Return one product that has not yet been marked ready."""
    response = (
        repository.client
        .table("internal_products")
        .select(
            "internal_product_id,"
            "candidate_id,"
            "primary_reference_id,"
            "product_code,"
            "product_type,"
            "title,"
            "author,"
            "isbn,"
            "publisher,"
            "page_count,"
            "weight_grams,"
            "metadata_status,"
            "pricing_status,"
            "image_status,"
            "content_status,"
            "woocommerce_status,"
            "review_required,"
            "review_reason,"
            "product_metadata"
        )
        .eq(
            "is_active",
            True,
        )
        .in_(
            "woocommerce_status",
            [
                "NOT_CREATED",
                "FAILED",
            ],
        )
        .order(
            "created_at",
            desc=False,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def get_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Return candidate identity data."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "candidate_code,"
            "identity_status,"
            "identity_confidence,"
            "workflow_status,"
            "review_required"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def get_approved_content(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return approved Vietnamese product content."""
    response = (
        repository.client
        .table("product_contents")
        .select(
            "product_content_id,"
            "product_name,"
            "short_description,"
            "long_description,"
            "product_details,"
            "content_status,"
            "review_required,"
            "approved_at"
        )
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .eq(
            "content_language",
            "vi",
        )
        .eq(
            "content_status",
            "APPROVED",
        )
        .eq(
            "review_required",
            False,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def get_selected_main_image(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Return the selected publishable main image."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id,"
            "candidate_id,"
            "source_type,"
            "storage_bucket,"
            "storage_path,"
            "mime_type,"
            "width_pixels,"
            "height_pixels,"
            "image_role,"
            "usage_rights_status,"
            "is_main_image_candidate,"
            "is_selected_main_image,"
            "is_publish_eligible,"
            "image_status"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "is_selected_main_image",
            True,
        )
        .eq(
            "is_publish_eligible",
            True,
        )
        .eq(
            "image_status",
            "VALIDATED",
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def evaluate_readiness(
    product: dict[str, Any],
    candidate: dict[str, Any] | None,
    content: dict[str, Any] | None,
    image: dict[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate whether a product can become a WooCommerce draft."""
    blocking_issues: list[str] = []
    warnings: list[str] = []

    if not candidate:
        blocking_issues.append(
            "Linked product candidate was not found."
        )

    elif candidate.get(
        "identity_status"
    ) != "IDENTITY_VERIFIED":
        blocking_issues.append(
            "Candidate identity is not verified."
        )

    if not product.get(
        "title"
    ):
        blocking_issues.append(
            "Internal product title is missing."
        )

    if product.get(
        "content_status"
    ) != "APPROVED":
        blocking_issues.append(
            "Internal product content is not approved."
        )

    if not content:
        blocking_issues.append(
            "Approved Vietnamese product content was not found."
        )

    else:
        if not content.get(
            "product_name"
        ):
            blocking_issues.append(
                "Approved product name is missing."
            )

        if not content.get(
            "short_description"
        ):
            blocking_issues.append(
                "Approved short description is missing."
            )

        if not content.get(
            "long_description"
        ):
            blocking_issues.append(
                "Approved long description is missing."
            )

    if product.get(
        "image_status"
    ) != "APPROVED":
        blocking_issues.append(
            "Internal product image status is not approved."
        )

    if not image:
        blocking_issues.append(
            "A selected publishable main image was not found."
        )

    if product.get(
        "review_required"
    ):
        blocking_issues.append(
            "Internal product still requires manual review."
        )

    # These fields do not block draft creation.
    if not product.get(
        "isbn"
    ):
        warnings.append(
            "ISBN is missing."
        )

    if product.get(
        "weight_grams"
    ) is None:
        warnings.append(
            "Product weight is missing."
        )

    if product.get(
        "pricing_status"
    ) != "APPROVED":
        warnings.append(
            "Pricing is not approved. "
            "The shop owner must add or review the price "
            "before publishing."
        )

    is_ready = not blocking_issues

    return {
        "is_ready": is_ready,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
    }


def print_result(
    product: dict[str, Any],
    candidate: dict[str, Any] | None,
    content: dict[str, Any] | None,
    image: dict[str, Any] | None,
    result: dict[str, Any],
) -> None:
    """Print readiness details."""
    print()
    print("=" * 72)
    print("WOOCOMMERCE DRAFT READINESS")
    print("=" * 72)

    print(
        "Product code: "
        f"{product.get('product_code')}"
    )

    print(
        "Title: "
        f"{product.get('title')}"
    )

    print(
        "Identity status: "
        f"{candidate.get('identity_status') if candidate else '[not found]'}"
    )

    print(
        "Content status: "
        f"{product.get('content_status')}"
    )

    print(
        "Approved content: "
        f"{'YES' if content else 'NO'}"
    )

    print(
        "Image status: "
        f"{product.get('image_status')}"
    )

    print(
        "Selected main image: "
        f"{image.get('image_id') if image else '[not found]'}"
    )

    print(
        "Pricing status: "
        f"{product.get('pricing_status')}"
    )

    print()

    if result["blocking_issues"]:
        print("Blocking issues:")

        for issue in result[
            "blocking_issues"
        ]:
            print(
                f"- {issue}"
            )

    else:
        print(
            "Blocking issues: none"
        )

    if result["warnings"]:
        print()
        print("Warnings:")

        for warning in result[
            "warnings"
        ]:
            print(
                f"- {warning}"
            )

    print()
    print(
        "Draft readiness: "
        f"{'READY' if result['is_ready'] else 'NOT READY'}"
    )


def build_readiness_metadata(
    product: dict[str, Any],
    content: dict[str, Any] | None,
    image: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Add readiness details without removing existing metadata."""
    existing_metadata = product.get(
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
        "draft_readiness"
    ] = {
        "checker_name": CHECKER_NAME,
        "checker_version": CHECKER_VERSION,
        "checked_at": utc_now(),
        "is_ready": result[
            "is_ready"
        ],
        "blocking_issues": result[
            "blocking_issues"
        ],
        "warnings": result[
            "warnings"
        ],
        "product_content_id": (
            content.get(
                "product_content_id"
            )
            if content
            else None
        ),
        "main_image_id": (
            image.get(
                "image_id"
            )
            if image
            else None
        ),
        "price_required_before_publish": True,
    }

    return product_metadata


def save_ready_status(
    repository: SupabaseRepository,
    product: dict[str, Any],
    content: dict[str, Any],
    image: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Mark the internal product as ready for WooCommerce draft."""
    if not result["is_ready"]:
        raise RuntimeError(
            "Product is not ready for WooCommerce draft creation."
        )

    product_metadata = build_readiness_metadata(
        product=product,
        content=content,
        image=image,
        result=result,
    )

    response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "woocommerce_status": "READY_FOR_DRAFT",
                "review_required": False,
                "review_reason": None,
                "product_metadata": product_metadata,
                "updated_at": utc_now(),
            }
        )
        .eq(
            "internal_product_id",
            product[
                "internal_product_id"
            ],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Internal product readiness update returned no data."
        )


def main() -> None:
    """Check one internal product for WooCommerce draft readiness."""
    load_dotenv()

    print("=" * 72)
    print("WOOCOMMERCE DRAFT READINESS CHECKER")
    print("=" * 72)
    print(
        f"Version: {CHECKER_VERSION}"
    )

    repository = SupabaseRepository()

    print(
        "Searching for a product to check..."
    )

    product = get_product_to_check(
        repository
    )

    if product is None:
        print(
            "No internal product requiring a readiness check was found."
        )
        return

    candidate = get_candidate(
        repository=repository,
        candidate_id=product[
            "candidate_id"
        ],
    )

    content = get_approved_content(
        repository=repository,
        internal_product_id=product[
            "internal_product_id"
        ],
    )

    image = get_selected_main_image(
        repository=repository,
        candidate_id=product[
            "candidate_id"
        ],
    )

    result = evaluate_readiness(
        product=product,
        candidate=candidate,
        content=content,
        image=image,
    )

    print_result(
        product=product,
        candidate=candidate,
        content=content,
        image=image,
        result=result,
    )

    if not result[
        "is_ready"
    ]:
        print()
        print(
            "The product was not updated because blocking "
            "issues remain."
        )
        return

    print()

    confirmation = input(
        "Type READY to mark this product as READY_FOR_DRAFT, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "READY":
        print(
            "Draft readiness update was cancelled."
        )
        return

    if content is None or image is None:
        raise RuntimeError(
            "Approved content and main image are required."
        )

    save_ready_status(
        repository=repository,
        product=product,
        content=content,
        image=image,
        result=result,
    )

    print()
    print(
        "WooCommerce status changed to READY_FOR_DRAFT."
    )

    print(
        "Pricing remains optional for draft creation "
        "and must be reviewed before publishing."
    )

    print()
    print(
        "Draft readiness check completed successfully."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Draft readiness check was cancelled by the user."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Draft readiness check failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)