import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


CREATOR_NAME = "internal_product_creator"
CREATOR_VERSION = "1.0.0"


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


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
) -> dict[str, Any] | None:
    """Return one verified candidate without an internal product."""
    candidate_response = (
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
            "source_evidence"
        )
        .eq(
            "identity_status",
            "IDENTITY_VERIFIED",
        )
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    candidates = (
        candidate_response.data
        or []
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

        existing_rows = (
            existing_response.data
            or []
        )

        if existing_rows:
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

    return references[0]


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
    """Determine whether images are available."""
    if not images:
        return "PENDING"

    return "AVAILABLE"


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
        "created_at": utc_now(),
    }


def create_internal_product(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    reference: dict[str, Any],
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create one internal product."""
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

    page_count = (
        candidate.get(
            "verified_page_count"
        )
        or reference.get(
            "reference_page_count"
        )
    )

    weight_grams = (
        candidate.get(
            "verified_weight_grams"
        )
        or reference.get(
            "reference_weight_grams"
        )
    )

    length_cm = (
        candidate.get(
            "verified_length_cm"
        )
        or reference.get(
            "reference_length_cm"
        )
    )

    width_cm = (
        candidate.get(
            "verified_width_cm"
        )
        or reference.get(
            "reference_width_cm"
        )
    )

    height_cm = (
        candidate.get(
            "verified_height_cm"
        )
        or reference.get(
            "reference_height_cm"
        )
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
        repository
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

    confirmation = input(
        "Type CREATE to create this internal product, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "CREATE":
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

    update_candidate_workflow(
        repository=repository,
        candidate_id=candidate[
            "candidate_id"
        ],
    )

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