"""Deterministic author-value validation and evidence-based resolution.

Production defect (2026-10-01 storefront audit): the publisher-page parser
stored the "Đọc thử" ("read a sample") button label as reference_author,
and it propagated to internal_products.author and the storefront.

  - is_invalid_author_value(): a value that is page chrome / a UI label,
    never a person. Used by the collector (never store it) and the repair.
  - resolve_author_from_references(): when a product's author is invalid,
    adopt the author of another registered reference ONLY when the
    evidence is deterministic (CLAUDE.md 2.2: never invent metadata):
      * the reference has registered provenance (source_url_id) and an
        approved source_type
      * its base title (edition suffix such as "(Tái Bản 2022)" removed --
        edition is not identity, CLAUDE.md 9.3) equals the product title
      * its publisher equals the product publisher after normalization
      * its author value is itself valid
      * every qualifying reference agrees on one author
    Anything else returns None -> the product is isolated for review.

Pure functions only.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from src.domain.identity_status import MatchDecision
from src.domain.reference_sources import SourceType
from src.domain.rules import storefront_text

# Approved catalogue sources only -- never a Facebook post or OTHER.
_AUTHOR_EVIDENCE_SOURCE_TYPES = frozenset(
    {
        SourceType.PUBLISHER,
        SourceType.AUTHORIZED_SUPPLIER,
        SourceType.BOOKSTORE,
        SourceType.FAHASA,
    }
)

# UI labels that publisher/bookstore pages render next to the author slot.
_UI_LABEL_AUTHOR_VALUES = frozenset(
    {
        "đọc thử",
        "xem thêm",
        "mua ngay",
        "thêm vào giỏ",
        "đang cập nhật",
        "updating",
    }
)

_EDITION_SUFFIX_RE = re.compile(r"\s*\((?:tái bản|tb|bản|phiên bản)[^)]*\)\s*$", re.IGNORECASE)


def _fold(value: str) -> str:
    """Lowercase, strip diacritics (đ -> d), keep alphanumerics only."""
    text = unicodedata.normalize("NFD", value.lower().replace("đ", "d"))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def is_invalid_author_value(value: str | None) -> bool:
    """True when a non-empty author value is page chrome, not a person."""
    if not value or not str(value).strip():
        return False
    text = " ".join(str(value).split())
    if text.lower() in _UI_LABEL_AUTHOR_VALUES:
        return True
    return bool(storefront_text.find_text_defects(text))


def base_title_key(title: str | None) -> str:
    """Comparable title with a trailing edition suffix removed."""
    if not title:
        return ""
    return _fold(_EDITION_SUFFIX_RE.sub("", str(title)))


def publisher_key(publisher: str | None) -> str:
    """Comparable publisher: drop the "NXB"/"Nhà xuất bản" prefix and
    collapse the common spellings of TP Hồ Chí Minh."""
    if not publisher:
        return ""
    key = _fold(str(publisher))
    key = re.sub(r"^(?:nxb|nha xuat ban)\s+", "", key)
    key = re.sub(r"\btp\s*ho chi minh\b|\btp\s*hcm\b|\btphcm\b|\bthanh pho ho chi minh\b", "tphcm", key)
    return " ".join(key.split())


# --- person-name parsing (shared by content consistency + discovery) ---
#
# Historical author fields carry several people with roles, e.g.
#   "Ji-Yoon Shin (tác giả); Ji-Hui Ha (minh họa); CMS Việt Nam (biên dịch)"
#   "Lời: Tạ Như; Tranh: Hình Bắc Ninh; Người dịch: Thùy Dương"
#   "ThS. Ngô Nam; Minh họa: Khánh Chi"
# split_person_names() returns only the names, never inventing one: a
# fragment that was itself cut off ("Phạm P...") is dropped, not guessed.

_PERSON_SEPARATOR_RE = re.compile(r"\s*(?:;|,|&|\s+và\s+|\s+and\s+|\s+und\s+)\s*", re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")
# A role label before a colon: "Lời:", "Tranh và lời:", "Người dịch:".
_ROLE_PREFIX_RE = re.compile(r"^[^:]{1,30}:\s*")
_HONORIFIC_RE = re.compile(
    r"^(?:ths|ts|pgs|gs|bs|dr|tiến sĩ|thạc sĩ|giáo sư|bác sĩ)\.?\s+",
    re.IGNORECASE,
)


def split_person_names(author_field: str | None) -> list[str]:
    """Names in an author field, roles/honorifics removed, order kept."""
    if not author_field:
        return []
    names: list[str] = []
    for segment in str(author_field).split(";"):
        segment = _ROLE_PREFIX_RE.sub("", _PARENTHETICAL_RE.sub(" ", segment).strip())
        for part in _PERSON_SEPARATOR_RE.split(segment):
            name = " ".join(_HONORIFIC_RE.sub("", part.strip()).split())
            if not name or "..." in name or "…" in name:
                continue
            if name not in names:
                names.append(name)
    return names


def person_name_key(name: str | None) -> str:
    """Comparable person name: case, diacritics, hyphens and dots folded
    ("Ji-Yoon Shin" == "Ji Yoon Shin", "Ngô Nam" == "Ngo Nam")."""
    return _fold(str(name or ""))


def _osa_distance(first: str, second: str) -> int:
    """Optimal-string-alignment edit distance (an adjacent transposition
    counts as one edit: "Hadfiled" -> "Hadfield" is 1)."""
    rows = len(first) + 1
    cols = len(second) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        table[i][0] = i
    for j in range(cols):
        table[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if first[i - 1] == second[j - 1] else 1
            table[i][j] = min(
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
                table[i - 1][j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and first[i - 1] == second[j - 2]
                and first[i - 2] == second[j - 1]
            ):
                table[i][j] = min(table[i][j], table[i - 2][j - 2] + 1)
    return table[-1][-1]


_WORD_RE = re.compile(r"\w+(?:-\w+)*")
_MIN_NEAR_MISS_TOKEN_LENGTH = 4


def find_name_near_misses(text: str | None, name: str) -> list[str]:
    """
    Occurrences in `text` that look like a misspelling of `name`: same
    number of words, every word identical (case-insensitive) except
    exactly one, and that one is a single-edit variant (OSA distance 1,
    e.g. a transposition or one wrong letter/diacritic) of at least
    _MIN_NEAR_MISS_TOKEN_LENGTH characters. Only multi-word names are
    checked -- a single word has too little context to call a near miss.
    """
    if not text:
        return []
    # NFC on both sides: a decomposed (NFD) diacritic must never look
    # like a one-letter misspelling of the same composed name.
    text = unicodedata.normalize("NFC", str(text))
    name_words = _WORD_RE.findall(unicodedata.normalize("NFC", name))
    if len(name_words) < 2:
        return []
    name_lower = [word.lower() for word in name_words]
    words = list(_WORD_RE.finditer(text))
    size = len(name_words)
    found: list[str] = []
    for start in range(len(words) - size + 1):
        window = words[start:start + size]
        window_lower = [match.group(0).lower() for match in window]
        differing = [i for i in range(size) if window_lower[i] != name_lower[i]]
        if len(differing) != 1:
            continue
        index = differing[0]
        if (
            len(name_lower[index]) >= _MIN_NEAR_MISS_TOKEN_LENGTH
            and _osa_distance(window_lower[index], name_lower[index]) == 1
        ):
            found.append(text[window[0].start():window[-1].end()])
    return found


def resolve_author_from_references(
    product: Mapping[str, Any],
    references: Sequence[Mapping[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    """(author, evidence) -- author is None unless deterministic."""
    title = base_title_key(product.get("title"))
    publisher = publisher_key(product.get("publisher"))
    evidence: dict[str, Any] = {"qualifying_reference_ids": [], "authors": []}

    if not title or not publisher:
        evidence["reason"] = "Product title or publisher missing; cannot corroborate."
        return None, evidence

    qualifying: list[Mapping[str, Any]] = []
    for reference in references:
        author = " ".join(str(reference.get("reference_author") or "").split())
        if (
            reference.get("source_url_id")
            and reference.get("source_type") in _AUTHOR_EVIDENCE_SOURCE_TYPES
            and reference.get("match_decision") not in {
                MatchDecision.NO_MATCH,
                MatchDecision.DIFFERENT_EDITION,
            }
            and author
            and not is_invalid_author_value(author)
            and base_title_key(reference.get("reference_title")) == title
            and publisher_key(reference.get("reference_publisher")) == publisher
        ):
            qualifying.append(reference)

    authors = sorted({" ".join(str(r["reference_author"]).split()) for r in qualifying})
    evidence["qualifying_reference_ids"] = [r.get("reference_id") for r in qualifying]
    evidence["authors"] = authors

    if len(authors) != 1:
        evidence["reason"] = (
            "No registered reference corroborates title+publisher with a valid author."
            if not authors
            else "Qualifying references disagree on the author."
        )
        return None, evidence

    evidence["reason"] = (
        "Base title and publisher match a registered reference with a valid author."
    )
    return authors[0], evidence
