import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


GENERATOR_NAME = "internal_product_content_generator"
GENERATOR_VERSION = "1.0.0"


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


def get_product_without_content(
    repository: SupabaseRepository,
) -> dict[str, Any] | None:
    """Return one active internal product without Vietnamese content."""
    product_response = (
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
            "product_metadata"
        )
        .eq(
            "is_active",
            True,
        )
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    products = product_response.data or []

    for product in products:
        existing_response = (
            repository.client
            .table("product_contents")
            .select(
                "product_content_id"
            )
            .eq(
                "internal_product_id",
                product[
                    "internal_product_id"
                ],
            )
            .eq(
                "content_language",
                "vi",
            )
            .limit(1)
            .execute()
        )

        if existing_response.data:
            print(
                "Skipping product because Vietnamese content "
                "already exists: "
                f"{product.get('product_code')}"
            )
            continue

        return product

    return None


def get_primary_reference(
    repository: SupabaseRepository,
    reference_id: str | None,
) -> dict[str, Any] | None:
    """Return the primary matched product reference."""
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
        .eq(
            "reference_id",
            reference_id,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def build_product_details(
    product: dict[str, Any],
) -> str:
    """Build a readable product specification section."""
    details: list[str] = []

    if product.get("author"):
        details.append(
            f"Tác giả: {product['author']}"
        )

    if product.get("publisher"):
        details.append(
            f"Nhà xuất bản: {product['publisher']}"
        )

    if product.get("page_count"):
        details.append(
            f"Số trang: {product['page_count']}"
        )

    dimensions: list[str] = []

    if product.get("length_cm") is not None:
        dimensions.append(
            str(product["length_cm"])
        )

    if product.get("width_cm") is not None:
        dimensions.append(
            str(product["width_cm"])
        )

    if product.get("height_cm") is not None:
        dimensions.append(
            str(product["height_cm"])
        )

    if dimensions:
        details.append(
            "Kích thước: "
            + " × ".join(dimensions)
            + " cm"
        )

    if product.get("isbn"):
        details.append(
            f"ISBN: {product['isbn']}"
        )

    if product.get("weight_grams"):
        details.append(
            f"Trọng lượng: {product['weight_grams']} g"
        )

    return "\n".join(details)


def build_short_description(
    product: dict[str, Any],
) -> str:
    """Build a concise, original product summary."""
    title = product["title"]
    author = product.get("author")

    if author:
        return (
            f"“{title}” của {author} là cuốn sách giúp người đọc "
            "khám phá các phương pháp rèn luyện trí nhớ và nâng cao "
            "khả năng ghi nhớ thông tin trong học tập và cuộc sống."
        )

    return (
        f"“{title}” giới thiệu những phương pháp giúp người đọc "
        "rèn luyện trí nhớ và nâng cao khả năng ghi nhớ thông tin."
    )


def build_long_description(
    product: dict[str, Any],
    reference: dict[str, Any] | None,
) -> str:
    """
    Build an original description.

    Reference descriptions are used only for understanding the book.
    They are not copied verbatim.
    """
    title = product["title"]
    author = product.get("author")

    paragraphs: list[str] = []

    if author:
        paragraphs.append(
            f"“{title}” là tác phẩm của {author}, dành cho những "
            "độc giả muốn cải thiện khả năng ghi nhớ và sử dụng "
            "thông tin hiệu quả hơn."
        )

    else:
        paragraphs.append(
            f"“{title}” dành cho những độc giả muốn cải thiện "
            "khả năng ghi nhớ và sử dụng thông tin hiệu quả hơn."
        )

    paragraphs.append(
        "Cuốn sách giới thiệu các nguyên tắc và kỹ thuật ghi nhớ "
        "có thể áp dụng vào việc học tập, công việc và sinh hoạt "
        "hằng ngày. Nội dung hướng người đọc đến việc hiểu cách "
        "trí nhớ hoạt động và xây dựng thói quen rèn luyện phù hợp."
    )

    paragraphs.append(
        "Sản phẩm phù hợp với học sinh, sinh viên, người đi làm "
        "và những ai quan tâm đến việc phát triển khả năng tập "
        "trung, ghi nhớ và học tập chủ động."
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
        f"Tác giả của cuốn sách là {author}."
    )


def build_seo_title(
    product: dict[str, Any],
) -> str:
    """Build a simple SEO title."""
    author = product.get("author")

    if author:
        return (
            f"{product['title']} – {author}"
        )

    return product["title"]


def build_seo_description(
    product: dict[str, Any],
) -> str:
    """Build a short SEO description."""
    author = product.get("author")

    if author:
        return (
            f"Khám phá sách {product['title']} của {author}. "
            "Thông tin sản phẩm, nội dung giới thiệu và chi tiết sách."
        )

    return (
        f"Khám phá sách {product['title']}. "
        "Thông tin sản phẩm, nội dung giới thiệu và chi tiết sách."
    )


def create_product_content(
    repository: SupabaseRepository,
    product: dict[str, Any],
    reference: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create one Vietnamese product content draft."""
    product_name = clean_text(
        product.get("title")
    )

    if not product_name:
        raise RuntimeError(
            "Internal product title is missing."
        )

    payload = {
        "internal_product_id": product[
            "internal_product_id"
        ],
        "content_language": "vi",
        "product_name": product_name,
        "short_description": (
            build_short_description(
                product
            )
        ),
        "long_description": (
            build_long_description(
                product,
                reference,
            )
        ),
        "author_summary": (
            build_author_summary(
                product
            )
        ),
        "product_details": (
            build_product_details(
                product
            )
        ),
        "seo_title": (
            build_seo_title(
                product
            )
        ),
        "seo_description": (
            build_seo_description(
                product
            )
        ),
        "content_status": "DRAFTED",
        "generation_method": "RULE_BASED",
        "generator_name": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "review_required": True,
        "review_notes": (
            "Review wording, product facts, and suitability "
            "before WooCommerce draft creation."
        ),
        "approved_at": None,
        "updated_at": utc_now(),
    }

    response = (
        repository.client
        .table("product_contents")
        .insert(payload)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "Product content insert returned no data."
        )

    return rows[0]


def update_internal_product_status(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> None:
    """Mark internal product content as drafted."""
    response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "content_status": "DRAFTED",
                "updated_at": utc_now(),
            }
        )
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Internal product content status update returned no data."
        )


def print_preview(
    product: dict[str, Any],
) -> None:
    """Print the internal product selected for content generation."""
    print()
    print("=" * 72)
    print("PRODUCT CONTENT PREVIEW")
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
        "Author: "
        f"{product.get('author') or '[not found]'}"
    )

    print(
        "Publisher: "
        f"{product.get('publisher') or '[not found]'}"
    )

    print(
        "Page count: "
        f"{product.get('page_count') or '[not found]'}"
    )

    print(
        "Image status: "
        f"{product.get('image_status')}"
    )

    print(
        "Current content status: "
        f"{product.get('content_status')}"
    )


def main() -> None:
    """Create one product content draft."""
    load_dotenv()

    print("=" * 72)
    print("PRODUCT CONTENT GENERATOR")
    print("=" * 72)
    print(
        f"Version: {GENERATOR_VERSION}"
    )

    repository = SupabaseRepository()

    print(
        "Searching for an internal product without content..."
    )

    product = get_product_without_content(
        repository
    )

    if product is None:
        print(
            "No internal product without Vietnamese content was found."
        )
        return

    reference = get_primary_reference(
        repository=repository,
        reference_id=product.get(
            "primary_reference_id"
        ),
    )

    print_preview(
        product
    )

    print()

    confirmation = input(
        "Type GENERATE to create the product content draft, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "GENERATE":
        print(
            "Product content generation was cancelled."
        )
        return

    product_content = create_product_content(
        repository=repository,
        product=product,
        reference=reference,
    )

    print(
        "Product content draft created."
    )

    update_internal_product_status(
        repository=repository,
        internal_product_id=product[
            "internal_product_id"
        ],
    )

    print(
        "Internal product content status changed to DRAFTED."
    )

    print()
    print("=" * 72)
    print("PRODUCT CONTENT RESULT")
    print("=" * 72)

    print(
        "Content ID: "
        f"{product_content.get('product_content_id')}"
    )

    print(
        "Product name: "
        f"{product_content.get('product_name')}"
    )

    print(
        "Content status: "
        f"{product_content.get('content_status')}"
    )

    print(
        "Review required: "
        f"{product_content.get('review_required')}"
    )

    print()
    print(
        "Product content generation completed successfully."
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
        print(
            "Product content generation failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)