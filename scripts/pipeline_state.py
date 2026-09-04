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
from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import IdentityStatus, MatchDecision
from src.domain.image_status import InternalProductImageStatus
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES
from src.domain.rules import image_rules, readiness_rules
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402
from src.services.historical_image_extraction import (  # noqa: E402
    check_capability as check_historical_image_capability,
    filter_image_paths as filter_historical_image_paths,
)

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
    "IMAGE_CAPABILITY_UNAVAILABLE",
    "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
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
    "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
    "IMAGE_CAPABILITY_UNAVAILABLE",
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

    # Historical (FB-HIST) capability + sibling-ownership signals.
    # Computed only when relevant (no images yet ingested for a candidate
    # whose source_evidence carries local_media_paths) -- this is I/O
    # (filesystem probe + one extra bounded read), which is exactly why
    # it lives here in the I/O layer rather than in the pure
    # derive_candidate_state() below (see that function's docstring: "a
    # pure derivation layer... it only reads fields the writer scripts
    # already produce").
    source_evidence = candidate.get("source_evidence") or {}
    local_media_paths = filter_historical_image_paths(
        source_evidence.get("local_media_paths") or []
    )

    historical_capability_available: bool | None = None
    historical_capability_reason: str | None = None
    sibling_candidate_codes: list[str] = []

    if local_media_paths and not images:
        capability = check_historical_image_capability(PROJECT_ROOT)
        historical_capability_available = capability.available
        historical_capability_reason = capability.reason

        raw_page_id = candidate.get("raw_page_id")

        if raw_page_id:
            sibling_rows = (
                repository.client
                .table("product_candidates")
                .select("candidate_id, candidate_code")
                .eq("raw_page_id", raw_page_id)
                .execute()
                .data
                or []
            )
            sibling_candidate_codes = [
                sibling["candidate_code"]
                for sibling in sibling_rows
                if sibling.get("candidate_id") != candidate_id
                and sibling.get("candidate_code")
            ]

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
        "historical_local_media_paths": local_media_paths,
        "historical_capability_available": historical_capability_available,
        "historical_capability_reason": historical_capability_reason,
        "sibling_candidate_codes": sibling_candidate_codes,
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


def _vietnamese_content_review_notes(
    contents: list[dict[str, Any]],
) -> str | None:
    """The Vietnamese product_contents row's own review_notes, if any --
    surfaces prepare_product_content.py's exact declined-approval reason
    (CLAUDE.md 15.3) in the batch summary instead of a generic "manual
    review required" placeholder. Read-only; this function decides
    nothing, it only re-states what the writer already recorded."""
    for content in contents:
        if content.get("content_language") == "vi":
            notes = content.get("review_notes")
            return str(notes) if notes else None
    return None


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
            historical_media_paths = bundle.get("historical_local_media_paths") or []

            if historical_media_paths:
                # FB-HIST candidate with no product_images rows yet: this
                # is the CLAUDE.md Phase 4 gate -- do not fall through to
                # the generic "run the collector" message below (that
                # message names the live-crawl collector, which cannot
                # help a historical candidate at all).
                ownership_decision = image_rules.evaluate_historical_image_ownership(
                    bundle.get("sibling_candidate_codes") or []
                )

                if ownership_decision.outcome != Outcome.AUTO_PASS:
                    return CandidateState(
                        candidate_code=candidate_code,
                        candidate_id=candidate_id,
                        product_code=product_code,
                        derived_state="IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
                        human_gate=True,
                        human_gate_reason=ownership_decision.reason,
                        warnings=warnings,
                    )

                capability_decision = image_rules.evaluate_historical_image_capability(
                    available=bool(bundle.get("historical_capability_available")),
                    reason=(
                        bundle.get("historical_capability_reason")
                        or "Historical image ingestion capability status is unknown."
                    ),
                )

                if capability_decision.outcome != Outcome.AUTO_PASS:
                    return CandidateState(
                        candidate_code=candidate_code,
                        candidate_id=candidate_id,
                        product_code=product_code,
                        derived_state="IMAGE_CAPABILITY_UNAVAILABLE",
                        blocked=True,
                        blocked_reason=capability_decision.reason,
                        warnings=warnings,
                    )

                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=product_code,
                    derived_state="IMAGE_PENDING",
                    human_gate=True,
                    human_gate_reason=(
                        "Historical local media is available and "
                        "unambiguously owned by this candidate. Run "
                        "scripts/extract_historical_facebook_images.py "
                        "then scripts/upload_facebook_images_to_supabase.py "
                        "to ingest images for this candidate."
                    ),
                    warnings=warnings,
                )

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
        # Not a human gate: CLAUDE.md section 15.3 explicitly allows
        # automatic content approval once deterministic validation
        # confirms verified-facts-only, no internal workflow language,
        # and a non-generic draft. run_batch.py's AUTOMATABLE_DISPATCH
        # dispatches prepare_product_content.py --action APPROVE for
        # this state; that script re-runs the same deterministic checks
        # (src.domain.rules.content_rules) and, when they do not all
        # pass, downgrades content_status to REVIEW_REQUIRED itself
        # instead of approving -- which re-derives as CONTENT_REVIEW_
        # REQUIRED below (a real human gate) on the next state read.
        # This function never approves anything itself; it only decides
        # DRAFTED is not, by itself, a reason to stop.
        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="CONTENT_DRAFTED",
            warnings=warnings,
        )

    # "REJECTED" is not itself a member of internal_products.content_status
    # (migrations/007_create_internal_products.sql only allows PENDING,
    # DRAFTED, REVIEW_REQUIRED, APPROVED) -- kept as a defensive literal
    # rather than invented as a domain constant that would not exist.
    if content_status in (InternalProductContentStatus.REVIEW_REQUIRED, "REJECTED"):
        review_notes = _vietnamese_content_review_notes(bundle["contents"])

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="CONTENT_REVIEW_REQUIRED",
            human_gate=True,
            human_gate_reason=(
                f"Content status is {content_status}. "
                + (
                    review_notes
                    if review_notes
                    else "Manual review is required before content can advance."
                )
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


# ---------------------------------------------------------------------
# Named stage preflights (CLAUDE.md pipeline stabilization Phase 4)
# ---------------------------------------------------------------------
#
# Each function below answers one narrow, named question -- "is this
# candidate structurally ready to attempt <stage>" -- for
# scripts/run_batch.py's --dry-run report and for tests that want to
# assert one milestone in isolation. None of them introduce a second
# state machine: every one either reuses derive_candidate_state()'s own
# classification, or (for READY_FOR_DRAFT) calls the exact same rule
# module scripts/check_draft_readiness.py already calls, over the same
# bundle load_candidate_bundle() already read. There is exactly one
# place that decides what happens next -- derive_candidate_state -- and
# these never contradict it.

READY_FOR_IDENTITY = "READY_FOR_IDENTITY"
READY_FOR_CONTENT = "READY_FOR_CONTENT"
READY_FOR_IMAGE = "READY_FOR_IMAGE"
READY_FOR_DRAFT_PREFLIGHT = "READY_FOR_DRAFT"

_IDENTITY_NOT_YET_READY_STATES = {"EXTRACTED", "REFERENCE_REGISTERED"}
_IMAGE_BLOCKED_STATES = {"IMAGE_CAPABILITY_UNAVAILABLE"}
_IMAGE_REVIEW_STATES = {
    "IMAGE_PENDING",
    "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
    "RIGHTS_REVIEW_REQUIRED",
    "IMAGE_REVIEW_REQUIRED",
}


def stage_preflight_identity(bundle: dict[str, Any]) -> DecisionResult:
    """READY_FOR_IDENTITY: true once a reference source has been
    registered and collected for this candidate (match_candidate_
    identity.py can run), or identity is already resolved."""
    state = derive_candidate_state(bundle)

    if state.derived_state in _IDENTITY_NOT_YET_READY_STATES:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED if state.human_gate else Outcome.BLOCKED,
            rule_code=READY_FOR_IDENTITY,
            reason=state.human_gate_reason
            or state.blocked_reason
            or f"Candidate is at {state.derived_state}; no reference has "
            "been collected yet.",
            evidence={"derived_state": state.derived_state},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=READY_FOR_IDENTITY,
        reason=f"Candidate is at {state.derived_state}; reference metadata "
        "is available for identity matching.",
        evidence={"derived_state": state.derived_state},
    )


def stage_preflight_content(bundle: dict[str, Any]) -> DecisionResult:
    """READY_FOR_CONTENT: true once an internal_products row exists
    (prepare_product_content.py requires internal_product_id)."""
    state = derive_candidate_state(bundle)

    if state.product_code is None:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED if state.human_gate else Outcome.BLOCKED,
            rule_code=READY_FOR_CONTENT,
            reason=state.human_gate_reason
            or state.blocked_reason
            or f"Candidate is at {state.derived_state}; no internal "
            "product has been created yet.",
            evidence={"derived_state": state.derived_state},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=READY_FOR_CONTENT,
        reason="Internal product exists; content drafting/approval can "
        "proceed.",
        evidence={
            "derived_state": state.derived_state,
            "product_code": state.product_code,
        },
    )


def stage_preflight_image(bundle: dict[str, Any]) -> DecisionResult:
    """READY_FOR_IMAGE: BLOCKED specifically at IMAGE_CAPABILITY_
    UNAVAILABLE -- the FB-HIST gate CLAUDE.md Phase 4 requires to fail
    before production work when the historical image capability (the
    Facebook export archive) is unavailable. REVIEW_REQUIRED for every
    other unresolved image gate (no images yet, ambiguous multi-product
    post ownership, unresolved rights, unresolved main-image selection).
    """
    state = derive_candidate_state(bundle)

    if state.derived_state in _IMAGE_BLOCKED_STATES:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=READY_FOR_IMAGE,
            reason=state.blocked_reason
            or "Historical image ingestion capability is unavailable.",
            evidence={"derived_state": state.derived_state},
        )

    if state.derived_state in _IMAGE_REVIEW_STATES:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=READY_FOR_IMAGE,
            reason=state.human_gate_reason
            or f"Candidate is at {state.derived_state}.",
            evidence={"derived_state": state.derived_state},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=READY_FOR_IMAGE,
        reason=f"Candidate is at {state.derived_state}; the image gate is "
        "satisfied or not yet reached.",
        evidence={"derived_state": state.derived_state},
    )


def stage_preflight_draft(bundle: dict[str, Any]) -> DecisionResult:
    """READY_FOR_DRAFT preflight: re-evaluates the exact same
    src.domain.rules.readiness_rules.evaluate_readiness() gate
    scripts/check_draft_readiness.py already calls, over the data
    load_candidate_bundle() already read -- no second copy of the
    readiness business rule, no extra DB round-trip."""
    internal_product = bundle["internal_product"]

    if not internal_product:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=READY_FOR_DRAFT_PREFLIGHT,
            reason="No internal product exists yet.",
        )

    approved_content = next(
        (
            content
            for content in bundle["contents"]
            if content.get("content_language") == "vi"
            and content.get("content_status") == InternalProductContentStatus.APPROVED
            and content.get("review_required") is False
        ),
        None,
    )

    selected_images = [
        image
        for image in bundle["images"]
        if image.get("is_selected_main_image") is True
    ]

    sync = bundle["sync"] or {}
    response_payload = sync.get("response_payload")
    recovery_required = bool(
        isinstance(response_payload, dict)
        and response_payload.get("recovery_required") is True
    )
    has_created_woo_sync = bool(
        sync.get("woocommerce_product_id")
        or sync.get("woocommerce_status") == WooCommerceSyncStatus.DRAFT_CREATED
    )

    return readiness_rules.evaluate_readiness(
        product=internal_product,
        candidate=bundle["candidate"],
        approved_content=approved_content,
        selected_images=selected_images,
        recovery_required=recovery_required,
        has_created_woo_sync=has_created_woo_sync,
    )


def evaluate_stage_preflights(bundle: dict[str, Any]) -> dict[str, DecisionResult]:
    """All four named stage preflights for one candidate, keyed by name --
    the shape scripts/run_batch.py's --dry-run report consumes."""
    return {
        READY_FOR_IDENTITY: stage_preflight_identity(bundle),
        READY_FOR_CONTENT: stage_preflight_content(bundle),
        READY_FOR_IMAGE: stage_preflight_image(bundle),
        READY_FOR_DRAFT_PREFLIGHT: stage_preflight_draft(bundle),
    }
