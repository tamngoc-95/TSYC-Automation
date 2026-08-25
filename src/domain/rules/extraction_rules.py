"""Deterministic Facebook-post candidate-extraction rules.

Covers classifying one cleaned Facebook post (scripts/clean_facebook_raw_
pages.py's output, already stripped of Facebook interface noise) into a
post type, and deterministically extracting one or more book
title/author/ISBN identities from it -- rule-based only, no ML/NLP model,
never guessing.

Rule codes implemented here (see docs/TSYC_DECISION_MATRIX.md-style
specification in this module's own docstrings below):

    EXTRACTION_ONE_BOOK           AUTO_PASS
    EXTRACTION_MULTIPLE_BOOKS     AUTO_PASS
    EXTRACTION_COMBO              AUTO_PASS
    EXTRACTION_GENERAL_POST       AUTO_REJECT
    EXTRACTION_AMBIGUOUS          REVIEW_REQUIRED
    EXTRACTION_INSUFFICIENT_TITLE REVIEW_REQUIRED
    EXTRACTION_DUPLICATE          decided by the caller (scripts/create_
                                   candidates_from_cleaned_posts.py), which
                                   already knows whether this raw_page has
                                   existing candidates -- this pure module
                                   has no database access, so it cannot
                                   itself detect a cross-run duplicate.

This module intentionally contains the one canonical implementation of
the title/author extraction regexes that scripts/create_candidates_from_
cleaned_posts.py's own extract_book_identity() already relied on --
that function is preserved unchanged (same name, same signature, same
raise-on-failure behavior, same matched_pattern labels: existing
regression tests in tests/test_facebook_text_normalization.py depend on
exactly that), refactored into a thin wrapper around
extract_single_book_identity() below so there is still only one place
the actual patterns live.

Import note: normalize_unicode_text (the U+034F COMBINING GRAPHEME
JOINER defensive-normalization fix -- CLAUDE.md section 12 / this
project's own regression-tested defect) lives in scripts/clean_facebook_
raw_pages.py, not in src/domain/*. scripts/create_candidates_from_
cleaned_posts.py already imports across that same script-to-script
boundary (`from clean_facebook_raw_pages import normalize_unicode_text`),
so this module does the same rather than duplicating or relocating that
security-sensitive fix.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.domain.decisions import DecisionResult, Outcome

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from clean_facebook_raw_pages import normalize_unicode_text  # noqa: E402


# --- rule codes ------------------------------------------------------------

EXTRACTION_ONE_BOOK = "EXTRACTION_ONE_BOOK"
EXTRACTION_MULTIPLE_BOOKS = "EXTRACTION_MULTIPLE_BOOKS"
EXTRACTION_COMBO = "EXTRACTION_COMBO"
EXTRACTION_GENERAL_POST = "EXTRACTION_GENERAL_POST"
EXTRACTION_AMBIGUOUS = "EXTRACTION_AMBIGUOUS"
EXTRACTION_INSUFFICIENT_TITLE = "EXTRACTION_INSUFFICIENT_TITLE"
EXTRACTION_DUPLICATE = "EXTRACTION_DUPLICATE"


# --- post-type classification -----------------------------------------

class PostType:
    """Internal classification concept -- not a persisted DB column, so
    (unlike src/domain/*_status.py) this has no migration to mirror."""

    ONE_BOOK = "ONE_BOOK"
    MULTIPLE_BOOKS = "MULTIPLE_BOOKS"
    COMBO = "COMBO"
    GENERAL_POST = "GENERAL_POST"
    AMBIGUOUS = "AMBIGUOUS"


# --- single-book title/author/ISBN extraction (moved from
# scripts/create_candidates_from_cleaned_posts.py's extract_book_identity,
# unchanged patterns/order/labels) -------------------------------------

LEADING_POST_MARKERS = (
    "Sách có sẵn ở Đức",
    "Sách có sẵn",
)

TITLE_DESCRIPTION_PATTERNS = (
    re.compile(
        r"^(?P<title>.{2,160}?)\s+"
        r"(?:là|là một)\s+cuốn sách\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<title>.{2,160}?)\s+"
        r"(?:là|là một)\s+bộ sách\b",
        flags=re.IGNORECASE,
    ),
)

TITLE_AUTHOR_PATTERNS = (
    re.compile(
        r"[“\"](?P<title>[^”\"]+)[”\"]\s+"
        r"(?:của|by)\s+"
        r"(?P<author>[A-ZÀ-Ỹ][^,\n.–—]+)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?P<title>[^\n]{3,160}?)\s+"
        r"(?:của|by)\s+"
        r"(?P<author>[A-ZÀ-Ỹ][^,\n.–—]+)",
        flags=re.IGNORECASE,
    ),
)

AUTHOR_REJECTION_MARKERS = (
    "cuốn sách",
    "bộ sách",
    "cơ thể",
    "tuổi dậy thì",
    "giúp các em",
    "giúp trẻ",
    "nội dung",
    "ngôn ngữ",
    "minh họa",
    "phát triển",
    "thay đổi",
    "hướng dẫn",
    "đây là",
)

MAX_TITLE_LENGTH = 160
MAX_AUTHOR_LENGTH = 100

ISBN_PATTERN = re.compile(
    r"\b(?:ISBN(?:-1[03])?\s*:?\s*)?"
    r"(?P<isbn>97[89][\d\-\s]{10,20}\d)\b",
    flags=re.IGNORECASE,
)

_UI_LABEL_TITLES = frozenset(
    {
        "bài viết",
        "sách có sẵn",
        "sách có sẵn ở đức",
        "yêu con",
        "tiệm sách yêu con ở đức",
    }
)


def normalize_text(value: str | None) -> str:
    """Normalize whitespace while preserving Vietnamese text."""
    if not value:
        return ""
    return " ".join(value.split()).strip()


def remove_leading_post_markers(value: str) -> str:
    """Remove known Facebook post labels before identity extraction."""
    cleaned = normalize_text(value)
    marker_removed = True

    while marker_removed:
        marker_removed = False
        lowered = cleaned.casefold()

        for marker in LEADING_POST_MARKERS:
            marker_text = normalize_text(marker)
            marker_lower = marker_text.casefold()

            if lowered == marker_lower:
                return ""

            if lowered.startswith(marker_lower + " "):
                cleaned = cleaned[len(marker_text):].lstrip(" \t\r\n:–—-")
                marker_removed = True
                break

    return cleaned


def clean_extracted_title(value: str) -> str:
    """Clean punctuation around an extracted book title."""
    return normalize_text(value).strip(" \t\r\n“”\"'.,;:–—-")


def clean_extracted_author(value: str) -> str:
    """Clean punctuation and trailing description from an author name."""
    cleaned = normalize_text(value).strip(" \t\r\n“”\"'.,;:–—-")

    stop_markers = (
        " là cuốn sách",
        " là một cuốn sách",
        " chia sẻ",
        " giới thiệu",
        " kể về",
        " mang đến",
        " giúp",
        " được",
    )

    cleaned_lower = cleaned.lower()

    for marker in stop_markers:
        marker_position = cleaned_lower.find(marker)

        if marker_position >= 0:
            cleaned = cleaned[:marker_position].strip()
            cleaned_lower = cleaned.lower()

    return cleaned


def looks_like_description_fragment(value: str | None) -> bool:
    """Return True when a value resembles prose rather than a person name."""
    cleaned = normalize_text(value)

    if not cleaned:
        return False

    lowered = cleaned.casefold()

    if any(marker in lowered for marker in AUTHOR_REJECTION_MARKERS):
        return True

    word_count = len(cleaned.split())

    if word_count > 8:
        return True

    if len(cleaned) > MAX_AUTHOR_LENGTH:
        return True

    if re.search(r"[.!?;:]", cleaned):
        return True

    return False


def normalize_isbn(value: str | None) -> str | None:
    """Normalize an ISBN candidate to digits only."""
    if not value:
        return None

    digits = re.sub(r"\D", "", value)

    if len(digits) in {10, 13}:
        return digits

    return None


def validate_extracted_identity(
    title: str | None,
    author: str | None,
) -> tuple[str, str | None, list[str]]:
    """Validate extracted fields and return normalized values plus warnings.

    Raises RuntimeError when no reliable title survives validation. Used
    directly by scripts/create_candidates_from_cleaned_posts.py's
    build_explicit_extractions() (an explicitly-supplied --candidate-
    title should fail loudly/immediately if invalid), and internally by
    extract_single_book_identity() below (which catches the RuntimeError
    and returns None instead -- "no title found" is an expected pure-
    function outcome there, not a raised error).
    """
    warnings: list[str] = []
    cleaned_title = clean_extracted_title(title or "")
    cleaned_author = clean_extracted_author(author) if author else None

    if not cleaned_title:
        raise RuntimeError(
            "No reliable book title could be extracted from the cleaned "
            "Facebook post."
        )

    if len(cleaned_title) > MAX_TITLE_LENGTH:
        raise RuntimeError(
            "The extracted title is too long and appears to contain post "
            "description text."
        )

    if cleaned_title.casefold() in _UI_LABEL_TITLES:
        raise RuntimeError(
            "The extracted title is a Facebook interface label, not a "
            "reliable book title."
        )

    if cleaned_author and looks_like_description_fragment(cleaned_author):
        warnings.append(
            "The extracted author resembled description text and was removed."
        )
        cleaned_author = None

    return cleaned_title, cleaned_author, warnings


@dataclass(frozen=True)
class SingleExtraction:
    """One deterministically extracted book identity."""

    extracted_title: str
    extracted_author: str | None
    possible_isbn: str | None
    extraction_confidence: float
    matched_pattern: str
    warnings: tuple[str, ...] = ()


def extract_single_book_identity(cleaned_text: str) -> SingleExtraction | None:
    """Deterministically extract one book's title/author/ISBN from text.

    Returns None (never raises) when no reliable title can be found --
    this is an expected outcome for a pure classification function, not
    an error. scripts/create_candidates_from_cleaned_posts.py's
    extract_book_identity() wraps this and raises RuntimeError on None
    to preserve its existing external behavior.
    """
    normalized_text = remove_leading_post_markers(normalize_unicode_text(cleaned_text or ""))

    if not normalized_text:
        return None

    extracted_title: str | None = None
    extracted_author: str | None = None
    matched_pattern: str | None = None

    for pattern_number, pattern in enumerate(TITLE_DESCRIPTION_PATTERNS, start=1):
        match = pattern.search(normalized_text)

        if not match:
            continue

        extracted_title = clean_extracted_title(match.group("title"))
        extracted_author = None
        matched_pattern = f"TITLE_DESCRIPTION_PATTERN_{pattern_number}"
        break

    if not extracted_title:
        for pattern_number, pattern in enumerate(TITLE_AUTHOR_PATTERNS, start=1):
            match = pattern.search(normalized_text)

            if not match:
                continue

            extracted_title = clean_extracted_title(match.group("title"))
            extracted_author = clean_extracted_author(match.group("author"))
            matched_pattern = f"TITLE_AUTHOR_PATTERN_{pattern_number}"

            if extracted_title:
                break

    try:
        extracted_title, extracted_author, warnings = validate_extracted_identity(
            title=extracted_title, author=extracted_author
        )
    except RuntimeError:
        return None

    isbn_match = ISBN_PATTERN.search(normalized_text)
    possible_isbn = normalize_isbn(isbn_match.group("isbn")) if isbn_match else None

    extraction_confidence = 0.90

    if matched_pattern and matched_pattern.startswith("TITLE_DESCRIPTION_PATTERN_"):
        extraction_confidence = 0.85

    if not extracted_author:
        extraction_confidence = min(extraction_confidence, 0.80)

    if possible_isbn:
        extraction_confidence = min(0.95, extraction_confidence + 0.03)

    return SingleExtraction(
        extracted_title=extracted_title,
        extracted_author=extracted_author,
        possible_isbn=possible_isbn,
        extraction_confidence=extraction_confidence,
        matched_pattern=matched_pattern or "UNKNOWN",
        warnings=tuple(warnings),
    )


# --- multi-book / combo / general-post classification ------------------

# Numbered ("1.", "2)"), circled-number, and common bullet-glyph list
# markers. Deliberately narrow: only lines that *look like* an explicit
# list item are treated as separate sellable-unit candidates -- ordinary
# prose sentences (even ones starting with a hyphen used as punctuation)
# are not list items just because MULTIPLE_BOOKS detection would be
# convenient; under-detecting a list falls through to AMBIGUOUS/ONE_BOOK
# instead of ever inventing a split that was not clearly there.
_BULLET_LINE_RE = re.compile(
    r"^\s*(?:[-*•‣▪●○]|\d{1,3}[.\)]|[①②③④⑤⑥⑦⑧⑨⑩])\s+(?P<content>\S.*)$"
)

# Explicit bundle/combo language only -- deliberately excludes the bare
# word "bộ sách" ("book set"), which is common as an ordinary single
# product's own name (e.g. a boxed set sold as one SKU, matched fine by
# TITLE_DESCRIPTION_PATTERNS above as ONE_BOOK) and is not, on its own,
# evidence of a bundle deal spanning otherwise-separate books.
_COMBO_KEYWORDS = (
    "combo",
    "trọn bộ",
    "nguyên bộ",
    "cả bộ",
)

_COMBO_SET_COUNT_RE = re.compile(r"\bbộ\s*\d+\s*cuốn\b", re.IGNORECASE)


def detect_combo_evidence(lowered_text: str) -> bool:
    """Return True when the post text contains explicit bundle/combo
    language (see _COMBO_KEYWORDS docstring for why "bộ sách" alone does
    not count)."""
    if any(keyword in lowered_text for keyword in _COMBO_KEYWORDS):
        return True

    return bool(_COMBO_SET_COUNT_RE.search(lowered_text))


_BOOK_SIGNAL_KEYWORDS = (
    "sách",
    "cuốn",
    "tác giả",
    "nxb",
    "nhà xuất bản",
    "isbn",
)


def has_book_signal(lowered_text: str) -> bool:
    """Return True when the text contains any book-domain vocabulary at
    all -- used only to distinguish GENERAL_POST (no signal -> AUTO_
    REJECT) from AMBIGUOUS (some signal, but no reliable title -> REVIEW_
    REQUIRED); never used to invent a title."""
    return any(keyword in lowered_text for keyword in _BOOK_SIGNAL_KEYWORDS)


def split_list_items(cleaned_text: str) -> list[str]:
    """Return the content of each explicit bulleted/numbered list line,
    in order, operating on the still-line-structured cleaned_text (never
    the whitespace-collapsed single-line normalized_text the single-book
    patterns use)."""
    items: list[str] = []

    for raw_line in (cleaned_text or "").splitlines():
        match = _BULLET_LINE_RE.match(raw_line)

        if match:
            content = match.group("content").strip()

            if content:
                items.append(content)

    return items


# Never create a title from a line matching any of these -- CLAUDE.md/
# task requirement: never guess a title from UI labels, timestamps,
# reactions, navigation text, price-only strings, emoji-only lines, or
# single-character noise. Most Facebook-chrome noise is already stripped
# by clean_facebook_raw_pages.py before this module ever sees the text;
# this is an independent, defense-in-depth check specifically for list-
# item *content* (a bullet could legitimately be a price line inside an
# otherwise real book list, which the page-level cleaner has no reason to
# strip since it is not chrome/interface noise).
_PRICE_ONLY_RE = re.compile(
    r"^[\d][\d.,\s]*\s*(?:đ|vnđ|k|€|eur|usd|\$)\.?$", re.IGNORECASE
)
_TIMESTAMP_ONLY_RE = re.compile(
    r"^\d{1,2}:\d{2}(:\d{2})?$"
    r"|^(hôm qua|hôm nay|vừa xong|\d+\s*(phút|giờ|ngày)\s*trước)\b",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "]"
)


def is_unusable_title_line(line: str) -> bool:
    """Return True when a line can never be used as (or as the basis
    for) a book title -- UI noise, a bare price, a bare timestamp, an
    emoji-only line, or single-character noise."""
    stripped = (line or "").strip(" \t-•*·.,:;–—")

    if not stripped:
        return True

    if len(stripped) <= 1:
        return True

    if _PRICE_ONLY_RE.match(stripped):
        return True

    if _TIMESTAMP_ONLY_RE.match(stripped):
        return True

    without_emoji = _EMOJI_RE.sub("", stripped).strip(" \t-•*·.,:;–—")

    if not without_emoji:
        return True

    return False


def _normalize_for_dedupe(title: str) -> str:
    return normalize_text(title).casefold()


@dataclass(frozen=True)
class ExtractedCandidate:
    """One book identity ready to become exactly one product_candidates row."""

    extracted_title: str
    extracted_author: str | None
    possible_isbn: str | None
    candidate_type: str
    extraction_confidence: float
    matched_pattern: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionRunResult:
    """The full result of classifying and extracting one cleaned post."""

    post_type: str
    decision: DecisionResult
    candidates: tuple[ExtractedCandidate, ...] = field(default_factory=tuple)


def _single_to_candidate(single: SingleExtraction, candidate_type: str) -> ExtractedCandidate:
    return ExtractedCandidate(
        extracted_title=single.extracted_title,
        extracted_author=single.extracted_author,
        possible_isbn=single.possible_isbn,
        candidate_type=candidate_type,
        extraction_confidence=single.extraction_confidence,
        matched_pattern=single.matched_pattern,
        warnings=single.warnings,
    )


def run_automatic_extraction(cleaned_text: str) -> ExtractionRunResult:
    """Classify one cleaned Facebook post and deterministically extract
    every distinct sellable book identity it contains.

    Pure function: no I/O, no database access. Cross-run idempotency
    (whether this raw_page already has candidates from a prior run) is
    the caller's responsibility -- see EXTRACTION_DUPLICATE's docstring
    above.
    """
    normalized_whole = normalize_unicode_text(cleaned_text or "")

    if not normalized_whole.strip():
        return ExtractionRunResult(
            post_type=PostType.GENERAL_POST,
            decision=DecisionResult(
                outcome=Outcome.AUTO_REJECT,
                rule_code=EXTRACTION_GENERAL_POST,
                reason="Cleaned post text is empty.",
            ),
        )

    lowered = normalized_whole.casefold()
    combo_evidence = detect_combo_evidence(lowered)

    raw_list_items = split_list_items(cleaned_text)
    usable_items = [item for item in raw_list_items if not is_unusable_title_line(item)]

    # Both combo/bundle language AND a clear itemized list present at
    # once: could be "combo of these N books" (COMBO) or "these are N
    # separate books, one of which happens to mention a combo deal"
    # (MULTIPLE_BOOKS). Never guess between them.
    if combo_evidence and len(usable_items) >= 2:
        return ExtractionRunResult(
            post_type=PostType.AMBIGUOUS,
            decision=DecisionResult(
                outcome=Outcome.REVIEW_REQUIRED,
                rule_code=EXTRACTION_AMBIGUOUS,
                reason=(
                    "Post contains both combo/bundle wording and a "
                    "multi-item list -- whether this is one combo or "
                    "multiple separate books cannot be determined "
                    "automatically."
                ),
                evidence={"combo_evidence": True, "list_item_count": len(usable_items)},
            ),
        )

    if combo_evidence:
        single = extract_single_book_identity(cleaned_text)

        if single is None:
            return ExtractionRunResult(
                post_type=PostType.AMBIGUOUS,
                decision=DecisionResult(
                    outcome=Outcome.REVIEW_REQUIRED,
                    rule_code=EXTRACTION_INSUFFICIENT_TITLE,
                    reason=(
                        "Combo/bundle wording was found but no reliable "
                        "combo title could be extracted."
                    ),
                ),
            )

        return ExtractionRunResult(
            post_type=PostType.COMBO,
            decision=DecisionResult(
                outcome=Outcome.AUTO_PASS,
                rule_code=EXTRACTION_COMBO,
                reason="Explicit combo/bundle wording matched a single sellable bundle.",
                confidence=single.extraction_confidence,
            ),
            candidates=(_single_to_candidate(single, "BOOK_COMBO"),),
        )

    if len(usable_items) >= 2:
        extracted: list[ExtractedCandidate] = []
        seen_titles: set[str] = set()

        for item in usable_items:
            single = extract_single_book_identity(item)

            if single is None:
                continue  # skip one unresolvable bullet; do not fail the whole post

            normalized_title = _normalize_for_dedupe(single.extracted_title)

            if normalized_title in seen_titles:
                continue

            seen_titles.add(normalized_title)
            extracted.append(_single_to_candidate(single, "SINGLE_BOOK"))

        if len(extracted) >= 2:
            return ExtractionRunResult(
                post_type=PostType.MULTIPLE_BOOKS,
                decision=DecisionResult(
                    outcome=Outcome.AUTO_PASS,
                    rule_code=EXTRACTION_MULTIPLE_BOOKS,
                    reason=(
                        f"{len(extracted)} distinct sellable books were "
                        "identified from an explicit list."
                    ),
                    evidence={"list_item_count": len(usable_items)},
                ),
                candidates=tuple(extracted),
            )

        return ExtractionRunResult(
            post_type=PostType.AMBIGUOUS,
            decision=DecisionResult(
                outcome=Outcome.REVIEW_REQUIRED,
                rule_code=EXTRACTION_INSUFFICIENT_TITLE,
                reason=(
                    "A multi-item list was detected but fewer than two "
                    "items yielded a reliable title."
                ),
                evidence={"list_item_count": len(usable_items), "resolved_count": len(extracted)},
            ),
        )

    # No list, no combo language: try the ordinary one-book pattern set.
    single = extract_single_book_identity(cleaned_text)

    if single is not None:
        return ExtractionRunResult(
            post_type=PostType.ONE_BOOK,
            decision=DecisionResult(
                outcome=Outcome.AUTO_PASS,
                rule_code=EXTRACTION_ONE_BOOK,
                reason="Exactly one book identity was reliably extracted.",
                confidence=single.extraction_confidence,
            ),
            candidates=(_single_to_candidate(single, "SINGLE_BOOK"),),
        )

    if has_book_signal(lowered):
        return ExtractionRunResult(
            post_type=PostType.AMBIGUOUS,
            decision=DecisionResult(
                outcome=Outcome.REVIEW_REQUIRED,
                rule_code=EXTRACTION_INSUFFICIENT_TITLE,
                reason=(
                    "Post appears to describe a book but no reliable "
                    "title could be extracted."
                ),
            ),
        )

    return ExtractionRunResult(
        post_type=PostType.GENERAL_POST,
        decision=DecisionResult(
            outcome=Outcome.AUTO_REJECT,
            rule_code=EXTRACTION_GENERAL_POST,
            reason="No book-related evidence was found in this post.",
        ),
    )
