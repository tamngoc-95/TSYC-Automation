"""Deterministic classification rules for the OFFLINE historical Facebook
migration-candidate screening layer.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    This module classifies one already-parsed historical Facebook record
    (see src/services/facebook_history_parser.py's HistoryRecord) into:

        tsyc_relevance     -- HIGH / MEDIUM / LOW
        post_type          -- PRODUCT_POST / CUSTOMER_FEEDBACK / BOOK_REVIEW /
                               PROMOTION / GENERAL_BUSINESS / PERSONAL / OTHER
        candidate_eligible -- bool

    This is an OFFLINE, PRE-INGESTION screening pass over a personal
    Facebook data-export HTML file. It has no Supabase dependency, no
    WooCommerce dependency, and does not call any LLM/Claude API. It never
    creates a source_urls/raw_pages/product_candidates row -- see
    scripts/classify_facebook_history_export.py's own docstring for the
    explicit list of things this layer must never do. A record with
    candidate_eligible=True is only a *screening signal* that a human (or
    a later, explicitly-approved AI-assisted stage) should look at it as a
    possible migration candidate -- it is not itself a product_candidates
    row and must not be treated as one.

    This is a distinct classification concept from
    src/domain/rules/extraction_rules.py's PostType (ONE_BOOK/
    MULTIPLE_BOOKS/COMBO/GENERAL_POST/AMBIGUOUS), which classifies an
    already-cleaned, already-in-pipeline raw_pages row for per-book title/
    author extraction. The two modules intentionally do not share a
    PostType enum: extraction_rules.PostType answers "how many sellable
    books does this contain and how", this module's PostType answers "what
    *kind* of historical Facebook activity is this at all" (a feedback
    screenshot vs. a review vs. a listing vs. a personal post) -- a
    question extraction_rules never has to ask because everything reaching
    it has already passed this screening.

Pure functions only: given plain text/lists, classify() returns a frozen
ClassificationResult with no I/O and no side effects. Calling classify()
twice with the same arguments always returns an equal result (see
tests/test_facebook_history_classification.py's idempotency test) --
there is no hidden state, randomness, or clock dependency anywhere in this
module.

Evidence tiers (matches the human-reviewed relevance analysis this module
codifies):

    STRONG evidence (-> tsyc_relevance HIGH) is any of:
      - an exact strong-marker phrase in the post's own text (never the
        Facebook UI heading -- see classify()'s docstring)
      - the manually verified "Sách Có Sẵn tại Đức" local media folder
        slug (a listing-photo album name)
      - the manually verified TSYC Feedback folder slug (a customer-
        feedback-screenshot album name)
      - a structural Facebook @mention of the verified TSYC Page id
        (2415122391976246 -- see AUTHORIZED_GROUP_ID's docstring below)

    WEAK evidence (-> tsyc_relevance MEDIUM, absent strong evidence) is
    any whole-word/whole-phrase book-sale vocabulary hit, or one of the
    known book-adjacent (but not TSYC-brand-confirmed) folder slugs.

    NO evidence (-> tsyc_relevance LOW) is everything else, including a
    record whose only "signal" is a generic Facebook action heading
    (added a photo/video, shared a link, updated status) or the mere
    presence of local media -- neither is used as relevance evidence
    (project requirement: "do not classify records merely because
    Facebook labels them as photo or video").

Whole-word/whole-phrase matching, not naive substring matching: every
marker phrase below is compiled into a Unicode word-boundary regex (see
_compile_phrase_pattern()). This is a hard, tested requirement --
"Relationship Map" must never match the marker "ship" merely because the
letters "ship" appear inside "Relationship".

Precision hardening (candidate_eligible, MEDIUM tier only): evidence is
split into two disjoint kinds --

    BOOK_SPECIFIC_TEXT_MARKERS  -- vocabulary that only makes sense next
        to an actual book ("sách", "cuốn", "tác giả", "nhà xuất bản", ...)
    COMMERCE_TEXT_MARKERS / GENERIC_LISTING_PHRASES / a price pattern /
        a bulleted list -- vocabulary that means "something is for sale"
        but says nothing about *what* ("giá", "ship", "km", "có sẵn",
        "thanh lý", ...)

A record with no confirmed TSYC brand marker (folder slug, structural
mention, or exact brand phrase) is only candidate_eligible=True when
BOTH kinds of evidence are present -- commerce/listing language alone
("giá", "ship", a price, a bulleted price list) is never enough by
itself, and it is exactly this gap that let unrelated side-business
content (a numerology "bản đồ Nhân số học" post, a "Relationship Map"
post, a "Zeus Team" promo) slip through when a bare price/commerce
pattern was treated as sufficient evidence -- see
NEGATIVE_BUSINESS_MARKERS below for the explicit deterministic
backstop against exactly that content, and _has_book_specific_evidence()
/_has_commerce_evidence() for the two checks themselves.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# remove_invisible_unicode_characters is the one canonical implementation
# of CLAUDE.md section 12's Facebook anti-scraping defense (stripping
# characters such as U+034F COMBINING GRAPHEME JOINER without damaging
# legitimate Vietnamese diacritics). Reused here rather than duplicated,
# the same way src/domain/rules/extraction_rules.py already imports
# across the scripts/-module boundary for the same reason.
from clean_facebook_raw_pages import remove_invisible_unicode_characters  # noqa: E402


# --- structural TSYC identity -------------------------------------------

# The verified Facebook Page id for "Tiệm sách Yêu Con" / "Tiệm sách Yêu
# Con ở Đức" (confirmed by two independent @mention name variants
# resolving to the same id in the historical export). Intentionally
# duplicated (not imported) from
# src/services/source_ingestion.py's AUTHORIZED_GROUP_ID -- that module
# is explicitly scoped to *live* Facebook group-post ingestion and must
# not be imported by this purely offline, pre-ingestion screening module.
# Kept as its own named constant here so a future divergence (if TSYC
# ever operates under more than one Page/Group id) does not silently
# change both call sites at once.
TSYC_PAGE_ID = "2415122391976246"

# Manually verified against the real export (see the read-only relevance
# analysis this module codifies): every record whose local media path
# contains this folder slug is a "Sách Có Sẵn tại Đức" (Books Available in
# Germany) listing-photo album -- i.e. a TSYC book-for-sale photo set.
STRONG_LISTING_FOLDER_SLUG = "SachCoSantaiDuc"

# Manually verified: every record whose local media path contains this
# folder slug is a "Feedbacks từ Yêu Con (Page/ Tiệm sách và Thư viện
# cộng đồng)" customer-feedback-screenshot album. Per explicit project
# requirement, this folder slug means tsyc_relevance=HIGH but
# post_type=CUSTOMER_FEEDBACK, never PRODUCT_POST, and is never
# candidate_eligible on its own.
FEEDBACK_FOLDER_SLUG = "FeedbackstuYeuConPageTiemsachvaThuviencongdong"

# Weak (book-adjacent but not TSYC-brand-confirmed) folder slugs,
# subdivided by what kind of historical activity they represent so
# post_type can be assigned without re-deriving this from free text.
REVIEW_FOLDER_SLUGS = frozenset(
    {"Reviewsachhay", "Reviewsachtoiratghet", "Albumsachhayvuavua"}
)
LISTING_FOLDER_SLUGS = frozenset({"Sachchobe4tuoi"})
GENERAL_BUSINESS_FOLDER_SLUGS = frozenset({"SachhayMienPhitrenFonoschohoivien"})
WEAK_FOLDER_SLUGS = REVIEW_FOLDER_SLUGS | LISTING_FOLDER_SLUGS | GENERAL_BUSINESS_FOLDER_SLUGS


# --- marker vocabulary ---------------------------------------------------

STRONG_TEXT_MARKERS: tuple[str, ...] = (
    "Tiệm sách Yêu Con",
    "Tiệm Sách Yêu Con ở Đức",
    "Tiem Sach Yeu Con",
    "Sách có sẵn tại Đức",
    "Sách có sẵn ở Đức",
)

# Vocabulary that only makes sense next to an actual book -- see this
# module's own docstring ("Precision hardening"). Used by
# _has_book_specific_evidence(), the new required half of MEDIUM-tier
# candidate_eligible.
BOOK_SPECIFIC_TEXT_MARKERS: tuple[str, ...] = (
    "sách",
    "bộ sách",
    "cuốn",
    "trọn bộ",
    "tác giả",
    "nhà xuất bản",
    "tái bản",
    "đặt sách",
    "sách có sẵn",
    "sách có sẵn tại Đức",
    "sách có sẵn ở Đức",
)

# Vocabulary that means "something is for sale" but says nothing about
# *what* -- deliberately never sufficient on its own to drive
# candidate_eligible=True (see _has_commerce_evidence()). "km" is kept
# only as weak relevance vocabulary; it is too ambiguous with the
# Vietnamese unit "kilometer" even as a whole word to safely drive a
# PROMOTION classification on its own, so it is excluded from
# PROMOTION_KEYWORDS below.
COMMERCE_TEXT_MARKERS: tuple[str, ...] = (
    "giá",
    "giá sách",
    "freeship",
    "ship",
    "sale",
    "giảm giá",
    "km",
    "inbox",
    "zalo",
)

# Generic "this is a concrete, currently-sellable listing" language that
# says nothing about the product being a book -- commerce-side evidence,
# grouped separately from COMMERCE_TEXT_MARKERS only for readability.
GENERIC_LISTING_PHRASES: tuple[str, ...] = (
    "có sẵn",
    "đặt trước",
    "preorder",
    "nguyên bộ",
    "cả bộ",
    "thanh lý",
)

# Explicit promotion/discount language.
PROMOTION_KEYWORDS: tuple[str, ...] = (
    "giảm giá",
    "khuyến mãi",
    "sale",
    "freeship",
)

REVIEW_PHRASES: tuple[str, ...] = (
    "review sách",
    "đánh giá sách",
    "cảm nhận sách",
)

COUNTERFEIT_WARNING_PHRASES: tuple[str, ...] = (
    "sách giả",
    "hàng giả",
    "sách nhái",
)

# Known non-TSYC side-business content the same personal Facebook
# account also posts (a numerology "map" service, a separate "Zeus
# Team" affiliated promo). Deliberately narrow -- named/branded phrases
# actually observed in the export, not generic words -- per the explicit
# "do not make the negative list overly broad" requirement. Only takes
# effect when there is no confirmed TSYC brand marker at all (see
# classify()'s negative-business override); a post that genuinely
# mentions both this content AND a strong TSYC marker is never excluded.
NEGATIVE_BUSINESS_MARKERS: tuple[str, ...] = (
    "Zeus Team",
    "Nhân số học",
    "bản đồ Nhân số học",
    "Relationship Map",
)

# A currency amount, e.g. "8€", "8,5€", "55 EUR", "50.000đ" -- concrete
# pricing is strong (if narrow) evidence of an actual for-sale listing.
#
# The trailing \b is deliberately only inside the word-currency-code
# alternative (eur/đ/vnđ/usd), not after the symbol alternatives (€/$):
# \b requires a word-char/non-word-char transition, and a symbol like
# "€" is itself a non-word character, so "8€" followed by end-of-string
# or another non-word character (a newline, a space) would never satisfy
# a trailing \b there -- it would silently never match a bare "8€".
_PRICE_RE = re.compile(
    r"\b\d{1,6}([.,]\d{1,2})?\s?(?:[€$]|(?:eur|đ|vnđ|usd)\b)",
    re.IGNORECASE,
)

# A numbered/bulleted list line (mirrors src/domain/rules/extraction_
# rules.py's _BULLET_LINE_RE narrowly-scoped bullet detection, duplicated
# rather than imported -- that module's regex is tied to its own
# candidate-splitting contract, this one only needs a cheap "does this
# look like an itemized list" signal for promotion/listing detection).
_BULLET_LINE_RE = re.compile(
    r"^\s*(?:[-*•‣▪●○]|\d{1,3}[.\)]|[①②③④⑤⑥⑦⑧⑨⑩])\s+\S"
)


def _normalize_for_matching(value: str | None) -> str:
    """NFC-normalize and strip Facebook anti-scraping invisible
    characters, without altering legitimate Vietnamese diacritics."""
    if not value:
        return ""

    return unicodedata.normalize("NFC", remove_invisible_unicode_characters(value))


def _compile_phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile one marker phrase into a case-insensitive, Unicode
    whole-word/whole-phrase boundary regex.

    \\b in Python's Unicode-mode `re` treats Vietnamese letters as word
    characters, so `\\bship\\b` correctly matches standalone "ship" but
    not the "ship" inside "Relationship" (no boundary between the 'n' and
    's' -- both are word characters) -- see
    tests/test_facebook_history_classification.py's regression test.
    """
    normalized_phrase = _normalize_for_matching(phrase)
    escaped_words = [re.escape(word) for word in normalized_phrase.split()]
    pattern = r"\b" + r"\s+".join(escaped_words) + r"\b"

    return re.compile(pattern, re.IGNORECASE)


_STRONG_TEXT_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in STRONG_TEXT_MARKERS
}
_BOOK_SPECIFIC_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in BOOK_SPECIFIC_TEXT_MARKERS
}
_COMMERCE_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in COMMERCE_TEXT_MARKERS
}
_GENERIC_LISTING_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in GENERIC_LISTING_PHRASES
}
_PROMOTION_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in PROMOTION_KEYWORDS
}
_REVIEW_PHRASE_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in REVIEW_PHRASES
}
_COUNTERFEIT_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in COUNTERFEIT_WARNING_PHRASES
}
_NEGATIVE_BUSINESS_PATTERNS = {
    marker: _compile_phrase_pattern(marker) for marker in NEGATIVE_BUSINESS_MARKERS
}


def _find_matches(text: str, patterns: dict[str, re.Pattern[str]]) -> tuple[str, ...]:
    return tuple(label for label, pattern in patterns.items() if pattern.search(text))


def _has_bulleted_list(text: str, *, minimum_items: int = 2) -> bool:
    matched_lines = sum(1 for line in text.splitlines() if _BULLET_LINE_RE.match(line))
    return matched_lines >= minimum_items


def _has_book_specific_evidence(book_specific_hits: tuple[str, ...]) -> bool:
    """True only when vocabulary that specifically means "this is a
    book" is present -- see BOOK_SPECIFIC_TEXT_MARKERS's docstring.
    Never true from generic commerce/listing language alone."""
    return bool(book_specific_hits)


def _has_commerce_evidence(
    normalized_text: str,
    commerce_hits: tuple[str, ...],
    generic_listing_hits: tuple[str, ...],
) -> bool:
    """Return True when concrete for-sale/commerce evidence exists -- a
    price, an itemized list, explicit generic listing language, or
    commerce vocabulary (giá/ship/sale/...). Says nothing about whether
    the product is a book -- see _has_book_specific_evidence() for that,
    and this module's own docstring for why the two are required
    together for MEDIUM-tier candidate_eligible."""
    if commerce_hits or generic_listing_hits:
        return True

    if _PRICE_RE.search(normalized_text):
        return True

    if _has_bulleted_list(normalized_text):
        return True

    return False


# --- public enums ---------------------------------------------------------


class TsycRelevance:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


ALL_RELEVANCE_LEVELS = frozenset({TsycRelevance.HIGH, TsycRelevance.MEDIUM, TsycRelevance.LOW})


class PostType:
    PRODUCT_POST = "PRODUCT_POST"
    CUSTOMER_FEEDBACK = "CUSTOMER_FEEDBACK"
    BOOK_REVIEW = "BOOK_REVIEW"
    PROMOTION = "PROMOTION"
    GENERAL_BUSINESS = "GENERAL_BUSINESS"
    PERSONAL = "PERSONAL"
    OTHER = "OTHER"


ALL_POST_TYPES = frozenset(
    {
        PostType.PRODUCT_POST,
        PostType.CUSTOMER_FEEDBACK,
        PostType.BOOK_REVIEW,
        PostType.PROMOTION,
        PostType.GENERAL_BUSINESS,
        PostType.PERSONAL,
        PostType.OTHER,
    }
)

# Stable, grep-able classification_reason codes (mirrors src.domain.
# decisions.DecisionResult.rule_code's contract: a rule_code/reason is
# never repurposed for a different meaning once shipped -- add a new one
# instead). Kept as one small fixed vocabulary (not per-record
# interpolated free text) so the summary's "top classification reasons"
# count is meaningful.
REASON_FEEDBACK_FOLDER_SLUG = (
    "FEEDBACK_FOLDER_SLUG: manually verified TSYC feedback-screenshot "
    "album slug present -> HIGH relevance, CUSTOMER_FEEDBACK, never "
    "candidate-eligible on its own."
)
REASON_STRONG_LISTING = (
    "STRONG_EVIDENCE_WITH_LISTING: strong TSYC brand/structural evidence "
    "plus concrete listing evidence (price/list/availability language) "
    "-> HIGH relevance, PRODUCT_POST, candidate-eligible."
)
REASON_STRONG_PROMOTION_ELIGIBLE = (
    "STRONG_EVIDENCE_PROMOTION_WITH_LISTING: strong TSYC brand/structural "
    "evidence, explicit promotion/discount language, and concrete "
    "listing evidence -> HIGH relevance, PROMOTION, candidate-eligible."
)
REASON_STRONG_PROMOTION_NO_LISTING = (
    "STRONG_EVIDENCE_PROMOTION_NO_LISTING: strong TSYC brand/structural "
    "evidence and promotion/discount language, but no concrete listing "
    "evidence -> HIGH relevance, PROMOTION, not yet candidate-eligible."
)
REASON_STRONG_REVIEW = (
    "STRONG_EVIDENCE_REVIEW: strong TSYC brand/structural evidence with "
    "book-review language -> HIGH relevance, BOOK_REVIEW, never "
    "automatically candidate-eligible."
)
REASON_STRONG_NO_LISTING = (
    "STRONG_EVIDENCE_NO_LISTING: strong TSYC brand/structural evidence "
    "but no concrete listing/promotion/review evidence (e.g. a "
    "thank-you, minigame, or general announcement) -> HIGH relevance, "
    "GENERAL_BUSINESS, not candidate-eligible."
)
REASON_WEAK_REVIEW_FOLDER = (
    "WEAK_REVIEW_FOLDER_SLUG: known book-review album slug -> MEDIUM "
    "relevance, BOOK_REVIEW, never automatically candidate-eligible."
)
REASON_WEAK_REVIEW_TEXT = (
    "WEAK_REVIEW_TEXT: book-review language in text -> MEDIUM relevance, "
    "BOOK_REVIEW, never automatically candidate-eligible."
)
REASON_WEAK_COUNTERFEIT_WARNING = (
    "WEAK_COUNTERFEIT_WARNING: counterfeit/fake-book warning language -> "
    "MEDIUM relevance, GENERAL_BUSINESS (market commentary, not a TSYC "
    "listing), not candidate-eligible."
)
REASON_WEAK_LISTING_ELIGIBLE = (
    "WEAK_EVIDENCE_BOOK_SPECIFIC_WITH_COMMERCE: book-specific vocabulary "
    "(sách/cuốn/tác giả/...) AND commerce/listing evidence (price/list/"
    "giá/ship/...) both present, no confirmed TSYC brand marker -> "
    "MEDIUM relevance, PROMOTION, candidate-eligible pending secondary "
    "review."
)
REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE = (
    "WEAK_EVIDENCE_COMMERCE_ONLY: commerce/listing evidence present "
    "(price/list/giá/ship/...) but no book-specific vocabulary at all -> "
    "MEDIUM relevance, GENERAL_BUSINESS, not candidate-eligible (commerce "
    "evidence alone never grants eligibility)."
)
REASON_NEGATIVE_BUSINESS_EXCLUSION = (
    "NEGATIVE_BUSINESS_EXCLUDED: text matches a known non-TSYC "
    "side-business marker (e.g. numerology/\"Nhân số học\"/\"Zeus Team\"/"
    "\"Relationship Map\" content) with no confirmed TSYC brand evidence "
    "-> candidate_eligible forced False regardless of any book-specific "
    "or commerce/listing language otherwise present."
)
REASON_WEAK_GENERAL_FOLDER = (
    "WEAK_GENERAL_FOLDER_SLUG: book-adjacent album slug with no listing "
    "evidence (e.g. third-party content) -> MEDIUM relevance, "
    "GENERAL_BUSINESS, not candidate-eligible."
)
REASON_WEAK_NO_LISTING = (
    "WEAK_EVIDENCE_NO_LISTING: only generic book-adjacent vocabulary, no "
    "concrete listing/review/promotion evidence -> MEDIUM relevance, "
    "GENERAL_BUSINESS, not candidate-eligible."
)
REASON_NO_EVIDENCE = (
    "NO_EVIDENCE: no strong or weak TSYC/book-sale evidence in the "
    "post's own text or media context (Facebook action heading and mere "
    "media presence are never used as evidence) -> LOW relevance, "
    "PERSONAL, not candidate-eligible."
)


@dataclass(frozen=True)
class ClassificationResult:
    """The immutable result of classifying one historical Facebook record.

    strong_markers/weak_markers combine text-phrase hits, folder-slug
    hits (prefixed "folder:"), and the structural mention hit (prefixed
    "mention:") into one flat, CSV-friendly tuple each -- callers that
    need to distinguish evidence *kind* should use structural_mention_id/
    folder_slug_evidence directly instead.
    """

    tsyc_relevance: str
    post_type: str
    candidate_eligible: bool
    classification_reason: str
    needs_secondary_review: bool
    strong_markers: tuple[str, ...] = field(default_factory=tuple)
    weak_markers: tuple[str, ...] = field(default_factory=tuple)
    structural_mention_id: str | None = None
    folder_slug_evidence: tuple[str, ...] = field(default_factory=tuple)
    negative_business_markers: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.tsyc_relevance not in ALL_RELEVANCE_LEVELS:
            raise ValueError(f"Unknown tsyc_relevance: {self.tsyc_relevance!r}")
        if self.post_type not in ALL_POST_TYPES:
            raise ValueError(f"Unknown post_type: {self.post_type!r}")


def classify(
    full_text: str,
    folder_slugs: Sequence[str] = (),
    mention_ids: Sequence[str] = (),
) -> ClassificationResult:
    """Deterministically classify one historical Facebook record.

    Arguments are exactly the fields of a record that are actually
    business-relevant evidence:

        full_text    -- the post's own caption/body text (see
                         src/services/facebook_history_parser.py's
                         HistoryRecord.full_text). Never pass a Facebook
                         action heading ("added a photo", "shared a
                         link", "updated status") here -- this function
                         does not look at headings at all, by design
                         (project requirement: do not use record headings
                         as business relevance evidence).
        folder_slugs -- the local media album/folder-name slugs found on
                         this record's image/video paths (structural
                         evidence, independent of caption text).
        mention_ids  -- numeric Facebook object ids found in this
                         record's structural @mention tags.

    Pure function: no I/O, no randomness, no clock. Calling this twice
    with the same arguments always returns an equal ClassificationResult.
    """
    normalized_text = _normalize_for_matching(full_text)
    folder_slug_set = frozenset(folder_slugs)
    mention_id_set = frozenset(mention_ids)

    strong_text_hits = _find_matches(normalized_text, _STRONG_TEXT_PATTERNS)
    book_specific_hits = _find_matches(normalized_text, _BOOK_SPECIFIC_PATTERNS)
    commerce_hits = _find_matches(normalized_text, _COMMERCE_PATTERNS)
    generic_listing_hits = _find_matches(normalized_text, _GENERIC_LISTING_PATTERNS)
    negative_business_hits = _find_matches(normalized_text, _NEGATIVE_BUSINESS_PATTERNS)

    has_feedback_folder = FEEDBACK_FOLDER_SLUG in folder_slug_set
    strong_folder_hits = tuple(
        sorted(folder_slug_set & {STRONG_LISTING_FOLDER_SLUG, FEEDBACK_FOLDER_SLUG})
    )
    weak_folder_hits = tuple(sorted(folder_slug_set & WEAK_FOLDER_SLUGS))

    structural_mention_id = TSYC_PAGE_ID if TSYC_PAGE_ID in mention_id_set else None

    has_strong_evidence = bool(strong_text_hits) or bool(strong_folder_hits) or bool(
        structural_mention_id
    )
    has_book_specific = _has_book_specific_evidence(book_specific_hits)
    has_commerce = _has_commerce_evidence(normalized_text, commerce_hits, generic_listing_hits)
    has_weak_evidence = has_book_specific or bool(commerce_hits) or bool(generic_listing_hits) or bool(
        weak_folder_hits
    )

    strong_markers = strong_text_hits + tuple(f"folder:{slug}" for slug in strong_folder_hits)
    if structural_mention_id:
        strong_markers = strong_markers + (f"mention:{structural_mention_id}",)
    weak_markers = (
        book_specific_hits
        + commerce_hits
        + generic_listing_hits
        + tuple(f"folder:{slug}" for slug in weak_folder_hits)
    )

    review_text_hit = bool(_find_matches(normalized_text, _REVIEW_PHRASE_PATTERNS))
    counterfeit_hit = bool(_find_matches(normalized_text, _COUNTERFEIT_PATTERNS))
    promotion_hit = bool(_find_matches(normalized_text, _PROMOTION_PATTERNS))

    def _decide() -> ClassificationResult:
        # --- Tier 1: feedback-screenshot album takes precedence over ---
        # everything else, per explicit project requirement.
        if has_feedback_folder:
            return ClassificationResult(
                tsyc_relevance=TsycRelevance.HIGH,
                post_type=PostType.CUSTOMER_FEEDBACK,
                candidate_eligible=False,
                classification_reason=REASON_FEEDBACK_FOLDER_SLUG,
                needs_secondary_review=has_book_specific or has_commerce,
                strong_markers=strong_markers,
                weak_markers=weak_markers,
                structural_mention_id=structural_mention_id,
                folder_slug_evidence=tuple(sorted(folder_slug_set)),
            )

        # --- Tier 2: strong TSYC evidence -------------------------------
        if has_strong_evidence:
            if review_text_hit:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.HIGH,
                    post_type=PostType.BOOK_REVIEW,
                    candidate_eligible=False,
                    classification_reason=REASON_STRONG_REVIEW,
                    needs_secondary_review=False,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            if promotion_hit:
                eligible = has_commerce
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.HIGH,
                    post_type=PostType.PROMOTION,
                    candidate_eligible=eligible,
                    classification_reason=(
                        REASON_STRONG_PROMOTION_ELIGIBLE
                        if eligible
                        else REASON_STRONG_PROMOTION_NO_LISTING
                    ),
                    needs_secondary_review=not eligible,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            if has_commerce:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.HIGH,
                    post_type=PostType.PRODUCT_POST,
                    candidate_eligible=True,
                    classification_reason=REASON_STRONG_LISTING,
                    needs_secondary_review=False,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            return ClassificationResult(
                tsyc_relevance=TsycRelevance.HIGH,
                post_type=PostType.GENERAL_BUSINESS,
                candidate_eligible=False,
                classification_reason=REASON_STRONG_NO_LISTING,
                needs_secondary_review=True,
                strong_markers=strong_markers,
                weak_markers=weak_markers,
                structural_mention_id=structural_mention_id,
                folder_slug_evidence=tuple(sorted(folder_slug_set)),
            )

        # --- Tier 3: weak evidence, OR concrete commerce/listing --------
        # evidence (a priced/bulleted item list is itself commerce-shaped
        # evidence even when no exact book-vocabulary word is present).
        if has_weak_evidence or has_commerce:
            if weak_folder_hits and set(weak_folder_hits) <= REVIEW_FOLDER_SLUGS:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.MEDIUM,
                    post_type=PostType.BOOK_REVIEW,
                    candidate_eligible=False,
                    classification_reason=REASON_WEAK_REVIEW_FOLDER,
                    needs_secondary_review=True,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            if review_text_hit:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.MEDIUM,
                    post_type=PostType.BOOK_REVIEW,
                    candidate_eligible=False,
                    classification_reason=REASON_WEAK_REVIEW_TEXT,
                    needs_secondary_review=True,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            if counterfeit_hit:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.MEDIUM,
                    post_type=PostType.GENERAL_BUSINESS,
                    candidate_eligible=False,
                    classification_reason=REASON_WEAK_COUNTERFEIT_WARNING,
                    needs_secondary_review=True,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            # Precision-hardened rule: MEDIUM-tier eligibility requires
            # BOTH book-specific evidence AND commerce/listing evidence.
            # Commerce evidence alone (a bare "giá", a price, "ship", a
            # bulleted price list with no book vocabulary at all) is
            # never sufficient by itself -- this is exactly the gap that
            # previously let unrelated side-business content (numerology
            # maps, "Zeus Team") through.
            if has_commerce:
                if has_book_specific:
                    return ClassificationResult(
                        tsyc_relevance=TsycRelevance.MEDIUM,
                        post_type=PostType.PROMOTION,
                        candidate_eligible=True,
                        classification_reason=REASON_WEAK_LISTING_ELIGIBLE,
                        needs_secondary_review=True,
                        strong_markers=strong_markers,
                        weak_markers=weak_markers,
                        structural_mention_id=structural_mention_id,
                        folder_slug_evidence=tuple(sorted(folder_slug_set)),
                    )

                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.MEDIUM,
                    post_type=PostType.GENERAL_BUSINESS,
                    candidate_eligible=False,
                    classification_reason=REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE,
                    needs_secondary_review=True,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            if weak_folder_hits and set(weak_folder_hits) <= GENERAL_BUSINESS_FOLDER_SLUGS:
                return ClassificationResult(
                    tsyc_relevance=TsycRelevance.MEDIUM,
                    post_type=PostType.GENERAL_BUSINESS,
                    candidate_eligible=False,
                    classification_reason=REASON_WEAK_GENERAL_FOLDER,
                    needs_secondary_review=True,
                    strong_markers=strong_markers,
                    weak_markers=weak_markers,
                    structural_mention_id=structural_mention_id,
                    folder_slug_evidence=tuple(sorted(folder_slug_set)),
                )

            return ClassificationResult(
                tsyc_relevance=TsycRelevance.MEDIUM,
                post_type=PostType.GENERAL_BUSINESS,
                candidate_eligible=False,
                classification_reason=REASON_WEAK_NO_LISTING,
                needs_secondary_review=True,
                strong_markers=strong_markers,
                weak_markers=weak_markers,
                structural_mention_id=structural_mention_id,
                folder_slug_evidence=tuple(sorted(folder_slug_set)),
            )

        # --- Tier 4: no evidence at all ----------------------------------
        return ClassificationResult(
            tsyc_relevance=TsycRelevance.LOW,
            post_type=PostType.PERSONAL,
            candidate_eligible=False,
            classification_reason=REASON_NO_EVIDENCE,
            needs_secondary_review=False,
            strong_markers=strong_markers,
            weak_markers=weak_markers,
            structural_mention_id=structural_mention_id,
            folder_slug_evidence=tuple(sorted(folder_slug_set)),
        )

    result = _decide()

    # --- Negative-business override ---------------------------------------
    # Applies only when there is no confirmed TSYC brand marker at all
    # (has_strong_evidence is only ever True from a Tier-2 branch, so this
    # can never fire against a genuine strong-evidence result -- see this
    # module's own docstring and NEGATIVE_BUSINESS_MARKERS's docstring).
    if negative_business_hits and not has_strong_evidence and result.candidate_eligible:
        return replace(
            result,
            candidate_eligible=False,
            classification_reason=REASON_NEGATIVE_BUSINESS_EXCLUSION,
            needs_secondary_review=True,
            negative_business_markers=negative_business_hits,
        )

    if negative_business_hits:
        return replace(result, negative_business_markers=negative_business_hits)

    return result
