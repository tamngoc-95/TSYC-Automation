"""OFFLINE historical Facebook export text cleaner.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    src/domain/rules/extraction_rules.py's run_automatic_extraction()
    and its title/author regexes were built and tested against
    scripts/clean_facebook_raw_pages.py's clean_facebook_text() output
    -- text with Facebook interface chrome already stripped by that
    cleaner's own rules (EXACT_LINES_TO_REMOVE, PREFIXES_TO_REMOVE,
    single-character/symbol noise, consecutive-duplicate-line removal).

    src/services/facebook_history_parser.py's HistoryRecord.full_text
    comes from a DIFFERENT source (a personal Facebook data-export HTML
    file, not the live Playwright-collected raw_pages the cleaner above
    was written for) and is NOT run through that cleaner. It is already
    fairly clean (NFC-normalized, per-photo caption duplication already
    collapsed when strictly consecutive -- see that module's own
    docstring), but it still carries two artifacts specific to this
    export format that the extraction engine's regexes were never
    built to expect:

      1. A leading Facebook action-heading line -- "Tải lên từ di động"
         (mobile upload) or "Ảnh" (desktop photo upload) -- fused
         directly ahead of the real caption as the record's own first
         line. Neither the parser's own boilerplate list (which only
         drops "Cập nhật <date>" and a video-placeholder label) nor
         extraction_rules.py's LEADING_POST_MARKERS (which only strips
         "Sách có sẵn"/"Sách có sẵn ở Đức" fused into the SAME line as
         real content) reaches a standalone chrome line like this.

      2. A whole-caption duplication where the export's per-photo
         caption block and the post's own outer-caption block do not
         end up strictly adjacent in HistoryRecord.full_text (see that
         module's own _extract_full_text() docstring on the general
         problem; its adjacent-block dedup only catches an
         IMMEDIATELY-repeated single block, not a repeated *sequence*
         of several paragraphs). Left in place, this makes extraction_
         rules.py's whole-text-flattening, non-greedy "X của/by Y"
         search latch onto the SECOND, un-intended occurrence, or grab
         a huge span crossing both copies.

    This module fixes ONLY those two, real, confirmed-observed
    artifacts -- see tests/test_historical_text_cleaner.py, which is
    built directly from actual records in the export (see the record
    ids named in each test's docstring). It does not touch, weaken, or
    reimplement any extraction_rules.py pattern; run_automatic_
    extraction() itself is called completely unchanged, on this
    module's output, by src/domain/rules/historical_candidate_
    extraction.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from clean_facebook_raw_pages import normalize_unicode_text  # noqa: E402

# Confirmed by directly scanning every FINAL INCLUDE historical record's
# own first line (2026-08-29 scan of the 116-record set) -- these three
# and only these three standalone first lines are genuine Facebook/
# export action-heading chrome, never real post content on their own.
# Every other first line observed (e.g. "Review sách hay🥰", "Sách cho
# bé 4 tuổi", a promotion heading) is real, user-authored content and
# must never be dropped here. Extend this set only with a new line
# confirmed present in real collected data the same way -- never
# speculatively.
KNOWN_LEADING_BOILERPLATE_LINES = frozenset(
    {
        "tải lên từ di động",  # Facebook's own mobile-upload action heading
        "ảnh",  # Facebook's own desktop photo-upload action heading ("Photo")
        "sách có sẵn tại đức",  # TSYC's own opening marker, this export's exact casing/suffix
    }
)


def _split_and_strip_paragraphs(text: str) -> list[str]:
    """Split on the parser's own paragraph boundary (a single "\\n" --
    see facebook_history_parser._extract_full_text(), which joins each
    leaf-block with exactly one "\\n"), stripping surrounding
    whitespace from each. A paragraph that becomes empty after
    stripping (the lone " " spacer artifact this export produces
    between a duplicated block-pair) is dropped entirely -- never
    treated as content."""
    return [paragraph.strip() for paragraph in text.split("\n")]


def _drop_leading_boilerplate(paragraphs: list[str]) -> list[str]:
    if not paragraphs:
        return paragraphs

    if paragraphs[0].casefold() in KNOWN_LEADING_BOILERPLATE_LINES:
        return paragraphs[1:]

    return paragraphs


def _collapse_exact_repeated_sequence(paragraphs: list[str]) -> list[str]:
    """Collapse a repeated block ONLY when it is a materially identical
    (casefold-normalized) whole-sequence duplicate -- the confirmed
    export artifact where the entire remaining paragraph list is
    exactly two back-to-back copies of the same k paragraphs (see this
    module's own docstring). Never collapses anything else: a genuinely
    different second paragraph, or a paragraph that merely shares a
    common opening phrase with an earlier one, is always preserved
    unchanged (Phase 3 requirement: "A + different caption B => preserve
    both")."""
    count = len(paragraphs)

    if count < 2 or count % 2 != 0:
        return paragraphs

    half = count // 2
    first_half = paragraphs[:half]
    second_half = paragraphs[half:]

    first_half_folded = [paragraph.casefold() for paragraph in first_half]
    second_half_folded = [paragraph.casefold() for paragraph in second_half]

    if first_half_folded == second_half_folded:
        return first_half

    return paragraphs


@dataclass(frozen=True)
class HistoricalCleaningStats:
    """Provenance for one cleaning run -- what, if anything, this
    module actually removed, for the preview pipeline's own summary
    reporting. Never affects the cleaned text itself."""

    dropped_leading_boilerplate: bool
    collapsed_duplicate_sequence: bool


def clean_historical_facebook_text_with_stats(
    full_text: str,
) -> tuple[str, HistoricalCleaningStats]:
    """Same cleaning pipeline as clean_historical_facebook_text() (see
    that function's own docstring for the full step-by-step reasoning),
    additionally reporting whether each step actually did anything for
    this specific record -- used only for audit/summary metrics, never
    to change extraction behavior."""
    normalized = normalize_unicode_text(full_text or "")

    if not normalized.strip():
        return "", HistoricalCleaningStats(False, False)

    paragraphs = _split_and_strip_paragraphs(normalized)
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]

    without_boilerplate = _drop_leading_boilerplate(paragraphs)
    dropped_leading_boilerplate = len(without_boilerplate) < len(paragraphs)

    collapsed = _collapse_exact_repeated_sequence(without_boilerplate)
    collapsed_duplicate_sequence = len(collapsed) < len(without_boilerplate)

    cleaned_text = "\n".join(collapsed).strip()
    return cleaned_text, HistoricalCleaningStats(
        dropped_leading_boilerplate, collapsed_duplicate_sequence
    )


def clean_historical_facebook_text(full_text: str) -> str:
    """Convert one historical HistoryRecord.full_text into extraction-
    ready text -- the same shape run_automatic_extraction()'s own
    regexes were built against.

    Pipeline (see this module's own docstring for exactly why each
    step exists, grounded in real observed records):

        1. NFKC-normalize and strip invisible Unicode characters
           (reuses clean_facebook_raw_pages.normalize_unicode_text --
           the same U+034F/NFKC protection extraction_rules.py itself
           already relies on; not reimplemented here).
        2. Split into paragraphs on "\\n" and strip surrounding
           whitespace from each; drop any paragraph left empty.
        3. Drop a single leading Facebook/export action-heading line
           when it exactly matches KNOWN_LEADING_BOILERPLATE_LINES.
        4. Collapse an exact whole-sequence duplicate (see
           _collapse_exact_repeated_sequence()'s own docstring).
        5. Rejoin with "\\n".

    Never removes or alters: real book titles, quoted titles, author
    phrases, price/listing lines, an ISBN, combo-structure keywords, or
    Vietnamese diacritics -- every step above operates only on whole-
    paragraph boilerplate/duplication, never on word-level content.
    Pure function: no I/O, no network, no Supabase/WooCommerce/Claude
    call.
    """
    cleaned_text, _stats = clean_historical_facebook_text_with_stats(full_text)
    return cleaned_text
