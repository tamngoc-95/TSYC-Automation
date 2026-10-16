"""Deterministic content-validation rules.

Covers product_contents.content_status (src.domain.content_status.
ContentStatus). CLAUDE.md section 15: content must be based only on
verified data, must never contain internal workflow instructions, and
default generated drafts must be customer-facing. REVISE is a
prepare_product_content.py --action, not a content_status value -- see
src.domain.content_status's module docstring.

Rule codes implemented here:

    CONTENT_VERIFIED_FACTS_ONLY        AUTO_PASS
    CONTENT_MISSING_OPTIONAL_METADATA  AUTO_PASS (non-blocking)
    CONTENT_SAFE_APPROVAL              AUTO_PASS / REVIEW_REQUIRED / BLOCKED
    CONTENT_INTERNAL_BOILERPLATE       AUTO_PASS / REVIEW_REQUIRED
    CONTENT_UNSUPPORTED_CLAIM          REVIEW_REQUIRED
    CONTENT_REFERENCE_CONFLICT         REVIEW_REQUIRED

See docs/TSYC_DECISION_MATRIX.md for the full specification.
"""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import MatchDecision
from src.domain.reference_sources import REFERENCE_SOURCE_PRIORITY
from src.domain.rules import author_rules, storefront_text
from src.domain.rules.identity_rules import reference_business_conflict_reason

# --- rule codes ----------------------------------------------------

CONTENT_STOREFRONT_TEXT_DEFECT = "CONTENT_STOREFRONT_TEXT_DEFECT"
CONTENT_VERIFIED_FACTS_ONLY = "CONTENT_VERIFIED_FACTS_ONLY"
CONTENT_MISSING_OPTIONAL_METADATA = "CONTENT_MISSING_OPTIONAL_METADATA"
CONTENT_INTERNAL_BOILERPLATE = "CONTENT_INTERNAL_BOILERPLATE"
CONTENT_UNSUPPORTED_CLAIM = "CONTENT_UNSUPPORTED_CLAIM"
CONTENT_REFERENCE_CONFLICT = "CONTENT_REFERENCE_CONFLICT"
CONTENT_SAFE_APPROVAL = "CONTENT_SAFE_APPROVAL"
# Historical-migration draft-safe content auto-enrichment (CLAUDE.md
# section 6.2/15) -- see select_historical_draft_safe_content_reference().
CONTENT_REFERENCE_DRAFT_SAFE_SELECTED = "CONTENT_REFERENCE_DRAFT_SAFE_SELECTED"
CONTENT_REFERENCE_NONE_DRAFT_SAFE = "CONTENT_REFERENCE_NONE_DRAFT_SAFE"
# Customer-facing text misspells the product's own structured author or
# title (2026-10-02 audit: "Chris Hadfiled" vs author "Chris Hadfield";
# quoted title "người thấy tốt" vs title "Người Thầy Tốt").
CONTENT_NAME_INCONSISTENCY = "CONTENT_NAME_INCONSISTENCY"

# A reference description shorter than this is treated as too thin to
# meaningfully identify the product -- CLAUDE.md 15.3 "description cannot
# identify the product meaningfully" is a human-review reason, not
# something this function silently accepts.
_MIN_USABLE_DESCRIPTION_LENGTH = 40

_DRAFT_SAFE_MATCH_DECISIONS = (
    MatchDecision.MATCH,
    MatchDecision.POSSIBLE_MATCH,
    MatchDecision.MANUAL_REVIEW,
)

_MATCH_DECISION_RANK = MappingProxyType(
    {
        MatchDecision.MATCH: 0,
        MatchDecision.POSSIBLE_MATCH: 1,
        MatchDecision.MANUAL_REVIEW: 2,
    }
)

# CLAUDE.md section 15.1's exact forbidden examples, plus close variants.
# Deliberately conservative substring/regex matching -- a false positive
# here just means one extra REVISE cycle, never a silently-shipped
# internal note reaching a customer.
_INTERNAL_WORKFLOW_PATTERNS = (
    re.compile(r"manager (?:must|should) review", re.IGNORECASE),
    re.compile(r"pending (?:manager|admin|staff) review", re.IGNORECASE),
    re.compile(r"should be completed later", re.IGNORECASE),
    re.compile(r"to be (?:filled|completed|updated) later", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bFIXME\b"),
    re.compile(r"placeholder text", re.IGNORECASE),
)

CUSTOMER_FACING_FIELDS = (
    "short_description",
    "long_description",
    "author_summary",
    "product_details",
    "seo_title",
    "seo_description",
)


def find_internal_workflow_language(
    content: Mapping[str, Any],
    fields: Sequence[str] = CUSTOMER_FACING_FIELDS,
) -> dict[str, str]:
    """Return {field_name: matched_phrase} for every customer-facing
    field that contains internal workflow language."""
    findings: dict[str, str] = {}
    for field in fields:
        value = content.get(field)
        if not value:
            continue
        for pattern in _INTERNAL_WORKFLOW_PATTERNS:
            match = pattern.search(str(value))
            if match:
                findings[field] = match.group(0)
                break
    return findings


def evaluate_internal_boilerplate(content: Mapping[str, Any]) -> DecisionResult:
    """
    CLAUDE.md section 15.1: customer-facing content must never contain
    internal workflow instructions ("manager must review this before
    publishing", "pending manager review", "this description should be
    completed later", ...). Any match routes to REVIEW_REQUIRED with the
    exact offending field/phrase named, so prepare_product_content.py's
    REVISE workflow has an exact deterministic target -- CLAUDE.md
    section 15.2 ("deterministic revise, validate again").
    """
    findings = find_internal_workflow_language(content)

    if findings:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_INTERNAL_BOILERPLATE,
            reason=(
                "Customer-facing content contains internal workflow "
                "language in: " + ", ".join(sorted(findings))
            ),
            evidence={"findings": findings},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_INTERNAL_BOILERPLATE,
        reason="No internal workflow language found in customer-facing content.",
        evidence={"findings": {}},
    )


def evaluate_storefront_text_quality(
    content: Mapping[str, Any],
    fields: Sequence[str] = CUSTOMER_FACING_FIELDS,
) -> DecisionResult:
    """
    CLAUDE.md 15.1/15.3 and TSYC_CONTENT_GUIDE.md sections 11-12:
    customer-facing content must not contain unresolved HTML entities or
    markup, text cut off by source extraction, internal provenance or
    workflow notes, retailer SEO boilerplate, or stock/shipping wording.
    Shared definitions: src.domain.rules.storefront_text.
    """
    findings = storefront_text.find_content_defects(content, fields)

    if findings:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_STOREFRONT_TEXT_DEFECT,
            reason=(
                "Customer-facing content has storefront text defects: "
                + "; ".join(
                    f"{field}={','.join(codes)}"
                    for field, codes in sorted(findings.items())
                )
            ),
            evidence={"findings": findings},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_STOREFRONT_TEXT_DEFECT,
        reason="No storefront text defects found in customer-facing content.",
        evidence={"findings": {}},
    )


_QUOTED_SPAN_RE = re.compile(r"[“\"«„]([^“”\"«»„]{3,160})[”\"»“]")


def find_name_inconsistencies(
    content: Mapping[str, Any],
    product: Mapping[str, Any],
    fields: Sequence[str] = CUSTOMER_FACING_FIELDS,
) -> dict[str, list[str]]:
    """
    {field: [offending text]} where customer-facing text contains a
    single-edit misspelling of the product's structured author name(s) or
    -- inside quotation marks -- of its title. Detection only: no rule
    here ever rewrites the text (a correction is a REVISE decision).
    """
    names = [
        name
        for name in author_rules.split_person_names(product.get("author"))
        if len(name.split()) >= 2
    ]
    title = str(product.get("title") or "").strip()
    findings: dict[str, list[str]] = {}

    for field in fields:
        value = content.get(field)
        if not value:
            continue
        text = str(value)
        hits: list[str] = []
        for name in names:
            hits.extend(author_rules.find_name_near_misses(text, name))
        if title and len(title.split()) >= 2:
            for match in _QUOTED_SPAN_RE.finditer(text):
                quoted = match.group(1).strip()
                if quoted.casefold() == title.casefold():
                    continue
                if author_rules.find_name_near_misses(quoted, title) == [quoted]:
                    hits.append(quoted)
        if hits:
            findings[field] = hits

    return findings


def evaluate_name_consistency(
    content: Mapping[str, Any],
    product: Mapping[str, Any],
) -> DecisionResult:
    """REVIEW_REQUIRED when customer-facing text misspells the product's
    own author/title (find_name_inconsistencies). Never auto-repaired."""
    findings = find_name_inconsistencies(content, product)

    if findings:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_NAME_INCONSISTENCY,
            reason=(
                "Customer-facing text misspells the product's author/title: "
                + "; ".join(
                    f"{field}={', '.join(repr(hit) for hit in hits)}"
                    for field, hits in sorted(findings.items())
                )
            ),
            evidence={"findings": findings},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_NAME_INCONSISTENCY,
        reason="Author/title spelling in customer-facing text is consistent.",
        evidence={"findings": {}},
    )


CONTENT_DESCRIPTION_SUBSTANCE = "CONTENT_DESCRIPTION_SUBSTANCE"


def evaluate_description_substance(content: Mapping[str, Any]) -> DecisionResult:
    """
    Automatic approval gate (2026-10-08 review of Fast Track drafts): the
    long description must actually describe the book as the shop --
    REVIEW_REQUIRED when it is
      - too thin (< storefront_text.MIN_DESCRIPTION_LENGTH characters,
        e.g. a two-sentence aphorism), or
      - the author's own first-person preface copied from the source
        ("tôi" narration outside quotation marks).
    Detection only; neither case is repairable without new text.
    """
    long_description = str(content.get("long_description") or "")
    reasons: list[str] = []
    codes: list[str] = []
    length = len(" ".join(long_description.split()))
    if length < storefront_text.MIN_DESCRIPTION_LENGTH:
        codes.append(storefront_text.THIN_DESCRIPTION)
        reasons.append(
            f"long_description has {length} characters (minimum "
            f"{storefront_text.MIN_DESCRIPTION_LENGTH}) -- too thin to describe the book"
        )
    if storefront_text.is_first_person_source(long_description):
        codes.append(storefront_text.FIRST_PERSON_SOURCE)
        reasons.append(
            "long_description is narrated in the first person (an author "
            "preface copied from the source), not a description of the book"
        )
    if reasons:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_DESCRIPTION_SUBSTANCE,
            reason="; ".join(reasons) + ".",
            evidence={"codes": codes},
        )
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_DESCRIPTION_SUBSTANCE,
        reason="Long description has substance and is not a first-person source preface.",
        evidence={"codes": []},
    )


# --- storefront description quality (2026-10-16 owner request) -------------
#
# TSYC_CONTENT_GUIDE.md sections 3/7/11/14: a description is the shop's own
# informative prose about the book -- not a copy of a retailer page, not a
# string of sales phrases, not an age recommendation the sources never made,
# and not the author's name repeated as metadata. Detection only: nothing
# here rewrites text (a fix is a REVISE decision from verified facts).
CONTENT_DESCRIPTION_QUALITY = "CONTENT_DESCRIPTION_QUALITY"

QUALITY_AUTHOR_REPETITION = "AUTHOR_REPETITION"
QUALITY_SALES_LANGUAGE = "REPETITIVE_SALES_LANGUAGE"
QUALITY_EXAGGERATED_CLAIM = "EXAGGERATED_CLAIM"
QUALITY_UNVERIFIED_AGE = "UNVERIFIED_AGE_RECOMMENDATION"
QUALITY_VERBATIM_COPY = "VERBATIM_SOURCE_COPY"
QUALITY_WALL_OF_TEXT = "WALL_OF_TEXT"

# The structured author's name more often than this in one long
# description reads as metadata repetition, not narrative. Calibrated on
# the 345 stored rows (2026-10-16): 4-5 mentions occur in legitimate
# author-centred descriptions; retailer blurbs repeat the name 6-24 times.
MAX_AUTHOR_MENTIONS_IN_DESCRIPTION = 5
# Generic sales/closing phrases: more than this many in one description is
# repetitive sales language (one closing "phù hợp cho ..." is the guide's
# own recommended ending, section 13).
MAX_SALES_PHRASES = 3
# This many consecutive words shared with a source description is copying,
# not a summary written from verified facts.
MAX_VERBATIM_RUN_WORDS = 40
# A long description at least this long must be split into paragraphs.
SINGLE_PARAGRAPH_MAX_LENGTH = 700

_SALES_PHRASE_RE = re.compile(
    r"\b(?:phù hợp (?:cho|với)|món quà (?:ý nghĩa|tuyệt vời)|tuyệt vời|hấp dẫn|đáng đọc|cực kỳ|vô cùng|"
    r"perfect (?:for|gift)|must[- ]have|wonderful|amazing|great choice|ideal for|"
    r"perfekt für|ideal für|wunderbar|ein muss)\b",
    re.IGNORECASE,
)
# Unverifiable superlatives: never storefront prose unless a source says so.
_EXAGGERATION_RE = re.compile(
    r"\b(?:hay nhất mọi thời đại|tuyệt vời nhất|số một thế giới|bán chạy nhất|"
    r"best(?:-selling)? (?:book|story) ever|of all time|bestsell(?:er|ing)|"
    r"beste[sn]? (?:buch|aller zeiten)|meistverkauft\w*)\b",
    re.IGNORECASE,
)
# An age recommendation: "0-6 tuổi", "từ 3 tuổi", "3+", "ages 4-8",
# "4 years and up", "ab 3 Jahren", "3 bis 6 Jahre".
_AGE_EXPRESSION_RE = re.compile(
    r"(?:\b\d{1,2}\s*(?:[-–]|đến|tới|to|bis)\s*\d{1,2}\s*(?:tuổi|years?|jahren?|j\.)"
    r"|\b(?:từ|trên|dưới)\s*\d{1,2}\s*tuổi"
    r"|\b\d{1,2}\s*tuổi\s*(?:trở lên|trở xuống)"
    r"|\bages?\s*\d{1,2}(?:\s*[-–+]\s*\d{0,2})?"
    r"|\b\d{1,2}\s*(?:years?|jahren?)\s*(?:and up|or older|und älter)"
    r"|\bab\s*\d{1,2}\s*jahren?"
    r"|\b\d{1,2}\+)",
    re.IGNORECASE,
)
_WORD_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _age_number_sets(text: str) -> list[frozenset[str]]:
    return [frozenset(re.findall(r"\d{1,2}", match.group(0))) for match in _AGE_EXPRESSION_RE.finditer(text)]


def _longest_common_word_run(first: str, second: str) -> int:
    """Length of the longest run of consecutive words (case-insensitive)
    shared by both texts. O(n*m) over words -- descriptions are short."""
    a = [word.lower() for word in _WORD_TOKEN_RE.findall(first)]
    b = [word.lower() for word in _WORD_TOKEN_RE.findall(second)]
    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for word in a:
        current = [0] * (len(b) + 1)
        for index, other in enumerate(b, start=1):
            if word == other:
                current[index] = previous[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        previous = current
    return best


def find_description_quality_issues(
    content: Mapping[str, Any],
    product: Mapping[str, Any] | None = None,
    source_texts: Sequence[str | None] | None = None,
    enforce_originality: bool = False,
) -> dict[str, str]:
    """
    {issue code: detail} for one content row (any language).

    source_texts are the verified factual inputs (reference descriptions,
    product metadata). When None the source-dependent checks (age, copy,
    superlative evidence) are skipped -- unknown is not evidence either
    way; an empty list means "no source", so any age claim is unverified.
    enforce_originality turns on the verbatim-copy check (live pipeline;
    the FB-HIST draft-safe policy deliberately reuses a normalized
    reference description, CLAUDE.md 6.2).
    """
    issues: dict[str, str] = {}
    long_description = str(content.get("long_description") or "")
    short_description = str(content.get("short_description") or "")
    product_name = str(content.get("product_name") or (product or {}).get("title") or "")
    # The product's own title may contain "Hay Nhất", "Tuyệt Vời", "3+".
    prose = f"{short_description}\n\n{long_description}"
    if product_name.strip():
        prose = re.sub(re.escape(product_name.strip()), " ", prose, flags=re.IGNORECASE)
    title = str((product or {}).get("title") or "").strip()
    if title:
        prose = re.sub(re.escape(title), " ", prose, flags=re.IGNORECASE)

    names = author_rules.split_person_names((product or {}).get("author"))
    for name in names:
        mentions = len(re.findall(re.escape(name), long_description, flags=re.IGNORECASE))
        if mentions > MAX_AUTHOR_MENTIONS_IN_DESCRIPTION:
            issues[QUALITY_AUTHOR_REPETITION] = (
                f"author {name!r} is named {mentions} times in long_description "
                f"(maximum {MAX_AUTHOR_MENTIONS_IN_DESCRIPTION})"
            )
            break

    sales = [match.group(0).lower() for match in _SALES_PHRASE_RE.finditer(prose)]
    if len(sales) > MAX_SALES_PHRASES:
        issues[QUALITY_SALES_LANGUAGE] = f"{len(sales)} sales phrases ({', '.join(sorted(set(sales)))})"

    sources = [str(text) for text in (source_texts or []) if text]
    source_blob = "\n".join(sources)

    exaggerations = [match.group(0) for match in _EXAGGERATION_RE.finditer(prose)]
    unsupported = [
        phrase for phrase in exaggerations
        if source_texts is None or phrase.lower() not in source_blob.lower()
    ]
    if unsupported:
        issues[QUALITY_EXAGGERATED_CLAIM] = "unverifiable superlative: " + ", ".join(sorted(set(unsupported)))

    if source_texts is not None:
        source_ages = _age_number_sets(source_blob + "\n" + title + "\n" + product_name)
        for numbers in _age_number_sets(prose):
            if not any(numbers <= verified for verified in source_ages):
                issues[QUALITY_UNVERIFIED_AGE] = (
                    f"age recommendation {sorted(numbers)} is not stated by any verified source"
                )
                break

    if enforce_originality and sources:
        longest = max(_longest_common_word_run(long_description, source) for source in sources)
        if longest >= MAX_VERBATIM_RUN_WORDS:
            issues[QUALITY_VERBATIM_COPY] = (
                f"long_description copies {longest} consecutive words from a source description "
                f"(maximum {MAX_VERBATIM_RUN_WORDS - 1})"
            )

    paragraphs = [p for p in storefront_text.normalize_paragraphs(long_description).split("\n\n") if p]
    if len(paragraphs) == 1 and len(paragraphs[0]) >= SINGLE_PARAGRAPH_MAX_LENGTH:
        issues[QUALITY_WALL_OF_TEXT] = (
            f"long_description is one {len(paragraphs[0])}-character paragraph "
            f"(split at {SINGLE_PARAGRAPH_MAX_LENGTH}+)"
        )

    return issues


def evaluate_description_quality(
    content: Mapping[str, Any],
    product: Mapping[str, Any] | None = None,
    source_texts: Sequence[str | None] | None = None,
    enforce_originality: bool = False,
) -> DecisionResult:
    """REVIEW_REQUIRED when find_description_quality_issues finds any
    issue; the codes name the exact REVISE target."""
    issues = find_description_quality_issues(content, product, source_texts, enforce_originality)
    if issues:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_DESCRIPTION_QUALITY,
            reason="Description quality: " + "; ".join(f"{code}: {detail}" for code, detail in sorted(issues.items())),
            evidence={"issues": issues},
        )
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_DESCRIPTION_QUALITY,
        reason="Description is natural storefront prose (no copy, hype, unverified age or author repetition).",
        evidence={"issues": {}},
    )


def evaluate_unsupported_claims(
    claimed_facts: Mapping[str, Any],
    verifiable_facts: Mapping[str, Any],
) -> DecisionResult:
    """
    REVIEW_REQUIRED when content asserts a fact that is not traceable to
    verified internal_product/reference data.

    claimed_facts and verifiable_facts are keyed the same way (e.g.
    {"author": "...", "publisher": "..."}); a claimed value that is
    present, non-empty, and differs from the corresponding verifiable
    value (when one exists) is treated as unsupported. A claimed fact
    with no corresponding verifiable key at all is also unsupported --
    this function never assumes an un-cross-checked claim is safe.
    """
    unsupported: dict[str, Any] = {}
    for field, claimed_value in claimed_facts.items():
        if not claimed_value:
            continue
        verifiable_value = verifiable_facts.get(field)
        if not verifiable_value:
            unsupported[field] = claimed_value
        elif (
            str(claimed_value).strip().lower()
            != str(verifiable_value).strip().lower()
        ):
            unsupported[field] = claimed_value

    if unsupported:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_UNSUPPORTED_CLAIM,
            reason=(
                "Content claims facts not traceable to verified data: "
                + ", ".join(sorted(unsupported))
            ),
            evidence={"unsupported_fields": unsupported},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_VERIFIED_FACTS_ONLY,
        reason="All claimed facts are traceable to verified data.",
        evidence={"checked_fields": tuple(sorted(claimed_facts))},
    )


def evaluate_reference_conflict(
    conflicting_fields: Sequence[str],
) -> DecisionResult:
    """
    REVIEW_REQUIRED when verified references disagree on a fact content
    would need to state as settled -- CLAUDE.md section 15.3 "verified
    references conflict".
    """
    if conflicting_fields:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_REFERENCE_CONFLICT,
            reason=(
                "Verified references conflict on: "
                + ", ".join(sorted(conflicting_fields))
            ),
            evidence={"conflicting_fields": tuple(conflicting_fields)},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_VERIFIED_FACTS_ONLY,
        reason="No reference conflicts affect this content.",
        evidence={"conflicting_fields": ()},
    )


def evaluate_optional_metadata(missing_fields: Sequence[str]) -> DecisionResult:
    """
    Non-blocking: missing optional metadata (ISBN, weight, dimensions,
    page count, ...) is a warning, never a reason to withhold automatic
    content approval -- CLAUDE.md section 2.2.
    """
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_MISSING_OPTIONAL_METADATA,
        reason=(
            "Optional metadata is missing but non-blocking: "
            + ", ".join(sorted(missing_fields))
            if missing_fields
            else "No optional metadata is missing."
        ),
        warnings=tuple(f"{field} is missing" for field in missing_fields),
        evidence={"missing_fields": tuple(missing_fields)},
    )


def evaluate_safe_approval(
    is_first_draft: bool,
    is_generic_safe_draft: bool,
    checks: Sequence[DecisionResult],
) -> DecisionResult:
    """
    The single gate prepare_product_content.py's --action APPROVE must
    pass through: content_status may only become APPROVED automatically
    once every other content rule result in `checks` (boilerplate,
    unsupported claims, reference conflicts, ...) has AUTO_PASSed, AND
    the content is not still the untouched, metadata-only generated
    draft (mirrors the existing is_generic_safe_draft() safety check).

    Never itself decides what "verified"/"boilerplate"/"unsupported"
    mean -- it only aggregates results the other rules already computed,
    so approval can never silently skip a check that was never run.
    """
    if is_first_draft:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason="Content has no prior saved draft to approve; save "
            "and enrich it first.",
        )

    if is_generic_safe_draft:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason="Content is still the generic metadata-only safe "
            "draft; it must be enriched from verified source material "
            "before approval.",
        )

    failing = [check for check in checks if not check.is_auto_pass]

    if failing:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason=(
                "Content cannot be approved automatically: "
                + "; ".join(f"{c.rule_code}: {c.reason}" for c in failing)
            ),
            evidence={"failing_rule_codes": tuple(c.rule_code for c in failing)},
        )

    all_warnings = tuple(warning for check in checks for warning in check.warnings)

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_SAFE_APPROVAL,
        reason="All content validation rules passed; content may be "
        "approved automatically.",
        warnings=all_warnings,
        evidence={"passed_rule_codes": tuple(c.rule_code for c in checks)},
    )


# --- historical draft-safe content auto-enrichment (CLAUDE.md 6.2/15) ------
#
# For an FB-HIST candidate whose content is still the generic metadata-
# only safe draft (prepare_product_content.py's is_generic_safe_draft()),
# normal enrichment should not require human review when a reference
# already carries verified, non-conflicting descriptive text -- CLAUDE.md
# section 2.2/15.1: content must be based only on verified data, never
# invented. This never applies to a live (non-historical) candidate; no
# caller in this codebase reaches it for one.


def select_historical_draft_safe_content_reference(
    candidate: dict[str, Any],
    references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    FB-HIST-only: deterministically pick one reference whose
    reference_description can safely enrich this candidate's generic
    content draft, without requiring match_decision==MATCH.

    A reference is eligible only when all of these hold:
      - match_decision is MATCH, POSSIBLE_MATCH, or MANUAL_REVIEW
      - source_type is a recognized reference source
        (REFERENCE_SOURCE_PRIORITY)
      - reference_description exists and is at least
        _MIN_USABLE_DESCRIPTION_LENGTH characters (not too thin to
        meaningfully identify the product)
      - after storefront normalization (entity decoding, retailer
        boilerplate removal) it is usable as storefront prose:
        storefront_text.is_usable_source_description -- a truncated
        meta snippet or pure SEO boilerplate is never selected
      - no ISBN/title/publisher/author/sellable-unit conflict with the
        candidate (identity_rules.reference_business_conflict_reason) --
        the same shared check image_rules.
        is_historical_reference_image_draft_safe uses, so "source
        evidence conflicts" and "wrong sellable unit" are refused
        identically for images and content.

    Ranking mirrors image_rules.select_historical_draft_safe_image_
    reference(): canonical source priority first, then a same-priority
    tie prefers a real MATCH over POSSIBLE_MATCH/MANUAL_REVIEW.

    Never invents a description -- evidence["reference_description"] is
    the exact text already collected and stored by
    collect_reference_metadata.py from an approved source, and
    evidence["normalized_description"] is that same text after the
    deterministic storefront normalization (no words added). Never
    decides match_decision or identity_status.
    """
    candidates_for_selection = [
        reference
        for reference in references
        if reference.get("match_decision") in _DRAFT_SAFE_MATCH_DECISIONS
        and reference.get("source_type") in REFERENCE_SOURCE_PRIORITY
        and len(str(reference.get("reference_description") or "").strip())
        >= _MIN_USABLE_DESCRIPTION_LENGTH
        and not storefront_text.contains_page_chrome(reference.get("reference_description"))
        and storefront_text.is_usable_source_description(
            normalized_reference_description(reference, candidate)
        )
    ]

    passing: list[tuple[dict[str, Any], str | None]] = []

    for reference in candidates_for_selection:
        conflict_reason = reference_business_conflict_reason(candidate, reference)

        if conflict_reason is None:
            passing.append((reference, None))

    if not passing:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=CONTENT_REFERENCE_NONE_DRAFT_SAFE,
            reason=(
                "No reference has a usable, non-conflicting description "
                f"for auto-enrichment (checked {len(candidates_for_selection)} "
                "candidate reference(s) with a description)."
            ),
            evidence={"checked_count": len(candidates_for_selection)},
        )

    best_priority = min(
        REFERENCE_SOURCE_PRIORITY[reference["source_type"]]
        for reference, _ in passing
    )
    top_tier = [
        (reference, _)
        for reference, _ in passing
        if REFERENCE_SOURCE_PRIORITY[reference["source_type"]] == best_priority
    ]
    top_tier.sort(
        key=lambda pair: _MATCH_DECISION_RANK.get(pair[0].get("match_decision"), 3)
    )

    selected_reference = top_tier[0][0]

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_REFERENCE_DRAFT_SAFE_SELECTED,
        reason=(
            "Reference is draft-safe for content enrichment: recognized "
            "source, usable description, no identity/sellable-unit "
            "conflict."
        ),
        evidence={
            "reference_id": selected_reference.get("reference_id"),
            "source_type": selected_reference.get("source_type"),
            "reference_description": selected_reference.get(
                "reference_description"
            ),
            "normalized_description": normalized_reference_description(
                selected_reference, candidate
            ),
            "match_decision": selected_reference.get("match_decision"),
        },
    )


def normalized_reference_description(
    reference: Mapping[str, Any],
    candidate: Mapping[str, Any] | None = None,
) -> str:
    """The reference description after deterministic storefront
    normalization, stripping a leading duplicated reference/candidate
    title (storefront_text.normalize_source_description)."""
    titles = [reference.get("reference_title")]
    if candidate:
        titles.append(candidate.get("extracted_title"))
    return storefront_text.normalize_source_description(
        reference.get("reference_description"),
        titles=titles,
    )
