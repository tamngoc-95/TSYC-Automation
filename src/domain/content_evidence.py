"""Evidence ledger for storefront descriptions (CLAUDE.md 2.2 / 15.1,
TSYC_CONTENT_GUIDE.md section 11).

A description is written from an explicit list of evidence items:

  {"id": "E1",
   "kind": VERIFIED_FACT | SOURCE_SUPPORTED_SUMMARY | EDITORIAL_INTERPRETATION | UNKNOWN,
   "statement": "...",                       # what the item says, in Vietnamese
   "source_type": PRODUCT_METADATA | REFERENCE_DESCRIPTION | PUBLISHER_DESCRIPTION
                  | COVER_TEXT | PREVIEW_PAGE | TABLE_OF_CONTENTS,
   "source_ref": "...",                      # reference_id / image URL / field name
   "anchors": ["Ê-la", "hoàng cung"],        # words the prose may use for it
   "supports_benefit": false,                # a source states a reader benefit
   "legibility": "READABLE"}                 # PREVIEW_PAGE / COVER_TEXT only

Machine checks (pure, no I/O):
  - validate_evidence: well-formed items; a REFERENCE_DESCRIPTION or
    PRODUCT_METADATA anchor must literally occur in that source text; a
    PREVIEW_PAGE / COVER_TEXT item must point at an image whose review
    record is APPROVED (and, for a preview, relevant) and be READABLE --
    an illegible page is never evidence.
  - evidence_strength: INSUFFICIENT / THIN / ADEQUATE / RICH, from the
    narrative (non-metadata) VERIFIED_FACT / SOURCE_SUPPORTED_SUMMARY items.
  - find_unsupported_statements (Vietnamese prose): proper names and
    numbers that no evidence, source or product field contains; benefit
    claims without a benefit-supporting source; anchors of UNKNOWN items.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from src.domain.rules import content_style_lexicon as lexicon

VERIFIED_FACT = "VERIFIED_FACT"
SOURCE_SUPPORTED_SUMMARY = "SOURCE_SUPPORTED_SUMMARY"
EDITORIAL_INTERPRETATION = "EDITORIAL_INTERPRETATION"
UNKNOWN = "UNKNOWN"
EVIDENCE_KINDS = frozenset({VERIFIED_FACT, SOURCE_SUPPORTED_SUMMARY, EDITORIAL_INTERPRETATION, UNKNOWN})
SUPPORTING_KINDS = frozenset({VERIFIED_FACT, SOURCE_SUPPORTED_SUMMARY})

PRODUCT_METADATA = "PRODUCT_METADATA"
REFERENCE_DESCRIPTION = "REFERENCE_DESCRIPTION"
PUBLISHER_DESCRIPTION = "PUBLISHER_DESCRIPTION"
COVER_TEXT = "COVER_TEXT"
PREVIEW_PAGE = "PREVIEW_PAGE"
TABLE_OF_CONTENTS = "TABLE_OF_CONTENTS"
SOURCE_TYPES = frozenset(
    {PRODUCT_METADATA, REFERENCE_DESCRIPTION, PUBLISHER_DESCRIPTION, COVER_TEXT, PREVIEW_PAGE, TABLE_OF_CONTENTS}
)
_TEXT_SOURCES = frozenset({PRODUCT_METADATA, REFERENCE_DESCRIPTION, PUBLISHER_DESCRIPTION})
_IMAGE_SOURCES = frozenset({COVER_TEXT, PREVIEW_PAGE, TABLE_OF_CONTENTS})
READABLE = "READABLE"

INSUFFICIENT = "INSUFFICIENT"
THIN = "THIN"
ADEQUATE = "ADEQUATE"
RICH = "RICH"

UNSUPPORTED_NAME = "UNSUPPORTED_NAME"
UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"
UNSUPPORTED_BENEFIT = "UNSUPPORTED_BENEFIT"
USES_UNKNOWN_FACT = "USES_UNKNOWN_FACT"

_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)
_NUMBER = re.compile(r"\d+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def fold(text: str | None) -> str:
    """Lower-case, NFC-normalized, whitespace-collapsed -- diacritics kept
    (Vietnamese meaning depends on them)."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).lower().split())


def validate_evidence(
    items: Sequence[Mapping[str, Any]],
    source_texts: Mapping[str, str],
    approved_images: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Problems with the ledger; [] when every item is well formed and
    checkable. source_texts maps source_ref -> text (reference_id ->
    reference_description, metadata field name -> value); approved_images
    maps image URL -> its review record."""
    problems: list[str] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "")
        label = item_id or "<missing id>"
        if not item_id or item_id in seen:
            problems.append(f"{label}: id missing or duplicated")
        seen.add(item_id)
        kind, source_type = item.get("kind"), item.get("source_type")
        if kind not in EVIDENCE_KINDS:
            problems.append(f"{label}: kind {kind!r} is not one of {sorted(EVIDENCE_KINDS)}")
        if source_type not in SOURCE_TYPES:
            problems.append(f"{label}: source_type {source_type!r} is not one of {sorted(SOURCE_TYPES)}")
            continue
        if not str(item.get("statement") or "").strip():
            problems.append(f"{label}: statement missing")
        anchors = [str(anchor) for anchor in item.get("anchors") or [] if str(anchor).strip()]
        if kind == UNKNOWN:
            continue
        if source_type in _TEXT_SOURCES:
            text = source_texts.get(str(item.get("source_ref") or ""))
            if text is None:
                problems.append(f"{label}: source_ref {item.get('source_ref')!r} is not a known source text")
                continue
            folded = fold(text)
            missing = [anchor for anchor in anchors if fold(anchor) not in folded]
            if missing:
                problems.append(f"{label}: anchors not found in the cited source: {missing}")
        else:
            record = approved_images.get(str(item.get("source_ref") or ""))
            if not record:
                problems.append(f"{label}: {source_type} must cite an APPROVED reviewed image of this product")
            elif source_type == PREVIEW_PAGE and (record.get("visual_review") or {}).get("relevant_preview") is not True:
                problems.append(f"{label}: preview page was not reviewed as relevant content")
            if item.get("legibility") != READABLE:
                problems.append(f"{label}: {source_type} evidence must be READABLE (illegible pages are not evidence)")
    return problems


def evidence_strength(items: Sequence[Mapping[str, Any]]) -> str:
    narrative = [
        item for item in items
        if item.get("kind") in SUPPORTING_KINDS and item.get("source_type") != PRODUCT_METADATA
    ]
    if not narrative:
        return INSUFFICIENT
    if len(narrative) < 2:
        return THIN
    if len(narrative) < 5:
        return ADEQUATE
    return RICH


def _support_corpus(
    items: Sequence[Mapping[str, Any]],
    product: Mapping[str, Any],
    source_texts: Sequence[str],
) -> str:
    parts: list[str] = list(source_texts)
    for item in items:
        if item.get("kind") == UNKNOWN:
            continue
        parts.append(str(item.get("statement") or ""))
        parts.extend(str(anchor) for anchor in item.get("anchors") or [])
    for field in ("title", "author", "publisher", "translator", "series_name"):
        parts.append(str(product.get(field) or ""))
    return fold("\n".join(parts))


def find_unsupported_statements(
    text: str,
    items: Sequence[Mapping[str, Any]],
    product: Mapping[str, Any],
    source_texts: Sequence[str] = (),
) -> dict[str, list[str]]:
    """{code: offending tokens} for Vietnamese storefront prose."""
    corpus = _support_corpus(items, product, source_texts)
    issues: dict[str, list[str]] = {}

    names: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        words = _WORD.findall(sentence)
        # The first word of a sentence is capitalized anyway; a quoted or
        # mid-sentence capital is a name/title that must be supported.
        for word in words[1:]:
            folded_word = fold(word)
            if (
                word[:1].isupper()
                and folded_word not in corpus
                and folded_word not in lexicon.STOPWORDS["vi"]
                and folded_word not in lexicon.COMMON_PROPER_NOUNS_VI
            ):
                names.append(word)
    if names:
        issues[UNSUPPORTED_NAME] = sorted(set(names))

    numbers = [number for number in _NUMBER.findall(text) if number not in corpus]
    if numbers:
        issues[UNSUPPORTED_NUMBER] = sorted(set(numbers))

    benefits = lexicon.phrases_in(text, lexicon.BENEFIT_CLAIMS["vi"])
    if benefits and not any(
        item.get("supports_benefit") is True and item.get("kind") in SUPPORTING_KINDS for item in items
    ):
        issues[UNSUPPORTED_BENEFIT] = sorted(set(benefits))

    folded = fold(text)
    unknown = [
        str(anchor)
        for item in items if item.get("kind") == UNKNOWN
        for anchor in item.get("anchors") or []
        if str(anchor).strip() and fold(anchor) in folded
    ]
    if unknown:
        issues[USES_UNKNOWN_FACT] = sorted(set(unknown))
    return issues
