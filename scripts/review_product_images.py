"""
Review and validate product images for the TSYC automation pipeline.

This script:
- selects one product candidate explicitly;
- lists all images linked to that candidate;
- assigns image role and usage-rights status;
- selects exactly one main image;
- validates publish eligibility;
- synchronizes internal_products.image_status;
- never uploads, deletes, or publishes WooCommerce media.

Designed for safe manual use and controlled non-interactive automation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from src.supabase_repository import SupabaseRepository


SCRIPT_VERSION = "1.0.0"

VALID_IMAGE_ROLES = {
    "FRONT_COVER",
    "BACK_COVER",
    "INSIDE_PAGE",
    "COMBO_IMAGE",
    "LIFESTYLE",
    "OTHER",
}

VALID_RIGHTS_STATUSES = {
    "STORE_OWNED",
    "PUBLISHER_AUTHORIZED",
    "SUPPLIER_AUTHORIZED",
    "LICENSED",
    "RIGHTS_UNKNOWN",
    "DO_NOT_USE",
}

PUBLISHABLE_RIGHTS_STATUSES = {
    "STORE_OWNED",
    "PUBLISHER_AUTHORIZED",
    "SUPPLIER_AUTHORIZED",
    "LICENSED",
}


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Review, validate, and select publishable product images "
            "for one TSYC product candidate."
        )
    )

    candidate_group = parser.add_mutually_exclusive_group(
        required=True
    )
    candidate_group.add_argument(
        "--candidate-code",
        help="Exact product candidate code.",
    )
    candidate_group.add_argument(
        "--candidate-id",
        help="Exact product candidate UUID.",
    )

    parser.add_argument(
        "--main-image-id",
        help=(
            "Exact image_id to select as the main image. "
            "Required for --approve in non-interactive mode."
        ),
    )

    parser.add_argument(
        "--main-role",
        choices=sorted(VALID_IMAGE_ROLES),
        default="FRONT_COVER",
        help="Role assigned to the selected main image.",
    )

    parser.add_argument(
        "--rights-status",
        choices=sorted(VALID_RIGHTS_STATUSES),
        help=(
            "Usage-rights status assigned to the selected main image. "
            "Required for approval."
        ),
    )

    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "Validate the selected main image and mark it publish eligible."
        ),
    )

    parser.add_argument(
        "--reject-image-id",
        action="append",
        default=[],
        help=(
            "Image ID to mark as rejected/non-publishable. "
            "Repeat to reject multiple images."
        ),
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable prompts. Requires explicit main image and rights "
            "when --approve is used."
        ),
    )

    parser.add_argument(
        "--confirm-approve",
        action="store_true",
        help=(
            "Explicit confirmation for image approval. "
            "Required with --non-interactive --approve."
        ),
    )

    return parser.parse_args()


def normalize_confirmation(value: str | None) -> str:
    """Normalize manual confirmation input."""
    return str(value or "").strip().upper()


def get_candidate(
    repository: SupabaseRepository,
    candidate_code: str | None,
    candidate_id: str | None,
) -> dict[str, Any]:
    """Return exactly one candidate."""
    query = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "candidate_code,"
            "candidate_type,"
            "extracted_title,"
            "verified_title,"
            "identity_status,"
            "workflow_status"
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

    response = query.limit(2).execute()
    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No product candidate matched the supplied selector."
        )

    if len(rows) != 1:
        raise RuntimeError(
            "Candidate selector did not resolve to exactly one record."
        )

    return rows[0]


def get_candidate_images(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Return all images linked to one candidate."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id,"
            "candidate_id,"
            "raw_page_id,"
            "reference_id,"
            "image_role,"
            "usage_rights_status,"
            "image_status,"
            "is_main_image_candidate,"
            "is_selected_main_image,"
            "is_publish_eligible,"
            "storage_bucket,"
            "storage_path,"
            "source_image_url,"
            "width_px,"
            "height_px,"
            "image_hash,"
            "created_at,"
            "updated_at"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    return response.data or []


def get_internal_product(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Return the internal product linked to one candidate, if it exists."""
    response = (
        repository.client
        .table("internal_products")
        .select(
            "internal_product_id,"
            "product_code,"
            "candidate_id,"
            "image_status,"
            "woocommerce_status"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if len(rows) > 1:
        raise RuntimeError(
            "More than one internal product is linked to the candidate."
        )

    return rows[0] if rows else None


def find_image(
    images: list[dict[str, Any]],
    image_id: str,
) -> dict[str, Any]:
    """Find one candidate image by exact image ID."""
    matches = [
        image
        for image in images
        if str(image.get("image_id")) == str(image_id)
    ]

    if not matches:
        raise RuntimeError(
            "The selected image ID is not linked to this candidate."
        )

    if len(matches) != 1:
        raise RuntimeError(
            "Image ID did not resolve to exactly one candidate image."
        )

    return matches[0]


def print_candidate_summary(
    candidate: dict[str, Any],
    images: list[dict[str, Any]],
    internal_product: dict[str, Any] | None,
) -> None:
    """Print candidate and image review context."""
    print()
    print("=" * 78)
    print("PRODUCT IMAGE REVIEW")
    print("=" * 78)
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Candidate ID: "
        f"{candidate.get('candidate_id')}"
    )
    print(
        "Title: "
        f"{candidate.get('verified_title') or candidate.get('extracted_title')}"
    )
    print(
        "Identity status: "
        f"{candidate.get('identity_status')}"
    )
    print(
        "Internal product: "
        f"{internal_product.get('product_code') if internal_product else '[not created]'}"
    )
    print(
        "Images linked: "
        f"{len(images)}"
    )
    print()

    for index, image in enumerate(
        images,
        start=1,
    ):
        print("-" * 78)
        print(
            f"Image #{index}"
        )
        print(
            "Image ID: "
            f"{image.get('image_id')}"
        )
        print(
            "Role: "
            f"{image.get('image_role') or '[unset]'}"
        )
        print(
            "Rights: "
            f"{image.get('usage_rights_status') or '[unset]'}"
        )
        print(
            "Status: "
            f"{image.get('image_status')}"
        )
        print(
            "Main candidate: "
            f"{image.get('is_main_image_candidate')}"
        )
        print(
            "Selected main: "
            f"{image.get('is_selected_main_image')}"
        )
        print(
            "Publish eligible: "
            f"{image.get('is_publish_eligible')}"
        )
        print(
            "Dimensions: "
            f"{image.get('width_px')} x {image.get('height_px')}"
        )
        print(
            "Storage: "
            f"{image.get('storage_bucket')}/{image.get('storage_path')}"
        )


def validate_approval_request(
    candidate: dict[str, Any],
    images: list[dict[str, Any]],
    main_image_id: str | None,
    rights_status: str | None,
) -> dict[str, Any]:
    """Validate an image approval request and return the selected image."""
    if candidate.get("identity_status") != "IDENTITY_VERIFIED":
        raise RuntimeError(
            "Image approval requires candidate identity_status=IDENTITY_VERIFIED."
        )

    if not images:
        raise RuntimeError(
            "No images are linked to this candidate."
        )

    if not main_image_id:
        raise RuntimeError(
            "--main-image-id is required for image approval."
        )

    if not rights_status:
        raise RuntimeError(
            "--rights-status is required for image approval."
        )

    if rights_status not in PUBLISHABLE_RIGHTS_STATUSES:
        raise RuntimeError(
            "The selected rights status is not publish eligible."
        )

    return find_image(
        images=images,
        image_id=main_image_id,
    )


def reject_images(
    repository: SupabaseRepository,
    images: list[dict[str, Any]],
    reject_image_ids: list[str],
) -> int:
    """Mark explicitly selected images as rejected and non-publishable."""
    if not reject_image_ids:
        return 0

    now = datetime.now(
        timezone.utc
    ).isoformat()

    rejected_count = 0

    for image_id in reject_image_ids:
        find_image(
            images=images,
            image_id=image_id,
        )

        (
            repository.client
            .table("product_images")
            .update(
                {
                    "image_status": "REJECTED",
                    "is_main_image_candidate": False,
                    "is_selected_main_image": False,
                    "is_publish_eligible": False,
                    "updated_at": now,
                }
            )
            .eq(
                "image_id",
                image_id,
            )
            .execute()
        )

        rejected_count += 1

    return rejected_count


def approve_main_image(
    repository: SupabaseRepository,
    candidate_id: str,
    main_image_id: str,
    main_role: str,
    rights_status: str,
) -> None:
    """Validate exactly one selected main image and demote all others."""
    now = datetime.now(
        timezone.utc
    ).isoformat()

    (
        repository.client
        .table("product_images")
        .update(
            {
                "is_selected_main_image": False,
                "is_publish_eligible": False,
                "updated_at": now,
            }
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    response = (
        repository.client
        .table("product_images")
        .update(
            {
                "image_role": main_role,
                "usage_rights_status": rights_status,
                "image_status": "VALIDATED",
                "is_main_image_candidate": True,
                "is_selected_main_image": True,
                "is_publish_eligible": True,
                "updated_at": now,
            }
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "image_id",
            main_image_id,
        )
        .execute()
    )

    rows = response.data or []

    if len(rows) != 1:
        raise RuntimeError(
            "Main image update did not affect exactly one image."
        )


def synchronize_internal_product_image_status(
    repository: SupabaseRepository,
    candidate_id: str,
) -> None:
    """Synchronize internal_products.image_status from validated image state."""
    images = get_candidate_images(
        repository=repository,
        candidate_id=candidate_id,
    )

    selected_publishable = [
        image
        for image in images
        if image.get("image_status") == "VALIDATED"
        and image.get("is_selected_main_image") is True
        and image.get("is_publish_eligible") is True
        and image.get("usage_rights_status") in PUBLISHABLE_RIGHTS_STATUSES
    ]

    if len(selected_publishable) > 1:
        raise RuntimeError(
            "More than one publishable selected main image exists."
        )

    image_status = (
        "APPROVED"
        if len(selected_publishable) == 1
        else "PENDING"
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    (
        repository.client
        .table("internal_products")
        .update(
            {
                "image_status": image_status,
                "updated_at": now,
            }
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )


def verify_final_state(
    repository: SupabaseRepository,
    candidate_id: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """Reload final image and internal product state."""
    images = get_candidate_images(
        repository=repository,
        candidate_id=candidate_id,
    )

    internal_product = get_internal_product(
        repository=repository,
        candidate_id=candidate_id,
    )

    selected_main_images = [
        image
        for image in images
        if image.get("is_selected_main_image") is True
    ]

    if len(selected_main_images) > 1:
        raise RuntimeError(
            "Final verification found more than one selected main image."
        )

    if selected_main_images:
        selected = selected_main_images[0]

        if (
            selected.get("image_status") != "VALIDATED"
            or selected.get("is_publish_eligible") is not True
            or selected.get("usage_rights_status")
            not in PUBLISHABLE_RIGHTS_STATUSES
        ):
            raise RuntimeError(
                "Selected main image failed final validation."
            )

    return images, internal_product


def main() -> None:
    """Run one image review operation."""
    load_dotenv()
    args = parse_arguments()

    if args.non_interactive and args.approve:
        if not args.confirm_approve:
            raise RuntimeError(
                "--non-interactive --approve requires --confirm-approve."
            )

        if not args.main_image_id:
            raise RuntimeError(
                "--non-interactive --approve requires --main-image-id."
            )

        if not args.rights_status:
            raise RuntimeError(
                "--non-interactive --approve requires --rights-status."
            )

    print(
        "TSYC product image review started."
    )
    print(
        f"Version: {SCRIPT_VERSION}"
    )

    repository = SupabaseRepository()

    candidate = get_candidate(
        repository=repository,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
    )

    candidate_id = str(
        candidate["candidate_id"]
    )

    images = get_candidate_images(
        repository=repository,
        candidate_id=candidate_id,
    )

    internal_product = get_internal_product(
        repository=repository,
        candidate_id=candidate_id,
    )

    print_candidate_summary(
        candidate=candidate,
        images=images,
        internal_product=internal_product,
    )

    if not images:
        raise RuntimeError(
            "No candidate images are available for review."
        )

    rejected_count = reject_images(
        repository=repository,
        images=images,
        reject_image_ids=args.reject_image_id,
    )

    if rejected_count:
        print()
        print(
            f"Rejected images: {rejected_count}"
        )

    if args.approve:
        selected_image = validate_approval_request(
            candidate=candidate,
            images=images,
            main_image_id=args.main_image_id,
            rights_status=args.rights_status,
        )

        print()
        print(
            "Selected main image ID: "
            f"{selected_image.get('image_id')}"
        )
        print(
            "Main image role: "
            f"{args.main_role}"
        )
        print(
            "Usage rights: "
            f"{args.rights_status}"
        )

        if args.confirm_approve:
            confirmation = "APPROVE"

        elif args.non_interactive:
            confirmation = ""

        else:
            confirmation = normalize_confirmation(
                input(
                    "Type APPROVE, CONFIRM, or VALIDATE "
                    "to validate this main image, or press Enter to cancel: "
                )
            )

        if confirmation not in {
            "APPROVE",
            "CONFIRM",
            "VALIDATE",
        }:
            print()
            print(
                "Image approval cancelled."
            )
        else:
            approve_main_image(
                repository=repository,
                candidate_id=candidate_id,
                main_image_id=str(
                    selected_image["image_id"]
                ),
                main_role=args.main_role,
                rights_status=str(
                    args.rights_status
                ),
            )

            synchronize_internal_product_image_status(
                repository=repository,
                candidate_id=candidate_id,
            )

            print()
            print(
                "Main image validated and approved."
            )

    elif rejected_count:
        synchronize_internal_product_image_status(
            repository=repository,
            candidate_id=candidate_id,
        )

    final_images, final_product = verify_final_state(
        repository=repository,
        candidate_id=candidate_id,
    )

    selected_main = [
        image
        for image in final_images
        if image.get("is_selected_main_image") is True
    ]

    print()
    print("=" * 78)
    print("FINAL IMAGE STATE")
    print("=" * 78)
    print(
        "Selected main image: "
        f"{selected_main[0].get('image_id') if len(selected_main) == 1 else '[none]'}"
    )
    print(
        "Selected main status: "
        f"{selected_main[0].get('image_status') if len(selected_main) == 1 else '[none]'}"
    )
    print(
        "Publish eligible: "
        f"{selected_main[0].get('is_publish_eligible') if len(selected_main) == 1 else False}"
    )
    print(
        "Internal product image status: "
        f"{final_product.get('image_status') if final_product else '[internal product not created]'}"
    )

    print()
    print(
        "TSYC product image review finished."
    )


if __name__ == "__main__":
    main()
