"""
Read-only integrity audit for the TSYC automation pipeline.

The script does not modify Supabase or WooCommerce.
It checks cross-table state consistency before unattended automation is enabled.

Examples:
    python scripts/audit_pipeline_state.py
    python scripts/audit_pipeline_state.py --product-code TSYC-FB-2026-001-CAN-0003
    python scripts/audit_pipeline_state.py --candidate-code FB-2026-001-CAN-0003
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


SCRIPT_VERSION = "1.0.1"

# Must exactly match product_images_publish_eligibility_check
# (migrations/009_add_product_image_review_guards.sql): is_publish_eligible
# may only be true when usage_rights_status is one of these three values.
PUBLISHABLE_RIGHTS = {
    "STORE_OWNED",
    "PUBLISHER_APPROVED",
    "SUPPLIER_APPROVED",
}

FETCH_PAGE_SIZE = 200
FETCH_MAX_RETRIES = 4
FETCH_RETRY_SECONDS = 1.5


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run a read-only TSYC pipeline integrity audit."
    )

    selector_group = parser.add_mutually_exclusive_group()

    selector_group.add_argument(
        "--product-code",
        help="Audit one exact internal product code.",
    )

    selector_group.add_argument(
        "--candidate-code",
        help="Audit one exact candidate code.",
    )

    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit with code 2 when warnings are found.",
    )

    return parser.parse_args()


def fetch_rows(
    repository: SupabaseRepository,
    table_name: str,
    columns: str = "*",
) -> list[dict[str, Any]]:
    """
    Fetch a Supabase table in small pages with retry protection.

    Loading large tables with one select("*") request can cause HTTP/2
    RemoteProtocolError / "Server disconnected" failures. Pagination keeps
    each PostgREST response small and retryable.
    """
    all_rows: list[dict[str, Any]] = []
    start_index = 0

    print(
        f"Loading {table_name}..."
    )

    while True:
        end_index = (
            start_index
            + FETCH_PAGE_SIZE
            - 1
        )

        last_error: Exception | None = None
        page_rows: list[dict[str, Any]] | None = None

        for attempt in range(
            1,
            FETCH_MAX_RETRIES + 1,
        ):
            try:
                response = (
                    repository.client
                    .table(table_name)
                    .select(columns)
                    .range(
                        start_index,
                        end_index,
                    )
                    .execute()
                )

                page_rows = response.data or []
                last_error = None
                break

            except Exception as error:
                last_error = error

                print(
                    f"  Request failed for rows "
                    f"{start_index}-{end_index} "
                    f"(attempt {attempt}/{FETCH_MAX_RETRIES}): "
                    f"{type(error).__name__}: {error}"
                )

                if attempt < FETCH_MAX_RETRIES:
                    time.sleep(
                        FETCH_RETRY_SECONDS * attempt
                    )

        if last_error is not None:
            raise RuntimeError(
                f"Unable to load table {table_name} after "
                f"{FETCH_MAX_RETRIES} attempts. "
                f"Last error: {type(last_error).__name__}: {last_error}"
            ) from last_error

        if page_rows is None:
            raise RuntimeError(
                f"Unexpected empty page state while loading {table_name}."
            )

        all_rows.extend(
            page_rows
        )

        print(
            f"  Loaded: {len(all_rows)} rows"
        )

        if len(page_rows) < FETCH_PAGE_SIZE:
            break

        start_index += FETCH_PAGE_SIZE

    return all_rows



def select_scope(
    products: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    product_code: str | None,
    candidate_code: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Limit products and candidates to the requested exact selector."""
    if product_code:
        selected_products = [
            product
            for product in products
            if product.get("product_code") == product_code
        ]

        if len(selected_products) != 1:
            raise RuntimeError(
                "Product selector did not resolve to exactly one internal product."
            )

        candidate_id = selected_products[0].get(
            "candidate_id"
        )

        selected_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("candidate_id") == candidate_id
        ]

        return (
            selected_products,
            selected_candidates,
        )

    if candidate_code:
        selected_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("candidate_code") == candidate_code
        ]

        if len(selected_candidates) != 1:
            raise RuntimeError(
                "Candidate selector did not resolve to exactly one product candidate."
            )

        candidate_id = selected_candidates[0].get(
            "candidate_id"
        )

        selected_products = [
            product
            for product in products
            if product.get("candidate_id") == candidate_id
        ]

        return (
            selected_products,
            selected_candidates,
        )

    return products, candidates


def add_issue(
    issues: list[dict[str, str]],
    severity: str,
    entity: str,
    code: str,
    message: str,
) -> None:
    """Append one normalized audit issue."""
    issues.append(
        {
            "severity": severity,
            "entity": entity,
            "code": code,
            "message": message,
        }
    )


def audit_candidate_uniqueness(
    candidates: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check candidate code and ID uniqueness."""
    for field in (
        "candidate_id",
        "candidate_code",
    ):
        counts = Counter(
            str(row.get(field))
            for row in candidates
            if row.get(field)
        )

        for value, count in counts.items():
            if count > 1:
                add_issue(
                    issues,
                    "ERROR",
                    value,
                    f"DUPLICATE_{field.upper()}",
                    f"{field} appears {count} times.",
                )


def audit_product_uniqueness(
    products: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check internal product uniqueness."""
    for field in (
        "internal_product_id",
        "product_code",
    ):
        counts = Counter(
            str(row.get(field))
            for row in products
            if row.get(field)
        )

        for value, count in counts.items():
            if count > 1:
                add_issue(
                    issues,
                    "ERROR",
                    value,
                    f"DUPLICATE_{field.upper()}",
                    f"{field} appears {count} times.",
                )

    candidate_counts = Counter(
        str(row.get("candidate_id"))
        for row in products
        if row.get("candidate_id")
    )

    for candidate_id, count in candidate_counts.items():
        if count > 1:
            add_issue(
                issues,
                "ERROR",
                candidate_id,
                "MULTIPLE_INTERNAL_PRODUCTS_PER_CANDIDATE",
                f"Candidate has {count} internal products.",
            )


def audit_candidate_product_linkage(
    products: list[dict[str, Any]],
    candidates_by_id: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check product-to-candidate linkage and identity state."""
    for product in products:
        product_code = str(
            product.get("product_code")
            or product.get("internal_product_id")
        )

        candidate_id = product.get(
            "candidate_id"
        )

        if not candidate_id:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "MISSING_CANDIDATE_ID",
                "Internal product has no candidate_id.",
            )
            continue

        candidate = candidates_by_id.get(
            str(candidate_id)
        )

        if not candidate:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "CANDIDATE_NOT_FOUND",
                "Linked product candidate was not found.",
            )
            continue

        if candidate.get(
            "identity_status"
        ) != "IDENTITY_VERIFIED":
            add_issue(
                issues,
                "ERROR",
                product_code,
                "IDENTITY_NOT_VERIFIED",
                "Internal product exists while candidate identity is not verified.",
            )

        if not product.get("isbn"):
            add_issue(
                issues,
                "WARNING",
                product_code,
                "ISBN_MISSING",
                "ISBN is missing. This does not block WooCommerce draft creation.",
            )

        if product.get("weight_grams") in (
            None,
            "",
        ):
            add_issue(
                issues,
                "WARNING",
                product_code,
                "WEIGHT_MISSING",
                "Weight is missing. This does not block WooCommerce draft creation.",
            )


def audit_content(
    products: list[dict[str, Any]],
    contents: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check content record cardinality and status synchronization."""
    content_by_product: dict[str, list[dict[str, Any]]] = {}

    for content in contents:
        product_id = str(
            content.get("internal_product_id")
        )

        content_by_product.setdefault(
            product_id,
            [],
        ).append(content)

    for product in products:
        product_id = str(
            product.get("internal_product_id")
        )
        product_code = str(
            product.get("product_code")
            or product_id
        )

        vi_rows = [
            row
            for row in content_by_product.get(
                product_id,
                [],
            )
            if row.get("content_language") == "vi"
        ]

        if len(vi_rows) > 1:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "MULTIPLE_VI_CONTENT_ROWS",
                f"Product has {len(vi_rows)} Vietnamese content rows.",
            )

        if not vi_rows:
            if product.get(
                "content_status"
            ) == "APPROVED":
                add_issue(
                    issues,
                    "ERROR",
                    product_code,
                    "APPROVED_CONTENT_ROW_MISSING",
                    "Internal product says APPROVED but no Vietnamese content exists.",
                )
            continue

        content = vi_rows[0]
        content_status = content.get(
            "content_status"
        )
        product_status = product.get(
            "content_status"
        )

        if content_status != product_status:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "CONTENT_STATUS_MISMATCH",
                (
                    "product_contents.content_status="
                    f"{content_status}; internal_products.content_status="
                    f"{product_status}."
                ),
            )

        if content_status == "APPROVED":
            if content.get(
                "review_required"
            ) is True:
                add_issue(
                    issues,
                    "ERROR",
                    product_code,
                    "APPROVED_CONTENT_REQUIRES_REVIEW",
                    "Approved content still has review_required=True.",
                )

            if not content.get(
                "approved_at"
            ):
                add_issue(
                    issues,
                    "WARNING",
                    product_code,
                    "APPROVED_AT_MISSING",
                    "Approved content has no approved_at timestamp.",
                )


def audit_images(
    products: list[dict[str, Any]],
    images: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check image linkage, main-image uniqueness, rights, and product status."""
    images_by_candidate: dict[str, list[dict[str, Any]]] = {}

    for image in images:
        candidate_id = image.get(
            "candidate_id"
        )

        if not candidate_id:
            add_issue(
                issues,
                "ERROR",
                str(image.get("image_id")),
                "IMAGE_NOT_LINKED_TO_CANDIDATE",
                "Product image has candidate_id=NULL.",
            )
            continue

        images_by_candidate.setdefault(
            str(candidate_id),
            [],
        ).append(image)

    for product in products:
        candidate_id = str(
            product.get("candidate_id")
        )

        product_code = str(
            product.get("product_code")
            or product.get("internal_product_id")
        )

        candidate_images = images_by_candidate.get(
            candidate_id,
            [],
        )

        selected = [
            image
            for image in candidate_images
            if image.get(
                "is_selected_main_image"
            ) is True
        ]

        approved_main = [
            image
            for image in selected
            if image.get("image_status") == "VALIDATED"
            and image.get("is_publish_eligible") is True
            and image.get(
                "usage_rights_status"
            ) in PUBLISHABLE_RIGHTS
        ]

        if len(selected) > 1:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "MULTIPLE_SELECTED_MAIN_IMAGES",
                f"{len(selected)} images are selected as main.",
            )

        if len(approved_main) > 1:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "MULTIPLE_PUBLISHABLE_MAIN_IMAGES",
                f"{len(approved_main)} selected main images are publishable.",
            )

        if product.get(
            "image_status"
        ) == "APPROVED":
            if len(approved_main) != 1:
                add_issue(
                    issues,
                    "ERROR",
                    product_code,
                    "IMAGE_STATUS_APPROVED_WITHOUT_VALID_MAIN",
                    (
                        "Internal product image_status=APPROVED but exactly one "
                        "validated publishable main image does not exist."
                    ),
                )

        elif len(approved_main) == 1:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "VALID_MAIN_WITH_PRODUCT_STATUS_NOT_APPROVED",
                (
                    "A valid publishable main image exists but internal product "
                    f"image_status={product.get('image_status')}."
                ),
            )


def audit_references(
    candidates: list[dict[str, Any]],
    references: list[dict[str, Any]],
    products: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check identity match and primary-reference linkage."""
    refs_by_id = {
        str(reference.get("reference_id")): reference
        for reference in references
        if reference.get("reference_id")
    }

    refs_by_candidate: dict[str, list[dict[str, Any]]] = {}

    for reference in references:
        candidate_id = reference.get(
            "candidate_id"
        )

        if candidate_id:
            refs_by_candidate.setdefault(
                str(candidate_id),
                [],
            ).append(reference)

    for candidate in candidates:
        candidate_id = str(
            candidate.get("candidate_id")
        )
        candidate_code = str(
            candidate.get("candidate_code")
            or candidate_id
        )

        if candidate.get(
            "identity_status"
        ) == "IDENTITY_VERIFIED":
            matched_refs = [
                ref
                for ref in refs_by_candidate.get(
                    candidate_id,
                    [],
                )
                if ref.get("match_decision") == "MATCH"
            ]

            if not matched_refs:
                add_issue(
                    issues,
                    "ERROR",
                    candidate_code,
                    "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE",
                    "Candidate is IDENTITY_VERIFIED but has no MATCH reference.",
                )

    for product in products:
        reference_id = product.get(
            "primary_reference_id"
        )

        product_code = str(
            product.get("product_code")
            or product.get("internal_product_id")
        )

        if not reference_id:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "PRIMARY_REFERENCE_MISSING",
                "Internal product has no primary_reference_id.",
            )
            continue

        reference = refs_by_id.get(
            str(reference_id)
        )

        if not reference:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "PRIMARY_REFERENCE_NOT_FOUND",
                "Primary product reference record was not found.",
            )
            continue

        if str(
            reference.get("candidate_id")
        ) != str(
            product.get("candidate_id")
        ):
            add_issue(
                issues,
                "ERROR",
                product_code,
                "PRIMARY_REFERENCE_CANDIDATE_MISMATCH",
                "Primary reference belongs to a different candidate.",
            )

        if reference.get(
            "match_decision"
        ) != "MATCH":
            add_issue(
                issues,
                "ERROR",
                product_code,
                "PRIMARY_REFERENCE_NOT_MATCHED",
                "Primary reference does not have match_decision=MATCH.",
            )


def audit_woocommerce(
    products: list[dict[str, Any]],
    syncs: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check local WooCommerce synchronization consistency."""
    syncs_by_product: dict[str, list[dict[str, Any]]] = {}

    for sync in syncs:
        internal_product_id = sync.get(
            "internal_product_id"
        )

        if internal_product_id:
            syncs_by_product.setdefault(
                str(internal_product_id),
                [],
            ).append(sync)

    for product in products:
        product_id = str(
            product.get("internal_product_id")
        )

        product_code = str(
            product.get("product_code")
            or product_id
        )

        product_syncs = syncs_by_product.get(
            product_id,
            [],
        )

        syncs_with_remote_id = [
            sync
            for sync in product_syncs
            if sync.get("woocommerce_product_id")
        ]

        if len(syncs_with_remote_id) > 1:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "MULTIPLE_REMOTE_WOO_IDS",
                (
                    "More than one synchronization record has a WooCommerce "
                    "product ID for this internal product."
                ),
            )

        if product.get(
            "woocommerce_status"
        ) == "DRAFT_CREATED":
            if len(syncs_with_remote_id) != 1:
                add_issue(
                    issues,
                    "ERROR",
                    product_code,
                    "DRAFT_CREATED_WITHOUT_SINGLE_REMOTE_ID",
                    (
                        "Internal product is DRAFT_CREATED but does not have "
                        "exactly one local sync with a WooCommerce product ID."
                    ),
                )

        if syncs_with_remote_id and product.get(
            "woocommerce_status"
        ) != "DRAFT_CREATED":
            add_issue(
                issues,
                "ERROR",
                product_code,
                "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH",
                (
                    "A WooCommerce product ID exists locally but internal "
                    f"product status is {product.get('woocommerce_status')}."
                ),
            )

        for sync in product_syncs:
            response_payload = sync.get(
                "response_payload"
            )

            if isinstance(
                response_payload,
                dict,
            ) and response_payload.get(
                "recovery_required"
            ) is True:
                add_issue(
                    issues,
                    "ERROR",
                    product_code,
                    "WOO_RECOVERY_REQUIRED",
                    (
                        "WooCommerce draft may already exist remotely. "
                        "Do not run draft creation again until reconciliation."
                    ),
                )


def audit_ready_for_draft(
    products: list[dict[str, Any]],
    contents: list[dict[str, Any]],
    images: list[dict[str, Any]],
    candidates_by_id: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Check products marked READY_FOR_DRAFT against the critical gates."""
    approved_content_products = {
        str(content.get("internal_product_id"))
        for content in contents
        if content.get("content_language") == "vi"
        and content.get("content_status") == "APPROVED"
        and content.get("review_required") is False
    }

    images_by_candidate: dict[str, list[dict[str, Any]]] = {}

    for image in images:
        candidate_id = image.get(
            "candidate_id"
        )

        if candidate_id:
            images_by_candidate.setdefault(
                str(candidate_id),
                [],
            ).append(image)

    for product in products:
        if product.get(
            "woocommerce_status"
        ) != "READY_FOR_DRAFT":
            continue

        product_id = str(
            product.get("internal_product_id")
        )
        product_code = str(
            product.get("product_code")
            or product_id
        )
        candidate_id = str(
            product.get("candidate_id")
        )

        candidate = candidates_by_id.get(
            candidate_id
        )

        blockers: list[str] = []

        if not candidate or candidate.get(
            "identity_status"
        ) != "IDENTITY_VERIFIED":
            blockers.append(
                "identity is not verified"
            )

        if product_id not in approved_content_products:
            blockers.append(
                "approved Vietnamese content is missing"
            )

        valid_main = [
            image
            for image in images_by_candidate.get(
                candidate_id,
                [],
            )
            if image.get("image_status") == "VALIDATED"
            and image.get("is_selected_main_image") is True
            and image.get("is_publish_eligible") is True
            and image.get(
                "usage_rights_status"
            ) in PUBLISHABLE_RIGHTS
        ]

        if len(valid_main) != 1:
            blockers.append(
                "exactly one publishable selected main image is required"
            )

        if blockers:
            add_issue(
                issues,
                "ERROR",
                product_code,
                "INVALID_READY_FOR_DRAFT_STATE",
                "; ".join(blockers),
            )


def print_summary(
    products: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    references: list[dict[str, Any]],
    contents: list[dict[str, Any]],
    images: list[dict[str, Any]],
    syncs: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Print audit counts and detailed issues."""
    error_count = sum(
        issue["severity"] == "ERROR"
        for issue in issues
    )
    warning_count = sum(
        issue["severity"] == "WARNING"
        for issue in issues
    )

    print()
    print("=" * 78)
    print("TSYC PIPELINE INTEGRITY AUDIT")
    print("=" * 78)
    print(f"Version: {SCRIPT_VERSION}")
    print(f"Candidates checked: {len(candidates)}")
    print(f"Internal products checked: {len(products)}")
    print(f"References loaded: {len(references)}")
    print(f"Content rows loaded: {len(contents)}")
    print(f"Images loaded: {len(images)}")
    print(f"Woo sync rows loaded: {len(syncs)}")
    print(f"Errors: {error_count}")
    print(f"Warnings: {warning_count}")

    if not issues:
        print()
        print("Result: PASS")
        print(
            "No cross-table integrity issues were detected in the selected scope."
        )
        return

    print()
    print("-" * 78)
    print("ISSUES")
    print("-" * 78)

    for index, issue in enumerate(
        issues,
        start=1,
    ):
        print()
        print(
            f"[{index}] {issue['severity']} | {issue['code']}"
        )
        print(
            f"Entity: {issue['entity']}"
        )
        print(
            f"Details: {issue['message']}"
        )

    print()
    print(
        "Result: FAIL"
        if error_count
        else "Result: PASS_WITH_WARNINGS"
    )


def main() -> int:
    """Run a read-only cross-table audit."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )
    args = parse_arguments()

    repository = SupabaseRepository()

    candidates = fetch_rows(
        repository,
        "product_candidates",
    )
    products = fetch_rows(
        repository,
        "internal_products",
    )
    references = fetch_rows(
        repository,
        "product_references",
    )
    contents = fetch_rows(
        repository,
        "product_contents",
    )
    images = fetch_rows(
        repository,
        "product_images",
    )
    syncs = fetch_rows(
        repository,
        "woocommerce_product_syncs",
    )

    products, candidates = select_scope(
        products=products,
        candidates=candidates,
        product_code=args.product_code,
        candidate_code=args.candidate_code,
    )

    scoped_candidate_ids = {
        str(candidate.get("candidate_id"))
        for candidate in candidates
        if candidate.get("candidate_id")
    }

    scoped_product_ids = {
        str(product.get("internal_product_id"))
        for product in products
        if product.get("internal_product_id")
    }

    if args.product_code or args.candidate_code:
        references = [
            reference
            for reference in references
            if str(reference.get("candidate_id"))
            in scoped_candidate_ids
        ]

        images = [
            image
            for image in images
            if str(image.get("candidate_id"))
            in scoped_candidate_ids
        ]

        contents = [
            content
            for content in contents
            if str(content.get("internal_product_id"))
            in scoped_product_ids
        ]

        syncs = [
            sync
            for sync in syncs
            if str(sync.get("internal_product_id"))
            in scoped_product_ids
        ]

    candidates_by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in candidates
        if candidate.get("candidate_id")
    }

    issues: list[dict[str, str]] = []

    audit_candidate_uniqueness(
        candidates,
        issues,
    )
    audit_product_uniqueness(
        products,
        issues,
    )
    audit_candidate_product_linkage(
        products,
        candidates_by_id,
        issues,
    )
    audit_references(
        candidates,
        references,
        products,
        issues,
    )
    audit_content(
        products,
        contents,
        issues,
    )
    audit_images(
        products,
        images,
        issues,
    )
    audit_woocommerce(
        products,
        syncs,
        issues,
    )
    audit_ready_for_draft(
        products,
        contents,
        images,
        candidates_by_id,
        issues,
    )

    print_summary(
        products=products,
        candidates=candidates,
        references=references,
        contents=contents,
        images=images,
        syncs=syncs,
        issues=issues,
    )

    error_count = sum(
        issue["severity"] == "ERROR"
        for issue in issues
    )

    warning_count = sum(
        issue["severity"] == "WARNING"
        for issue in issues
    )

    if error_count:
        return 1

    if warning_count and args.fail_on_warning:
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
