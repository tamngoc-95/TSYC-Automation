import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


CREATOR_NAME = "internal_product_creator"
CREATOR_VERSION = "1.2.0"

VALID_CONFIRMATIONS = {
    "CREATE",
    "CREATE_PRODUCT",
    "CREATE_INTERNAL_PRODUCT",
}


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_confirmation(
    value: str | None,
) -> str:
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
        description=(
            "Create one internal product from an identity-verified candidate."
        )
    )

    parser.add_argument(
        "--candidate-code",
        help="Process one exact candidate code.",
    )

    parser.add_argument(
        "--candidate-id",
        help="Process one exact candidate UUID.",
    )

    parser.add_argument(
        "--confirm-create",
        action="store_true",
        help="Confirm internal product creation without prompting.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable input prompts. Requires a candidate selector and "
            "--confirm-create."
        ),
    )

    return parser.parse_args()


def clean_text(
    value: str | None,
) -> str | None:
    """Normalize whitespace in text."""
    if not value:
        return None

    normalized = " ".join(
        value.split()
    ).strip()

    return normalized or None


def get_verified_candidate(
    repository: SupabaseRepository,
    candidate_code: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Return one identity-verified candidate without an internal product.

    When a candidate code or ID is supplied, only that candidate is evaluated.
    """
    query = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "candidate_code,"
            "candidate_type,"
            "extracted_title,"
            "extracted_author,"
            "possible_isbn,"
            "verified_title,"
            "verified_isbn,"
            "verified_author,"
            "verified_publisher,"
            "verified_page_count,"
            "verified_weight_grams,"
            "verified_length_cm,"
            "verified_width_cm,"
            "verified_height_cm,"
            "identity_status,"
            "workflow_status,"
            "identity_confidence,"
            "source_evidence,"
            "created_at"
        )
        .eq(
            "identity_status",
            "IDENTITY_VERIFIED",
        )
    )

    if candidate_code:
        query = query.eq(
            "candidate_code",
            candidate_code,
        )

    if candidate_id:
        query = query.eq(
            "candidate_id",
            candidate_id,
        )

    candidate_response = (
        query
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    candidates = candidate_response.data or []

    if (candidate_code or candidate_id) and not candidates:
        raise RuntimeError(
            "No IDENTITY_VERIFIED candidate matched the supplied selector."
        )

    for candidate in candidates:
        existing_response = (
            repository.client
            .table("internal_products")
            .select(
                "internal_product_id,"
                "candidate_id,"
                "product_code"
            )
            .eq(
                "candidate_id",
                candidate["candidate_id"],
            )
            .limit(1)
            .execute()
        )

        existing_rows = existing_response.data or []

        if existing_rows:
            existing = existing_rows[0]

            if candidate_code or candidate_id:
                raise RuntimeError(
                    "An internal product already exists for candidate "
                    f"{candidate.get('candidate_code')}: "
                    f"{existing.get('product_code')}"
                )

            print(
                "Skipping candidate because an internal product "
                "already exists: "
                f"{candidate.get('candidate_code')}"
            )
            continue

        return candidate

    return None

def get_best_matched_reference(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Return the highest-priority matched reference."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id,"
            "candidate_id,"
            "source_url_id,"
            "source_type,"
            "source_name,"
            "source_url,"
            "reference_title,"
            "reference_isbn,"
            "reference_author,"
            "reference_publisher,"
            "reference_page_count,"
            "reference_weight_grams,"
            "reference_length_cm,"
            "reference_width_cm,"
            "reference_height_cm,"
            "reference_cover_price_vnd,"
            "reference_description,"
            "reference_image_url,"
            "match_decision,"
            "match_confidence,"
            "source_priority,"
            "raw_metadata"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "match_decision",
            "MATCH",
        )
        .not_.is_(
            "source_url_id",
            "null",
        )
        .order(
            "source_priority",
            desc=False,
        )
        .order(
            "match_confidence",
            desc=True,
        )
        .limit(1)
        .execute()
    )

    references = (
        response.data
        or []
    )

    if not references:
        return None

    reference = references[0]

    if str(reference.get("candidate_id")) != str(candidate_id):
        raise RuntimeError(
            "Matched reference candidate_id does not match the requested candidate."
        )

    return reference


def get_candidate_images(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Return images linked to a candidate."""
    response = (
        repository.client
        .table("product_images")
        .select("*")
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    return response.data or []


def map_product_type(
    candidate_type: str | None,
) -> str:
    """Map candidate type to internal product type."""
    mapping = {
        "SINGLE_BOOK": "BOOK",
        "BOOK_COMBO": "BOOK_COMBO",
        "BOOK_SET": "BOOK_SET",
        "ACTIVITY_PRODUCT": "ACTIVITY_PRODUCT",
        "OTHER": "OTHER",
    }

    return mapping.get(
        candidate_type or "",
        "OTHER",
    )


def build_product_code(
    candidate_code: str,
) -> str:
    """Build a stable internal product code."""
    return f"TSYC-{candidate_code}"


def determine_metadata_status(
    title: str | None,
    author: str | None,
    publisher: str | None,
    isbn: str | None,
    weight_grams: Any,
) -> tuple[str, bool, str | None]:
    """Determine metadata readiness."""
    missing_fields: list[str] = []

    if not title:
        missing_fields.append(
            "title"
        )

    if not author:
        missing_fields.append(
            "author"
        )

    if not publisher:
        missing_fields.append(
            "publisher"
        )

    if not isbn:
        missing_fields.append(
            "isbn"
        )

    if weight_grams is None:
        missing_fields.append(
            "weight_grams"
        )

    blocking_fields = {
        "title",
    }

    if any(
        field in blocking_fields
        for field in missing_fields
    ):
        return (
            "REVIEW_REQUIRED",
            True,
            "Missing required fields: "
            + ", ".join(
                missing_fields
            ),
        )

    if missing_fields:
        return (
            "INCOMPLETE",
            False,
            "Missing metadata: "
            + ", ".join(
                missing_fields
            ),
        )

    return (
        "READY",
        False,
        None,
    )


def determine_image_status(
    images: list[dict[str, Any]],
) -> str:
    """
    Determine internal product image workflow status.

    Images being present is not enough for approval. Exactly one selected
    main image must already be validated and publish eligible. Otherwise the
    internal product remains PENDING until review_product_images.py approves it.
    """
    approved_main_images = [
        image
        for image in images
        if image.get("image_status") == "VALIDATED"
        and image.get("is_selected_main_image") is True
        and image.get("is_publish_eligible") is True
    ]

    if len(approved_main_images) > 1:
        raise RuntimeError(
            "More than one validated publishable main image is linked "
            "to this candidate."
        )

    if len(approved_main_images) == 1:
        return "APPROVED"

    return "PENDING"


def build_product_metadata(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build traceable internal product metadata."""
    return {
        "creator_name": CREATOR_NAME,
        "creator_version": CREATOR_VERSION,
        "created_from_candidate": (
            candidate.get(
                "candidate_code"
            )
        ),
        "identity_confidence": (
            candidate.get(
                "identity_confidence"
            )
        ),
        "primary_reference": {
            "reference_id": reference.get(
                "reference_id"
            ),
            "source_url_id": reference.get(
                "source_url_id"
            ),
            "source_type": reference.get(
                "source_type"
            ),
            "source_name": reference.get(
                "source_name"
            ),
            "source_url": reference.get(
                "source_url"
            ),
            "match_confidence": reference.get(
                "match_confidence"
            ),
        },
        "reference_description": (
            reference.get(
                "reference_description"
            )
        ),
        "reference_image_url": (
            reference.get(
                "reference_image_url"
            )
        ),
        "candidate_image_count": len(
            images
        ),
        "metadata_warnings": [
            field_name
            for field_name, field_value in (
                (
                    "isbn",
                    candidate.get("verified_isbn")
                    or reference.get("reference_isbn")
                    or candidate.get("possible_isbn"),
                ),
                (
                    "weight_grams",
                    candidate.get("verified_weight_grams")
                    if candidate.get("verified_weight_grams") is not None
                    else reference.get("reference_weight_grams"),
                ),
            )
            if field_value in (None, "")
        ],
        "created_at": utc_now(),
    }


def create_internal_product(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    reference: dict[str, Any],
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create one internal product."""
    if candidate.get("identity_status") != "IDENTITY_VERIFIED":
        raise RuntimeError(
            "Candidate identity must be IDENTITY_VERIFIED."
        )

    if reference.get("match_decision") != "MATCH":
        raise RuntimeError(
            "Primary reference must have match_decision = MATCH."
        )

    if str(reference.get("candidate_id")) != str(
        candidate.get("candidate_id")
    ):
        raise RuntimeError(
            "Primary reference is not linked to the selected candidate."
        )

    if not reference.get("reference_id"):
        raise RuntimeError(
            "Primary reference has no reference_id."
        )

    title = clean_text(
        candidate.get(
            "verified_title"
        )
        or reference.get(
            "reference_title"
        )
        or candidate.get(
            "extracted_title"
        )
    )

    author = clean_text(
        candidate.get(
            "verified_author"
        )
        or reference.get(
            "reference_author"
        )
        or candidate.get(
            "extracted_author"
        )
    )

    isbn = clean_text(
        candidate.get(
            "verified_isbn"
        )
        or reference.get(
            "reference_isbn"
        )
        or candidate.get(
            "possible_isbn"
        )
    )

    publisher = clean_text(
        candidate.get(
            "verified_publisher"
        )
        or reference.get(
            "reference_publisher"
        )
    )

    page_count = candidate.get(
        "verified_page_count"
    )

    if page_count is None:
        page_count = reference.get(
            "reference_page_count"
        )

    weight_grams = candidate.get(
        "verified_weight_grams"
    )

    if weight_grams is None:
        weight_grams = reference.get(
            "reference_weight_grams"
        )

    length_cm = candidate.get(
        "verified_length_cm"
    )

    if length_cm is None:
        length_cm = reference.get(
            "reference_length_cm"
        )

    width_cm = candidate.get(
        "verified_width_cm"
    )

    if width_cm is None:
        width_cm = reference.get(
            "reference_width_cm"
        )

    height_cm = candidate.get(
        "verified_height_cm"
    )

    if height_cm is None:
        height_cm = reference.get(
            "reference_height_cm"
        )

    cover_price_vnd = reference.get(
        "reference_cover_price_vnd"
    )

    (
        metadata_status,
        review_required,
        review_reason,
    ) = determine_metadata_status(
        title=title,
        author=author,
        publisher=publisher,
        isbn=isbn,
        weight_grams=weight_grams,
    )

    image_status = determine_image_status(
        images
    )

    product_code = build_product_code(
        candidate[
            "candidate_code"
        ]
    )

    payload = {
        "candidate_id": candidate[
            "candidate_id"
        ],
        "primary_reference_id": reference[
            "reference_id"
        ],
        "product_code": product_code,
        "product_type": map_product_type(
            candidate.get(
                "candidate_type"
            )
        ),
        "title": title,
        "author": author,
        "isbn": isbn,
        "publisher": publisher,
        "language_code": None,
        "page_count": page_count,
        "weight_grams": weight_grams,
        "length_cm": length_cm,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "cover_price_vnd": cover_price_vnd,
        "purchase_price_vnd": None,
        "purchase_currency": "VND",
        "suggested_price_eur": None,
        "metadata_status": metadata_status,
        "pricing_status": "PENDING",
        "image_status": image_status,
        "content_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
        "review_required": review_required,
        "review_reason": review_reason,
        "is_active": True,
        "product_metadata": build_product_metadata(
            candidate=candidate,
            reference=reference,
            images=images,
        ),
        "updated_at": utc_now(),
    }

    response = (
        repository.client
        .table("internal_products")
        .insert(payload)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "Internal product insert returned no data."
        )

    return rows[0]


def rollback_internal_product(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> None:
    """Delete a newly created internal product after downstream failure."""
    response = (
        repository.client
        .table("internal_products")
        .delete()
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .execute()
    )

    if response.data is None:
        raise RuntimeError(
            "Internal product rollback returned no response data."
        )


def update_candidate_workflow(
    repository: SupabaseRepository,
    candidate_id: str,
) -> None:
    """Move the candidate to content preparation."""
    response = (
        repository.client
        .table("product_candidates")
        .update(
            {
                "workflow_status": "CONTENT_PENDING",
                "updated_at": utc_now(),
            }
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Candidate workflow update returned no data."
        )


def print_preview(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    images: list[dict[str, Any]],
) -> None:
    """Print the product creation source data."""
    print()
    print("=" * 72)
    print("INTERNAL PRODUCT PREVIEW")
    print("=" * 72)

    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )

    print(
        "Candidate identity status: "
        f"{candidate.get('identity_status')}"
    )

    print(
        "Reference source: "
        f"{reference.get('source_name')}"
    )

    print(
        "Reference match: "
        f"{reference.get('match_decision')}"
    )

    print()
    print(
        "Title: "
        f"{candidate.get('verified_title') or reference.get('reference_title')}"
    )

    print(
        "Author: "
        f"{candidate.get('verified_author') or reference.get('reference_author')}"
    )

    print(
        "ISBN: "
        f"{candidate.get('verified_isbn') or reference.get('reference_isbn') or '[not found]'}"
    )

    print(
        "Publisher: "
        f"{candidate.get('verified_publisher') or reference.get('reference_publisher') or '[not found]'}"
    )

    print(
        "Page count: "
        f"{candidate.get('verified_page_count') or reference.get('reference_page_count') or '[not found]'}"
    )

    print(
        "Weight grams: "
        f"{candidate.get('verified_weight_grams') or reference.get('reference_weight_grams') or '[not found]'}"
    )

    print(
        "Cover price VND: "
        f"{reference.get('reference_cover_price_vnd') or '[not found]'}"
    )

    print(
        "Candidate images: "
        f"{len(images)}"
    )


def main() -> None:
    """Create one internal product from one verified candidate."""
    load_dotenv()

    args = parse_arguments()

    if args.candidate_code and args.candidate_id:
        raise RuntimeError(
            "Use either --candidate-code or --candidate-id, not both."
        )

    if args.non_interactive:
        if not (args.candidate_code or args.candidate_id):
            raise RuntimeError(
                "--non-interactive requires --candidate-code or --candidate-id."
            )

        if not args.confirm_create:
            raise RuntimeError(
                "--non-interactive requires --confirm-create."
            )

    print("=" * 72)
    print("INTERNAL PRODUCT CREATOR")
    print("=" * 72)
    print(
        f"Version: {CREATOR_VERSION}"
    )

    repository = SupabaseRepository()

    print(
        "Searching for a verified candidate..."
    )

    candidate = get_verified_candidate(
        repository=repository,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
    )

    if candidate is None:
        print(
            "No verified candidate without an internal product was found."
        )
        return

    reference = get_best_matched_reference(
        repository=repository,
        candidate_id=candidate[
            "candidate_id"
        ],
    )

    if reference is None:
        raise RuntimeError(
            "No matched product reference was found "
            "for the verified candidate."
        )

    images = get_candidate_images(
        repository=repository,
        candidate_id=candidate[
            "candidate_id"
        ],
    )

    print_preview(
        candidate=candidate,
        reference=reference,
        images=images,
    )

    print()

    if args.confirm_create:
        confirmation = "CREATE"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = normalize_confirmation(
            input(
                "Type CREATE, CREATE_PRODUCT, or CREATE_INTERNAL_PRODUCT "
                "to create this internal product, or press Enter to cancel: "
            )
        )

    if confirmation not in VALID_CONFIRMATIONS:
        print()
        print(
            "Invalid confirmation. Use CREATE, CREATE_PRODUCT, "
            "or CREATE_INTERNAL_PRODUCT."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
        print(
            "Internal product creation was cancelled."
        )
        return

    internal_product = create_internal_product(
        repository=repository,
        candidate=candidate,
        reference=reference,
        images=images,
    )

    print(
        "Internal product created."
    )

    try:
        update_candidate_workflow(
            repository=repository,
            candidate_id=candidate[
                "candidate_id"
            ],
        )

    except Exception:
        internal_product_id = internal_product.get(
            "internal_product_id"
        )

        if internal_product_id:
            print(
                "Candidate workflow update failed. "
                "Rolling back the newly created internal product..."
            )

            rollback_internal_product(
                repository=repository,
                internal_product_id=str(
                    internal_product_id
                ),
            )

            print(
                "Internal product rollback completed."
            )

        raise

    print(
        "Candidate workflow changed to CONTENT_PENDING."
    )

    print()
    print("=" * 72)
    print("INTERNAL PRODUCT RESULT")
    print("=" * 72)

    print(
        "Internal product ID: "
        f"{internal_product.get('internal_product_id')}"
    )

    print(
        "Product code: "
        f"{internal_product.get('product_code')}"
    )

    print(
        "Title: "
        f"{internal_product.get('title')}"
    )

    print(
        "Metadata status: "
        f"{internal_product.get('metadata_status')}"
    )

    print(
        "Pricing status: "
        f"{internal_product.get('pricing_status')}"
    )

    print(
        "Image status: "
        f"{internal_product.get('image_status')}"
    )

    print(
        "WooCommerce status: "
        f"{internal_product.get('woocommerce_status')}"
    )

    print()
    print(
        "Internal product creation completed successfully."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Internal product creation was cancelled by the user."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Internal product creation failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)