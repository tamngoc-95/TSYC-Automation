"""Selling-price labels on historical shop images (evidence-based).

Production finding (2026-10-02 Fast Track Batch A pre-validation): most
historical Facebook-export shop photos carry a burned-in selling price
("5,99€", "24€/combo", "23,99"). A storefront PRIMARY image showing a
stale price contradicts the owner-set Woo price, so -- by the shop
owner's explicit Fast Track instruction of 2026-10-02 -- such an image
must not automatically become the PRIMARY (or a GALLERY) image when the
candidate is FB-HIST. It may stay registered internally; it is simply
not selected. Images are never edited/cropped here (no image-editing
policy exists).

Detection uses persisted evidence only -- this repository has no OCR or
vision dependency, so pixels are never inspected:

  1. source_evidence["price_label_visible"] (bool) -- an explicit,
     persisted visual-review verdict; it always wins when present.
  2. otherwise source_evidence["evidence_text"] -- the text the
     MANUAL_VISUAL_REVIEW importer recorded ("5,99€ visible").

Known gap (reported, not papered over): visual review did not always
write the price into evidence_text, so absence of a price mention is
"unknown", not "price-free". Callers treat unknown as price-free (the
pre-existing behavior) -- false positives must never silently reject a
product, and an unrecorded label cannot be detected deterministically.

Pure functions only.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

PRICE_LABEL_VISIBLE = "PRICE_LABEL_VISIBLE"
PRICE_LABEL_NOT_RECORDED = "PRICE_LABEL_NOT_RECORDED"
PRICE_LABEL_EXPLICITLY_ABSENT = "PRICE_LABEL_EXPLICITLY_ABSENT"

_FACEBOOK_EXPORT_MEDIA_PREFIX = "facebook-export-media://"

_PRICE_PATTERNS = (
    # 5,99€ / 9,99 € / 24€ / 15.99 EUR / 40 euro
    re.compile(r"\d{1,4}(?:[.,]\d{1,2})?\s?(?:€|eur\b|euro\b)", re.IGNORECASE),
    # €5,99 / € 24
    re.compile(r"€\s?\d"),
    # bare "23,99" -- two decimals after a comma, not part of a longer
    # number or a range ("4-1", "5-9 tuổi", "tập 1 & 2" never match).
    re.compile(r"(?<![\d.,/-])\d{1,3},\d{2}(?![\d.,])"),
)


def text_mentions_price(text: str | None) -> bool:
    """True when free text records a commercial price label."""
    if not text:
        return False
    return any(pattern.search(str(text)) for pattern in _PRICE_PATTERNS)


def candidate_price_label_status(source_evidence: Mapping[str, Any] | None) -> str:
    """PRICE_LABEL_VISIBLE / PRICE_LABEL_EXPLICITLY_ABSENT /
    PRICE_LABEL_NOT_RECORDED for one candidate's own shop image(s)."""
    evidence = source_evidence or {}
    explicit = evidence.get("price_label_visible")
    if explicit is True:
        return PRICE_LABEL_VISIBLE
    if explicit is False:
        return PRICE_LABEL_EXPLICITLY_ABSENT
    if text_mentions_price(evidence.get("evidence_text")):
        return PRICE_LABEL_VISIBLE
    return PRICE_LABEL_NOT_RECORDED


def is_own_facebook_export_image(
    image: Mapping[str, Any],
    source_evidence: Mapping[str, Any] | None,
) -> bool:
    """True when the image row is one of this candidate's own
    Facebook-export media files (the only images source_evidence's
    price verdict describes)."""
    source_url = str(image.get("source_url") or "")
    if not source_url.startswith(_FACEBOOK_EXPORT_MEDIA_PREFIX):
        return False
    media_path = source_url[len(_FACEBOOK_EXPORT_MEDIA_PREFIX):]
    local_paths = (source_evidence or {}).get("local_media_paths") or []
    return any(media_path == str(path) for path in local_paths)


def image_has_price_label(
    image: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """
    True when this image is the candidate's own shop photo AND the
    candidate's persisted evidence says a price label is visible. With
    several own photos and one candidate-level verdict, every own photo
    is treated as labelled (the evidence does not say which one) -- the
    conservative reading; a reference image is never affected.
    """
    source_evidence = candidate.get("source_evidence") or {}
    return is_own_facebook_export_image(image, source_evidence) and (
        candidate_price_label_status(source_evidence) == PRICE_LABEL_VISIBLE
    )
