"""Explainable content-quality score for storefront descriptions (VI/EN/DE).

    Specificity and usefulness        30
    Natural language and engagement   25
    Structure and readability         20
    Reader relevance                  15
    Originality and non-repetition    10

Every point lost is named in the dimension's notes and in `defects`, so a
REVISE target is always explicit. The score is a calibration signal for the
editorial loop, never a substitute for verification: truthfulness, identity,
provenance and the existing content policies are HARD GATES evaluated
separately (`hard_gates`) -- a high score never passes content that fails
one, and a hard-gate failure is never "repaired" by adding text.

Pure: no I/O. Heuristics are deterministic and calibrated on the stored
descriptions (2026-10-17): the generic safe drafts fail the gates, the
FAHASA-2026-001 template descriptions score in the 60s-70s, and the
owner-approved example scores above the 85 target.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Mapping, Sequence

from src.domain import content_evidence as evidence
from src.domain.rules import content_rules
from src.domain.rules import content_style_lexicon as lexicon
from src.domain.rules import storefront_text

TARGET_SCORE = 85
SCORE_VERSION = "tsyc-content-score/1"

DIMENSION_WEIGHTS = {
    "specificity": 30,
    "natural_language": 25,
    "structure": 20,
    "reader_relevance": 15,
    "originality": 10,
}

# Defect codes (each names a REVISE target).
GENERIC_FILLER = "GENERIC_FILLER"
LOW_SPECIFICITY = "LOW_SPECIFICITY"
METADATA_OPENING = "METADATA_OPENING"
CLOSING_CLICHE = "CLOSING_CLICHE"
SALES_STACKING = "SALES_STACKING"
REPEATED_OPENERS = "REPEATED_SENTENCE_OPENERS"
MONOTONE_RHYTHM = "MONOTONE_RHYTHM"
PARAGRAPHING = "PARAGRAPHING"
SENTENCE_LENGTH = "SENTENCE_LENGTH"
NO_READER_RELEVANCE = "NO_READER_RELEVANCE"
MULTIPLE_SUITABILITY_CLOSINGS = "MULTIPLE_SUITABILITY_CLOSINGS"
BENEFIT_STACKING = "BENEFIT_STACKING"
CLOSE_TO_SOURCE = "CLOSE_TO_SOURCE_WORDING"
INTERNAL_REPETITION = "INTERNAL_REPETITION"
TEMPLATE_REUSE = "TEMPLATE_REUSE"

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_SUITABILITY = {
    "vi": re.compile(r"\b(?:phù hợp (?:cho|với)|dành cho)\b", re.IGNORECASE),
    "en": re.compile(r"\b(?:perfect for|ideal for|suitable for|for (?:all|every) (?:children|readers|families))\b", re.IGNORECASE),
    "de": re.compile(r"\b(?:geeignet für|ideal für|perfekt für|für alle,? die)\b", re.IGNORECASE),
}


# Average words per sentence that read well. Vietnamese "words" here are
# syllables, and the guide's Vietnamese style favours long literary
# sentences (section 4), so its band is wider.
_SENTENCE_WORD_BANDS = {"vi": (12, 45), "en": (9, 32), "de": (8, 30)}
# Vocabulary of the shop's own boilerplate / metadata, never "specific".
_GENERIC_VOCABULARY = frozenset(
    "thông tin tác giả nhà xuất bản sản phẩm tiệm sách yêu con dữ liệu ấn phẩm cuốn quyển "
    "trang tác phẩm hiện được chuẩn bị".split()
)


def _specific_phrases(text: str, language: str, exclude: str, corpus: Sequence[str]) -> set[tuple[str, str]]:
    """Two-word phrases (syllable pairs for Vietnamese) made of content
    words, minus identity words and phrases common across the catalog."""
    stop = lexicon.STOPWORDS.get(language, frozenset()) | _GENERIC_VOCABULARY
    excluded = set(_words(exclude))
    words = [w for w in _words(text) if w not in excluded]
    pairs = {
        (a, b) for a, b in zip(words, words[1:])
        if a not in stop and b not in stop and len(a) >= 2 and len(b) >= 2
    }
    if len(corpus) >= 5:
        common = {
            pair for pair in pairs
            if sum(1 for other in corpus if other and " ".join(pair) in " ".join(_words(other))) / len(corpus) > 0.2
        }
        pairs -= common
    return pairs


def _paragraphs(text: str) -> list[str]:
    return [p for p in storefront_text.normalize_paragraphs(text).split("\n\n") if p.strip()]


def _sentences(text: str) -> list[str]:
    return [s for p in _paragraphs(text) for s in _SENTENCE_SPLIT.split(p) if s.strip()]


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _ngrams(words: Sequence[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def hard_gates(
    content: Mapping[str, Any],
    product: Mapping[str, Any],
    language: str,
    source_texts: Sequence[str],
    evidence_items: Sequence[Mapping[str, Any]] | None,
    enforce_originality: bool = True,
) -> list[str]:
    """Non-negotiable failures: the existing approval validators plus, for
    the canonical Vietnamese text, the evidence-support check."""
    failures: list[str] = []
    for decision in (
        content_rules.evaluate_internal_boilerplate(content),
        content_rules.evaluate_storefront_text_quality(content),
        content_rules.evaluate_description_substance(content) if language == "vi" else None,
        content_rules.evaluate_description_quality(
            content, product, source_texts=list(source_texts), enforce_originality=enforce_originality
        ),
    ):
        if decision is not None and not decision.is_auto_pass:
            failures.append(f"[{decision.rule_code}] {decision.reason}")
    if language == "vi":
        name_check = content_rules.evaluate_name_consistency(content, product)
        if not name_check.is_auto_pass:
            failures.append(f"[{name_check.rule_code}] {name_check.reason}")
        if evidence_items is not None:
            prose = f"{content.get('short_description') or ''}\n{content.get('long_description') or ''}"
            for code, tokens in evidence.find_unsupported_statements(
                prose, evidence_items, product, source_texts
            ).items():
                failures.append(f"[{code}] {', '.join(tokens)}")
    return failures


def score_content(
    content: Mapping[str, Any],
    product: Mapping[str, Any],
    language: str = "vi",
    source_texts: Sequence[str] = (),
    evidence_items: Sequence[Mapping[str, Any]] | None = None,
    sibling_texts: Sequence[str] = (),
    enforce_originality: bool = True,
    reference_specificity: float | None = None,
) -> dict[str, Any]:
    """The explainable score of one content row (any language).
    reference_specificity (0..1, from the Vietnamese row's result
    "specificity_ratio") is used for EN/DE, which have no same-language
    sources to compare with."""
    long_description = str(content.get("long_description") or "")
    short_description = str(content.get("short_description") or "")
    prose = f"{short_description}\n\n{long_description}"
    title = str(content.get("product_name") or product.get("title") or "")
    identity_words = f"{title} {product.get('author') or ''} {product.get('publisher') or ''}"
    defects: list[str] = []
    dims: dict[str, dict[str, Any]] = {}
    sentences = _sentences(long_description)
    paragraphs = _paragraphs(long_description)
    lowered = long_description.lower()

    # -- specificity and usefulness (30) --------------------------------
    notes: list[str] = []
    narrative = [
        item for item in (evidence_items or [])
        if item.get("kind") in evidence.SUPPORTING_KINDS and item.get("source_type") != evidence.PRODUCT_METADATA
    ]
    if reference_specificity is not None and language != "vi":
        # A localization is as specific as its Vietnamese semantic reference.
        ratio = reference_specificity
        notes.append(f"inherits the Vietnamese reference specificity ({ratio:.0%})")
    elif narrative:
        folded = evidence.fold(prose)
        hits = [item for item in narrative if any(evidence.fold(a) in folded for a in item.get("anchors") or [])]
        ratio = len(hits) / min(len(narrative), 6)
        notes.append(f"{len(hits)}/{len(narrative)} narrative evidence items used")
    else:
        shared = _specific_phrases(prose, language, identity_words, sibling_texts) & _specific_phrases(
            "\n".join(source_texts), language, identity_words, sibling_texts
        )
        ratio = len(shared) / 10
        notes.append(f"{len(shared)} source-specific phrases used")
    ratio = min(1.0, ratio)
    specificity = 24 * ratio
    if ratio < 0.5:
        defects.append(LOW_SPECIFICITY)
    filler = lexicon.phrases_in(prose, lexicon.GENERIC_FILLER.get(language, ()))
    specificity += max(0, 6 - 3 * len(filler))
    if filler:
        defects.append(GENERIC_FILLER)
        notes.append(f"generic filler: {sorted(set(filler))}")
    dims["specificity"] = {"score": round(specificity, 1), "notes": notes, "ratio": round(ratio, 3)}

    # -- natural language and engagement (25) ----------------------------
    notes = []
    natural = 25.0
    first_sentence = sentences[0] if sentences else ""
    if lexicon.METADATA_OPENING_PATTERNS[language].search(first_sentence):
        natural -= 7
        defects.append(METADATA_OPENING)
        notes.append("opening restates author/publisher metadata")
    cliches = lexicon.phrases_in(lowered, lexicon.CLOSING_CLICHES.get(language, ()))
    if cliches:
        natural -= 5
        defects.append(CLOSING_CLICHE)
        notes.append(f"stock closing: {sorted(set(cliches))}")
    sales = [m.group(0) for m in content_rules._SALES_PHRASE_RE.finditer(prose)]
    if len(sales) > 1:
        natural -= 3 * (len(sales) - 1)
        defects.append(SALES_STACKING)
        notes.append(f"{len(sales)} sales phrases")
    openers = [" ".join(_words(s)[:2]) for s in sentences if len(_words(s)) >= 2]
    repeats = len(openers) - len(set(openers))
    if repeats:
        natural -= 3 * repeats
        defects.append(REPEATED_OPENERS)
        notes.append(f"{repeats} repeated sentence opener(s)")
    lengths = [len(_words(s)) for s in sentences]
    if len(lengths) >= 4 and statistics.pstdev(lengths) < 3:
        natural -= 3
        defects.append(MONOTONE_RHYTHM)
        notes.append("sentence lengths are monotone")
    dims["natural_language"] = {"score": max(0.0, natural), "notes": notes}

    # -- structure and readability (20) ----------------------------------
    notes = []
    structure = 0.0
    if 2 <= len(paragraphs) <= 5:
        structure += 8
    elif len(paragraphs) == 1 and len(long_description) <= 450:
        structure += 6
    else:
        structure += 3
        defects.append(PARAGRAPHING)
        notes.append(f"{len(paragraphs)} paragraph(s)")
    if paragraphs and all(len(p) <= 700 for p in paragraphs):
        structure += 6
    else:
        notes.append("a paragraph exceeds 700 characters")
    average = statistics.mean(lengths) if lengths else 0
    low, high = _SENTENCE_WORD_BANDS.get(language, (9, 32))
    if low <= average <= high:
        structure += 6
    else:
        structure += 2
        defects.append(SENTENCE_LENGTH)
        notes.append(f"average sentence length {average:.0f} words")
    dims["structure"] = {"score": structure, "notes": notes}

    # -- reader relevance (15) --------------------------------------------
    notes = []
    relevance = 0.0
    if any(cue in lowered for cue in lexicon.READER_CUES.get(language, ())):
        relevance += 9
    else:
        defects.append(NO_READER_RELEVANCE)
        notes.append("no sentence about the reader experience")
    closings = _SUITABILITY[language].findall(long_description)
    if len(closings) <= 1:
        relevance += 6
    else:
        defects.append(MULTIPLE_SUITABILITY_CLOSINGS)
        notes.append(f"{len(closings)} suitability phrases")
    benefits = lexicon.phrases_in(prose, lexicon.BENEFIT_CLAIMS.get(language, ()))
    if len(benefits) > 2:
        relevance = max(0.0, relevance - 4)
        defects.append(BENEFIT_STACKING)
        notes.append(f"{len(benefits)} benefit claims stacked")
    dims["reader_relevance"] = {"score": relevance, "notes": notes}

    # -- originality and non-repetition (10) -----------------------------
    notes = []
    originality = 0.0
    run = max((content_rules._longest_common_word_run(long_description, s) for s in source_texts if s), default=0)
    if run < 15:
        originality += 6
    elif run < 25:
        originality += 3
        defects.append(CLOSE_TO_SOURCE)
        notes.append(f"{run} consecutive words shared with a source")
    else:
        originality += 1
        defects.append(CLOSE_TO_SOURCE)
        notes.append(f"{run} consecutive words shared with a source")
    words = _words(long_description)
    # Repeating the title character's or author's name is not stylistic
    # repetition: skip phrases made mostly of title/identity words.
    identity = set(_words(identity_words))
    four_grams = [
        gram for gram in (tuple(words[i:i + 4]) for i in range(len(words) - 3))
        if sum(word in identity for word in gram) < 3
    ]
    if len(four_grams) == len(set(four_grams)):
        originality += 2
    else:
        defects.append(INTERNAL_REPETITION)
        notes.append("a 4-word phrase repeats inside the text")
    own = _ngrams(words, 5)
    overlap = max(
        (len(own & _ngrams(_words(other), 5)) / max(1, len(own)) for other in sibling_texts if other),
        default=0.0,
    )
    if overlap < 0.12:
        originality += 2
    else:
        defects.append(TEMPLATE_REUSE)
        notes.append(f"{overlap:.0%} of 5-word phrases shared with another product")
    dims["originality"] = {"score": originality, "notes": notes}

    total = round(sum(d["score"] for d in dims.values()), 1)
    gates = hard_gates(content, product, language, source_texts, evidence_items, enforce_originality)
    for name, weight in DIMENSION_WEIGHTS.items():
        dims[name]["max"] = weight
    return {
        "score": total,
        "target": TARGET_SCORE,
        "dimensions": dims,
        "defects": sorted(set(defects)),
        "hard_gate_failures": gates,
        "passed": not gates and total >= TARGET_SCORE,
        "version": SCORE_VERSION,
        "lexicon": lexicon.LEXICON_VERSION,
    }
