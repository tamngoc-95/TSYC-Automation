import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from create_internal_product import is_historical_candidate_code
from src.cli_bootstrap import configure_utf8_console
from src.domain.content_package import ContentPackage, build_content_package
from src.domain.content_status import ContentStatus
from src.domain.decisions import Outcome
from src.domain.rules import content_rules, multilingual_consistency, translation_rules
from src.repositories.supabase_repository import SupabaseRepository
from src.services.translation_provider import (
    DEFAULT_PACKAGE_DIR,
    TranslationProvider,
    get_translation_provider,
    package_file_path,
)

configure_utf8_console()


GENERATOR_NAME = "internal_product_content_generator"
GENERATOR_VERSION = "1.5.0"
TRANSLATION_GENERATOR_NAME = "internal_product_content_translation"
TRANSLATION_GENERATOR_VERSION = "1.0.0"
VALID_ACTIONS = {
    "PREVIEW",
    "SAVE",
    "APPROVE",
    "REVISE",
    "AUTO_REVISE",
    "SKIP",
    # Multilingual path (CLAUDE_AUTOMATION.md section 9): en+de together.
    "TRANSLATE",
    "EXPORT_PACKAGE",
}

# product_contents.content_language values (migrations/008 check
# constraint). 'vi' is the canonical source; 'en'/'de' are localizations
# of an already-APPROVED 'vi' row (CLAUDE_AUTOMATION.md section 9).
VALID_CONTENT_LANGUAGES = ("vi", "en", "de")
TRANSLATION_ACTIONS = {"PREVIEW", "SAVE", "APPROVE", "SKIP"}

# Fields an en/de --content-file may carry: the translated text fields
# plus section 9.2 provenance. Anything else is rejected, so a translation
# file can never write an arbitrary column.
TRANSLATION_PROVENANCE_FIELDS = {"content_source", "facts_used", "generation_method"}
TRANSLATION_FILE_FIELDS = (
    set(translation_rules.TRANSLATABLE_TEXT_FIELDS) | TRANSLATION_PROVENANCE_FIELDS
)
TRANSLATION_GENERATION_METHODS = {"MANUAL", "AI_ASSISTED", "HYBRID"}

# Minimum length of an excerpt used as short_description/seo_description
# when auto-enriching from a reference description -- short enough to stay
# a summary, long enough to be meaningfully distinct from the generic
# safe-draft placeholder text.
_AUTO_ENRICH_EXCERPT_LENGTH = 200

# Customer-facing product_contents fields a human reviewer is allowed to
# revise through --action REVISE. product_name and author_summary are
# intentionally excluded: they are derived from verified internal_product
# identity data (title/author), not reviewer prose, and REVISE must never
# touch identity-derived fields.
REVIEWER_EDITABLE_FIELDS = {
    "short_description",
    "long_description",
    "seo_title",
    "seo_description",
    "product_details",
}

# The full set of product_contents free-text fields save_content() writes.
# REVISE always carries product_name/author_summary over unchanged from the
# existing row and only ever overrides fields in REVIEWER_EDITABLE_FIELDS.
CONTENT_TEXT_FIELDS = (
    "product_name",
    "short_description",
    "long_description",
    "author_summary",
    "product_details",
    "seo_title",
    "seo_description",
)


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
        help=(
            "PREVIEW, SAVE, APPROVE, REVISE, AUTO_REVISE, or SKIP; "
            "TRANSLATE (en+de from APPROVED vi via --translation-provider, "
            "cross-language validation, existing translation save/approve "
            "path) or EXPORT_PACKAGE (write the content package JSON, no "
            "database write)."
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --product-code and --action.",
    )
    parser.add_argument(
        "--content-file",
        help=(
            "Path to a UTF-8 JSON file of reviewer-edited content fields. "
            "Required for --action REVISE. The JSON root must be an object "
            "containing only reviewer-editable fields (short_description, "
            "long_description, seo_title, seo_description, product_details) "
            "-- all optional; omitted fields keep their current database "
            "value."
        ),
    )
    parser.add_argument(
        "--confirm-revise",
        action="store_true",
        help=(
            "Explicit machine-readable confirmation that replaces the "
            "interactive 'Type REVISE' prompt. Required with "
            "--non-interactive --action REVISE."
        ),
    )
    parser.add_argument(
        "--content-language",
        choices=VALID_CONTENT_LANGUAGES,
        default="vi",
        type=str.lower,
        help=(
            "Content language (default vi; existing Vietnamese behavior "
            "unchanged). en/de require an existing APPROVED vi row for "
            "--product-code, accept content only via --content-file, and "
            "support PREVIEW (validate, no write), SAVE, APPROVE, SKIP."
        ),
    )
    parser.add_argument(
        "--translation-provider",
        choices=("package-file", "claude"),
        default="package-file",
        help=(
            "EN/DE source for --action TRANSLATE. package-file (default, "
            "offline) reads data/processed/content_packages/"
            "<candidate_code>.json (create it with --action "
            "EXPORT_PACKAGE, then fill the en/de fields); claude calls the "
            "Claude API (opt-in, requires ANTHROPIC_API_KEY)."
        ),
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

    response = query.order(
        "created_at",
        desc=False,
    ).execute()

    rows = response.data or []

    if product_code and len(rows) > 1:
        raise RuntimeError(
            "Product code did not resolve to exactly one internal product."
        )

    return rows


def get_existing_content(
    repository: SupabaseRepository,
    internal_product_id: str,
    content_language: str = "vi",
) -> dict[str, Any] | None:
    """Return existing content in one language (default Vietnamese) for
    one internal product."""
    response = (
        repository.client
        .table("product_contents")
        .select("*")
        .eq("internal_product_id", internal_product_id)
        .eq("content_language", content_language)
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

            if status in {ContentStatus.APPROVED, ContentStatus.REJECTED}:
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


def is_generic_safe_draft(
    content: dict[str, Any],
    generated: dict[str, Any],
) -> bool:
    """
    Return True when content is still the untouched metadata-only safe draft.

    Generic safe drafts are useful for SAVE/PREVIEW but must never be approved
    automatically because they contain no verified thematic book description.
    """
    fields = (
        "short_description",
        "long_description",
        "author_summary",
        "seo_description",
    )

    return all(
        clean_text(content.get(field))
        == clean_text(generated.get(field))
        for field in fields
    )


def validate_approval_content(
    existing: dict[str, Any] | None,
    content: dict[str, Any],
    generated: dict[str, Any],
) -> None:
    """
    Reject approval when content validation does not pass.

    The is-still-a-generic-draft safety check is unchanged; the shared
    internal-workflow-boilerplate rule (src.domain.rules.content_rules)
    is a new addition -- CLAUDE.md section 15.1: customer-facing content
    must never contain internal workflow instructions. Neither check
    was previously enforced automatically before APPROVE.
    """
    if not existing:
        raise RuntimeError(
            "Content cannot be approved on its first generated metadata-only "
            "draft. Save it first, enrich it from verified source material, "
            "then approve the reviewed content."
        )

    if is_generic_safe_draft(
        content=content,
        generated=generated,
    ):
        raise RuntimeError(
            "Content is still the generic metadata-only safe draft. "
            "Approval is blocked until the book description is enriched "
            "from verified source material and reviewed."
        )

    boilerplate_check = content_rules.evaluate_internal_boilerplate(content)
    if not boilerplate_check.is_auto_pass:
        raise RuntimeError(
            f"[{boilerplate_check.rule_code}] {boilerplate_check.reason}"
        )


def restore_existing_content(
    repository: SupabaseRepository,
    existing: dict[str, Any],
) -> None:
    """Restore the previous content record after a downstream failure."""
    payload = {
        key: value
        for key, value in existing.items()
        if key not in {
            "product_content_id",
            "internal_product_id",
            "created_at",
        }
    }

    (
        repository.client
        .table("product_contents")
        .update(payload)
        .eq(
            "product_content_id",
            existing["product_content_id"],
        )
        .execute()
    )


def delete_new_content(
    repository: SupabaseRepository,
    product_content_id: str,
) -> None:
    """Delete a newly inserted content row after a downstream failure."""
    (
        repository.client
        .table("product_contents")
        .delete()
        .eq(
            "product_content_id",
            product_content_id,
        )
        .execute()
    )


def save_content(
    repository: SupabaseRepository,
    product: dict[str, Any],
    existing: dict[str, Any] | None,
    content: dict[str, Any],
    approve: bool,
    generation_method: str | None = None,
    review_notes: str | None = None,
    status_override: str | None = None,
) -> dict[str, Any]:
    """
    Insert or update one Vietnamese content record atomically as possible.

    generation_method and review_notes are optional overrides used by
    --action REVISE to record that a write came from human review rather
    than the rule-based generator. Callers that omit them (the original
    SAVE/APPROVE flow) get the original inferred values, unchanged.

    status_override lets a caller write ContentStatus.REVIEW_REQUIRED
    explicitly -- used only by the automatic-approval-declined path in
    main() below, when a non-interactive --action APPROVE attempt fails
    deterministic validation. It always wins over `approve` when set.
    """
    now = utc_now()
    # Written to both product_contents.content_status and
    # internal_products.content_status below -- APPROVED/DRAFTED/
    # REVIEW_REQUIRED are valid members of both (src.domain.content_status)
    # enums, so one shared string is correct for both writes.
    if status_override is not None:
        status = status_override
    else:
        status = ContentStatus.APPROVED if approve else ContentStatus.DRAFTED

    is_approved = status == ContentStatus.APPROVED

    payload = {
        **content,
        "content_language": "vi",
        "content_status": status,
        "generation_method": (
            generation_method
            if generation_method
            else (
                existing.get("generation_method")
                if existing and existing.get("generation_method")
                else "RULE_BASED"
            )
        ),
        "generator_name": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "review_required": not is_approved,
        "review_notes": (
            review_notes
            if review_notes is not None
            else (
                "Content reviewed and approved for WooCommerce draft."
                if is_approved
                else (
                    "Draft saved. Review the wording, product facts, and "
                    "topic before WooCommerce draft creation."
                )
            )
        ),
        "approved_at": now if is_approved else None,
        "updated_at": now,
    }

    table = repository.client.table(
        "product_contents"
    )

    inserted_new = existing is None
    written_row: dict[str, Any] | None = None

    if existing:
        response = (
            table.update(payload)
            .eq(
                "product_content_id",
                existing["product_content_id"],
            )
            .execute()
        )
    else:
        payload[
            "internal_product_id"
        ] = product[
            "internal_product_id"
        ]

        response = (
            table.insert(payload)
            .execute()
        )

    rows = response.data or []

    if len(rows) != 1:
        raise RuntimeError(
            "Product content write did not return exactly one row."
        )

    written_row = rows[0]

    try:
        internal_response = (
            repository.client
            .table("internal_products")
            .update(
                {
                    "content_status": status,
                    "updated_at": now,
                }
            )
            .eq(
                "internal_product_id",
                product["internal_product_id"],
            )
            .execute()
        )

        internal_rows = (
            internal_response.data
            or []
        )

        if len(internal_rows) != 1:
            raise RuntimeError(
                "Internal product content status update did not affect "
                "exactly one row."
            )

    except Exception:
        if written_row:
            if inserted_new:
                delete_new_content(
                    repository=repository,
                    product_content_id=str(
                        written_row[
                            "product_content_id"
                        ]
                    ),
                )

            elif existing:
                restore_existing_content(
                    repository=repository,
                    existing=existing,
                )

        raise

    return written_row


def attempt_content_approval(
    repository: SupabaseRepository,
    product: dict[str, Any],
    existing: dict[str, Any] | None,
    content: dict[str, Any],
    generated: dict[str, Any],
    non_interactive: bool,
) -> tuple[dict[str, Any], str | None]:
    """
    Run --action APPROVE's deterministic validation and save accordingly.

    Returns (written_row, declined_reason). declined_reason is None on a
    genuine approval (content_status=APPROVED). When validation fails:

    - non_interactive=True (the orchestrator's automated path, CLAUDE.md
      15.3 "stop for human review" -- not "crash the batch"): the content
      row is written with content_status=REVIEW_REQUIRED instead of
      raising, and declined_reason carries why. This is never a silent
      downgrade of previously APPROVED content -- validate_approval_content
      already refuses a first-generation or still-untouched draft, and
      REVISE separately refuses to touch an APPROVED row; APPROVE only
      ever reaches here from DRAFTED.
    - non_interactive=False (a human explicitly typed/passed APPROVE):
      the RuntimeError is re-raised unchanged so the human sees the
      failure immediately, exactly as before this function existed.
    """
    try:
        validate_approval_content(
            existing=existing,
            content=content,
            generated=generated,
        )
    except RuntimeError as error:
        if not non_interactive:
            raise

        result = save_content(
            repository=repository,
            product=product,
            existing=existing,
            content=content,
            approve=False,
            status_override=ContentStatus.REVIEW_REQUIRED,
            review_notes=f"Automatic approval declined: {error}",
        )
        return result, f"{type(error).__name__}: {error}"

    result = save_content(
        repository=repository,
        product=product,
        existing=existing,
        content=content,
        approve=True,
    )
    return result, None


def load_reviewer_content_file(path: Path) -> dict[str, Any]:
    """
    Read a reviewer content JSON file as strict UTF-8 and parse its object.

    Fails loudly (RuntimeError) on: a missing file, invalid UTF-8, invalid
    JSON, or a JSON root that is not an object. Never silently substitutes
    an empty payload.
    """
    try:
        # utf-8-sig transparently strips a leading UTF-8 byte-order mark
        # (common from Windows editors like Notepad) while behaving exactly
        # like plain utf-8 for files that have none. Any other invalid byte
        # sequence still raises UnicodeDecodeError.
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Content file was not found: {path}"
        ) from error
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"Content file is not valid UTF-8: {path} ({error})"
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"Content file could not be read: {path} ({error})"
        ) from error

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Content file is not valid JSON: {path} ({error})"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Content file JSON root must be an object with reviewer-"
            f"editable fields: {path}"
        )

    return payload


def validate_reviewer_content_payload(
    payload: dict[str, Any],
) -> dict[str, str]:
    """
    Validate a reviewer content JSON payload against the REVISE schema.

    Only REVIEWER_EDITABLE_FIELDS may be present -- this is the only
    boundary that keeps REVISE from writing arbitrary DB columns. Every
    present field must be a non-null string; a field is left at its current
    database value by omitting it entirely, not by setting it to null.
    """
    unknown_fields = sorted(set(payload) - REVIEWER_EDITABLE_FIELDS)

    if unknown_fields:
        raise RuntimeError(
            "Content file contains unsupported field(s): "
            + ", ".join(unknown_fields)
            + ". Allowed fields: "
            + ", ".join(sorted(REVIEWER_EDITABLE_FIELDS))
        )

    validated: dict[str, str] = {}

    for field, value in payload.items():
        if value is None:
            raise RuntimeError(
                f"Content file field {field!r} is null. Omit the field "
                "entirely to keep its current value -- REVISE does not "
                "accept null as 'clear this field'."
            )

        if not isinstance(value, str):
            raise RuntimeError(
                f"Content file field {field!r} must be a string, got "
                f"{type(value).__name__}."
            )

        validated[field] = value

    return validated


def get_content_for_exact_product(
    repository: SupabaseRepository,
    product_code: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """
    Resolve exactly one internal product by product_code, plus its existing
    Vietnamese content row if any.

    Uses the same targeting mechanism (get_products) as the rest of the
    script -- no implicit newest/all selection.
    """
    products = get_products(repository, product_code)

    if not products:
        raise RuntimeError(
            f"Internal product was not found: {product_code}"
        )

    product = products[0]
    existing = get_existing_content(
        repository,
        product["internal_product_id"],
    )

    return product, existing


def validate_revise_target(
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Enforce the REVISE safety gates on the existing content row.

    REVISE only ever updates an existing DRAFTED or REVIEW_REQUIRED row
    (CLAUDE.md section 15.2: "refuse APPROVED/REJECTED rows" -- REVIEW_
    REQUIRED is explicitly not in that refuse list, since REVISE is the
    normal way a reviewer's edit resolves a REVIEW_REQUIRED content row).
    It never creates a second content row, and it never touches APPROVED
    or REJECTED content.
    """
    if existing is None:
        raise RuntimeError(
            "REVISE requires an existing product_contents row. None was "
            "found for this product -- run --action SAVE first to create "
            "the initial draft; REVISE never creates a new content row."
        )

    status = existing.get("content_status")

    if status == ContentStatus.APPROVED:
        raise RuntimeError(
            "REVISE refuses to modify APPROVED content. Approved content "
            "must never be silently overwritten."
        )

    if status == ContentStatus.REJECTED:
        raise RuntimeError(
            "REVISE refuses to modify REJECTED content."
        )

    if status not in (ContentStatus.DRAFTED, ContentStatus.REVIEW_REQUIRED):
        raise RuntimeError(
            "REVISE requires content_status=DRAFTED or REVIEW_REQUIRED, "
            f"got {status!r}."
        )

    return existing


def build_revised_content_payload(
    existing: dict[str, Any],
    validated_payload: dict[str, str],
) -> dict[str, Any]:
    """
    Build the full product_contents text-field payload for a REVISE update.

    Every field starts from the existing row's current value. product_name
    and author_summary are never reviewer-editable and are always carried
    over unchanged; only fields present in validated_payload (already
    restricted to REVIEWER_EDITABLE_FIELDS) are overridden.
    """
    revised = {
        field: existing.get(field)
        for field in CONTENT_TEXT_FIELDS
    }
    revised.update(validated_payload)
    return revised


def diff_content_fields(
    existing: dict[str, Any],
    revised: dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    """Return (field, old_value, new_value) for reviewer fields that changed."""
    changes: list[tuple[str, Any, Any]] = []

    for field in sorted(REVIEWER_EDITABLE_FIELDS):
        old_value = existing.get(field)
        new_value = revised.get(field)

        if old_value != new_value:
            changes.append((field, old_value, new_value))

    return changes


def print_revise_preview(
    product: dict[str, Any],
    existing: dict[str, Any],
    changes: list[tuple[str, Any, Any]],
) -> None:
    """Print the exact field-level diff a REVISE write would apply."""
    print()
    print("=" * 78)
    print("REVISE PREVIEW")
    print("=" * 78)
    print(f"Product code: {product.get('product_code')}")
    print(f"Title: {product.get('title')}")
    print(f"Current content status: {existing.get('content_status')}")

    if not changes:
        print()
        print("No fields changed.")
        return

    for field, old_value, new_value in changes:
        print()
        print("FIELD")
        print(field)
        print("OLD VALUE")
        print(old_value if old_value not in (None, "") else "[empty]")
        print("NEW VALUE")
        print(new_value if new_value not in (None, "") else "[empty]")


def write_revise_audit_log(
    repository: SupabaseRepository,
    product: dict[str, Any],
    changes: list[tuple[str, Any, Any]],
) -> None:
    """
    Record a process_logs entry for a REVISE write.

    Reuses the existing process_logs table (migrations/001_initial_schema.sql)
    -- no schema change -- through SupabaseRepository.write_process_log(),
    the same supported helper method other future writers are expected to
    use; it also validates that the insert actually returned a row.

    This is called only after save_content() has already committed the
    content revision. A failure here must never be reported as though the
    content write itself failed -- see the try/except around this call in
    run_revise_action().
    """
    changed_field_names = ", ".join(field for field, _, _ in changes)

    repository.write_process_log(
        message=(
            "Reviewer-supplied content revision applied to "
            f"{product.get('product_code')}. Changed fields: "
            f"{changed_field_names}."
        ),
        process_name="prepare_product_content",
        candidate_id=product.get("candidate_id"),
        process_step="REVISE",
        log_level="INFO",
        status="HUMAN_REVIEW",
    )


def run_revise_action(
    repository: SupabaseRepository,
    product_code: str | None,
    content_file: str | None,
    non_interactive: bool,
    confirm_revise: bool,
    prompt: Callable[[str], str] = input,
) -> dict[str, Any] | None:
    """
    Run --action REVISE end to end: resolve target, validate the content
    file, compute a diff, confirm, write, and log. Returns the written row,
    or None when the run is a no-op or is cancelled.
    """
    if not product_code:
        raise RuntimeError(
            "--action REVISE requires --product-code (exact targeting "
            "only; no implicit newest/all selection)."
        )

    if not content_file:
        raise RuntimeError(
            "--action REVISE requires --content-file."
        )

    if non_interactive and not confirm_revise:
        raise RuntimeError(
            "--non-interactive --action REVISE requires --confirm-revise."
        )

    product, existing = get_content_for_exact_product(
        repository=repository,
        product_code=product_code,
    )
    existing = validate_revise_target(existing)

    payload = load_reviewer_content_file(Path(content_file))
    validated_payload = validate_reviewer_content_payload(payload)

    revised_content = build_revised_content_payload(
        existing=existing,
        validated_payload=validated_payload,
    )
    changes = diff_content_fields(
        existing=existing,
        revised=revised_content,
    )

    print_revise_preview(
        product=product,
        existing=existing,
        changes=changes,
    )

    if not changes:
        print()
        print("Result: NO_OP -- no field actually changed.")
        print("No database changes were made.")
        return None

    if confirm_revise:
        # non_interactive REVISE always reaches here: the guard above
        # already raises unless confirm_revise is True in that case.
        confirmation = "REVISE"
    else:
        confirmation = normalize_confirmation(
            prompt(
                "Type REVISE, CONFIRM, or UPDATE to write the revised "
                "content, or press Enter to cancel: "
            )
        )

    if confirmation not in {"REVISE", "CONFIRM", "UPDATE"}:
        print()
        print("REVISE cancelled. No database changes were made.")
        return None

    result = save_content(
        repository=repository,
        product=product,
        existing=existing,
        content=revised_content,
        approve=False,
        generation_method="MANUAL",
        review_notes=(
            "Revised via HUMAN_REVIEW (REVISE). Changed fields: "
            + ", ".join(field for field, _, _ in changes)
            + "."
        ),
    )

    # The content revision is already committed at this point. A failure
    # writing the audit trail must be surfaced clearly but must never be
    # reported as though the content write itself failed -- an operator
    # (or a caller checking the exit code) needs to know the revision
    # succeeded even if this best-effort log entry did not.
    try:
        write_revise_audit_log(
            repository=repository,
            product=product,
            changes=changes,
        )
    except Exception as error:
        print()
        print(
            "Warning: content revision was saved, but writing the "
            "process_logs audit entry failed."
        )
        print(f"Audit log error type: {type(error).__name__}")
        print(f"Audit log error details: {error}")

    print()
    print("=" * 78)
    print("PRODUCT CONTENT RESULT")
    print("=" * 78)
    print(f"Content ID: {result.get('product_content_id')}")
    print(f"Content status: {result.get('content_status')}")
    print(f"Review required: {result.get('review_required')}")
    print("Product content revision completed successfully.")

    return result


def get_candidate_for_product(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any]:
    """Return the exact product_candidates row linked to one internal product."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "candidate_code,"
            "candidate_type,"
            "extracted_title,"
            "verified_title,"
            "verified_isbn,"
            "possible_isbn,"
            "verified_author,"
            "verified_publisher,"
            "identity_status"
        )
        .eq("candidate_id", candidate_id)
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if len(rows) != 1:
        raise RuntimeError(
            "candidate_id did not resolve to exactly one product_candidates "
            f"row: {candidate_id}"
        )

    return rows[0]


def get_references_for_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Return every product_references row for one candidate."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id,"
            "candidate_id,"
            "source_type,"
            "source_url_id,"
            "match_decision,"
            "reference_title,"
            "reference_isbn,"
            "reference_author,"
            "reference_publisher,"
            "reference_description"
        )
        .eq("candidate_id", candidate_id)
        .execute()
    )

    return response.data or []


def excerpt(text: str, max_length: int) -> str:
    """Trim `text` to at most max_length characters, at a word boundary."""
    cleaned = clean_text(text) or ""

    if len(cleaned) <= max_length:
        return cleaned

    truncated = cleaned[:max_length].rsplit(" ", 1)[0].strip()
    return f"{truncated}…" if truncated else cleaned[:max_length]


def build_historical_enrichment_content(
    generated: dict[str, Any],
    product: dict[str, Any],
    reference_description: str,
    reference_source_type: str,
) -> dict[str, Any]:
    """
    Build enriched short/long/SEO description fields for an FB-HIST
    candidate's generic draft, from an already-verified, non-conflicting
    reference description (content_rules.select_historical_draft_safe_
    content_reference already confirmed eligibility -- this function only
    formats the text, it never re-decides eligibility).

    Reuses the exact reference_description text collected by collect_
    reference_metadata.py from an approved source -- never invents new
    prose. product_name, author_summary, and product_details are carried
    over unchanged from the existing safe draft (verified-metadata-only
    fields; enrichment only ever touches the descriptive/SEO fields).
    """
    title = clean_text(product.get("title")) or ""
    author = clean_text(product.get("author"))
    author_phrase = f" của {author}" if author else ""
    description_text = clean_text(reference_description) or ""

    enriched = dict(generated)
    enriched["short_description"] = (
        f"“{title}”{author_phrase} là ấn phẩm đang có tại Tiệm Sách Yêu Con. "
        + excerpt(description_text, _AUTO_ENRICH_EXCERPT_LENGTH)
    )
    enriched["long_description"] = (
        f"“{title}”{author_phrase} hiện có tại Tiệm Sách Yêu Con.\n\n"
        f"{description_text}\n\n"
        f"(Mô tả tham khảo từ nguồn {reference_source_type} đã được xác minh.)"
    )
    enriched["seo_description"] = (
        f"{title}"
        + (f" của {author}" if author else "")
        + " – "
        + excerpt(description_text, 120)
    )
    return enriched


def run_auto_revise_action(
    repository: SupabaseRepository,
    product_code: str | None,
    non_interactive: bool,
    confirm_revise: bool,
) -> dict[str, Any] | None:
    """
    Run --action AUTO_REVISE end to end: for an FB-HIST candidate whose
    content is still the generic metadata-only safe draft, deterministically
    enrich it from an already-verified, non-conflicting reference
    description, then run the exact same deterministic APPROVE validation
    as a human-triggered --action APPROVE would (attempt_content_approval)
    -- APPROVED if it passes, REVIEW_REQUIRED (never a crash) if it does
    not. Live (non-historical) candidates are refused outright.

    This never invents content: the enrichment source is always an
    existing reference_description already collected from an approved
    source (CLAUDE.md 2.2/15.1), and every validation APPROVE already
    enforces (generic-draft check, internal-boilerplate check) still runs
    before anything is marked APPROVED.
    """
    if not product_code:
        raise RuntimeError(
            "--action AUTO_REVISE requires --product-code (exact "
            "targeting only)."
        )

    if non_interactive and not confirm_revise:
        raise RuntimeError(
            "--non-interactive --action AUTO_REVISE requires "
            "--confirm-revise."
        )

    product, existing = get_content_for_exact_product(
        repository=repository,
        product_code=product_code,
    )
    candidate = get_candidate_for_product(
        repository=repository,
        candidate_id=product["candidate_id"],
    )

    if not is_historical_candidate_code(candidate.get("candidate_code")):
        raise RuntimeError(
            "--action AUTO_REVISE is only available for FB-HIST "
            "candidates under the historical draft-safe policy. "
            f"{candidate.get('candidate_code')!r} is a live-pipeline "
            "candidate -- use --action REVISE with a human-reviewed "
            "--content-file instead."
        )

    existing = validate_revise_target(existing)

    generated = build_safe_draft(product)

    if not is_generic_safe_draft(content=existing, generated=generated):
        raise RuntimeError(
            "AUTO_REVISE only applies when content is still the generic "
            "metadata-only safe draft. This content has already been "
            "edited -- use --action APPROVE, or --action REVISE with a "
            "human-reviewed --content-file, instead."
        )

    references = get_references_for_candidate(
        repository=repository,
        candidate_id=product["candidate_id"],
    )
    selection = content_rules.select_historical_draft_safe_content_reference(
        candidate=candidate,
        references=references,
    )

    if selection.outcome != Outcome.AUTO_PASS:
        raise RuntimeError(
            f"[{selection.rule_code}] No draft-safe reference description "
            f"is available for auto-enrichment: {selection.reason}"
        )

    enriched_content = build_historical_enrichment_content(
        generated=generated,
        product=product,
        reference_description=selection.evidence["reference_description"],
        reference_source_type=selection.evidence["source_type"],
    )

    print()
    print("=" * 78)
    print("AUTO-REVISE PREVIEW (historical draft-safe enrichment)")
    print("=" * 78)
    print(f"Product code: {product.get('product_code')}")
    print(f"Source reference: {selection.evidence['reference_id']}")
    print(f"Source type: {selection.evidence['source_type']}")

    result, declined_reason = attempt_content_approval(
        repository=repository,
        product=product,
        existing=existing,
        content=enriched_content,
        generated=generated,
        non_interactive=True,
    )

    try:
        repository.write_process_log(
            message=(
                "Historical draft-safe auto-enrichment applied to "
                f"{product.get('product_code')} from reference "
                f"{selection.evidence['reference_id']} "
                f"(source_type={selection.evidence['source_type']})."
            ),
            process_name="prepare_product_content",
            candidate_id=product.get("candidate_id"),
            process_step="AUTO_REVISE",
            log_level="INFO",
            status=(
                "APPROVED" if declined_reason is None else "REVIEW_REQUIRED"
            ),
        )
    except Exception as error:
        print()
        print(
            "Warning: auto-revision was saved, but writing the "
            "process_logs audit entry failed."
        )
        print(f"Audit log error type: {type(error).__name__}")
        print(f"Audit log error details: {error}")

    print()
    print("=" * 78)
    print("PRODUCT CONTENT RESULT")
    print("=" * 78)
    print(f"Content ID: {result.get('product_content_id')}")
    print(f"Content status: {result.get('content_status')}")
    print(f"Review required: {result.get('review_required')}")

    if declined_reason is not None:
        print(
            "Automatic content approval was declined "
            f"({declined_reason}); routed to REVIEW_REQUIRED for human "
            "review."
        )
    else:
        print("Historical draft-safe auto-enrichment approved.")

    return result


def validate_translation_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Validate an en/de --content-file payload against the translation
    schema. Only TRANSLATION_FILE_FIELDS may be present; text fields must
    be strings; facts_used must be a list of strings; generation_method
    must be a product_contents-allowed non-RULE_BASED method.
    """
    unknown_fields = sorted(set(payload) - TRANSLATION_FILE_FIELDS)

    if unknown_fields:
        raise RuntimeError(
            "Translation content file contains unsupported field(s): "
            + ", ".join(unknown_fields)
            + ". Allowed fields: "
            + ", ".join(sorted(TRANSLATION_FILE_FIELDS))
        )

    validated: dict[str, Any] = {}

    for field, value in payload.items():
        if field == "facts_used":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise RuntimeError(
                    "Translation content file field 'facts_used' must be a "
                    "list of strings."
                )
            validated[field] = value
            continue

        if value is None:
            continue

        if not isinstance(value, str):
            raise RuntimeError(
                f"Translation content file field {field!r} must be a "
                f"string, got {type(value).__name__}."
            )

        validated[field] = value

    method = validated.get("generation_method")
    if method is not None and method not in TRANSLATION_GENERATION_METHODS:
        raise RuntimeError(
            "Translation generation_method must be one of "
            + ", ".join(sorted(TRANSLATION_GENERATION_METHODS))
            + f", got {method!r}."
        )

    return validated


def require_approved_vietnamese_content(
    vi_content: dict[str, Any] | None,
    product_code: str,
) -> dict[str, Any]:
    """en/de content is a localization of the APPROVED vi row -- refuse
    when there is none (CLAUDE_AUTOMATION.md section 9.1)."""
    if (
        vi_content is None
        or vi_content.get("content_status") != ContentStatus.APPROVED
        or vi_content.get("review_required") is not False
    ):
        raise RuntimeError(
            "Translation refused: product "
            f"{product_code} has no APPROVED Vietnamese content "
            "(content_status=APPROVED, review_required=false). Vietnamese "
            "is the semantic source; approve it first."
        )

    return vi_content


def validate_translation_target(
    existing: dict[str, Any] | None,
    language: str,
    *,
    allow_missing: bool,
) -> dict[str, Any] | None:
    """Never touch an APPROVED or REJECTED translation row."""
    if existing is None:
        if allow_missing:
            return None
        raise RuntimeError(
            f"No {language!r} product_contents row exists. Run --action "
            "SAVE with --content-file first."
        )

    status = existing.get("content_status")

    if status == ContentStatus.APPROVED:
        raise RuntimeError(
            f"Refusing to modify APPROVED {language!r} content. Approved "
            "content must never be silently overwritten."
        )

    if status == ContentStatus.REJECTED:
        raise RuntimeError(f"Refusing to modify REJECTED {language!r} content.")

    if status not in (ContentStatus.DRAFTED, ContentStatus.REVIEW_REQUIRED):
        raise RuntimeError(
            f"{language!r} content requires content_status=DRAFTED or "
            f"REVIEW_REQUIRED, got {status!r}."
        )

    return existing


def build_translation_provenance(
    vi_content: dict[str, Any],
    product: dict[str, Any],
    validated_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CLAUDE_AUTOMATION.md 9.2 content_source/facts_used. product_contents
    has no dedicated columns, so this is serialized into review_notes."""
    payload = validated_payload or {}
    return {
        "content_source": payload.get("content_source")
        or f"product_contents:{vi_content.get('product_content_id')} (vi APPROVED)",
        "facts_used": payload.get("facts_used")
        or translation_rules.facts_used(vi_content, product),
    }


def evaluate_translation_for_product(
    repository: SupabaseRepository,
    product: dict[str, Any],
    language: str,
    translation: dict[str, Any],
    vi_content: dict[str, Any],
):
    """Run translation_rules.evaluate_translation() with the candidate's
    candidate_type (combo/individual distinction)."""
    candidate_type = None
    if product.get("candidate_id"):
        candidate_type = get_candidate_for_product(
            repository=repository,
            candidate_id=product["candidate_id"],
        ).get("candidate_type")

    return translation_rules.evaluate_translation(
        language=language,
        translation=translation,
        vi_content=vi_content,
        product=product,
        candidate_type=candidate_type,
    )


def save_translation_content(
    repository: SupabaseRepository,
    product: dict[str, Any],
    language: str,
    existing: dict[str, Any] | None,
    content: dict[str, Any],
    status: str,
    review_notes: str,
    generation_method: str,
) -> dict[str, Any]:
    """
    Insert or update exactly one en/de product_contents row.

    Unlike save_content(), this never writes internal_products:
    internal_products.content_status mirrors the Vietnamese row
    (audit_pipeline_state.py CONTENT_STATUS_MISMATCH), and a translation
    must never move it. The (internal_product_id, content_language) unique
    constraint is the final duplicate guard; the caller already resolved
    `existing` for this exact language.
    """
    now = utc_now()
    is_approved = status == ContentStatus.APPROVED

    payload = {
        **{
            field: content.get(field)
            for field in translation_rules.TRANSLATABLE_TEXT_FIELDS
        },
        "content_language": language,
        "content_status": status,
        "generation_method": generation_method,
        "generator_name": TRANSLATION_GENERATOR_NAME,
        "generator_version": TRANSLATION_GENERATOR_VERSION,
        "review_required": not is_approved,
        "review_notes": review_notes,
        "approved_at": now if is_approved else None,
        "updated_at": now,
    }

    table = repository.client.table("product_contents")

    if existing:
        response = (
            table.update(payload)
            .eq("product_content_id", existing["product_content_id"])
            .eq("content_language", language)
            .execute()
        )
    else:
        payload["internal_product_id"] = product["internal_product_id"]
        response = table.insert(payload).execute()

    rows = response.data or []

    if len(rows) != 1:
        raise RuntimeError(
            f"{language!r} product content write did not return exactly one row."
        )

    return rows[0]


def save_translation_draft(
    repository: SupabaseRepository,
    product: dict[str, Any],
    language: str,
    vi_content: dict[str, Any],
    existing: dict[str, Any] | None,
    translation: dict[str, Any],
    validated_payload: dict[str, Any],
) -> dict[str, Any]:
    """Translation SAVE: create, or update in place a DRAFTED/
    REVIEW_REQUIRED, row for this language as DRAFTED. Never touches an
    APPROVED/REJECTED row."""
    existing = validate_translation_target(existing, language, allow_missing=True)
    provenance = build_translation_provenance(vi_content, product, validated_payload)
    return save_translation_content(
        repository=repository,
        product=product,
        language=language,
        existing=existing,
        content=translation,
        status=ContentStatus.DRAFTED,
        review_notes=(
            "Translation draft saved from APPROVED vi content. "
            "provenance=" + json.dumps(provenance, ensure_ascii=False)
        ),
        generation_method=validated_payload.get("generation_method", "AI_ASSISTED"),
    )


def approve_translation(
    repository: SupabaseRepository,
    product: dict[str, Any],
    language: str,
    vi_content: dict[str, Any],
    existing: dict[str, Any] | None,
):
    """Translation APPROVE: deterministic translation_rules validation of
    the stored row -> APPROVED on pass, REVIEW_REQUIRED (this language
    only) on failure. Returns (row, decision)."""
    existing = validate_translation_target(existing, language, allow_missing=False)
    translation = {
        field: existing.get(field)
        for field in translation_rules.TRANSLATABLE_TEXT_FIELDS
    }
    decision = evaluate_translation_for_product(
        repository, product, language, translation, vi_content
    )
    provenance = build_translation_provenance(vi_content, product)
    provenance_note = "provenance=" + json.dumps(provenance, ensure_ascii=False)

    if decision.is_auto_pass:
        status = ContentStatus.APPROVED
        review_notes = (
            "Translation approved by deterministic validation. "
            + provenance_note
        )
    else:
        status = ContentStatus.REVIEW_REQUIRED
        review_notes = (
            f"CONTENT_REVIEW_REQUIRED ({language}): automatic approval "
            f"declined: [{decision.rule_code}] {decision.reason} "
            + provenance_note
        )

    result = save_translation_content(
        repository=repository,
        product=product,
        language=language,
        existing=existing,
        content=translation,
        status=status,
        review_notes=review_notes,
        generation_method=existing.get("generation_method") or "AI_ASSISTED",
    )
    return result, decision


def run_translation_action(
    repository: SupabaseRepository,
    action: str,
    language: str,
    product_code: str | None,
    content_file: str | None,
    non_interactive: bool,
) -> dict[str, Any] | None:
    """
    en/de content workflow (CLAUDE_AUTOMATION.md section 9).

    PREVIEW  validate --content-file against the APPROVED vi row; no write.
    SAVE     validate the file schema, then create (or update in place a
             DRAFTED/REVIEW_REQUIRED) row for this language as DRAFTED.
    APPROVE  run deterministic translation validation on the stored row:
             APPROVED on pass; on failure REVIEW_REQUIRED for this
             language only (never vi, never internal_products).
    SKIP     no-op.

    Always refuses without an APPROVED vi row, never touches an APPROVED
    or REJECTED translation, never writes internal_products.
    """
    if language not in translation_rules.TRANSLATION_LANGUAGES:
        raise RuntimeError(f"Unsupported translation language: {language!r}.")

    if action not in TRANSLATION_ACTIONS:
        raise RuntimeError(
            f"--action {action} is not supported with --content-language "
            f"{language}. Use PREVIEW, SAVE, APPROVE, or SKIP."
        )

    if not product_code:
        raise RuntimeError(
            f"--content-language {language} requires --product-code (exact "
            "targeting only)."
        )

    if action in {"SAVE", "APPROVE"} and not non_interactive:
        raise RuntimeError(
            f"--content-language {language} --action {action} requires "
            "--non-interactive."
        )

    if action in {"PREVIEW", "SAVE"} and not content_file:
        raise RuntimeError(
            f"--content-language {language} --action {action} requires "
            "--content-file."
        )

    if action == "SKIP":
        print("No database changes were made.")
        return None

    products = get_products(repository, product_code)

    if not products:
        raise RuntimeError(f"Internal product was not found: {product_code}")

    product = products[0]
    vi_content = require_approved_vietnamese_content(
        get_existing_content(repository, product["internal_product_id"], "vi"),
        product_code,
    )
    existing = get_existing_content(
        repository, product["internal_product_id"], language
    )

    print()
    print("=" * 78)
    print(f"TRANSLATION {action} ({language})")
    print("=" * 78)
    print(f"Product code: {product_code}")
    print(
        "Existing content status: "
        f"{existing.get('content_status') if existing else '[missing]'}"
    )

    if action in {"PREVIEW", "SAVE"}:
        validated_payload = validate_translation_payload(
            load_reviewer_content_file(Path(content_file))
        )
        translation = {
            field: validated_payload.get(field)
            for field in translation_rules.TRANSLATABLE_TEXT_FIELDS
        }
        decision = evaluate_translation_for_product(
            repository, product, language, translation, vi_content
        )
        print(
            "Deterministic validation: "
            f"{'PASS' if decision.is_auto_pass else 'FAIL'} -- {decision.reason}"
        )

        if action == "PREVIEW":
            print("No database changes were made.")
            return None

        result = save_translation_draft(
            repository=repository,
            product=product,
            language=language,
            vi_content=vi_content,
            existing=existing,
            translation=translation,
            validated_payload=validated_payload,
        )
    else:  # APPROVE
        result, decision = approve_translation(
            repository=repository,
            product=product,
            language=language,
            vi_content=vi_content,
            existing=existing,
        )
        print(
            "Deterministic validation: "
            f"{'PASS' if decision.is_auto_pass else 'FAIL'} -- {decision.reason}"
        )

    print(f"Content ID: {result.get('product_content_id')}")
    print(f"Content status: {result.get('content_status')}")
    print(f"Review required: {result.get('review_required')}")
    return result


def load_content_package(
    repository: SupabaseRepository,
    product_code: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], ContentPackage]:
    """Read-only: the product, all its product_contents rows, and its
    CLAUDE_AUTOMATION.md 9.2 package. Raises ContentPackageRefused
    (VI_CONTENT_NOT_APPROVED) for this candidate only."""
    products = get_products(repository, product_code)

    if not products:
        raise RuntimeError(f"Internal product was not found: {product_code}")

    product = products[0]
    candidate = (
        get_candidate_for_product(repository=repository, candidate_id=product["candidate_id"])
        if product.get("candidate_id")
        else {}
    )
    contents = (
        repository.client.table("product_contents")
        .select("*")
        .eq("internal_product_id", product["internal_product_id"])
        .execute()
        .data
        or []
    )
    package = build_content_package(candidate=candidate, product=product, contents=contents)
    return product, contents, package


def run_translate_action(
    repository: SupabaseRepository,
    product_code: str | None,
    provider: TranslationProvider,
    non_interactive: bool,
) -> dict[str, Any]:
    """
    Multilingual generation path (CLAUDE_AUTOMATION.md section 9.1):
    APPROVED vi package -> provider EN/DE -> deterministic cross-language
    consistency (src.domain.rules.multilingual_consistency) -> existing
    translation save path (DRAFTED) -> on consistency PASS, the existing
    translation APPROVE path (translation_rules; APPROVED or
    REVIEW_REQUIRED), on consistency FAIL, REVIEW_REQUIRED for that
    language with the failure evidence in review_notes.

    Never touches vi, internal_products, an APPROVED or a REJECTED row.
    """
    if not product_code:
        raise RuntimeError("--action TRANSLATE requires --product-code (exact targeting only).")

    if not non_interactive:
        raise RuntimeError("--action TRANSLATE requires --non-interactive.")

    product, contents, package = load_content_package(repository, product_code)
    vi_content = require_approved_vietnamese_content(
        next((c for c in contents if c.get("content_language") == "vi"), None),
        product_code,
    )

    # Resolve write targets before any provider call: skip APPROVED rows
    # (never overwritten), refuse REJECTED ones.
    targets: dict[str, dict[str, Any] | None] = {}
    for language in translation_rules.TRANSLATION_LANGUAGES:
        existing = next((c for c in contents if c.get("content_language") == language), None)
        if existing and existing.get("content_status") == ContentStatus.APPROVED:
            continue
        targets[language] = validate_translation_target(existing, language, allow_missing=True)

    report: dict[str, Any] = {
        "candidate_code": package.candidate_code,
        "product_code": product_code,
        "provider": None,
        "consistency": None,
        "reason_codes": [],
        "statuses": {},
    }

    if not targets:
        print("en and de content are already APPROVED. No database changes were made.")
        report["statuses"] = {"en": ContentStatus.APPROVED, "de": ContentStatus.APPROVED}
        return report

    result = provider.translate(package)
    translated = package
    for language, fields in result.translations.items():
        translated = translated.with_translation(
            language,
            product_title=fields.get("product_name"),
            short_description=fields.get("short_description"),
            description=fields.get("long_description"),
        )

    consistency = multilingual_consistency.evaluate_package(translated)
    report["provider"] = result.provenance()
    report["consistency"] = consistency.status
    report["reason_codes"] = list(consistency.reason_codes)

    print()
    print("=" * 78)
    print("MULTILINGUAL TRANSLATE (en, de)")
    print("=" * 78)
    print(f"Product code: {product_code}")
    print(f"Provider: {result.provider}")
    print(f"Cross-language consistency: {consistency.status}")
    for failure in consistency.failures:
        print(f"  - {failure}")

    payload = {
        "generation_method": result.generation_method,
        "content_source": (
            f"{package.content_source}; provider={result.provider}"
            + (f"; model={result.model}" if result.model else "")
        ),
        "facts_used": list(package.facts_used),
    }

    for language, existing in targets.items():
        translation = {
            field: None for field in translation_rules.TRANSLATABLE_TEXT_FIELDS
        }
        translation.update(result.translations.get(language) or {})

        row = save_translation_draft(
            repository=repository,
            product=product,
            language=language,
            vi_content=vi_content,
            existing=existing,
            translation=translation,
            validated_payload=payload,
        )

        if consistency.language_passed(language):
            row, decision = approve_translation(
                repository=repository,
                product=product,
                language=language,
                vi_content=vi_content,
                existing=row,
            )
            print(
                f"{language}: translation validation "
                f"{'PASS' if decision.is_auto_pass else 'FAIL'} -- {decision.reason}"
            )
        else:
            details = [f for f in consistency.failures if f.startswith(f"[{language}]")]
            provenance = build_translation_provenance(vi_content, product, payload)
            row = save_translation_content(
                repository=repository,
                product=product,
                language=language,
                existing=row,
                content=translation,
                status=ContentStatus.REVIEW_REQUIRED,
                review_notes=(
                    f"CONTENT_REVIEW_REQUIRED ({language}): cross-language "
                    f"consistency failed: [{multilingual_consistency.RULE_CODE}] "
                    + "; ".join(details)
                    + " provenance="
                    + json.dumps(provenance, ensure_ascii=False)
                ),
                generation_method=result.generation_method,
            )

        report["statuses"][language] = row.get("content_status")
        print(f"{language}: content status {row.get('content_status')}")

    return report


def run_export_package_action(
    repository: SupabaseRepository,
    product_code: str | None,
    directory: Path = DEFAULT_PACKAGE_DIR,
) -> Path:
    """Read-only against the database: write this candidate's package
    (APPROVED vi side, current en/de side) to
    data/processed/content_packages/<candidate_code>.json for the
    package-file translation provider. Never overwrites an existing
    package file."""
    if not product_code:
        raise RuntimeError("--action EXPORT_PACKAGE requires --product-code.")

    _product, _contents, package = load_content_package(repository, product_code)
    path = package_file_path(package.candidate_code, Path(directory))

    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing content package {path}.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(package.to_json() + "\n", encoding="utf-8")
    print(f"Content package written: {path}")
    print("No database changes were made.")
    return path


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
        "Type PREVIEW, SAVE, APPROVE, REVISE, AUTO_REVISE, or SKIP: "
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

    action = resolve_action(args)

    if action not in VALID_ACTIONS:
        print(
            "Invalid action. Use PREVIEW, SAVE, APPROVE, REVISE, "
            "AUTO_REVISE, or SKIP."
        )
        return

    if action == "TRANSLATE":
        run_translate_action(
            repository=repository,
            product_code=args.product_code,
            provider=get_translation_provider(args.translation_provider),
            non_interactive=args.non_interactive,
        )
        return

    if action == "EXPORT_PACKAGE":
        run_export_package_action(
            repository=repository,
            product_code=args.product_code,
        )
        return

    content_language = getattr(args, "content_language", "vi") or "vi"

    if content_language != "vi":
        run_translation_action(
            repository=repository,
            action=action,
            language=content_language,
            product_code=args.product_code,
            content_file=args.content_file,
            non_interactive=args.non_interactive,
        )
        return

    if action == "REVISE":
        run_revise_action(
            repository=repository,
            product_code=args.product_code,
            content_file=args.content_file,
            non_interactive=args.non_interactive,
            confirm_revise=args.confirm_revise,
        )
        return

    if action == "AUTO_REVISE":
        run_auto_revise_action(
            repository=repository,
            product_code=args.product_code,
            non_interactive=args.non_interactive,
            confirm_revise=args.confirm_revise,
        )
        return

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

    if action in {"PREVIEW", "SKIP"}:
        print("No database changes were made.")
        return

    if action == "APPROVE":
        result, declined_reason = attempt_content_approval(
            repository=repository,
            product=product,
            existing=existing,
            content=content,
            generated=generated,
            non_interactive=args.non_interactive,
        )

        if declined_reason is not None:
            print()
            print("=" * 78)
            print("PRODUCT CONTENT RESULT")
            print("=" * 78)
            print(f"Content ID: {result.get('product_content_id')}")
            print(f"Content status: {result.get('content_status')}")
            print(f"Review required: {result.get('review_required')}")
            print(
                "Automatic content approval was declined "
                f"({declined_reason}); routed to REVIEW_REQUIRED for "
                "human review."
            )
            return
    else:
        result = save_content(
            repository=repository,
            product=product,
            existing=existing,
            content=content,
            approve=False,
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
