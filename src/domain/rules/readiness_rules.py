"""Deterministic WooCommerce-draft readiness rules.

Covers internal_products.woocommerce_status = READY_FOR_DRAFT
(src.domain.woocommerce_status.WooCommerceStatus). This is CLAUDE.md
section 16's readiness gate -- the last deterministic stage before the
single required human authorization (section 6).

Only one rule code is defined: READY_FOR_DRAFT itself is a single
all-or-nothing gate (every blocking condition must hold at once), not a
set of independently-triggerable rules the way identity/image/content
are. AUTO_PASS/REVIEW_REQUIRED/BLOCKED all carry the same rule_code with
the specific blocking reasons in `evidence`/`reason`.

    READY_FOR_DRAFT   AUTO_PASS / REVIEW_REQUIRED / BLOCKED

See docs/TSYC_DECISION_MATRIX.md for the full specification.

Scope note: "no duplicate remote SKU blocker" (CLAUDE.md section 17,
"search remote by SKU" before POST) is inherently a live WooCommerce API
check, not a local Supabase-readable condition -- it is deliberately
NOT evaluated here. It remains create_woocommerce_draft.py's own
immediate pre-POST responsibility (find_product_by_sku()), performed
fresh, in addition to this gate, exactly as CLAUDE.md section 17
requires ("fresh re-read all readiness gates" right before POST).
"""
from __future__ import annotations

from typing import Any, Sequence

from src.domain.content_status import InternalProductContentStatus
from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import IdentityStatus
from src.domain.image_status import ImageStatus, InternalProductImageStatus
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES

READY_FOR_DRAFT = "READY_FOR_DRAFT"


def evaluate_readiness(
    product: dict[str, Any],
    candidate: dict[str, Any] | None,
    approved_content: dict[str, Any] | None,
    selected_images: Sequence[dict[str, Any]],
    recovery_required: bool = False,
    has_created_woo_sync: bool = False,
) -> DecisionResult:
    """
    Decide whether one internal product may become READY_FOR_DRAFT.

    AUTO_PASS requires all of:
      - identity_status = IDENTITY_VERIFIED
      - content_status = APPROVED, and an APPROVED content row exists
      - image aggregate status = APPROVED
      - exactly one selected main image
      - selected image = VALIDATED
      - selected image publish eligible
      - selected image rights publishable
      - recovery_required = false
      - no created Woo sync already exists for this product

    Non-blocking warnings: missing ISBN, weight, dimensions, page
    count, and pricing_status != APPROVED (the shop owner may review
    price later -- CLAUDE.md section 2.5/16).

    Any blocking condition -> REVIEW_REQUIRED (candidate is None ->
    BLOCKED instead: a missing linked row is a structural precondition
    failure, not a business judgment call).
    """
    if candidate is None:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=READY_FOR_DRAFT,
            reason="Linked product candidate was not found.",
        )

    blockers: list[str] = []
    warnings: list[str] = []

    if candidate.get("identity_status") != IdentityStatus.IDENTITY_VERIFIED:
        blockers.append("Product identity is not verified.")

    if product.get("content_status") != InternalProductContentStatus.APPROVED:
        blockers.append("Internal product content is not approved.")

    if approved_content is None:
        blockers.append("Approved Vietnamese product content was not found.")

    if product.get("image_status") != InternalProductImageStatus.APPROVED:
        blockers.append("Internal product image status is not approved.")

    if len(selected_images) != 1:
        blockers.append("Exactly one selected main image is required.")
    else:
        image = selected_images[0]

        if image.get("image_status") != ImageStatus.VALIDATED:
            blockers.append("Selected main image is not validated.")

        if image.get("is_publish_eligible") is not True:
            blockers.append("Selected main image is not publish eligible.")

        if image.get("usage_rights_status") not in PUBLISHABLE_RIGHTS_STATUSES:
            blockers.append("Selected main image usage rights are not publishable.")

    if recovery_required:
        blockers.append(
            "A WooCommerce recovery condition is active; do not proceed "
            "until it is resolved."
        )

    if has_created_woo_sync:
        blockers.append(
            "A WooCommerce sync record already exists for this product; "
            "draft creation must not be repeated."
        )

    if not product.get("isbn"):
        warnings.append("ISBN is missing.")

    if product.get("weight_grams") is None:
        warnings.append("Product weight is missing.")

    if product.get("length_cm") is None or product.get("width_cm") is None or (
        product.get("height_cm") is None
    ):
        warnings.append("Product dimensions are missing.")

    if product.get("page_count") is None:
        warnings.append("Page count is missing.")

    if product.get("pricing_status") != "APPROVED":
        warnings.append(
            "Pricing is not approved. The shop owner must add or review "
            "the price before publishing."
        )

    evidence = {"blockers": tuple(blockers), "warning_count": len(warnings)}

    if blockers:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=READY_FOR_DRAFT,
            reason="; ".join(blockers),
            warnings=tuple(warnings),
            evidence=evidence,
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=READY_FOR_DRAFT,
        reason="All readiness gates are satisfied.",
        warnings=tuple(warnings),
        evidence=evidence,
    )
