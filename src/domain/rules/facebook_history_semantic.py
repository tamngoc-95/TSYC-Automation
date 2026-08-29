"""Provider-agnostic SECONDARY (semantic) classification contract, routing,
and final-decision synthesis for the OFFLINE historical Facebook
migration screening pipeline.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    src/domain/rules/facebook_history_classification.py (the FIRST,
    deterministic layer) already classifies every historical record into
    tsyc_relevance/post_type/candidate_eligible using only regex/marker
    evidence. That layer is complete and does not change here.

    This module adds a SECOND, optional layer for the subset of records
    the first layer could not confidently decide (needs_secondary_
    review=True) -- e.g. a record whose only book-specific evidence is a
    bare "sách" next to an incidental commerce word ("Tỉ giá giờ quá
    đẹp...1€=30000vnd" is a currency-conversion note, not a book price).

    It defines:
      - the structured, provider-neutral request/response shape a
        semantic classifier speaks (SemanticClassificationInput/Result)
      - the classifier interface itself (HistoricalPostSemanticClassifier)
        -- concrete providers (an offline mock now, a future Claude-API-
        backed provider) live in src/services/facebook_history_semantic_
        provider.py and implement this interface; this module never
        constructs one and never performs I/O of any kind
      - which records even need a second opinion at all (route_record())
      - how a deterministic result, an optional semantic result, and a
        routing decision combine into one final_migration_decision
        (synthesize_final_decision())

    This is still OFFLINE-ONLY, pre-ingestion screening. Nothing here
    creates a source_urls/raw_pages/product_candidates row, writes to
    Supabase, or calls WooCommerce. A final_migration_decision of
    INCLUDE is a screening signal for a human (or a later, explicitly-
    approved import stage) -- never itself a product_candidates row.

Pure functions/dataclasses only: no I/O, no randomness, no clock,
network, or Supabase/WooCommerce/Claude-API dependency anywhere in this
module. route_record() and synthesize_final_decision() are both pure --
same arguments always produce the same result (see tests/test_facebook_
history_routing.py's idempotency test).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.domain.rules.facebook_history_classification import (
    ALL_POST_TYPES,
    ClassificationResult,
    TsycRelevance,
)


# --- semantic classifier input/output contract ----------------------------


@dataclass(frozen=True)
class SemanticClassificationInput:
    """Everything a semantic classifier is given about one historical
    record -- the record's own content plus the first layer's already-
    computed deterministic evidence and decision. A provider must never
    be handed raw HTML, a Facebook action heading used as evidence (see
    facebook_history_classification.classify()'s own docstring for why),
    or anything beyond what is listed here.
    """

    record_id: int
    date_text: str
    full_text: str
    heading: str
    strong_markers: tuple[str, ...]
    weak_markers: tuple[str, ...]
    folder_slug_evidence: tuple[str, ...]
    structural_mention_id: str | None
    local_image_count: int
    local_video_count: int
    deterministic_tsyc_relevance: str
    deterministic_post_type: str
    deterministic_candidate_eligible: bool
    deterministic_classification_reason: str


@dataclass(frozen=True)
class SemanticClassificationResult:
    """A provider's structured, provider-neutral opinion about one
    record. Every provider (mock or real) must return exactly this
    shape regardless of how it internally arrived at it."""

    semantic_post_type: str
    product_migration_relevant: bool
    confidence: float
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    extracted_product_hints: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.semantic_post_type not in ALL_POST_TYPES:
            raise ValueError(f"Unknown semantic_post_type: {self.semantic_post_type!r}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {self.confidence!r}")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "extracted_product_hints", tuple(self.extracted_product_hints))


@dataclass(frozen=True)
class SemanticCallProvenance:
    """Provenance for exactly one classify() call against a real (or
    cached) provider -- kept separate from SemanticClassificationResult
    because it describes *how the answer was obtained*, not part of the
    provider-neutral answer itself, and a provider is free to leave it
    unset (the mock provider has no model/cache/prompt-version concept).

    A caller obtains this by calling a provider's own
    `classify_with_provenance()` method where one is offered (see
    src.services.facebook_history_semantic_provider.
    ClaudeHistoricalSemanticProvider) -- the ABC's plain classify()
    method never returns it, to keep every provider's minimal required
    surface exactly SemanticClassificationInput -> SemanticClassification
    Result.
    """

    provider: str
    model: str
    prompt_version: str
    input_hash: str
    cache_hit: bool


class HistoricalPostSemanticClassifier(ABC):
    """The provider boundary every semantic classifier must implement.

    Concrete implementations live in src/services/facebook_history_
    semantic_provider.py:
      - MockHistoricalSemanticProvider -- deterministic, offline, no
        network, safe for tests and for this OFFLINE-only pipeline phase.
      - ClaudeHistoricalSemanticProvider -- a documented, NOT-YET-
        IMPLEMENTED stub. Constructing or calling it raises
        NotImplementedError until Claude API integration is explicitly
        approved -- see that class's own docstring.

    A caller (src/services/facebook_history_secondary_classification.py)
    only ever depends on this interface, never on a concrete provider
    class, so swapping the mock for a real LLM later requires no change
    to routing, synthesis, or CSV/summary output code.
    """

    @abstractmethod
    def classify(self, request: SemanticClassificationInput) -> SemanticClassificationResult:
        """Return a structured opinion about one record. Must not raise
        for a well-formed request; must never perform a network call in
        an implementation used during this OFFLINE phase."""
        raise NotImplementedError


# --- routing: which records even need a second opinion --------------------


class RoutingDecision:
    """Every possible outcome of route_record() -- see its docstring for
    the exact partition. Every deterministic ClassificationResult maps to
    exactly one of these four; the set is exhaustive and mutually
    exclusive (tests/test_facebook_history_routing.py enumerates every
    branch of classify() and asserts this)."""

    SKIP_LOW = "SKIP_LOW"
    BYPASS_STRONG_INCLUDE = "BYPASS_STRONG_INCLUDE"
    BYPASS_CONFIDENT_EXCLUDE = "BYPASS_CONFIDENT_EXCLUDE"
    SEND_TO_SEMANTIC = "SEND_TO_SEMANTIC"


ALL_ROUTING_DECISIONS = frozenset(
    {
        RoutingDecision.SKIP_LOW,
        RoutingDecision.BYPASS_STRONG_INCLUDE,
        RoutingDecision.BYPASS_CONFIDENT_EXCLUDE,
        RoutingDecision.SEND_TO_SEMANTIC,
    }
)


def route_record(deterministic: ClassificationResult) -> str:
    """Decide whether one record needs a semantic second opinion at all.

    Returns exactly one RoutingDecision:

        SKIP_LOW              -- tsyc_relevance=LOW. The first layer
            found no evidence whatsoever; there is nothing for a
            semantic classifier to add. Never sent to a provider.

        BYPASS_STRONG_INCLUDE -- tsyc_relevance=HIGH AND
            candidate_eligible=True. Strong TSYC brand/structural
            evidence plus concrete listing evidence already
            deterministically established this is a real product post
            (or an eligible strong-brand promotion) -- unambiguous,
            never sent to a provider.

        BYPASS_CONFIDENT_EXCLUDE -- everything else that the first layer
            did NOT flag for secondary review (needs_secondary_review=
            False) and that is not already covered above. In practice
            this is a HIGH-relevance record the first layer confidently
            decided is NOT a product post at all (e.g. strong TSYC brand
            evidence together with explicit book-review language) --
            still unambiguous, still never sent to a provider.

        SEND_TO_SEMANTIC -- needs_secondary_review=True: the first layer
            itself flagged genuine ambiguity (a customer-feedback post
            that also reads like a solicitation, book-adjacent vocabulary
            without a confirmed brand marker, a counterfeit warning, a
            commerce-only post with no book word, a negative-business
            exclusion, ...). Only this bucket is ever handed to a
            semantic classifier.

    Pure function: no I/O, depends only on the given ClassificationResult.
    """
    if deterministic.tsyc_relevance == TsycRelevance.LOW:
        return RoutingDecision.SKIP_LOW

    if deterministic.tsyc_relevance == TsycRelevance.HIGH and deterministic.candidate_eligible:
        return RoutingDecision.BYPASS_STRONG_INCLUDE

    if deterministic.needs_secondary_review:
        return RoutingDecision.SEND_TO_SEMANTIC

    return RoutingDecision.BYPASS_CONFIDENT_EXCLUDE


# --- final decision synthesis ----------------------------------------------


class FinalDecision:
    INCLUDE = "INCLUDE"
    EXCLUDE = "EXCLUDE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


ALL_FINAL_DECISIONS = frozenset(
    {FinalDecision.INCLUDE, FinalDecision.EXCLUDE, FinalDecision.REVIEW_REQUIRED}
)

# Centralized confidence gate -- see synthesize_final_decision(). Never
# hard-code a competing threshold literal elsewhere; import this (or pass
# an explicit override) instead. 0.75 is a deliberately conservative
# starting point for an offline mock/early-LLM phase: a provider must be
# clearly, not just marginally, confident before its opinion alone flips
# a record to INCLUDE or EXCLUDE without a human ever reviewing it.
DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 0.75

# Decision-source labels recorded on every FinalDecisionResult -- see its
# docstring. A stable, small vocabulary (never per-record interpolated
# text) so downstream aggregation ("decision_source_counts" in the
# summary JSON) is meaningful.
DECISION_SOURCE_DETERMINISTIC_LOW = "DETERMINISTIC_LOW"
DECISION_SOURCE_DETERMINISTIC_STRONG = "DETERMINISTIC_STRONG"
DECISION_SOURCE_DETERMINISTIC_CONFIDENT = "DETERMINISTIC_CONFIDENT"
DECISION_SOURCE_SEMANTIC = "SEMANTIC"
DECISION_SOURCE_SEMANTIC_LOW_CONFIDENCE = "SEMANTIC_LOW_CONFIDENCE"


@dataclass(frozen=True)
class FinalDecisionResult:
    """The immutable, fully-provenanced result of deciding one record's
    migration fate. Both deterministic and semantic (when present) are
    kept verbatim -- this dataclass only ever *adds* a layer on top; it
    never mutates or discards either sub-result (project requirement:
    "Never overwrite the original deterministic classification")."""

    record_id: int
    routing_decision: str
    final_migration_decision: str
    decision_source: str
    reason_codes: tuple[str, ...]
    deterministic: ClassificationResult
    semantic: SemanticClassificationResult | None = None

    def __post_init__(self) -> None:
        if self.routing_decision not in ALL_ROUTING_DECISIONS:
            raise ValueError(f"Unknown routing_decision: {self.routing_decision!r}")
        if self.final_migration_decision not in ALL_FINAL_DECISIONS:
            raise ValueError(
                f"Unknown final_migration_decision: {self.final_migration_decision!r}"
            )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


def synthesize_final_decision(
    record_id: int,
    deterministic: ClassificationResult,
    routing_decision: str,
    semantic: SemanticClassificationResult | None,
    *,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
) -> FinalDecisionResult:
    """Combine a routing decision, the deterministic result, and an
    optional semantic result into one final, fully-provenanced decision.

    Rules (project requirement, applied in this order):
      1. routing_decision=BYPASS_STRONG_INCLUDE  -> INCLUDE
             (deterministic strong PRODUCT_POST/PROMOTION evidence)
      2. routing_decision=SKIP_LOW                -> EXCLUDE
             (no evidence at all)
      3. routing_decision=BYPASS_CONFIDENT_EXCLUDE -> EXCLUDE
             (deterministically, confidently NOT a product post)
      4. routing_decision=SEND_TO_SEMANTIC, semantic is required:
           - confidence >= high_confidence_threshold AND
             product_migration_relevant=True  -> INCLUDE
           - confidence >= high_confidence_threshold AND
             product_migration_relevant=False -> EXCLUDE
           - confidence <  high_confidence_threshold (low confidence, or
             the provider itself could not resolve a conflict)
                                                 -> REVIEW_REQUIRED

    Pure function: no I/O, no randomness, no clock. Raises ValueError if
    routing_decision=SEND_TO_SEMANTIC but semantic is None (a caller
    contract violation -- routing decided a second opinion was required,
    so one must have actually been obtained before calling this).
    """
    if routing_decision == RoutingDecision.BYPASS_STRONG_INCLUDE:
        return FinalDecisionResult(
            record_id=record_id,
            routing_decision=routing_decision,
            final_migration_decision=FinalDecision.INCLUDE,
            decision_source=DECISION_SOURCE_DETERMINISTIC_STRONG,
            reason_codes=(deterministic.classification_reason,),
            deterministic=deterministic,
            semantic=None,
        )

    if routing_decision == RoutingDecision.SKIP_LOW:
        return FinalDecisionResult(
            record_id=record_id,
            routing_decision=routing_decision,
            final_migration_decision=FinalDecision.EXCLUDE,
            decision_source=DECISION_SOURCE_DETERMINISTIC_LOW,
            reason_codes=(deterministic.classification_reason,),
            deterministic=deterministic,
            semantic=None,
        )

    if routing_decision == RoutingDecision.BYPASS_CONFIDENT_EXCLUDE:
        return FinalDecisionResult(
            record_id=record_id,
            routing_decision=routing_decision,
            final_migration_decision=FinalDecision.EXCLUDE,
            decision_source=DECISION_SOURCE_DETERMINISTIC_CONFIDENT,
            reason_codes=(deterministic.classification_reason,),
            deterministic=deterministic,
            semantic=None,
        )

    if routing_decision != RoutingDecision.SEND_TO_SEMANTIC:
        raise ValueError(f"Unknown routing_decision: {routing_decision!r}")

    if semantic is None:
        raise ValueError(
            "routing_decision=SEND_TO_SEMANTIC requires a semantic result; "
            "got None. The caller must obtain one from a "
            "HistoricalPostSemanticClassifier before calling this function."
        )

    reason_codes = (deterministic.classification_reason,) + semantic.reason_codes

    if semantic.confidence >= high_confidence_threshold:
        final = FinalDecision.INCLUDE if semantic.product_migration_relevant else FinalDecision.EXCLUDE
        return FinalDecisionResult(
            record_id=record_id,
            routing_decision=routing_decision,
            final_migration_decision=final,
            decision_source=DECISION_SOURCE_SEMANTIC,
            reason_codes=reason_codes,
            deterministic=deterministic,
            semantic=semantic,
        )

    return FinalDecisionResult(
        record_id=record_id,
        routing_decision=routing_decision,
        final_migration_decision=FinalDecision.REVIEW_REQUIRED,
        decision_source=DECISION_SOURCE_SEMANTIC_LOW_CONFIDENCE,
        reason_codes=reason_codes,
        deterministic=deterministic,
        semantic=semantic,
    )
