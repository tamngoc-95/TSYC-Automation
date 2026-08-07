import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


GENERATOR_NAME = "internal_product_content_generator"
GENERATOR_VERSION = "1.2.0"
VALID_ACTIONS = {"PREVIEW", "SAVE", "APPROVE", "SKIP"}


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_confirmation(value: str | None) -> str:
    """Normalize interactive and command-line confirmation values."""
    if not value:
        return ""

    return (
        value.strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def clean_text(value: str | None) -> str | None:
    """Normalize whitespace without changing the meaning of the text."""
    if not value:
        return None

    normalized = " ".join(value.split()).strip()
    return normalized or None


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate, review, save, or approve Vietnamese product content."
        )
    )
    parser.add_argument(
        "--product-code",
        help="Process one exact internal product code.",
    )
    parser.add_argument(
        "--action",
        choices=sorted(VALID_ACTIONS),
        type=str.upper,
        help="PREVIEW, SAVE, APPROVE, or SKIP.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --product-code and --action.",
    )
    return parser.parse_args()


def get_products(
    repository: SupabaseRepository,
    product_code: str | None,
) -> list[dict[str, Any]]:
    """Return active internal products in deterministic order."""
    query = (
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
    )

    if product_code:
        query = query.eq("product_code", product_code)

    response = query.order("created_at", desc=False).execute()
    return response.data or []


def get_existing_content(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return existing Vietnamese content for one internal product."""
    response = (
        repository.client
        .table("product_contents")
        .select("*")
        .eq("internal_product_id", internal_product_id)
        .eq("content_language", "vi")
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return rows[0] if rows else None


def select_product_for_review(
    repository: SupabaseRepository,
    product_code: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """
    Select one product that is missing content or has reviewable content.

    Approved and rejected content is never overwritten automatically.
    """
    products = get_products(repository, product_code)

    if product_code and not products:
        raise RuntimeError(
            f"Internal product was not found: {product_code}"
        )

    for product in products:
        existing = get_existing_content(
            repository,
            product["internal_product_id"],
        )

        if existing:
            status = existing.get("content_status")

            if status in {"APPROVED", "REJECTED"}:
                print(
                    "Skipping product because finalized Vietnamese content "
                    f"already exists: {product.get('product_code')} ({status})"
                )
                continue

        return product, existing

    return None


def build_product_details(product: dict[str, Any]) -> str:
    """Build product details only from verified internal metadata."""
    details: list[str] = []

    mappings = [
        ("Tác giả", product.get("author")),
        ("Nhà xuất bản", product.get("publisher")),
        ("Số trang", product.get("page_count")),
        ("ISBN", product.get("isbn")),
    ]

    for label, value in mappings:
        if value not in (None, ""):
            details.append(f"{label}: {value}")

    dimensions = [
        product.get("length_cm"),
        product.get("width_cm"),
        product.get("height_cm"),
    ]
    dimensions = [str(value) for value in dimensions if value is not None]

    if dimensions:
        details.append(f"Kích thước: {' × '.join(dimensions)} cm")

    if product.get("weight_grams") is not None:
        details.append(f"Trọng lượng: {product['weight_grams']} g")

    if product.get("language_code"):
        language_label = {
            "vi": "Tiếng Việt",
            "de": "Tiếng Đức",
            "en": "Tiếng Anh",
        }.get(
            str(product["language_code"]).lower(),
            product["language_code"],
        )
        details.append(f"Ngôn ngữ: {language_label}")

    return "\n".join(details)


def build_safe_draft(product: dict[str, Any]) -> dict[str, Any]:
    """
    Build a conservative draft from verified metadata only.

    The generator intentionally avoids inventing the book topic. A reviewer
    must enrich thematic content from an authorized source before approval.
    """
    title = clean_text(product.get("title"))
    author = clean_text(product.get("author"))

    if not title:
        raise RuntimeError("Internal product title is missing.")

    author_phrase = f" của {author}" if author else ""

    short_description = (
        f"“{title}”{author_phrase} là ấn phẩm đang có tại Tiệm Sách Yêu Con. "
        "Thông tin cơ bản của sách được tổng hợp từ dữ liệu sản phẩm đã xác minh."
    )

    long_description = (
        f"“{title}”{author_phrase} hiện được chuẩn bị dưới dạng sản phẩm nháp "
        "tại Tiệm Sách Yêu Con.\n\n"
        "Phần giới thiệu nội dung chi tiết cần được người quản lý kiểm tra và "
        "bổ sung dựa trên bài đăng được phép sử dụng hoặc nguồn tham khảo đã "
        "được xác minh trước khi sản phẩm được xuất bản.\n\n"
        "Vui lòng xem phần thông tin sản phẩm để biết tác giả, nhà xuất bản, "
        "số trang, kích thước và các dữ liệu hiện có."
    )

    author_summary = (
        f"Tác giả của ấn phẩm là {author}."
        if author
        else None
    )

    seo_title = f"{title} – {author}" if author else title
    seo_description = (
        f"Thông tin sách {title}"
        + (f" của {author}" if author else "")
        + " tại Tiệm Sách Yêu Con."
    )

    return {
        "product_name": title,
        "short_description": short_description,
        "long_description": long_description,
        "author_summary": author_summary,
        "product_details": build_product_details(product),
        "seo_title": seo_title,
        "seo_description": seo_description,
    }


def merge_with_existing(
    generated: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Preserve existing reviewed wording.

    Generated values fill only missing fields. Existing text is never silently
    replaced, which prevents accidental loss of manual corrections.
    """
    if not existing:
        return generated

    merged = generated.copy()

    for field in generated:
        existing_value = existing.get(field)

        if clean_text(existing_value):
            merged[field] = existing_value

    return merged


def print_preview(
    product: dict[str, Any],
    content: dict[str, Any],
    existing: dict[str, Any] | None,
) -> None:
    """Print the complete content preview before any write."""
    print()
    print("=" * 78)
    print("PRODUCT CONTENT REVIEW")
    print("=" * 78)
    print(f"Product code: {product.get('product_code')}")
    print(f"Title: {product.get('title')}")
    print(f"Current internal status: {product.get('content_status')}")
    print(
        "Existing content status: "
        f"{existing.get('content_status') if existing else '[missing]'}"
    )

    sections = [
        ("PRODUCT NAME", content.get("product_name")),
        ("SHORT DESCRIPTION", content.get("short_description")),
        ("LONG DESCRIPTION", content.get("long_description")),
        ("AUTHOR SUMMARY", content.get("author_summary")),
        ("PRODUCT DETAILS", content.get("product_details")),
        ("SEO TITLE", content.get("seo_title")),
        ("SEO DESCRIPTION", content.get("seo_description")),
    ]

    for label, value in sections:
        print()
        print(f"[{label}]")
        print(value or "[empty]")


def save_content(
    repository: SupabaseRepository,
    product: dict[str, Any],
    existing: dict[str, Any] | None,
    content: dict[str, Any],
    approve: bool,
) -> dict[str, Any]:
    """Insert or update one Vietnamese content record."""
    now = utc_now()
    status = "APPROVED" if approve else "DRAFTED"

    payload = {
        **content,
        "content_language": "vi",
        "content_status": status,
        "generation_method": "RULE_BASED",
        "generator_name": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "review_required": not approve,
        "review_notes": (
            "Content reviewed and approved for WooCommerce draft."
            if approve
            else (
                "Draft saved. Review the wording, product facts, and topic "
                "before WooCommerce draft creation."
            )
        ),
        "approved_at": now if approve else None,
        "updated_at": now,
    }

    table = repository.client.table("product_contents")

    if existing:
        response = (
            table.update(payload)
            .eq("product_content_id", existing["product_content_id"])
            .execute()
        )
    else:
        payload["internal_product_id"] = product["internal_product_id"]
        response = table.insert(payload).execute()

    rows = response.data or []

    if not rows:
        raise RuntimeError("Product content write returned no data.")

    internal_response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "content_status": status,
                "updated_at": now,
            }
        )
        .eq("internal_product_id", product["internal_product_id"])
        .execute()
    )

    if not internal_response.data:
        raise RuntimeError(
            "Internal product content status update returned no data."
        )

    return rows[0]


def resolve_action(
    args: argparse.Namespace,
) -> str:
    """Resolve a normalized action from CLI arguments or interactive input."""
    if args.non_interactive:
        if not args.product_code or not args.action:
            raise RuntimeError(
                "--non-interactive requires --product-code and --action."
            )
        return normalize_confirmation(args.action)

    if args.action:
        return normalize_confirmation(args.action)

    value = input(
        "Type PREVIEW, SAVE, APPROVE, or SKIP: "
    )
    return normalize_confirmation(value)


def main() -> None:
    """Generate or review one Vietnamese product content record."""
    load_dotenv()
    args = parse_arguments()

    print("=" * 78)
    print("PRODUCT CONTENT GENERATOR AND REVIEW")
    print("=" * 78)
    print(f"Version: {GENERATOR_VERSION}")

    repository = SupabaseRepository()

    selected = select_product_for_review(
        repository=repository,
        product_code=args.product_code,
    )

    if selected is None:
        print("No Vietnamese product content requires generation or review.")
        return

    product, existing = selected
    generated = build_safe_draft(product)
    content = merge_with_existing(generated, existing)

    print_preview(product, content, existing)

    action = resolve_action(args)

    if action not in VALID_ACTIONS:
        print(
            "Invalid action. Use PREVIEW, SAVE, APPROVE, or SKIP."
        )
        return

    if action in {"PREVIEW", "SKIP"}:
        print("No database changes were made.")
        return

    result = save_content(
        repository=repository,
        product=product,
        existing=existing,
        content=content,
        approve=(action == "APPROVE"),
    )

    print()
    print("=" * 78)
    print("PRODUCT CONTENT RESULT")
    print("=" * 78)
    print(f"Content ID: {result.get('product_content_id')}")
    print(f"Content status: {result.get('content_status')}")
    print(f"Review required: {result.get('review_required')}")
    print("Product content processing completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("Product content processing was cancelled by the user.")
        sys.exit(130)
    except Exception as error:
        print()
        print("Product content processing failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
