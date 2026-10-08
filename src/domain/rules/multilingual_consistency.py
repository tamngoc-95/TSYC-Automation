"""Deterministic cross-language consistency validation (VI -> EN/DE).

CLAUDE_AUTOMATION.md section 9.1 step "cross-language consistency check":
after EN and DE are generated from the APPROVED Vietnamese package, this
module checks that the three versions state the same facts. It runs
BEFORE the per-language translation_rules.evaluate_translation() check
that prepare_product_content.py's existing APPROVE path already applies;
it does not replace it.

Pure function, no I/O. Returns PASS only when every check passes; every
failure is reported (never just the first) with a stable English reason
code:

    TRANSLATION_EMPTY_FIELD         required en/de field empty
    TRANSLATION_ADDED_FACT          a number/ISBN/year/age/page count/
                                    dimension/price token absent from VI
                                    and from verified metadata
    TRANSLATION_DROPPED_FACT        a number stated in VI missing from the
                                    translation (so EN and DE carry the
                                    same numeric facts as VI and as each
                                    other)
    TRANSLATION_TITLE_NOT_PRESERVED translated title does not contain the
                                    verified Vietnamese title
    TRANSLATION_NAME_NOT_PRESERVED  a verified title/author/publisher named
                                    in VI is missing from the translation
                                    (unless an explicit transliteration is
                                    supplied in the package)
    TRANSLATION_COMMERCE_LANGUAGE   price, stock, shipping, pre-order,
                                    delivery-date or "only one left"
                                    wording (CLAUDE.md 2.5/15.1,
                                    CLAUDE_AUTOMATION.md 9.3)
    TRANSLATION_WRONG_LANGUAGE      text does not read as the target
                                    language
    TRANSLATION_MIXED_LANGUAGE      one field concatenates several
                                    languages
    TRANSLATION_LENGTH_RATIO        length outside the documented bound vs
                                    the Vietnamese field
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.domain.rules import author_rules

PASS = "PASS"
FAIL = "FAIL"

RULE_CODE = "MULTILINGUAL_CONSISTENCY"

TRANSLATION_EMPTY_FIELD = "TRANSLATION_EMPTY_FIELD"
TRANSLATION_ADDED_FACT = "TRANSLATION_ADDED_FACT"
TRANSLATION_DROPPED_FACT = "TRANSLATION_DROPPED_FACT"
TRANSLATION_TITLE_NOT_PRESERVED = "TRANSLATION_TITLE_NOT_PRESERVED"
TRANSLATION_NAME_NOT_PRESERVED = "TRANSLATION_NAME_NOT_PRESERVED"
TRANSLATION_COMMERCE_LANGUAGE = "TRANSLATION_COMMERCE_LANGUAGE"
TRANSLATION_WRONG_LANGUAGE = "TRANSLATION_WRONG_LANGUAGE"
TRANSLATION_MIXED_LANGUAGE = "TRANSLATION_MIXED_LANGUAGE"
TRANSLATION_LENGTH_RATIO = "TRANSLATION_LENGTH_RATIO"

LANGUAGES = ("en", "de")

# Length bounds, translated/Vietnamese character count. The long
# description uses the same bound as translation_rules (0.4-1.8): far
# longer almost always means added prose, far shorter a truncated stub.
# Short descriptions are one or two sentences, where word-length
# differences between languages dominate, so their bound is wider.
LONG_DESCRIPTION_RATIO_BOUNDS = (0.4, 1.8)
SHORT_DESCRIPTION_RATIO_BOUNDS = (0.3, 2.5)

_TEXT_FIELDS = ("product_name", "short_description", "long_description")
_PROSE_FIELDS = ("short_description", "long_description")

_SHOP_NAME = "tiệm sách yêu con"

_NUMBER_PATTERN = re.compile(r"\d[\d.,]*\d|\d")

# Function words. Deliberately excludes words shared by both languages
# ("an", "in", "so", "die" is rare in English prose but kept German-only).
_STOPWORDS = {
    "en": frozenset(
        {"the", "and", "is", "are", "with", "for", "of", "this", "to", "its",
         "it", "by", "from", "that", "at", "a", "on", "as", "be", "has"}
    ),
    "de": frozenset(
        {"der", "die", "das", "und", "ist", "sind", "mit", "für", "ein",
         "eine", "einen", "dem", "den", "des", "sich", "auf", "von", "nicht",
         "bei", "zu", "im", "wie", "hat", "dieses", "diese", "über"}
    ),
}
_WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)

_VIETNAMESE_SPECIFIC_LETTERS = frozenset(
    "ăâđêôơư"
    "ạảãàáằắẳẵặầấẩẫậẹẻẽèéềếểễệịỉĩìíọỏõòóồốổỗộờớởỡợụủũùúừứửữựỳýỷỹỵ"
)
_VIETNAMESE_SENTENCE_RATIO = 0.08

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

_LANGUAGE_LABEL = re.compile(
    r"(?im)^\s*(?:EN|DE|VI|English|Englisch|Deutsch|German|Vietnamese|"
    r"Vietnamesisch|Tiếng Việt|Tiếng Anh|Tiếng Đức)\s*[:\-–—|]"
)

_COMMERCE_PATTERNS = (
    re.compile(
        r"\b(?:price[sd]?|pricing|costs?|discount\w*|on sale|sale price|"
        r"in stock|out of stock|stock|only (?:\d+|one|a few) left|last copy|"
        r"limited (?:stock|quantity)|ship(?:s|ping|ped)?|free shipping|"
        r"deliver(?:y|ed|s)?|dispatch\w*|pre-?orders?|order now|"
        r"business days?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        # "Kosten"/"Liefer..." nouns: see _CASE_SENSITIVE_COMMERCE.
        r"\b(?:Preis\w*|kostet|Rabatt\w*|Angebot\w*|auf Lager|vorrätig|"
        r"Lagerbestand|nur noch|letzte[sn]? Exemplar|Versand\w*|"
        r"versandkostenfrei|geliefert|liefern|lieferbar|vorbestell\w*|"
        r"Vorbestellung|Werktag\w*|sofort lieferbar)\b",
        re.IGNORECASE,
    ),
    re.compile(r"[€$£]|\b(?:EUR|USD|VND|VNĐ)\b|\d\s?đ\b", re.IGNORECASE),
)


# --- false-positive guards (2026-10-08 review of 14 flagged drafts) ------

# Idioms that use a commerce word in a non-commerce sense. Removed before
# the commerce patterns run; nothing else is exempted.
_COMMERCE_IDIOMS = (
    re.compile(r"\b(?:comes?|came) at a price\b|\ba price to pay\b", re.IGNORECASE),
    re.compile(r"\b(?:dashi|soup|vegetable|chicken|fish|bone|kelp) stock\b", re.IGNORECASE),
    # "haben ihren Preis" (= come at a price); a named award "Preis X",
    # "den Preis Psychologies-Fnac"
    re.compile(r"\b(?:ihren|seinen) Preis\b"),
    re.compile(r"\bPreis\s+(?=[A-ZÄÖÜ][\w-]*[A-Za-z])"),
    re.compile(r"\bauf Kosten\b"),
)
# German nouns are capitalized: "Kosten" (costs) / "Lieferung" (delivery)
# are commerce; the verbs "kosten" (to taste) and "liefert" (provides) are
# not. These two are matched case-sensitively.
_CASE_SENSITIVE_COMMERCE = re.compile(r"\b(?:Kosten|Liefer\w*)\b")

_QUOTED_SHORT_SPAN = re.compile(r"[“\"„«‚'‘][^“”\"„«»‚'‘’\n]{1,80}[”\"“»‘’']")
_MIN_UNCLASSIFIABLE_LENGTH = 120

_NUMBER_WORDS = {
    word: str(value)
    for value, words in enumerate(
        [
            ("zero", "null"), ("one", "first", "eins", "ein", "eine", "erste", "ersten"),
            ("two", "second", "zwei", "zweite"), ("three", "third", "drei", "dritte"),
            ("four", "fourth", "vier", "vierte"), ("five", "fifth", "fünf", "fünfte"),
            ("six", "sixth", "sechs", "sechste"), ("seven", "seventh", "sieben", "siebte"),
            ("eight", "eighth", "acht", "achte"), ("nine", "ninth", "neun", "neunte"),
            ("ten", "tenth", "zehn", "zehnte"), ("eleven", "elf"), ("twelve", "zwölf"),
        ]
    )
    for word in words
}
_PUBLISHER_CONTEXT = ("nxb {p}", "nhà xuất bản {p}", "do {p} phát hành", "do {p} xuất bản", "{p} phát hành")


def _strip_quoted(text: str) -> str:
    """Remove short quoted spans (titles, short quotes) -- a quoted
    Vietnamese title inside an English sentence is legitimate."""
    return _QUOTED_SHORT_SPAN.sub(" ", text)


def _commerce_match(value: str) -> re.Match[str] | None:
    for idiom in _COMMERCE_IDIOMS:
        value = idiom.sub(" ", value)
    for pattern in _COMMERCE_PATTERNS:
        match = pattern.search(value)
        if match:
            return match
    return _CASE_SENSITIVE_COMMERCE.search(value)


def _number_words(text: str) -> set[str]:
    return {_NUMBER_WORDS[word] for word in re.findall(r"[^\W\d_]+", _normalize(text)) if word in _NUMBER_WORDS}


def _dropped_numbers(vi_text: str, target_text: str) -> list[str]:
    """VI numbers absent from the target. A VI token whose separated parts
    ("8,9" = 8 or 9) all appear, or a small number written as a word
    ("five", "one of 4"), counts as carried over."""
    target = _numbers(target_text) | _number_words(target_text)
    dropped = []
    for raw in _NUMBER_PATTERN.findall(vi_text or ""):
        joined = re.sub(r"[.,]", "", raw)
        if joined in target:
            continue
        parts = [part for part in re.split(r"[.,]", raw) if part]
        if len(parts) > 1 and all(part in target for part in parts):
            continue
        dropped.append(joined)
    return sorted(set(dropped))


def _title_variants(title: str) -> list[str]:
    """Title plus the title without a trailing edition/parenthetical
    ("Tuổi Trẻ Đáng Giá Bao Nhiêu (Tái Bản 2021)")."""
    variants = [title]
    stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title).strip()
    if stripped and stripped != title:
        variants.append(stripped)
    return variants


def _required_names(vi_text_normalized: str, vi_title: str, verified_facts: Mapping[str, Any]) -> list[str]:
    """Names a translation must carry: the title and each author person
    named in VI, and the publisher only where VI names it as publisher
    ("NXB Thế Giới") -- "thế giới" is also the ordinary word "world"."""
    required: list[str] = []
    for title in (vi_title, _normalize(verified_facts.get("title"))):
        if title and any(variant in vi_text_normalized for variant in _title_variants(title)):
            required.append(title)
    author_field = verified_facts.get("author")
    people = [_normalize(name) for name in author_rules.split_person_names(author_field)] or [_normalize(author_field)]
    required.extend(person for person in people if person and person in vi_text_normalized)
    publisher = _normalize(verified_facts.get("publisher"))
    if publisher and any(form.format(p=publisher) in vi_text_normalized for form in _PUBLISHER_CONTEXT):
        required.append(publisher)
    return list(dict.fromkeys(required))


@dataclass(frozen=True)
class ConsistencyResult:
    status: str
    reason_codes: tuple[str, ...]
    failures: tuple[str, ...]
    per_language: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def language_passed(self, language: str) -> bool:
        return not self.per_language.get(language)


def _normalize(text: Any) -> str:
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFC", str(text)).split()).casefold()


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[.,]", "", match) for match in _NUMBER_PATTERN.findall(text or "")}


def _strip_names(text: str, names: list[str]) -> str:
    result = _normalize(text)
    for name in names:
        if name:
            result = result.replace(name, " ")
    return result


def _vietnamese_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char in _VIETNAMESE_SPECIFIC_LETTERS) / len(letters)


def classify_language(text: str) -> str | None:
    """'vi', 'en', 'de', or None when there is not enough signal. Callers
    strip verified names first -- a quoted Vietnamese title inside an
    English sentence is legitimate."""
    normalized = _normalize(text)
    if not normalized:
        return None
    if _vietnamese_ratio(normalized) > _VIETNAMESE_SENTENCE_RATIO:
        return "vi"
    words = _WORD_PATTERN.findall(normalized)
    scores = {
        language: sum(1 for word in words if word in stopwords)
        for language, stopwords in _STOPWORDS.items()
    }
    best = max(scores, key=lambda language: scores[language])
    others = [score for language, score in scores.items() if language != best]
    if scores[best] == 0 or scores[best] <= max(others):
        return None
    return best


def _field_languages(text: str, names: list[str]) -> set[str]:
    # Split before normalizing: _normalize() collapses the newlines that
    # separate a concatenated "VI paragraph / EN paragraph" field.
    return {
        language
        for sentence in _SENTENCE_SPLIT.split(unicodedata.normalize("NFC", text))
        if (language := classify_language(_strip_names(sentence, names))) is not None
    }


def evaluate_multilingual_consistency(
    *,
    vi: Mapping[str, Any],
    translations: Mapping[str, Mapping[str, Any]],
    verified_facts: Mapping[str, Any] | None = None,
    name_transliterations: Mapping[str, list[str]] | None = None,
) -> ConsistencyResult:
    """
    vi / translations[language]: dicts with product_name,
    short_description, long_description (product_contents column names).
    verified_facts: verified internal_product values a translation may
    restate (translation_rules.verified_fact_values()).
    """
    verified_facts = dict(verified_facts or {})
    transliterations = {
        _normalize(name): [_normalize(alt) for alt in alts]
        for name, alts in (name_transliterations or {}).items()
    }

    vi_title = _normalize(vi.get("product_name"))
    vi_text = "\n".join(str(vi.get(name) or "") for name in _TEXT_FIELDS)
    vi_text_normalized = _normalize(vi_text)

    name_candidates = [
        vi_title,
        _normalize(verified_facts.get("title")),
        _normalize(verified_facts.get("author")),
        _normalize(verified_facts.get("publisher")),
    ]
    names_in_vi = _required_names(vi_text_normalized, vi_title, verified_facts)
    strip_names = (
        [variant for name in name_candidates if name for variant in _title_variants(name)]
        + [_normalize(person) for person in author_rules.split_person_names(verified_facts.get("author"))]
        + [_SHOP_NAME]
        + [alt for alts in transliterations.values() for alt in alts]
    )
    # Longest first so a title containing the author is removed whole.
    strip_names.sort(key=len, reverse=True)

    vi_numbers = _numbers(vi_text)
    allowed_numbers = vi_numbers | _numbers(
        " ".join(str(value) for value in verified_facts.values())
    )

    failures: list[str] = []
    per_language: dict[str, list[str]] = {}

    def fail(language: str, code: str, detail: str) -> None:
        failures.append(f"[{language}] {code}: {detail}")
        per_language.setdefault(language, []).append(code)

    for language in LANGUAGES:
        translation = translations.get(language) or {}

        for name in _TEXT_FIELDS:
            if not _normalize(translation.get(name)):
                fail(language, TRANSLATION_EMPTY_FIELD, f"{name} is empty.")

        target_text = "\n".join(str(translation.get(name) or "") for name in _TEXT_FIELDS)
        target_normalized = _normalize(target_text)
        target_numbers = _numbers(target_text)

        added = sorted(target_numbers - allowed_numbers)
        if added:
            fail(language, TRANSLATION_ADDED_FACT, "numbers not in Vietnamese source or verified metadata: " + ", ".join(added) + ".")

        dropped = _dropped_numbers(vi_text, target_text)
        if dropped:
            fail(language, TRANSLATION_DROPPED_FACT, "Vietnamese numbers missing from translation: " + ", ".join(dropped) + ".")

        target_title = _normalize(translation.get("product_name"))
        if vi_title and target_title and vi_title not in target_title:
            fail(language, TRANSLATION_TITLE_NOT_PRESERVED, "translated title does not contain the verified Vietnamese title.")

        for name in names_in_vi:
            accepted = _title_variants(name) + transliterations.get(name, [])
            if not any(alt and alt in target_normalized for alt in accepted):
                fail(language, TRANSLATION_NAME_NOT_PRESERVED, f"verified name {name!r} is missing.")

        for name in _TEXT_FIELDS:
            value = str(translation.get(name) or "")
            match = _commerce_match(value)
            if match:
                fail(language, TRANSLATION_COMMERCE_LANGUAGE, f"{name} contains {match.group(0)!r}.")

        for name in _PROSE_FIELDS:
            value = str(translation.get(name) or "")
            if not value.strip():
                continue

            if _LANGUAGE_LABEL.search(value):
                fail(language, TRANSLATION_MIXED_LANGUAGE, f"{name} contains a per-language label.")
                continue

            unquoted = _strip_quoted(value)
            languages = _field_languages(unquoted, strip_names)
            stripped = _strip_names(unquoted, strip_names)
            detected = classify_language(stripped)
            if len(languages) > 1:
                fail(language, TRANSLATION_MIXED_LANGUAGE, f"{name} mixes languages: {', '.join(sorted(languages))}.")
            elif detected != language and (
                detected is not None or len(stripped.strip()) >= _MIN_UNCLASSIFIABLE_LENGTH
            ):
                # Too little signal (a short quoted line) is not proof of
                # the wrong language; a detected other language is.
                fail(language, TRANSLATION_WRONG_LANGUAGE, f"{name} does not read as {language!r}.")

        for name, bounds in (
            ("long_description", LONG_DESCRIPTION_RATIO_BOUNDS),
            ("short_description", SHORT_DESCRIPTION_RATIO_BOUNDS),
        ):
            source = _normalize(vi.get(name))
            target = _normalize(translation.get(name))
            if source and target:
                ratio = len(target) / len(source)
                if not bounds[0] <= ratio <= bounds[1]:
                    fail(language, TRANSLATION_LENGTH_RATIO, f"{name} length ratio {ratio:.2f} outside [{bounds[0]}, {bounds[1]}].")

    reason_codes = tuple(dict.fromkeys(code for codes in per_language.values() for code in codes))
    return ConsistencyResult(
        status=FAIL if failures else PASS,
        reason_codes=reason_codes,
        failures=tuple(failures),
        per_language={language: tuple(codes) for language, codes in per_language.items()},
    )


def evaluate_package(package: Any) -> ConsistencyResult:
    """Convenience wrapper for src.domain.content_package.ContentPackage."""
    return evaluate_multilingual_consistency(
        vi=package.vietnamese(),
        translations={language: package.translation(language) for language in LANGUAGES},
        verified_facts=package.verified_facts,
        name_transliterations=package.name_transliterations,
    )
