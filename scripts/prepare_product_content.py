import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


GENERATOR_NAME = "internal_product_content_generator"
GENERATOR_VERSION = "1.4.0"
VALID_ACTIONS = {"PREVIEW", "SAVE", "APPROVE", "REVISE", "SKIP"}

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
        help="PREVIEW, SAVE, APPROVE, REVISE, or SKIP.",
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
    """Reject approval when the content is still an untouched safe draft."""
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
) -> dict[str, Any]:
    """
    Insert or update one Vietnamese content record atomically as possible.

    generation_method and review_notes are optional overrides used by
    --action REVISE to record that a write came from human review rather
    than the rule-based generator. Callers that omit them (the original
    SAVE/APPROVE flow) get the original inferred values, unchanged.
    """
    now = utc_now()
    status = "APPROVED" if approve else "DRAFTED"

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
        "review_required": not approve,
        "review_notes": (
            review_notes
            if review_notes is not None
            else (
                "Content reviewed and approved for WooCommerce draft."
                if approve
                else (
                    "Draft saved. Review the wording, product facts, and "
                    "topic before WooCommerce draft creation."
                )
            )
        ),
        "approved_at": now if approve else None,
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

    REVISE only ever updates an existing DRAFTED row. It never creates a
    second content row, and it never touches APPROVED or REJECTED content.
    """
    if existing is None:
        raise RuntimeError(
            "REVISE requires an existing product_contents row. None was "
            "found for this product -- run --action SAVE first to create "
            "the initial draft; REVISE never creates a new content row."
        )

    status = existing.get("content_status")

    if status == "APPROVED":
        raise RuntimeError(
            "REVISE refuses to modify APPROVED content. Approved content "
            "must never be silently overwritten."
        )

    if status == "REJECTED":
        raise RuntimeError(
            "REVISE refuses to modify REJECTED content."
        )

    if status != "DRAFTED":
        raise RuntimeError(
            f"REVISE requires content_status=DRAFTED, got {status!r}."
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
        "Type PREVIEW, SAVE, APPROVE, REVISE, or SKIP: "
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
            "Invalid action. Use PREVIEW, SAVE, APPROVE, REVISE, or SKIP."
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
        validate_approval_content(
            existing=existing,
            content=content,
            generated=generated,
        )

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
