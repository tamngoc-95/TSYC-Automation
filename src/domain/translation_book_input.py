"""Approved product_contents + the recorded VI mapping row -> one runner BookInput (no I/O).

The missing link between TSYC's records and src/services/translation_draft_runner.py:
until now only the lab pilots and tests built a BookInput, from synthetic data.
This module builds it from the real row shapes and refuses, with one exact code,
any book that is not unambiguously ready. It never picks between candidates,
never fills a missing field and never generates text: content comes only from
APPROVED rows, rendered by the same renderer the VI create path uses
(scripts/create_woocommerce_draft.build_description / build_short_description).

Refusal codes, decided in this order (the first that applies wins):

  VI_ROW_NOT_RECORDED              the VI mapping row is not a TSYC-created, RECORDED vi row with a Woo ID
  VI_BASELINE_MISSING              no create/write baseline: owner edits could not be detected
  VI_CONTENT_NOT_APPROVED          the canonical VI text is not APPROVED and final
  TRANSLATION_NOT_APPROVED         EN or DE has no APPROVED, final row
  AMBIGUOUS_APPROVED_CONTENT       more than one APPROVED row for one language: never choose
  CONTENT_ID_MISSING               an approved row has no product_content_id (no stable request ID)
  TRANSLATION_PREDATES_VI_APPROVAL VI was approved after the translation: it may describe older text
  CONTENT_NOT_RENDERABLE           the shared renderer or text-only payload refused the row

A ready book is still only an input: the adapter, the Polylang map and the
bridge decide whether anything is created, and production cannot be written.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import woo_translation_group as wtg

APPROVED = "APPROVED"

VI_ROW_NOT_RECORDED = "VI_ROW_NOT_RECORDED"
VI_BASELINE_MISSING = "VI_BASELINE_MISSING"
VI_CONTENT_NOT_APPROVED = "VI_CONTENT_NOT_APPROVED"
TRANSLATION_NOT_APPROVED = "TRANSLATION_NOT_APPROVED"
AMBIGUOUS_APPROVED_CONTENT = "AMBIGUOUS_APPROVED_CONTENT"
CONTENT_ID_MISSING = "CONTENT_ID_MISSING"
TRANSLATION_PREDATES_VI_APPROVAL = "TRANSLATION_PREDATES_VI_APPROVAL"
CONTENT_NOT_RENDERABLE = "CONTENT_NOT_RENDERABLE"

REFUSAL_CODES = (
    VI_ROW_NOT_RECORDED, VI_BASELINE_MISSING, VI_CONTENT_NOT_APPROVED, TRANSLATION_NOT_APPROVED,
    AMBIGUOUS_APPROVED_CONTENT, CONTENT_ID_MISSING, TRANSLATION_PREDATES_VI_APPROVAL, CONTENT_NOT_RENDERABLE,
)

# The columns this module reads; a loader selects exactly these.
CONTENT_COLUMNS = (
    "product_content_id", "internal_product_id", "content_language", "content_status", "review_required",
    "approved_at", "product_name", "short_description", "long_description", "author_summary", "product_details",
)


@dataclass(frozen=True)
class BookInputResult:
    """Either `contents` (lang -> approved row, en and de) or a refusal."""

    product_code: str
    internal_product_id: str
    vi_row: lpm.LanguageProduct
    contents: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    refusal: str | None = None
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.refusal is None

    def as_dict(self) -> dict[str, Any]:
        return {"product_code": self.product_code, "internal_product_id": self.internal_product_id,
                "vi_woo_id": self.vi_row.woocommerce_product_id, "ready": self.ready,
                "refusal": self.refusal, "detail": self.detail,
                "product_content_ids": {lang: str(c.get("product_content_id")) for lang, c in self.contents.items()}}


def _timestamp(value: Any) -> datetime | None:
    """An aware datetime, or None. Naive values are refused, never assumed UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def _is_final_approved(row: Mapping[str, Any]) -> bool:
    return row.get("content_status") == APPROVED and row.get("review_required") is False


def _approved_by_language(rows: Sequence[Mapping[str, Any]], internal_product_id: str) -> dict[str, list[Mapping[str, Any]]]:
    found: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if str(row.get("internal_product_id") or "") != internal_product_id or not _is_final_approved(row):
            continue
        found.setdefault(str(row.get("content_language") or "").strip().lower(), []).append(row)
    return found


def build_book_input(
    vi_row: lpm.LanguageProduct,
    content_rows: Sequence[Mapping[str, Any]],
    render: wtg.Renderer,
) -> BookInputResult:
    """One book's runner input, or the first reason it cannot have one."""
    book = vi_row.internal_product_id

    def refuse(code: str, detail: str) -> BookInputResult:
        return BookInputResult(vi_row.product_code, book, vi_row, refusal=code, detail=detail)

    if (vi_row.language_code != wtg.SOURCE_LANGUAGE or vi_row.mapping_status != lpm.RECORDED
            or vi_row.origin != lpm.ORIGIN_TSYC_CREATED or not vi_row.woocommerce_product_id):
        return refuse(VI_ROW_NOT_RECORDED, f"{vi_row.language_code}/{vi_row.mapping_status}/{vi_row.origin} "
                                           f"#{vi_row.woocommerce_product_id}")
    if not vi_row.baseline_modified_gmt or vi_row.baseline_text is None:
        return refuse(VI_BASELINE_MISSING, "The VI row has no recorded modified time and text baseline.")

    approved = _approved_by_language(content_rows, book)
    for lang in wtg.SUPPORTED_LANGUAGES:
        rows = approved.get(lang, [])
        if not rows:
            code = VI_CONTENT_NOT_APPROVED if lang == wtg.SOURCE_LANGUAGE else TRANSLATION_NOT_APPROVED
            return refuse(code, f"No APPROVED, final {lang} content.")
        if len(rows) > 1:
            return refuse(AMBIGUOUS_APPROVED_CONTENT, f"{len(rows)} APPROVED {lang} rows.")
        if not str(rows[0].get("product_content_id") or "").strip():
            return refuse(CONTENT_ID_MISSING, f"The APPROVED {lang} row has no product_content_id.")

    vi_approved_at = _timestamp(approved[wtg.SOURCE_LANGUAGE][0].get("approved_at"))
    contents: dict[str, Mapping[str, Any]] = {}
    for lang in wtg.TRANSLATION_LANGUAGES:
        row = approved[lang][0]
        approved_at = _timestamp(row.get("approved_at"))
        # A missing or unreadable time is not evidence of freshness.
        if approved_at is None or vi_approved_at is None or approved_at < vi_approved_at:
            return refuse(TRANSLATION_PREDATES_VI_APPROVAL,
                          f"{lang} approved {row.get('approved_at') or 'never'}, VI approved "
                          f"{approved[wtg.SOURCE_LANGUAGE][0].get('approved_at') or 'never'}.")
        try:
            ma.build_text_payload(row, lang, render)
        except ValueError as error:
            return refuse(CONTENT_NOT_RENDERABLE, f"{lang}: {error}")
        contents[lang] = row
    return BookInputResult(vi_row.product_code, book, vi_row, contents)


def summarize(results: Sequence[BookInputResult]) -> dict[str, Any]:
    counts = {code: 0 for code in REFUSAL_CODES}
    for result in results:
        if result.refusal:
            counts[result.refusal] += 1
    return {"books": len(results), "ready": sum(1 for r in results if r.ready), "by_refusal": counts}
