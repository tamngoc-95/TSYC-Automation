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

from create_internal_product import is_historical_candidate_code  # noqa: E402
from prepare_product_content import (  # noqa: E402
    build_safe_draft,
    is_generic_safe_draft,
)
from src.domain.content_status import InternalProductContentStatus
from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import IdentityStatus, MatchDecision
from src.domain.image_status import InternalProductImageStatus
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES
from src.domain.rules import content_rules, image_rules, readiness_rules
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
    # Historical-migration draft-safe policy (explicit shop-owner
    # business authorization, CLAUDE.md section 9/13): an internal
    # product created for an FB-HIST candidate whose identity is not yet
    # fully verified (but is not a confirmed IDENTITY_CONFLICT) is
    # expected and accepted -- see
    # audit_pipeline_state.py::audit_candidate_product_linkage().
    "IDENTITY_NOT_VERIFIED_HISTORICAL",
    # Same policy, applied to primary-reference linkage (CLAUDE.md
    # section 6.2/9.4/13): an FB-HIST internal product may have a
    # POSSIBLE_MATCH/MANUAL_REVIEW primary reference (enrichment only)
    # or none at all -- see audit_pipeline_state.py::audit_references().
    "PRIMARY_REFERENCE_MISSING_HISTORICAL",
    "PRIMARY_REFERENCE_NOT_MATCHED_HISTORICAL",
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
    "IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE",
    "IDENTITY_CONFLICT",
    "IDENTITY_VERIFIED",
    "INTERNAL_PRODUCT_CREATED",
    "CONTENT_DRAFTED",
    "CONTENT_APPROVED",
    "IMAGE_PENDING",
    "IMAGE_INGEST_PENDING_HISTORICAL",
    "IMAGE_APPROVAL_PENDING_HISTORICAL",
    "IMAGE_REFERENCE_FALLBACK_PENDING_HISTORICAL",
    "IMAGE_CAPABILITY_UNAVAILABLE",
    "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
    "CONTENT_REVISE_PENDING_HISTORICAL",
    "IMAGE_VALIDATED",
    "READY_FOR_DRAFT",
    "READY_FOR_DRAFT_HISTORICAL",
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
    # Populated only for derived_state=="IMAGE_APPROVAL_PENDING_HISTORICAL"
    # -- the exact single image_id and usage_rights_status
    # scripts/run_batch.py's dispatch passes through to
    # review_product_images.py --main-image-id/--rights-status. Kept on
    # CandidateState (rather than recomputed by run_batch.py) so there is
    # exactly one place that decides them, matching this module's own
    # "pure derivation layer" contract.
    auto_main_image_id: str | None = None
    auto_rights_status: str | None = None
    # Populated alongside auto_main_image_id/auto_rights_status when more
    # than one eligible image belongs to this same candidate --
    # image_rules.select_primary_candidate_image()'s "gallery" evidence,
    # each entry (image_id, rights_status). scripts/run_batch.py's
    # dispatch passes these through to review_product_images.py's
    # --gallery-image-id/--gallery-rights-status. Empty when only the
    # one primary image exists (the common case).
    auto_gallery_images: tuple[tuple[str, str], ...] = ()
    # Populated only for derived_state=="IMAGE_REFERENCE_FALLBACK_PENDING_
    # HISTORICAL" -- the exact single draft-safe reference_id
    # scripts/run_batch.py's dispatch passes through to
    # download_bookstore_product_image.py (which re-resolves and
    # re-validates it itself; this is not a trust-blindly hand-off).
    auto_reference_id: str | None = None

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
    sibling_source_evidence: list[dict[str, Any]] = []

    if local_media_paths and not images:
        capability = check_historical_image_capability(PROJECT_ROOT)
        historical_capability_available = capability.available
        historical_capability_reason = capability.reason

        raw_page_id = candidate.get("raw_page_id")

        if raw_page_id:
            sibling_rows = (
                repository.client
                .table("product_candidates")
                .select("candidate_id, candidate_code, source_evidence")
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
            # Only used by resolve_historical_candidate_images() below,
            # as a second-step check after evaluate_historical_image_
            # ownership() itself reports ambiguity. Never used to widen
            # sibling_candidate_codes itself, which stays the exact same
            # post-membership list every other caller already relies on.
            sibling_source_evidence = [
                sibling.get("source_evidence") or {}
                for sibling in sibling_rows
                if sibling.get("candidate_id") != candidate_id
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
        "sibling_source_evidence": sibling_source_evidence,
    }


def load_all_candidate_bundles(
    repository: SupabaseRepository,
) -> dict[str, dict[str, Any]]:
    """
    Bulk equivalent of load_candidate_bundle(): reads each relevant table
    exactly once (the same full-table-read pattern
    scripts/audit_pipeline_state.py already uses for its own read-only
    cross-table audit) and returns the same per-candidate bundle shape
    load_candidate_bundle() returns, keyed by candidate_code, without a
    per-candidate round trip. Intended for read-only aggregate reporting
    over the whole backlog (scripts/export_historical_orchestration_
    state.py); scripts/run_batch.py and check_draft_readiness.py keep
    using load_candidate_bundle() for one bounded, explicitly-allowlisted
    candidate at a time -- this function does not replace that contract.
    """
    candidates = (
        repository.client.table("product_candidates").select("*").execute().data
        or []
    )
    references = (
        repository.client.table("product_references").select("*").execute().data
        or []
    )
    discovery_sources = (
        repository.client.table("candidate_reference_sources")
        .select("*")
        .execute()
        .data
        or []
    )
    images = (
        repository.client.table("product_images").select("*").execute().data or []
    )
    internal_products = (
        repository.client.table("internal_products").select("*").execute().data or []
    )
    contents = (
        repository.client.table("product_contents").select("*").execute().data or []
    )
    syncs = (
        repository.client.table("woocommerce_product_syncs")
        .select("*")
        .execute()
        .data
        or []
    )

    references_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for reference in references:
        references_by_candidate.setdefault(
            str(reference.get("candidate_id")), []
        ).append(reference)

    discovery_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for source in discovery_sources:
        discovery_by_candidate.setdefault(
            str(source.get("candidate_id")), []
        ).append(source)

    images_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for image in images:
        images_by_candidate.setdefault(str(image.get("candidate_id")), []).append(
            image
        )

    internal_product_by_candidate: dict[str, dict[str, Any]] = {
        str(product.get("candidate_id")): product
        for product in internal_products
        if product.get("candidate_id")
    }

    contents_by_product: dict[str, list[dict[str, Any]]] = {}
    for content in contents:
        contents_by_product.setdefault(
            str(content.get("internal_product_id")), []
        ).append(content)

    sync_by_product: dict[str, dict[str, Any]] = {
        str(sync.get("internal_product_id")): sync
        for sync in syncs
        if sync.get("internal_product_id")
    }

    candidates_by_raw_page: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        raw_page_id = candidate.get("raw_page_id")
        if raw_page_id:
            candidates_by_raw_page.setdefault(str(raw_page_id), []).append(candidate)

    # check_historical_image_capability() is a filesystem probe, not a DB
    # call, and its result does not vary per candidate -- computed at
    # most once and reused, instead of once per candidate.
    capability_cache: dict[str, Any] = {}

    def _capability() -> Any:
        if "value" not in capability_cache:
            capability_cache["value"] = check_historical_image_capability(
                PROJECT_ROOT
            )
        return capability_cache["value"]

    bundles: dict[str, dict[str, Any]] = {}

    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id"))
        candidate_code = candidate.get("candidate_code")
        candidate_images = images_by_candidate.get(candidate_id, [])

        source_evidence = candidate.get("source_evidence") or {}
        local_media_paths = filter_historical_image_paths(
            source_evidence.get("local_media_paths") or []
        )

        historical_capability_available: bool | None = None
        historical_capability_reason: str | None = None
        sibling_candidate_codes: list[str] = []
        sibling_source_evidence: list[dict[str, Any]] = []

        if local_media_paths and not candidate_images:
            capability = _capability()
            historical_capability_available = capability.available
            historical_capability_reason = capability.reason

            raw_page_id = candidate.get("raw_page_id")

            if raw_page_id:
                siblings = candidates_by_raw_page.get(str(raw_page_id), [])
                sibling_candidate_codes = [
                    sibling["candidate_code"]
                    for sibling in siblings
                    if sibling.get("candidate_id") != candidate.get("candidate_id")
                    and sibling.get("candidate_code")
                ]
                sibling_source_evidence = [
                    sibling.get("source_evidence") or {}
                    for sibling in siblings
                    if sibling.get("candidate_id") != candidate.get("candidate_id")
                ]

        internal_product = internal_product_by_candidate.get(candidate_id)
        candidate_contents: list[dict[str, Any]] = []
        sync: dict[str, Any] | None = None

        if internal_product:
            internal_product_id = str(internal_product.get("internal_product_id"))
            candidate_contents = contents_by_product.get(internal_product_id, [])
            sync = sync_by_product.get(internal_product_id)

        bundles[candidate_code] = {
            "candidate": candidate,
            "references": references_by_candidate.get(candidate_id, []),
            "discovery_sources": discovery_by_candidate.get(candidate_id, []),
            "images": candidate_images,
            "internal_product": internal_product,
            "contents": candidate_contents,
            "sync": sync,
            "historical_local_media_paths": local_media_paths,
            "historical_capability_available": historical_capability_available,
            "historical_capability_reason": historical_capability_reason,
            "sibling_candidate_codes": sibling_candidate_codes,
            "sibling_source_evidence": sibling_source_evidence,
        }

    return bundles


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


def derive_sync_recovery_state(
    sync: dict[str, Any] | None,
    internal_product: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """
    Canonical, reusable recovery-condition check over one
    woocommerce_product_syncs row and its internal_products row.

    Returns (derived_state, recovery_state) when a recovery condition is
    active, else None. Pure function, no I/O -- every check here mirrors
    a field create_woocommerce_draft.py or
    sync_woocommerce_product_status.py already writes; this function
    reads those fields, it does not decide their meaning independently.

    This is the single source of truth for "is this product in a Woo
    recovery condition" (CLAUDE.md section 2.6/18: never blindly retry
    an uncertain remote Woo operation) -- both derive_candidate_state()
    (via _derive_recovery_state, below, over one candidate's full
    bundle) and scripts/preflight_pipeline.py's recovery-health check
    (over every product, sync-row-only) call this exact function so the
    two can never silently disagree about what counts as "needs
    recovery review."
    """
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


def _derive_recovery_state(
    bundle: dict[str, Any],
) -> tuple[str, str] | None:
    """Return (derived_state, recovery_state) if the candidate is in a
    recovery condition, else None. Thin wrapper over
    derive_sync_recovery_state() -- see that function's docstring for
    why this must stay a wrapper, not a second copy."""
    return derive_sync_recovery_state(bundle["sync"], bundle["internal_product"])


def _historical_reference_image_fallback_hint(bundle: dict[str, Any]) -> str | None:
    """
    For a historical candidate that cannot get a deterministic single
    main image from its own Facebook export (zero or several images),
    surface -- but never auto-execute -- an available approved-reference
    image fallback (CLAUDE.md section 8.1 source priority:
    image_rules.select_preferred_image_reference).

    Deliberately does not dispatch scripts/download_bookstore_product_
    image.py itself: that script drives a real browser against a live
    external site, which this orchestrator does not invoke unattended
    across a production batch. This function only tells a human
    reviewer that a usable MATCH reference exists, naming its source
    type, so they do not have to search for one manually.
    """
    candidate = bundle["candidate"]
    match_references = [
        reference
        for reference in bundle["references"]
        if reference.get("match_decision") == MatchDecision.MATCH
    ]

    if not match_references:
        return None

    decision = image_rules.select_preferred_image_reference(
        candidate, match_references
    )

    if decision.outcome != Outcome.AUTO_PASS:
        return None

    source_type = decision.evidence.get("source_type")

    return (
        f"A {source_type} reference is available as a fallback image "
        "source (download_bookstore_product_image.py currently supports "
        "BOOKSTORE references only; other source types require manual "
        "download)."
    )


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
                    # Second-step deterministic resolver (see
                    # resolve_historical_candidate_images's own
                    # docstring): a shared source post is not itself
                    # proof of ambiguity when this candidate's own
                    # image-derived (MANUAL_VISUAL_REVIEW) provenance
                    # already, explicitly pins it to exact image
                    # path(s) -- CLAUDE.md section 11's "explicit
                    # candidate mapping" may already be persisted, not
                    # merely inferred from post membership.
                    provenance_decision = (
                        image_rules.resolve_historical_candidate_images(
                            candidate_source_evidence=(
                                candidate.get("source_evidence") or {}
                            ),
                            sibling_source_evidence=(
                                bundle.get("sibling_source_evidence") or []
                            ),
                        )
                    )

                    if provenance_decision.outcome == Outcome.AUTO_PASS:
                        ownership_decision = provenance_decision

                if ownership_decision.outcome != Outcome.AUTO_PASS:
                    # Historical-migration draft-safe policy (CLAUDE.md
                    # section 6.2/8.1): before stopping at the ownership-
                    # ambiguous human gate, try a deterministic reference-
                    # image fallback -- an approved-source reference that
                    # already, unambiguously identifies this exact
                    # candidate can supply a usable image without ever
                    # needing to resolve which shared Facebook-post image
                    # belongs to which candidate. Only ever attempted for
                    # a historical candidate; a live candidate reaching
                    # this branch (it cannot today -- ownership ambiguity
                    # is itself a historical-only concept) would never
                    # reach select_historical_draft_safe_image_reference.
                    fallback_decision = (
                        image_rules.select_historical_draft_safe_image_reference(
                            candidate=candidate,
                            references=bundle["references"],
                        )
                        if is_historical_candidate_code(candidate_code)
                        else DecisionResult(
                            outcome=Outcome.BLOCKED,
                            rule_code="IMAGE_REFERENCE_NONE_DRAFT_SAFE",
                            reason="Not a historical candidate.",
                        )
                    )

                    if fallback_decision.outcome == Outcome.AUTO_PASS:
                        return CandidateState(
                            candidate_code=candidate_code,
                            candidate_id=candidate_id,
                            product_code=product_code,
                            derived_state=(
                                "IMAGE_REFERENCE_FALLBACK_PENDING_HISTORICAL"
                            ),
                            auto_reference_id=str(
                                fallback_decision.evidence["reference_id"]
                            ),
                            warnings=warnings,
                        )

                    return CandidateState(
                        candidate_code=candidate_code,
                        candidate_id=candidate_id,
                        product_code=product_code,
                        derived_state="IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
                        human_gate=True,
                        human_gate_reason=(
                            f"{ownership_decision.reason} A draft-safe "
                            "reference-image fallback was also attempted "
                            f"and did not qualify: {fallback_decision.reason}"
                        ),
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

                # Historical-migration draft-safe policy (explicit
                # shop-owner business authorization, CLAUDE.md section
                # 6.2/14.7): ownership is unambiguous (checked above) and
                # the extraction capability is available (checked above)
                # -- ingesting this candidate's own Facebook-export
                # images is a deterministic, bounded, non-judgment
                # action. Automatable: no human_gate.
                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=product_code,
                    derived_state="IMAGE_INGEST_PENDING_HISTORICAL",
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

        is_historical = is_historical_candidate_code(candidate_code)

        # Historical-migration draft-safe rights auto-classification
        # (CLAUDE.md 6.2/14.7): rights classification is per-image and
        # never gated on how many images the candidate carries --
        # classify_historical_image_rights() decides each image's
        # provenance-based rights independently. Main-image *selection*
        # (CLAUDE.md 14.5: "exactly one eligible image") is the separate
        # question of which single rights-eligible image is PRIMARY --
        # image_rules.select_primary_candidate_image() decides that
        # deterministically for any number of eligible images already
        # confirmed to belong to this one candidate; the remaining
        # eligible images become GALLERY images (never demoted to
        # non-publishable), never a human gate merely because several
        # images all belong to the same candidate.
        eligible_images: list[tuple[dict[str, Any], str]] = []

        if is_historical:
            reference_by_id = {
                str(reference["reference_id"]): reference
                for reference in bundle["references"]
                if reference.get("reference_id")
            }

            for image in images:
                rights_decision = image_rules.classify_historical_image_rights(
                    image,
                    reference_by_id=reference_by_id,
                )

                if rights_decision.outcome == Outcome.AUTO_PASS:
                    eligible_images.append(
                        (image, rights_decision.evidence["rights_status"])
                    )

            if eligible_images:
                primary_decision = image_rules.select_primary_candidate_image(
                    eligible_images
                )

                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=product_code,
                    derived_state="IMAGE_APPROVAL_PENDING_HISTORICAL",
                    auto_main_image_id=str(
                        primary_decision.evidence["primary_image_id"]
                    ),
                    auto_rights_status=primary_decision.evidence[
                        "primary_rights_status"
                    ],
                    auto_gallery_images=tuple(
                        (str(image_id), rights_status)
                        for image_id, rights_status in primary_decision.evidence[
                            "gallery"
                        ]
                    ),
                    warnings=warnings,
                )

        historical_fallback_hint = (
            _historical_reference_image_fallback_hint(bundle)
            if is_historical
            else None
        )

        # Historical candidates: rely on the freshly-computed per-image
        # classification above (which already accounts for own-Facebook-
        # export and approved-reference provenance regardless of image
        # count) instead of only the persisted usage_rights_status --
        # otherwise a candidate with 2+ own-export images would show
        # "rights unknown" even though every one of them is really
        # STORE_OWNED-eligible (the exact stale-state bug this replaces).
        # Live candidates: unchanged, persisted usage_rights_status only.
        has_publishable_rights = (
            bool(eligible_images)
            if is_historical
            else any(
                image.get("usage_rights_status") in PUBLISHABLE_RIGHTS_STATUSES
                for image in images
            )
        )

        if not has_publishable_rights:
            if is_historical:
                fallback_decision = (
                    image_rules.select_historical_draft_safe_image_reference(
                        candidate=candidate,
                        references=bundle["references"],
                    )
                )

                if fallback_decision.outcome == Outcome.AUTO_PASS:
                    return CandidateState(
                        candidate_code=candidate_code,
                        candidate_id=candidate_id,
                        product_code=product_code,
                        derived_state=(
                            "IMAGE_REFERENCE_FALLBACK_PENDING_HISTORICAL"
                        ),
                        auto_reference_id=str(
                            fallback_decision.evidence["reference_id"]
                        ),
                        warnings=warnings,
                    )

            reason = (
                "No image has a publishable usage-rights status. Image "
                "rights cannot be inferred automatically -- confirm "
                "rights via review_product_images.py."
            )

            if historical_fallback_hint:
                reason += f" {historical_fallback_hint}"

            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=product_code,
                derived_state="RIGHTS_REVIEW_REQUIRED",
                human_gate=True,
                human_gate_reason=reason,
                warnings=warnings,
            )

        # Note: for a historical candidate, eligible_images is always
        # empty by this point -- any non-empty eligible_images already
        # returned above via select_primary_candidate_image(). This
        # branch is reached only for a live candidate with publishable-
        # rights images but no single approved selection yet.
        reason = (
            "Images with usable rights exist, but no single validated, "
            "selected, publish-eligible main image has been approved. "
            "Run review_product_images.py to select and approve one."
        )

        if historical_fallback_hint:
            reason += f" {historical_fallback_hint}"

        return CandidateState(
            candidate_code=candidate_code,
            candidate_id=candidate_id,
            product_code=product_code,
            derived_state="IMAGE_REVIEW_REQUIRED",
            human_gate=True,
            human_gate_reason=reason,
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
        # Historical-migration draft-safe content auto-enrichment
        # (CLAUDE.md 6.2/15): when the sole reason content is stuck at
        # REVIEW_REQUIRED is that it is still the generic metadata-only
        # safe draft (never a boilerplate/other content_rules failure --
        # is_generic_safe_draft() recomputes the exact same comparison
        # prepare_product_content.py's own APPROVE path already used to
        # decline it), and a draft-safe reference description exists,
        # this is automatable -- no human judgment is needed to notice
        # that verified source material is available and unused.
        vi_content = next(
            (
                content
                for content in bundle["contents"]
                if content.get("content_language") == "vi"
            ),
            None,
        )

        if (
            is_historical_candidate_code(candidate_code)
            and vi_content is not None
            and content_status == InternalProductContentStatus.REVIEW_REQUIRED
        ):
            generated = build_safe_draft(internal_product)

            if is_generic_safe_draft(content=vi_content, generated=generated):
                content_selection = (
                    content_rules.select_historical_draft_safe_content_reference(
                        candidate=candidate,
                        references=bundle["references"],
                    )
                )

                if content_selection.outcome == Outcome.AUTO_PASS:
                    return CandidateState(
                        candidate_code=candidate_code,
                        candidate_id=candidate_id,
                        product_code=product_code,
                        derived_state="CONTENT_REVISE_PENDING_HISTORICAL",
                        warnings=warnings,
                    )

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
    is_historical = is_historical_candidate_code(candidate_code)

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
                if is_historical:
                    # Historical-migration draft-safe policy (explicit
                    # shop-owner business authorization, CLAUDE.md
                    # section 6.2/9.4): an unresolved (never match_
                    # decision-written) reference does not, by itself,
                    # block the draft-safe path when match_candidate_
                    # identity.py has already evaluated this candidate
                    # and genuinely concluded insufficient evidence (no
                    # active isbn/author/page_count/publisher conflict)
                    # -- evidenced by a stamped decision_fingerprint plus
                    # an empty conflict_fields. This is the exact same
                    # business situation CLAUDE.md 9.4 already accepts
                    # (a POSSIBLE_MATCH/MANUAL_REVIEW/no-reference
                    # candidate proceeding as enrichment-only), not a
                    # relaxation of it -- match_candidate_identity.py's
                    # own AUTO mode simply never persists a match_
                    # decision for a REVIEW_REQUIRED-insufficient-
                    # evidence outcome (by design: it never forces a
                    # decision, CLAUDE.md 9.2), so "unresolved" alone
                    # cannot distinguish "genuinely never evaluated yet"
                    # from "evaluated, and missing data is all that's
                    # stopping it" without this check. Never sets
                    # IDENTITY_VERIFIED, never invents a missing author
                    # -- identity_status stays exactly IDENTITY_PENDING.
                    source_evidence = candidate.get("source_evidence") or {}
                    already_evaluated_no_conflict = bool(
                        source_evidence.get("decision_fingerprint")
                    ) and not (candidate.get("conflict_fields") or [])

                    if already_evaluated_no_conflict:
                        return CandidateState(
                            candidate_code=candidate_code,
                            candidate_id=candidate_id,
                            product_code=None,
                            derived_state="IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE",
                            warnings=[
                                "Identity matching evaluated this "
                                "candidate and found insufficient "
                                "evidence (no active conflict) rather "
                                "than a MATCH -- proceeding under the "
                                "historical draft-safe policy with "
                                "unverified identity. Reference remains "
                                "unresolved (match_decision is not "
                                "set); this is enrichment only."
                            ],
                        )

                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=None,
                    derived_state="REFERENCE_COLLECTED",
                )

            if is_historical:
                # Historical-migration draft-safe policy (explicit shop-
                # owner business authorization): reference discovery is
                # enrichment, not a mandatory blocker. Resolved references
                # with no MATCH still let create_internal_product.py run
                # (it accepts POSSIBLE_MATCH/MANUAL_REVIEW/no-reference for
                # FB-HIST candidates) -- readiness itself is decided later
                # by evaluate_historical_draft_safe_readiness().
                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=None,
                    derived_state="IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE",
                    warnings=[
                        "Reference metadata was collected, but automatic "
                        "identity matching was inconclusive (no MATCH "
                        "decision). Proceeding under the historical "
                        "draft-safe policy with unverified identity."
                    ],
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

        # Only a discovery still in discovery_status=="SELECTED" is
        # actually collectible: collect_reference_metadata.py's own
        # select_next_reference_queue_item() requires exactly that status
        # (plus the joined source_urls.crawl_status=="PENDING") and raises
        # RuntimeError("No selected PENDING reference URL matched the
        # supplied selector.") for anything else. A discovery whose crawl
        # already permanently failed (discovery_status=="FAILED", e.g. no
        # metadata parser for that domain) still has
        # is_selected_for_crawl==True forever, so checking that flag alone
        # falsely reports REFERENCE_REGISTERED as "safe to auto-invoke" --
        # run_batch.py would then redispatch collect_reference_metadata.py
        # every run and fail the same way indefinitely, never surfacing
        # for review. Matching the collector's own status check here
        # keeps the two in sync instead of duplicating separate criteria.
        selected_sources = [
            source
            for source in discovery_sources
            if source.get("is_selected_for_crawl") is True
            and source.get("discovery_status") == "SELECTED"
        ]

        if selected_sources:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="REFERENCE_REGISTERED",
            )

        failed_selected_sources = [
            source
            for source in discovery_sources
            if source.get("is_selected_for_crawl") is True
            and source.get("discovery_status") == "FAILED"
        ]

        if is_historical:
            if failed_selected_sources:
                # A reference source was found and selected, but its
                # crawl already permanently failed (see comment above) --
                # not something to keep silently retrying. Under CLAUDE.md
                # 6.2, an unavailable reference is enrichment, not a
                # blocker, so this still proceeds draft-safe; it just no
                # longer masquerades as REFERENCE_REGISTERED.
                return CandidateState(
                    candidate_code=candidate_code,
                    candidate_id=candidate_id,
                    product_code=None,
                    derived_state="IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE",
                    warnings=[
                        "The selected reference source failed to crawl "
                        "and cannot be automatically retried. Proceeding "
                        "under the historical draft-safe policy with "
                        "unverified identity and no enrichment "
                        "reference."
                    ],
                )

            # No reference source is selected/registered at all for this
            # historical candidate. Per the shop owner's business
            # authorization, this is enrichment that was not found -- not
            # a mandatory blocker -- so fall straight through to the
            # draft-safe path instead of stopping for a human source-
            # priority decision (the discovery/no-discovery branches
            # below remain a human gate for non-historical candidates).
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE",
                warnings=[
                    "No reference source was found/selected for this "
                    "historical candidate. Proceeding under the "
                    "historical draft-safe policy with unverified "
                    "identity and no enrichment reference."
                ],
            )

        if failed_selected_sources:
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=None,
                derived_state="EXTRACTED",
                human_gate=True,
                human_gate_reason=(
                    "The selected reference source failed to crawl and "
                    "cannot be automatically retried. Human decision "
                    "required: select a different reference source "
                    "(register_reference_source.py --select-for-crawl), "
                    "using the CLAUDE.md reference identity priority: "
                    "publisher > authorized supplier > reliable bookstore "
                    "> Fahasa > Facebook, or resolve the crawl failure "
                    "(see the source_urls row's last_error)."
                ),
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
        if is_historical_candidate_code(candidate_code):
            # Historical-migration draft-safe policy (explicit shop-owner
            # business authorization, CLAUDE.md section 6/17): WooCommerce
            # DRAFT creation (status="draft", never "publish", never a
            # price) is a reversible, authorized migration operation for
            # FB-HIST candidates that already passed
            # evaluate_historical_draft_safe_readiness(). No per-batch
            # --allow-woo-draft confirmation is required. Non-historical
            # candidates are completely unaffected -- they keep the
            # human_gate=True branch below unconditionally.
            return CandidateState(
                candidate_code=candidate_code,
                candidate_id=candidate_id,
                product_code=product_code,
                derived_state="READY_FOR_DRAFT_HISTORICAL",
                warnings=warnings,
            )

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
    src.domain.rules.readiness_rules gate scripts/check_draft_readiness.py
    already calls (evaluate_readiness for non-historical candidates,
    evaluate_historical_draft_safe_readiness for FB-HIST candidates), over
    the data load_candidate_bundle() already read -- no second copy of the
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

    candidate = bundle["candidate"]
    evaluator = (
        readiness_rules.evaluate_historical_draft_safe_readiness
        if is_historical_candidate_code(candidate.get("candidate_code"))
        else readiness_rules.evaluate_readiness
    )

    return evaluator(
        product=internal_product,
        candidate=candidate,
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
