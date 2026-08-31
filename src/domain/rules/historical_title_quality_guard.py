"""OFFLINE historical-preview title-quality guard.

This is the "historical safety adapter" step in the historical
candidate-extraction preview pipeline:

    HistoryRecord.full_text
    -> historical_text_cleaner.clean_historical_facebook_text()
    -> extraction_rules.run_automatic_extraction()    (UNCHANGED)
    -> THIS MODULE (reject/keep each candidate)
    -> preview result

It never touches, weakens, or reimplements extraction_rules.py's own
title/author regex patterns -- those remain the single source of truth
for WHAT gets extracted. This module only judges, after the fact,
whether an already-extracted candidate title is plausible enough to
show as an AUTO_PASS preview, or should instead be demoted so a human
reviews it (the project's own stated preference -- CLAUDE.md section
5.2, this task's own Phase 6: "If a plausible title cannot be isolated
confidently: REVIEW_REQUIRED is preferred over AUTO_PASS").

Every check here is a narrow, structural, explainable heuristic aimed
at one specific, real, observed failure signature (see each function's
own docstring for the exact historical record id(s) it was built from)
-- deliberately NOT an attempt to solve general Vietnamese semantic
ambiguity. When in doubt, a check does nothing (favors keeping the
candidate) rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Same three confirmed chrome lines historical_text_cleaner.py already
# strips as whole leading paragraphs -- checked again here as a
# substring guard because a regex match can still pull one of these
# phrases into the MIDDLE of a captured title (e.g. when it survives
# inside a longer flattened span) even after the leading-line strip.
_UI_CHROME_SUBSTRINGS = (
    "tải lên từ di động",
    "sách có sẵn tại đức",
)

# Deliberately narrow, well-established emoji code blocks -- a title
# containing several of these is a decorative/promotional fragment,
# never a genuine printed book title. Mirrors the ranges extraction_
# rules.py's own (private, list-item-scoped) emoji filter uses; defined
# independently here rather than importing that private name, since
# this guard's purpose (title-level rejection) and threshold (count,
# not "is emoji-only") are both different.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)
_MIN_EMOJI_COUNT_FOR_REJECTION = 3

# A period/question/exclamation mark followed by whitespace and another
# capitalized word is a strong "this is at least two sentences of
# prose" signal -- a real title essentially never has this shape.
_INTERNAL_SENTENCE_BREAK_RE = re.compile(r"[.!?]\s+[A-ZÀ-Ỹ]")

# Word-count thresholds, calibrated against every AUTO_PASS candidate
# observed across the 116 FINAL INCLUDE records during this fix
# (longest genuine title observed: 8 words) with generous headroom.
_MAX_PLAUSIBLE_TITLE_WORDS = 18
_MODERATE_LENGTH_WORDS = 8

# TITLE_DESCRIPTION_PATTERN_* ("X là (một) cuốn sách") anchors at the
# START of the flattened text and can capture almost nothing (record
# #1104: captured "Đây", a bare pronoun) when the real title is never
# actually named in the text. A plausible title preceding "là (một)
# cuốn sách" is essentially never fewer than 3 words in this corpus.
_MIN_DESCRIPTION_PATTERN_WORDS = 3

# TITLE_AUTHOR_PATTERN_2 (unquoted "X của/by Y", non-greedy, searched
# anywhere in the flattened text -- NOT the quote-delimited PATTERN_1,
# which is far more trustworthy and is never subject to this check) is
# the pattern most prone to truncating a title that itself legitimately
# contains "của" (record #1064: "Sức mạnh của thói quen" truncated to
# "Sức mạnh" with "thói quen 9e" misread as an author). A very short
# title plus a successfully-extracted author from this specific loose
# pattern is a real, observed truncation signature.
_MAX_SUSPICIOUS_SHORT_TITLE_WORDS = 4


def _word_count(value: str) -> int:
    return len(value.split())


def _first_alphabetic_char_is_lowercase(value: str) -> bool:
    for character in value:
        if character.isalpha():
            return character == character.lower() and character != character.upper()
    return False


def _contains_ui_chrome(lowered_title: str) -> bool:
    return any(marker in lowered_title for marker in _UI_CHROME_SUBSTRINGS)


def _is_emoji_heavy(title: str) -> bool:
    return len(_EMOJI_RE.findall(title)) >= _MIN_EMOJI_COUNT_FOR_REJECTION


def _looks_like_prose_fragment(title: str) -> bool:
    """Records #1156/#1189/#1560/#1568: a long span grabbed by a non-
    greedy 'X của Y' search landing in the middle of an unrelated
    descriptive paragraph -- and record #1863: a moderate-length span
    crossing an actual sentence boundary."""
    word_count = _word_count(title)

    if word_count > _MAX_PLAUSIBLE_TITLE_WORDS:
        return True

    if word_count > _MODERATE_LENGTH_WORDS and _INTERNAL_SENTENCE_BREAK_RE.search(title):
        return True

    return False


def _looks_like_mid_sentence_continuation(title: str) -> bool:
    """Records #1156/#1189/#1560/#1568: the non-greedy search's match
    start often lands mid-word/mid-clause, so the first letter this
    guard sees is lowercase -- something a real title, which starts a
    new sentence/label, essentially never does in this corpus."""
    return _first_alphabetic_char_is_lowercase(title)


def _looks_like_insufficient_description_capture(
    title: str, matched_pattern: str
) -> bool:
    """Record #1104: TITLE_DESCRIPTION_PATTERN_* anchored at the very
    start of the flattened text and captured only a bare pronoun
    ("Đây") because the record never actually names its book."""
    if not matched_pattern.startswith("TITLE_DESCRIPTION_PATTERN_"):
        return False

    return _word_count(title) < _MIN_DESCRIPTION_PATTERN_WORDS


def _looks_like_suspicious_short_truncation(
    title: str, matched_pattern: str, extracted_author: str | None
) -> bool:
    """Record #1064 (and #1482's "Thuật Xử Thế"/"Bí Mật"): a very short
    title plus an author extracted via the loose, unquoted PATTERN_2 --
    the specific, observed truncation signature for a title that
    itself contains "của". PATTERN_1 (quote-delimited, e.g. record
    #1231's "Suối nguồn") is exempt -- an explicit quoted phrase is not
    a truncation artifact merely for being short."""
    if matched_pattern != "TITLE_AUTHOR_PATTERN_2":
        return False

    if not extracted_author:
        return False

    return _word_count(title) <= _MAX_SUSPICIOUS_SHORT_TITLE_WORDS


@dataclass(frozen=True)
class TitleQualityVerdict:
    is_acceptable: bool
    rejection_reason: str | None = None


def evaluate_title_quality(
    title: str,
    *,
    matched_pattern: str,
    extracted_author: str | None,
) -> TitleQualityVerdict:
    """Judge one already-extracted candidate title. Never mutates or
    re-derives the title itself -- only accepts or rejects it as-is."""
    lowered = title.casefold()

    if _contains_ui_chrome(lowered):
        return TitleQualityVerdict(
            False, "Extracted title contains Facebook/export UI-chrome text."
        )

    if _is_emoji_heavy(title):
        return TitleQualityVerdict(
            False, "Extracted title is emoji-heavy, not a plausible book title."
        )

    if _looks_like_prose_fragment(title):
        return TitleQualityVerdict(
            False, "Extracted title looks like a prose sentence fragment, not a title."
        )

    if _looks_like_mid_sentence_continuation(title):
        return TitleQualityVerdict(
            False,
            "Extracted title starts mid-sentence/mid-clause (lowercase start), "
            "not a plausible title.",
        )

    if _looks_like_insufficient_description_capture(title, matched_pattern):
        return TitleQualityVerdict(
            False,
            "Extracted title is too short to be a plausible description-pattern "
            "capture -- the record likely never names a specific title.",
        )

    if _looks_like_suspicious_short_truncation(title, matched_pattern, extracted_author):
        return TitleQualityVerdict(
            False,
            "Extracted title is suspiciously short for an unquoted 'X của Y' "
            "match -- likely truncated a title that itself contains 'của'.",
        )

    return TitleQualityVerdict(True)
