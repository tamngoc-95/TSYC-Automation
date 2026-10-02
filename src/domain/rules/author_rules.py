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
