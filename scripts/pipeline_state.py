"""
Read-only candidate pipeline state derivation layer.

This module never writes to Supabase. It reads the same tables the existing
pipeline scripts already write to (product_candidates, product_references,
candidate_reference_sources, internal_products, product_contents,
product_images, woocommerce_product_syncs) and derives, for one candidate at
a time, a single named state consistent with the TSYC state machine plus the
mandatory gates already enforced by the individual scripts.

This is a pure derivation layer: it does not decide *how* to advance a
candidate (that is scripts/run_batch.py's job, using this module's output),
and it does not duplicate any writer script's business logic -- it only
reads fields the writer scripts already produce and re-states, in one place,
what stage a candidate is currently sitting at.

Chosen implementation: a pure-Python resolver rather than a SQL view. A view
would need to reach across seven tables with candidate/product-scoped
subqueries and would still have to be read out of Postgres one row at a
time for a bounded candidate allowlist -- no simpler than doing the same
joins in Python, and it would add a migration to maintain in lockstep with
every script that touches these status columns. A pure function is also
directly unit-testable with plain dicts, no live database required.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.domain.content_status import InternalProductContentStatus
from src.domain.decisions import Outcome
from src.domain.identity_status import IdentityStatus, MatchDecision
from src.domain.image_status import InternalProductImageStatus
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

# PUBLISHABLE_RIGHTS_STATUSES (imported above) must exactly match
# product_images_publish_eligibility_check
# (migrations/009_add_product_image_review_guards.sql), the same set
# scripts/audit_pipeline_state.py uses.

# Warning codes CLAUDE.md's "Golden principles" #5 and "Audit rule" accept as
# non-blocking for WooCommerce draft creation. Any WARNING-severity audit
# issue outside this set must stop the batch, not just be noted.
ACCEPTED_WARNING_CODES = {
    "ISBN_MISSING",
    "WEIGHT_MISSING",
}

# The full named state machine from the Phase C plan. run_batch.py's
# dispatch table is keyed by a subset of these; the rest are recognized
# outcomes with no automated dispatch entry (human gate, recovery, or
# terminal).
DERIVED_STATES = {
    "EXTRACTED",
    "REFERENCE_REGISTERED",
    "REFERENCE_COLLECTED",
    "IDENTITY_PENDING",
    "IDENTITY_CONFLICT",
    "IDENTITY_VERIFIED",
    "INTERNAL_PRODUCT_CREATED",
    "CONTENT_DRAFTED",
    "CONTENT_APPROVED",
    "IMAGE_PENDING",
    "IMAGE_VALIDATED",
    "READY_FOR_DRAFT",
    "DRAFT_CREATION_IN_PROGRESS",
    "DRAFT_CREATED",
    "RECONCILED",
}

RECOVERY_STATES = {
    "MEDIA_UPLOAD_INCOMPLETE",
    "CREATE_RESULT_UNCERTAIN",
    "REMOTE_CREATED_LOCAL_DIRTY",
    "RECONCILIATION_REQUIRED",
}

TERMINAL_OR_MANUAL_STATES = {
    "DUPLICATE_REJECTED",
    "IDENTITY_CONFLICT",
    "CONTENT_REVIEW_REQUIRED",
    "IMAGE_REVIEW_REQUIRED",
    "RIGHTS_REVIEW_REQUIRED",
    "RECOVERY_REVIEW_REQUIRED",
}


@dataclass
class CandidateState:
    """The derived pipeline position of exactly one candidate."""

    candidate_code: str
    candidate_id: str | None
    product_code: str | None
    derived_state: str
    recovery_state: str | None = None
    human_gate: bool = False
    human_gate_reason: str | None = None
    terminal: bool = False
    blocked: bool = False
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """
        Map this derived state onto the canonical
        src.domain.decisions.Outcome vocabulary (CLAUDE.md decision-
        engine architecture, section 7): AUTO_PASS / AUTO_REJECT /
        REVIEW_REQUIRED / BLOCKED.

        A recovery condition or a structural blocker is BLOCKED (a
        precondition that must be resolved, not a business judgment
        call); a human gate is REVIEW_REQUIRED; a confirmed terminal
        rejection is AUTO_REJECT; everything else -- the candidate is
        clear to advance -- is AUTO_PASS.

        Purely a reporting/consistency view: run_batch.py's own richer
        `result` vocabulary (HUMAN_GATE, STAGE_FAILED, DRY_RUN, ...)
        remains authoritative for actual dispatch decisions.
        """
        if self.recovery_state is not None or self.blocked:
            return Outcome.BLOCKED
        if self.human_gate:
            return Outcome.REVIEW_REQUIRED
        if self.derived_state == "DUPLICATE_REJECTED":
            return Outcome.AUTO_REJECT
        return Outcome.AUTO_PASS

    @property
    def outcome_reason(self) -> str | None:
        """The human-readable reason paired with `.outcome`, if any."""
        return self.blocked_reason or self.human_gate_reason


class CandidateNotFoundError(RuntimeError):
    """Raised when an explicitly requested candidate_code does not exist."""


def load_candidate_bundle(
    repository: SupabaseRepository,
    candidate_code: str,
) -> dict[str, Any] | None:
    """
    Read every row needed to derive one candidate's state.

    Returns None when candidate_code does not resolve to any
    product_candidates row. Performs reads only.
    """
    candidate_rows = (
        repository.client
        .table("product_candidates")
        .select("*")
        .eq("candidate_code", candidate_code)
        .limit(1)
        .execute()
        .data
        or []
    )

    if not candidate_rows:
        return None

    candidate = candidate_rows[0]
    candidate_id = candidate["candidate_id"]

    references = (
        repository.client
        .table("product_references")
        .select("*")
        .eq("candidate_id", candidate_id)
        .execute()
        .data
        or []
    )

    discovery_sources = (
        repository.client
        .table("candidate_reference_sources")
        .select("*")
        .eq("candidate_id", candidate_id)
        .execute()
        .data
        or []
    )

    images = (
        repository.client
        .table("product_images")
        .select("*")
        .eq("candidate_id", candidate_id)
        .execute()
        .data
        or []
    )

    internal_product_rows = (
        repository.client
        .table("internal_products")
        .select("*")
        .eq("candidate_id", candidate_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    internal_product = internal_product_rows[0] if internal_product_rows else None

    contents: list[dict[str, Any]] = []
    sync: dict[str, Any] | None = None

    if internal_product:
        internal_product_id = internal_product["internal_product_id"]

        contents = (
            repository.client
            .table("product_contents")
            .select("*")
            .eq("internal_product_id", internal_product_id)
            .execute()
            .data
            or []
        )

        sync_rows = (
            repository.client
            .table("woocommerce_product_syncs")
            .select("*")
            .eq("internal_product_id", internal_product_id)
            .limit(1)
            .execute()
            .data
            or []
        )

        sync = sync_rows[0] if sync_rows else None

    return {
        "candidate": candidate,
        "references": references,
        "discovery_sources": discovery_sources,
        "images": images,
        "internal_product": internal_product,
        "contents": contents,
        "sync": sync,
    }


def _warnings_for_internal_product(
    internal_product: dict[str, Any],
) -> list[str]:
    """Non-blocking warnings, mirroring audit_pipeline_state.py exactly."""
    warnings: list[str] = []

    if not internal_product.get("isbn"):
        warnings.append("ISBN_MISSING")

    if internal_product.get("weight_grams") in (None, ""):
        warnings.append("WEIGHT_MISSING")

    return warnings


def _derive_recovery_state(
    bundle: dict[str, Any],
) -> tuple[str, str] | None:
    """
    Return (derived_state, recovery_state) if the candidate is in a
    recovery condition, else None.

    Every check here mirrors a field create_woocommerce_draft.py or
    sync_woocommerce_product_status.py already writes -- this function
    reads those fields, it does not decide their meaning independently.
    """
    internal_product = bundle["internal_product"]
    sync = bundle["sync"]

    if sync:
        response_payload = sync.get("response_payload")

        if (
            isinstance(response_payload, dict)
            and response_payload.get("recovery_required") is True
        ):
            return ("RECOVERY_REVIEW_REQUIRED", "REMOTE_CREATED_LOCAL_DIRTY")

        if (
            sync.get("woocommerce_status") == WooCommerceSyncStatus.IN_PROGRESS
            and not sync.get("woocommerce_product_id")
        ):
            return ("RECOVERY_REVIEW_REQUIRED", "CREATE_RESULT_UNCERTAIN")

        if sync.get("woocommerce_status") == WooCommerceSyncStatus.FAILED:
            payload = response_payload if isinstance(response_payload, dict) else {}

            if payload.get("uploaded_media") and not payload.get(
                "media_upload_completed"
            ):
                return ("RECOVERY_REVIEW_REQUIRED", "MEDIA_UPLOAD_INCOMPLETE")

            return ("RECOVERY_REVIEW_REQUIRED", "CREATE_RESULT_UNCERTAIN")

    if internal_product and internal_product.get("woocommerce_status") == WooCommerceStatus.FAILED:
        return ("RECOVERY_REVIEW_REQUIRED", "RECONCILIATION_REQUIRED")

    return None


def _derive_image_content_state(
    bundle: dict[str, Any],
) -> CandidateState:
    """
    Derive state for a candidate that already has an internal_products row
    with woocommerce_status = NOT_CREATED.

    Ordering follows CLAUDE.md's "Required pipeline order": review_product
    _images.py (step 10) runs before prepare_product_content.py (step 11),
    so an unapproved image blocks before an unapproved content draft does.
    """
    candidate = bundle["candidate"]
    internal_product = bundle["internal_product"]
    images = bundle["images"]

    candidate_code = candidate["candidate_code"]
    candidate_id = candidate["candidate_id"]
    product_code = internal_product.get("product_code")
    warnings = _warnings_for_internal_product(internal_product)

    if internal_product.get("image_status") != InternalProductImageStatus.APPROVED:
        if not images:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=product_code,
                derived_state="IMAGE_PENDING",
                human_gate=True,
                human_gate_reason=(
                    "No images are available for review. An upstream image "
                    "collection step (upload_facebook_images_to_supabase.py) "
                    "was not completed for this candidate."
                ),
                warnings=warnings,
            )

        has_publishable_rights = any(
            image.get("usage_rights_status") in PUBLISHABLE_RIGHTS_STATUSES
            for image in images
        )

        if not has_publishable_rights:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=product_code,
                derived_state="RIGHTS_REVIEW_REQUIRED",
                human_gate=True,
                human_gate_reason=(
                    "No image has a publishable usage-rights status. Image "
                    "rights cannot be inferred automatically -- confirm "
                    "rights via review_product_images.py."
                ),
                warnings=warnings,
            )

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="IMAGE_REVIEW_REQUIRED",
            human_gate=True,
            human_gate_reason=(
                "Images with usable rights exist, but no single validated, "
                "selected, publish-eligible main image has been approved. "
                "Run review_product_images.py to select and approve one."
            ),
            warnings=warnings,
        )

    content_status = internal_product.get("content_status")

    if content_status == InternalProductContentStatus.PENDING:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="INTERNAL_PRODUCT_CREATED",
            warnings=warnings,
        )

    if content_status == InternalProductContentStatus.DRAFTED:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="CONTENT_DRAFTED",
            human_gate=True,
            human_gate_reason=(
                "A content draft exists but is not APPROVED. Content "
                "approval requires human review/enrichment "
                "(prepare_product_content.py --action APPROVE) -- it is "
                "never approved automatically."
            ),
            warnings=warnings,
        )

    # "REJECTED" is not itself a member of internal_products.content_status
    # (migrations/007_create_internal_products.sql only allows PENDING,
    # DRAFTED, REVIEW_REQUIRED, APPROVED) -- kept as a defensive literal
    # rather than invented as a domain constant that would not exist.
    if content_status in (InternalProductContentStatus.REVIEW_REQUIRED, "REJECTED"):
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="CONTENT_REVIEW_REQUIRED",
            human_gate=True,
            human_gate_reason=(
                f"Content status is {content_status}; manual review is "
                "required before content can advance."
            ),
            warnings=warnings,
        )

    if content_status == InternalProductContentStatus.APPROVED:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="IMAGE_VALIDATED",
            warnings=warnings,
        )

    return CandidateState(
        candidate_code=candidate_code,
        candidate_id=candidate_id,
        product_code=product_code,
        derived_state="CONTENT_APPROVED",
        human_gate=True,
        human_gate_reason=(
            f"Unrecognized content_status={content_status!r}; manual "
            "review required."
        ),
        warnings=warnings,
    )


def _derive_pre_product_state(
    bundle: dict[str, Any],
) -> CandidateState:
    """Derive state for a candidate with no internal_products row yet."""
    candidate = bundle["candidate"]
    references = bundle["references"]
    discovery_sources = bundle["discovery_sources"]

    candidate_code = candidate["candidate_code"]
    candidate_id = candidate["candidate_id"]
    identity_status = candidate.get("identity_status")

    if identity_status == IdentityStatus.REJECTED:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=None,
            derived_state="DUPLICATE_REJECTED",
            terminal=True,
        )

    if identity_status == IdentityStatus.IDENTITY_CONFLICT:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=None,
            derived_state="IDENTITY_CONFLICT",
            human_gate=True,
            human_gate_reason=(
                "Identity conflict was detected during matching. Manual "
                "resolution is required (match_candidate_identity.py "
                "--mode SINGLE, or additional reference collection)."
            ),
        )

    if identity_status == IdentityStatus.IDENTITY_VERIFIED:
        match_references = [
            reference
            for reference in references
            if reference.get("match_decision") == MatchDecision.MATCH
        ]

        if match_references:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="IDENTITY_VERIFIED",
            )

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=None,
            derived_state="IDENTITY_VERIFIED",
            blocked=True,
            blocked_reason=(
                "candidate.identity_status = IDENTITY_VERIFIED but no "
                "MATCH product_reference exists. The mandatory gate "
                "before create_internal_product.py cannot be satisfied. "
                "This indicates a data inconsistency -- run "
                "audit_pipeline_state.py."
            ),
        )

    if identity_status == IdentityStatus.ACCEPTED_WITH_LIMITED_METADATA:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=None,
            derived_state="IDENTITY_PENDING",
            human_gate=True,
            human_gate_reason=(
                "identity_status = ACCEPTED_WITH_LIMITED_METADATA. This "
                "is not IDENTITY_VERIFIED, so create_internal_product.py's "
                "mandatory gate is not satisfied. An explicit human "
                "decision is required before proceeding."
            ),
        )

    if identity_status == IdentityStatus.IDENTITY_PENDING:
        if references:
            unresolved = [
                reference
                for reference in references
                if reference.get("match_decision") is None
            ]

            if unresolved:
                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=None,
                    derived_state="REFERENCE_COLLECTED",
                )

            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="IDENTITY_PENDING",
                human_gate=True,
                human_gate_reason=(
                    "Reference metadata was collected and evaluated, but "
                    "automatic identity matching was inconclusive (no "
                    "MATCH decision). Manual review required "
                    "(match_candidate_identity.py --mode SINGLE, or "
                    "register/collect an additional reference source)."
                ),
            )

        selected_sources = [
            source
            for source in discovery_sources
            if source.get("is_selected_for_crawl") is True
        ]

        if selected_sources:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="REFERENCE_REGISTERED",
            )

        if discovery_sources:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="EXTRACTED",
                human_gate=True,
                human_gate_reason=(
                    "A reference source was discovered but not yet "
                    "selected for crawl. Human decision required "
                    "(register_reference_source.py --select-for-crawl), "
                    "using the CLAUDE.md reference identity priority: "
                    "publisher > authorized supplier > reliable bookstore "
                    "> Fahasa > Facebook."
                ),
            )

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=None,
            derived_state="EXTRACTED",
            human_gate=True,
            human_gate_reason=(
                "No reference source has been registered yet. Register "
                "one via register_reference_source.py (requires human "
                "judgment on authorized source priority) or "
                "manual_create_product_reference.py."
            ),
        )

    return CandidateState(
        candidate_code=candidate_code,
        candidate_id=candidate_id,
        product_code=None,
        derived_state=identity_status or "UNKNOWN",
        human_gate=True,
        human_gate_reason=(
            f"Unrecognized candidate.identity_status={identity_status!r}; "
            "manual review required."
        ),
    )


def derive_candidate_state(
    bundle: dict[str, Any],
) -> CandidateState:
    """
    Derive the single current pipeline state of one candidate.

    Pure function: takes only the rows load_candidate_bundle() already read,
    performs no I/O, and always returns exactly one CandidateState.
    """
    candidate = bundle["candidate"]
    internal_product = bundle["internal_product"]

    candidate_code = candidate["candidate_code"]
    candidate_id = candidate["candidate_id"]

    if not internal_product:
        return _derive_pre_product_state(bundle)

    product_code = internal_product.get("product_code")
    warnings = _warnings_for_internal_product(internal_product)

    recovery = _derive_recovery_state(bundle)

    if recovery is not None:
        derived_state, recovery_state = recovery

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state=derived_state,
            recovery_state=recovery_state,
            human_gate=True,
            human_gate_reason=(
                f"Recovery state {recovery_state}: WooCommerce remote "
                "state is uncertain. Do not retry draft creation "
                "automatically -- run the WooCommerce status "
                "synchronization/recovery workflow and resolve manually."
            ),
            warnings=warnings,
        )

    woocommerce_status = internal_product.get("woocommerce_status")

    if woocommerce_status in (
        WooCommerceStatus.READY_TO_PUBLISH,
        WooCommerceStatus.PUBLISHED,
    ):
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="RECONCILED",
            terminal=True,
            warnings=warnings,
        )

    if woocommerce_status == WooCommerceStatus.DRAFT_CREATED:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="DRAFT_CREATED",
            warnings=warnings,
        )

    if woocommerce_status == WooCommerceStatus.READY_FOR_DRAFT:
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="READY_FOR_DRAFT",
            human_gate=True,
            human_gate_reason=(
                "WooCommerce draft creation is a human gate by default. "
                "Pass --allow-woo-draft with an exact bounded allowlist to "
                "authorize create_woocommerce_draft.py for this run."
            ),
            warnings=warnings,
        )

    # woocommerce_status == "NOT_CREATED" (or any other pre-draft value):
    # advance through the image/content sub-state machine.
    return _derive_image_content_state(bundle)
