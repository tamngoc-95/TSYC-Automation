import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


GENERATOR_NAME = "internal_product_content_generator"
GENERATOR_VERSION = "1.1.0"


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    """Normalize whitespace while preserving paragraph breaks."""
    if not value:
        return None

    paragraphs = []

    for paragraph in re.split(r"\n\s*\n", value.strip()):
        normalized = " ".join(paragraph.split()).strip()

        if normalized:
            paragraphs.append(normalized)

    return "\n\n".join(paragraphs) or None


def normalize_inline_text(value: str | None) -> str | None:
    """Normalize a value to one line."""
    if not value:
        return None

    normalized = " ".join(value.split()).strip()
    return normalized or None


def remove_post_markers(value: str | None) -> str | None:
    """Remove known Facebook sales markers from cleaned post content."""
    cleaned = clean_text(value)

    if not cleaned:
        return None

    lines = cleaned.splitlines()

    removable_markers = {
        "Sách có sẵn",
        "Sách có sẵn ở Đức",
    }

    while lines and lines[0].strip() in removable_markers:
        lines.pop(0)

    return "\n".join(lines).strip() or None


def get_existing_content(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return existing Vietnamese content for an internal product."""
    response = (
        repository.client
        .table("product_contents")
        .select(
            "product_content_id,"
            "internal_product_id,"
            "content_language,"
            "product_name,"
            "short_description,"
            "long_description,"
            "author_summary,"
            "product_details,"
            "seo_title,"
            "seo_description,"
            "content_status,"
            "review_required,"
            "review_notes,"
            "approved_at,"
            "created_at,"
            "updated_at"
        )
        .eq("internal_product_id", internal_product_id)
        .eq("content_language", "vi")
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return rows[0] if rows else None


def get_product_for_content(
    repository: SupabaseRepository,
) -> dict[str, Any] | None:
    """
    Return one product that needs Vietnamese content creation or review.

    Existing DRAFTED or REVIEW_REQUIRED content is selected before a
    product without content, allowing incorrect drafts to be regenerated.
    """
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
            "language_code,"
            "page_count,"
            "weight_grams,"
            "length_cm,"
            "width_cm,"
            "height_cm,"
            "cover_price_vnd,"
            "metadata_status,"
            "image_status,"
            "content_status,"
            "woocommerce_status,"
            "product_metadata,"
            "created_at"
        )
        .eq("is_active", True)
        .order("created_at", desc=False)
        .execute()
    )

    products = response.data or []
    fallback_without_content: dict[str, Any] | None = None

    for product in products:
        existing_content = get_existing_content(
            repository=repository,
            internal_product_id=product["internal_product_id"],
        )

        product["existing_content"] = existing_content

        if existing_content:
            status = existing_content.get("content_status")

            if status in {"DRAFTED", "REVIEW_REQUIRED"}:
                return product

            print(
                "Skipping product because approved/rejected Vietnamese "
                "content already exists: "
                f"{product.get('product_code')} ({status})"
            )
            continue

        if fallback_without_content is None:
            fallback_without_content = product

    return fallback_without_content


def get_primary_reference(
    repository: SupabaseRepository,
    reference_id: str | None,
) -> dict[str, Any] | None:
    """Return the primary product reference."""
    if not reference_id:
        return None

    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id,"
            "source_type,"
            "source_name,"
            "source_url,"
            "reference_title,"
            "reference_author,"
            "reference_publisher,"
            "reference_page_count,"
            "reference_description,"
            "raw_metadata"
        )
        .eq("reference_id", reference_id)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return rows[0] if rows else None


def get_facebook_source_text(
    repository: SupabaseRepository,
    candidate_id: str | None,
) -> str | None:
    """Return cleaned Facebook post text linked to the candidate."""
    if not candidate_id:
        return None

    candidate_response = (
        repository.client
        .table("product_candidates")
        .select("candidate_id,raw_page_id")
        .eq("candidate_id", candidate_id)
        .limit(1)
        .execute()
    )

    candidates = candidate_response.data or []

    if not candidates:
        return None

    raw_page_id = candidates[0].get("raw_page_id")

    if not raw_page_id:
        return None

    raw_page_response = (
        repository.client
        .table("raw_pages")
        .select("raw_page_id,cleaned_text,raw_text,cleaning_status")
        .eq("raw_page_id", raw_page_id)
        .limit(1)
        .execute()
    )

    rows = raw_page_response.data or []

    if not rows:
        return None

    source_text = (
        rows[0].get("cleaned_text")
        or rows[0].get("raw_text")
    )

    return remove_post_markers(source_text)


def get_selected_main_image(
    repository: SupabaseRepository,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    """Return the selected, publish-eligible main image."""
    if not candidate_id:
        return None

    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id,"
            "candidate_id,"
            "image_role,"
            "image_status,"
            "usage_rights_status,"
            "is_selected_main_image,"
            "is_publish_eligible"
        )
        .eq("candidate_id", candidate_id)
        .eq("is_selected_main_image", True)
        .eq("is_publish_eligible", True)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return rows[0] if rows else None


def build_product_details(product: dict[str, Any]) -> str:
    """Build a readable product specification section."""
    details: list[str] = []

    if product.get("author"):
        details.append(f"Tác giả: {product['author']}")

    if product.get("publisher"):
        details.append(f"Nhà xuất bản: {product['publisher']}")

    if product.get("page_count"):
        details.append(f"Số trang: {product['page_count']}")

    dimensions: list[str] = []

    for field_name in ("length_cm", "width_cm", "height_cm"):
        if product.get(field_name) is not None:
            dimensions.append(str(product[field_name]))

    if dimensions:
        details.append(
            "Kích thước: "
            + " × ".join(dimensions)
            + " cm"
        )

    if product.get("isbn"):
        details.append(f"ISBN: {product['isbn']}")

    if product.get("weight_grams"):
        details.append(
            f"Trọng lượng: {product['weight_grams']} g"
        )

    if product.get("language_code"):
        language_label = {
            "vi": "Tiếng Việt",
            "de": "Tiếng Đức",
            "en": "Tiếng Anh",
        }.get(
            str(product["language_code"]).lower(),
            str(product["language_code"]),
        )

        details.append(f"Ngôn ngữ: {language_label}")

    return "\n".join(details)


def split_sentences(value: str | None) -> list[str]:
    """Split source content into readable Vietnamese sentences."""
    if not value:
        return []

    normalized = " ".join(value.split())

    sentences = re.split(
        r"(?<=[.!?])\s+",
        normalized,
    )

    return [
        sentence.strip()
        for sentence in sentences
        if sentence.strip()
    ]


def build_short_description(
    product: dict[str, Any],
    facebook_text: str | None,
) -> str:
    """Build a concise summary from the store-owned Facebook content."""
    sentences = split_sentences(facebook_text)

    if sentences:
        first_sentence = sentences[0]

        if len(first_sentence) <= 420:
            return first_sentence

        return first_sentence[:417].rstrip() + "..."

    title = product["title"]
    author = product.get("author")

    if author:
        return (
            f"“{title}” của {author} là cuốn sách dành cho độc giả "
            "muốn tìm hiểu nội dung và thông tin sản phẩm trước khi chọn mua."
        )

    return (
        f"“{title}” là cuốn sách dành cho độc giả muốn tìm hiểu "
        "nội dung và thông tin sản phẩm trước khi chọn mua."
    )


def build_long_description(
    product: dict[str, Any],
    facebook_text: str | None,
    reference: dict[str, Any] | None,
) -> str:
    """
    Build an original description from the store's Facebook post.

    Reference descriptions are not copied. They are only fallback context
    when the store-owned Facebook post has no usable text.
    """
    source_text = clean_text(facebook_text)

    if source_text:
        return source_text

    title = product["title"]
    author = product.get("author")

    paragraphs: list[str] = []

    if author:
        paragraphs.append(
            f"“{title}” là tác phẩm của {author}."
        )
    else:
        paragraphs.append(
            f"“{title}” là một sản phẩm sách đang có tại Tiệm Sách Yêu Con."
        )

    paragraphs.append(
        "Thông tin sản phẩm đã được đối chiếu từ các nguồn tham khảo "
        "đáng tin cậy. Nội dung giới thiệu chi tiết cần được người quản lý "
        "kiểm tra trước khi sản phẩm được xuất bản."
    )

    return "\n\n".join(paragraphs)


def build_author_summary(
    product: dict[str, Any],
) -> str | None:
    """Build a minimal author line without unsupported biography claims."""
    author = product.get("author")

    if not author:
        return None

    return (
        f"{author} là tác giả của cuốn sách "
        f"“{product['title']}”."
    )


def build_seo_title(product: dict[str, Any]) -> str:
    """Build a simple SEO title."""
    author = product.get("author")

    if author:
        return f"{product['title']} – {author}"

    return product["title"]


def build_seo_description(
    product: dict[str, Any],
    short_description: str,
) -> str:
    """Build a concise SEO description."""
    plain_text = re.sub(
        r"[“”\"]",
        "",
        short_description,
    )

    if len(plain_text) <= 160:
        return plain_text

    return plain_text[:157].rstrip() + "..."


def build_content_payload(
    product: dict[str, Any],
    reference: dict[str, Any] | None,
    facebook_text: str | None,
    approval_mode: str,
) -> dict[str, Any]:
    """Build normalized Vietnamese product content."""
    product_name = normalize_inline_text(
        product.get("title")
    )

    if not product_name:
        raise RuntimeError(
            "Internal product title is missing."
        )

    short_description = build_short_description(
        product=product,
        facebook_text=facebook_text,
    )

    long_description = build_long_description(
        product=product,
        facebook_text=facebook_text,
        reference=reference,
    )

    is_approved = approval_mode == "APPROVE"

    return {
        "internal_product_id": product["internal_product_id"],
        "content_language": "vi",
        "product_name": product_name,
        "short_description": short_description,
        "long_description": long_description,
        "author_summary": build_author_summary(product),
        "product_details": build_product_details(product),
        "seo_title": build_seo_title(product),
        "seo_description": build_seo_description(
            product=product,
            short_description=short_description,
        ),
        "content_status": (
            "APPROVED"
            if is_approved
            else "DRAFTED"
        ),
        "generation_method": "RULE_BASED",
        "generator_name": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "review_required": not is_approved,
        "review_notes": (
            "Content reviewed and approved for WooCommerce draft."
            if is_approved
            else (
                "Review wording, product facts, and suitability "
                "before WooCommerce draft creation."
            )
        ),
        "approved_at": (
            utc_now()
            if is_approved
            else None
        ),
        "updated_at": utc_now(),
    }


def save_product_content(
    repository: SupabaseRepository,
    product: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Insert new content or update the existing Vietnamese content."""
    existing_content = product.get("existing_content")

    if existing_content:
        response = (
            repository.client
            .table("product_contents")
            .update(payload)
            .eq(
                "product_content_id",
                existing_content["product_content_id"],
            )
            .execute()
        )

        action = "UPDATED"

    else:
        response = (
            repository.client
            .table("product_contents")
            .insert(payload)
            .execute()
        )

        action = "CREATED"

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "Product content save returned no data."
        )

    return rows[0], action


def update_internal_product_status(
    repository: SupabaseRepository,
    product: dict[str, Any],
    approval_mode: str,
    selected_image: dict[str, Any] | None,
) -> None:
    """Synchronize content and image approval statuses."""
    is_approved = approval_mode == "APPROVE"

    payload: dict[str, Any] = {
        "content_status": (
            "APPROVED"
            if is_approved
            else "DRAFTED"
        ),
        "updated_at": utc_now(),
    }

    if (
        is_approved
        and selected_image
        and selected_image.get("image_status") == "VALIDATED"
        and selected_image.get("is_publish_eligible") is True
    ):
        payload["image_status"] = "APPROVED"

    response = (
        repository.client
        .table("internal_products")
        .update(payload)
        .eq(
            "internal_product_id",
            product["internal_product_id"],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Internal product status update returned no data."
        )


def print_preview(
    product: dict[str, Any],
    payload: dict[str, Any],
    facebook_text: str | None,
    selected_image: dict[str, Any] | None,
) -> None:
    """Print generated content before saving."""
    print()
    print("=" * 78)
    print("PRODUCT CONTENT PREVIEW")
    print("=" * 78)
    print(f"Product code: {product.get('product_code')}")
    print(f"Title: {product.get('title')}")
    print(f"Author: {product.get('author') or '[not found]'}")
    print(f"Publisher: {product.get('publisher') or '[not found]'}")
    print(
        "Existing content: "
        + (
            str(
                product.get("existing_content", {}).get(
                    "content_status"
                )
            )
            if product.get("existing_content")
            else "NO"
        )
    )
    print(
        "Facebook source text: "
        + ("FOUND" if facebook_text else "NOT FOUND")
    )
    print(
        "Selected publishable image: "
        + (
            str(selected_image.get("image_id"))
            if selected_image
            else "NOT FOUND"
        )
    )

    print()
    print("Short description:")
    print("-" * 78)
    print(payload["short_description"])

    print()
    print("Long description:")
    print("-" * 78)
    print(payload["long_description"])

    print()
    print("Product details:")
    print("-" * 78)
    print(payload["product_details"] or "[not found]")

    print()
    print("SEO title:")
    print(payload["seo_title"])

    print()
    print("SEO description:")
    print(payload["seo_description"])
    print("-" * 78)


def main() -> None:
    """Create, regenerate, review, and approve Vietnamese product content."""
    load_dotenv(PROJECT_ROOT / ".env")

    print("=" * 78)
    print("PRODUCT CONTENT GENERATOR AND REVIEW")
    print("=" * 78)
    print(f"Version: {GENERATOR_VERSION}")

    repository = SupabaseRepository()

    print(
        "Searching for content that is missing or requires review..."
    )

    product = get_product_for_content(repository)

    if product is None:
        print(
            "No Vietnamese product content requires generation or review."
        )
        return

    reference = get_primary_reference(
        repository=repository,
        reference_id=product.get("primary_reference_id"),
    )

    facebook_text = get_facebook_source_text(
        repository=repository,
        candidate_id=product.get("candidate_id"),
    )

    selected_image = get_selected_main_image(
        repository=repository,
        candidate_id=product.get("candidate_id"),
    )

    draft_payload = build_content_payload(
        product=product,
        reference=reference,
        facebook_text=facebook_text,
        approval_mode="DRAFT",
    )

    print_preview(
        product=product,
        payload=draft_payload,
        facebook_text=facebook_text,
        selected_image=selected_image,
    )

    print()
    print("Available actions:")
    print("  APPROVE    Save this content as approved.")
    print("  SAVE       Save this content as a draft.")
    print("  SKIP       Leave the database unchanged.")

    confirmation = input(
        "Enter APPROVE, SAVE, or SKIP: "
    ).strip().upper()

    if confirmation == "SKIP" or not confirmation:
        print("Product content was not changed.")
        return

    if confirmation not in {"APPROVE", "SAVE"}:
        print("Unsupported action. Product content was not changed.")
        return

    approval_mode = (
        "APPROVE"
        if confirmation == "APPROVE"
        else "DRAFT"
    )

    final_payload = build_content_payload(
        product=product,
        reference=reference,
        facebook_text=facebook_text,
        approval_mode=approval_mode,
    )

    product_content, action = save_product_content(
        repository=repository,
        product=product,
        payload=final_payload,
    )

    update_internal_product_status(
        repository=repository,
        product=product,
        approval_mode=approval_mode,
        selected_image=selected_image,
    )

    print()
    print("=" * 78)
    print("PRODUCT CONTENT RESULT")
    print("=" * 78)
    print(f"Action: {action}")
    print(
        "Content ID: "
        f"{product_content.get('product_content_id')}"
    )
    print(
        "Content status: "
        f"{product_content.get('content_status')}"
    )
    print(
        "Review required: "
        f"{product_content.get('review_required')}"
    )

    if approval_mode == "APPROVE":
        if selected_image:
            print(
                "Internal product image status was approved because "
                "a validated, publish-eligible main image exists."
            )
        else:
            print(
                "Content was approved, but image status was not approved "
                "because no selected publish-eligible image was found."
            )

    print()
    print(
        "Product content generation and review completed successfully."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Product content generation was cancelled by the user."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print("Product content generation failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
