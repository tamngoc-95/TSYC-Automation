"""Deterministic EN/DE translation-validation rules.

CLAUDE_AUTOMATION.md section 9: Vietnamese is the semantic source of
truth. An English or German product_contents row is a localization of the
already-APPROVED Vietnamese row -- never an independently written
description. This module decides, deterministically and without I/O,
whether a proposed translation stays inside the facts the approved
Vietnamese row (plus verified internal_product metadata) already states.

It is intentionally conservative: a false positive only routes that one
language to CONTENT_REVIEW_REQUIRED (a human looks at it), while a false
negative could ship an invented fact to a customer (CLAUDE.md 2.2 / 15.1,
CLAUDE_AUTOMATION.md 9.3).

Rule code:

    TRANSLATION_VALIDATION   AUTO_PASS / REVIEW_REQUIRED
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping

from src.domain.decisions import DecisionResult, Outcome
from src.domain.rules import content_rules

TRANSLATION_VALIDATION = "TRANSLATION_VALIDATION"

TRANSLATION_LANGUAGES = ("en", "de")

TRANSLATABLE_TEXT_FIELDS = (
    "product_name",
    "short_description",
    "long_description",
    "author_summary",
    "product_details",
    "seo_title",
    "seo_description",
)

REQUIRED_TRANSLATION_FIELDS = (
    "product_name",
    "short_description",
    "long_description",
)

_MIN_DESCRIPTION_LENGTH = 40

# long_description length relative to the Vietnamese source. A localized
# description far longer than its source almost always carries added
# (invented) plot/benefit prose; one far shorter is a truncated stub.
_MIN_LENGTH_RATIO = 0.4
_MAX_LENGTH_RATIO = 1.8

# Letters that exist in Vietnamese orthography but not in English/German.
# A "translation" whose prose is still mostly Vietnamese was not localized.
_VIETNAMESE_SPECIFIC_LETTERS = frozenset(
    "ăâđêôơư"
    "ạảãàáằắẳẵặầấẩẫậẹẻẽèéềếểễệịỉĩìíọỏõòóồốổỗộờớởỡợụủũùúừứửữựỳýỷỹỵ"
)
_MAX_VIETNAMESE_LETTER_RATIO = 0.03

_FUNCTION_WORDS = {
    "en": re.compile(r"\b(?:the|and|is|with|for|of|this|a|an)\b", re.IGNORECASE),
    "de": re.compile(r"\b(?:der|die|das|und|ist|mit|für|ein|eine|dieses|diese)\b", re.IGNORECASE),
}

# Workflow language in the target languages, on top of the shared
# content_rules patterns (CLAUDE.md 15.1).
_TRANSLATION_WORKFLOW_PATTERNS = (
    re.compile(r"\b(?:needs?|requires?|pending|awaiting) (?:manager |human |further )?review\b", re.IGNORECASE),
    re.compile(r"\bto be (?:reviewed|translated|verified)\b", re.IGNORECASE),
    re.compile(r"\b(?:internal|workflow) note\b", re.IGNORECASE),
    re.compile(r"\btranslation (?:pending|missing|draft)\b", re.IGNORECASE),
    re.compile(r"\b(?:muss|soll|wird) (?:noch )?(?:geprüft|überprüft|ergänzt|übersetzt)", re.IGNORECASE),
    re.compile(r"\bspäter ergänzt\b", re.IGNORECASE),
    re.compile(r"\bPlatzhalter\b", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
    re.compile(r"\bTBD\b"),
    re.compile(r"\[(?:description|beschreibung|todo|text)[^\]]*\]", re.IGNORECASE),
)

# Fact categories CLAUDE_AUTOMATION.md 9.3 forbids inventing. A category
# is "claimed" when any target-language pattern matches; it is "supported"
# when the Vietnamese source text mentions the same concept or the named
# verified internal_product field is populated.
_FACT_CATEGORIES: tuple[tuple[str, tuple[str, ...], re.Pattern[str], tuple[str, ...]], ...] = (
    (
        "isbn",
        ("isbn",),
        re.compile(r"\bISBN\b", re.IGNORECASE),
        ("isbn",),
    ),
    (
        "author",
        ("tác giả", "tác phẩm của", "viết bởi"),
        re.compile(r"\b(?:author|written by|Autor(?:in)?|geschrieben von|verfasst von)\b", re.IGNORECASE),
        ("author",),
    ),
    (
        "publisher",
        ("nhà xuất bản", "nxb", "phát hành", "xuất bản"),
        re.compile(r"\b(?:publisher|published by|Verlag|herausgegeben|erschienen bei)\b", re.IGNORECASE),
        ("publisher",),
    ),
    (
        "page_count",
        ("trang",),
        re.compile(r"\b(?:pages?|Seiten?)\b", re.IGNORECASE),
        ("page_count",),
    ),
    (
        "dimensions",
        ("kích thước", "khổ", "cm"),
        re.compile(r"\b(?:cm|dimensions?|size|Format|Größe|Maße|Abmessungen)\b", re.IGNORECASE),
        ("length_cm", "width_cm", "height_cm"),
    ),
    (
        "weight",
        ("trọng lượng", "gram", "khối lượng"),
        re.compile(r"\b(?:weight|weighs|grams?|Gewicht|Gramm|kg)\b", re.IGNORECASE),
        ("weight_grams",),
    ),
    (
        "edition",
        ("tái bản", "phiên bản", "ấn bản", "bản đặc biệt", "bản giới hạn"),
        re.compile(r"\b(?:edition|reprint|Auflage|Ausgabe|Neuauflage)\b", re.IGNORECASE),
        (),
    ),
    (
        "age_recommendation",
        ("tuổi",),
        re.compile(
            r"\b(?:ages?\s+\d|aged\s+\d|years? old|year-olds?|toddlers?|preschool(?:ers)?|"
            r"Alter|Jahren|Jährige|ab \d+|Kleinkind(?:er)?|Vorschul\w*)\b",
            re.IGNORECASE,
        ),
        (),
    ),
    (
        "award",
        ("giải thưởng", "giải", "bán chạy", "best seller", "bestseller"),
        re.compile(r"\b(?:award\w*|prize\w*|best-?sell\w*|Preis(?:träger)?|Auszeichnung\w*|ausgezeichnet|Bestseller)\b", re.IGNORECASE),
        (),
    ),
    (
        "stock",
        ("còn hàng", "sẵn hàng", "có sẵn", "số lượng có hạn", "hết hàng"),
        re.compile(r"\b(?:in stock|limited stock|only \d+ left|auf Lager|vorrätig|nur noch|sofort lieferbar|begrenzt verfügbar)\b", re.IGNORECASE),
        (),
    ),
    (
        "shipping",
        ("giao hàng", "vận chuyển", "ship", "freeship", "miễn phí vận chuyển"),
        re.compile(r"\b(?:shipping|ships|delivery|delivered|Versand\w*|Lieferung|geliefert|versandkostenfrei)\b", re.IGNORECASE),
        (),
    ),
    (
        "educational_benefit",
        ("giúp", "phát triển", "kỹ năng", "giáo dục", "học", "rèn luyện", "nuôi dưỡng"),
        re.compile(
            r"\b(?:helps?|develop\w*|educational|learn\w*|skills?|fördert|Förderung|Entwicklung|"
            r"entwickeln|lernen|Fähigkeiten|pädagogisch)\b",
            re.IGNORECASE,
        ),
        (),
    ),
)

_COMBO_CANDIDATE_TYPES = frozenset({"BOOK_COMBO", "BOOK_SET"})
# "tập N" alone means "volume N" (an individual volume), so it is
# deliberately not a combo marker.
_VI_COMBO_MARKERS = ("combo", "trọn bộ", "bộ sách", "bộ ", "set ")

# The shop's own (Vietnamese) name legitimately appears in localized prose.
_SHOP_NAME = "tiệm sách yêu con"
_TARGET_COMBO_PATTERN = re.compile(
    r"\b(?:set|combo|bundle|volumes|collection|box|Set|Reihe|Bände|Band \d|Paket|Kombi\w*|Sammlung|Box)\b",
    re.IGNORECASE,
)

_NUMBER_PATTERN = re.compile(r"\d[\d.,]*\d|\d")


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFC", str(text)).split()).casefold()


def _numbers(text: str) -> set[str]:
    """Every digit run in `text`, with thousands/decimal separators
    removed so '1.200' and '1,200' and '1200' compare equal."""
    return {re.sub(r"[.,]", "", match) for match in _NUMBER_PATTERN.findall(text)}


def _vietnamese_letter_ratio(text: str) -> float:
    letters = [char for char in text.casefold() if char.isalpha()]
    if not letters:
        return 0.0
    vietnamese = sum(1 for char in letters if char in _VIETNAMESE_SPECIFIC_LETTERS)
    return vietnamese / len(letters)


def source_text(vi_content: Mapping[str, Any]) -> str:
    """Concatenate every text field of the APPROVED Vietnamese row."""
    return "\n".join(
        str(vi_content.get(field) or "") for field in TRANSLATABLE_TEXT_FIELDS
    )


def verified_fact_values(product: Mapping[str, Any]) -> dict[str, Any]:
    """Verified internal_product metadata a translation may restate."""
    keys = (
        "title",
        "author",
        "publisher",
        "isbn",
        "page_count",
        "weight_grams",
        "length_cm",
        "width_cm",
        "height_cm",
    )
    return {key: product.get(key) for key in keys if product.get(key) not in (None, "")}


def facts_used(
    vi_content: Mapping[str, Any],
    product: Mapping[str, Any],
) -> list[str]:
    """Names of the verified facts a translation of this row may rely on
    (CLAUDE_AUTOMATION.md 9.2 facts_used)."""
    used = [f"vi_content.{field}" for field in TRANSLATABLE_TEXT_FIELDS if vi_content.get(field)]
    used.extend(f"internal_product.{key}" for key in verified_fact_values(product))
    return used


def is_combo(
    vi_content: Mapping[str, Any],
    candidate_type: str | None,
) -> bool:
    if candidate_type in _COMBO_CANDIDATE_TYPES:
        return True
    name = _normalize(vi_content.get("product_name"))
    return any(marker in f"{name} " for marker in _VI_COMBO_MARKERS)


def evaluate_translation(
    *,
    language: str,
    translation: Mapping[str, Any],
    vi_content: Mapping[str, Any],
    product: Mapping[str, Any],
    candidate_type: str | None = None,
) -> DecisionResult:
    """
    Validate one proposed EN/DE translation against the APPROVED
    Vietnamese row it localizes. Returns AUTO_PASS only when every check
    passes; otherwise REVIEW_REQUIRED with every failing reason listed in
    evidence["failures"] (never just the first).

    Checks:
      1. language is en/de; vi source is APPROVED and not review_required.
      2. required fields present; descriptions not placeholder-short.
      3. product_name preserves the verified Vietnamese product name
         (identity is never renamed away by localization).
      4. no internal workflow language (shared content_rules patterns plus
         EN/DE equivalents).
      5. text is actually localized (not still Vietnamese) and reads as the
         target language.
      6. no number absent from the Vietnamese source / verified metadata
         (catches invented ISBN, page count, dimensions, weight, age,
         year, price, stock counts).
      7. no fact category (ISBN, author, publisher, page count,
         dimensions, weight, edition, age, awards, stock, shipping,
         educational benefit) the Vietnamese source does not also state.
      8. same combo/individual-volume distinction as the source.
      9. long_description length within a bounded ratio of the source
         (added plot/benefit prose, or a truncated stub).
    """
    failures: list[str] = []

    if language not in TRANSLATION_LANGUAGES:
        failures.append(f"Unsupported translation language: {language!r}.")

    if vi_content.get("content_status") != "APPROVED" or vi_content.get("review_required") is not False:
        failures.append("Vietnamese source content is not APPROVED with review_required=false.")

    for field in REQUIRED_TRANSLATION_FIELDS:
        if not _normalize(translation.get(field)):
            failures.append(f"Required field {field!r} is empty.")

    for field in ("short_description", "long_description"):
        value = _normalize(translation.get(field))
        if value and len(value) < _MIN_DESCRIPTION_LENGTH:
            failures.append(f"Field {field!r} is placeholder-short ({len(value)} chars).")

    vi_name = _normalize(vi_content.get("product_name"))
    target_name = _normalize(translation.get("product_name"))
    if vi_name and target_name and vi_name not in target_name:
        failures.append(
            "product_name does not preserve the verified Vietnamese product name."
        )

    boilerplate = content_rules.evaluate_internal_boilerplate(translation)
    if not boilerplate.is_auto_pass:
        failures.append(boilerplate.reason)

    for field in content_rules.CUSTOMER_FACING_FIELDS:
        value = str(translation.get(field) or "")
        for pattern in _TRANSLATION_WORKFLOW_PATTERNS:
            match = pattern.search(value)
            if match:
                failures.append(f"Internal workflow language in {field!r}: {match.group(0)!r}.")
                break

    prose = "\n".join(
        str(translation.get(field) or "")
        for field in ("short_description", "long_description", "seo_description")
    )
    # Quoted Vietnamese titles/names are legitimate; strip every
    # occurrence of the verified product name before measuring.
    prose_without_names = _normalize(prose)
    for name in (
        vi_name,
        _normalize(product.get("title")),
        _normalize(product.get("author")),
        _normalize(product.get("publisher")),
        _SHOP_NAME,
    ):
        if name:
            prose_without_names = prose_without_names.replace(name, " ")
    ratio = _vietnamese_letter_ratio(prose_without_names)
    if ratio > _MAX_VIETNAMESE_LETTER_RATIO:
        failures.append(
            f"Text is not localized: Vietnamese-specific letter ratio {ratio:.1%}."
        )

    function_words = _FUNCTION_WORDS.get(language)
    if function_words is not None and prose.strip() and not function_words.search(prose):
        failures.append(f"Text does not read as {language!r}.")

    source = source_text(vi_content)
    source_normalized = _normalize(source)
    verified = verified_fact_values(product)
    allowed_numbers = _numbers(source) | _numbers(
        " ".join(str(value) for value in verified.values())
    )
    target_text = "\n".join(
        str(translation.get(field) or "") for field in TRANSLATABLE_TEXT_FIELDS
    )
    invented_numbers = sorted(_numbers(target_text) - allowed_numbers)
    if invented_numbers:
        failures.append(
            "Numbers not present in the Vietnamese source or verified metadata: "
            + ", ".join(invented_numbers)
            + "."
        )

    target_without_names = _normalize(target_text)
    for name in (vi_name, _normalize(product.get("title")), _SHOP_NAME):
        if name:
            target_without_names = target_without_names.replace(name, " ")

    for category, vi_markers, target_pattern, product_fields in _FACT_CATEGORIES:
        match = target_pattern.search(target_without_names)
        if not match:
            continue
        supported_by_source = any(marker in source_normalized for marker in vi_markers)
        supported_by_product = any(field in verified for field in product_fields)
        if not (supported_by_source or supported_by_product):
            failures.append(
                f"Unsupported {category} claim {match.group(0)!r}: not stated in the "
                "Vietnamese source or verified metadata."
            )

    source_is_combo = is_combo(vi_content, candidate_type)
    target_claims_combo = bool(_TARGET_COMBO_PATTERN.search(target_without_names))
    if source_is_combo and not target_claims_combo:
        failures.append("Source is a combo/set but the translation does not say so.")
    if not source_is_combo and target_claims_combo:
        failures.append("Source is an individual product but the translation describes a set/combo.")

    vi_long = _normalize(vi_content.get("long_description"))
    target_long = _normalize(translation.get("long_description"))
    if vi_long and target_long:
        length_ratio = len(target_long) / len(vi_long)
        if not (_MIN_LENGTH_RATIO <= length_ratio <= _MAX_LENGTH_RATIO):
            failures.append(
                f"long_description length ratio {length_ratio:.2f} outside "
                f"[{_MIN_LENGTH_RATIO}, {_MAX_LENGTH_RATIO}] of the Vietnamese source."
            )

    if failures:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=TRANSLATION_VALIDATION,
            reason="; ".join(failures),
            evidence={"language": language, "failures": tuple(failures)},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=TRANSLATION_VALIDATION,
        reason=f"{language} translation stays within the approved Vietnamese facts.",
        evidence={"language": language, "failures": ()},
    )
