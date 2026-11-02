"""What stands between one recorded VI book and automated EN/DE drafts (no I/O).

The dry-run view of the multilingual flow: approved content, then the VI draft
and its mapping, then the Polylang group. Reports the first blocker per book.
Nothing here generates content or writes anywhere, and a missing link map is
never read as "no translations exist".
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.domain import language_product_mapping as lpm
from src.domain import woo_translation_group as wtg

VI_NOT_READ = "VI_NOT_READ"                       # gone from the catalog read: the remote audit owns it
VI_PUBLISHED = "VI_PUBLISHED_OWNER_TERRITORY"     # the bridge refuses a published source
VI_NOT_PREORDER = "VI_NOT_PREORDER_SHAPE"         # stock 0, backorders yes, no price is required
TRANSLATIONS_NOT_APPROVED = "TRANSLATIONS_NOT_APPROVED"
LINK_READER_NOT_INSTALLED = "LINK_READER_NOT_INSTALLED"  # the only remaining blocker on a clean draft

# Decided in this order: the first that applies is the book's blocker.
BLOCKERS = (VI_NOT_READ, VI_PUBLISHED, TRANSLATIONS_NOT_APPROVED, VI_NOT_PREORDER, LINK_READER_NOT_INSTALLED)


@dataclass
class BookPlan:
    product_code: str
    vi_woo_id: int | None
    vi_status: str | None
    approved_languages: list[str] = field(default_factory=list)
    blocker: str = LINK_READER_NOT_INSTALLED

    def as_dict(self) -> dict[str, Any]:
        return {"product_code": self.product_code, "vi_woo_id": self.vi_woo_id, "vi_status": self.vi_status,
                "approved_languages": list(self.approved_languages), "blocker": self.blocker}


def is_preorder_shape(product: Mapping[str, Any]) -> bool:
    """The inventory shape the bridge requires of a source (and sets on a copy)."""
    return (product.get("manage_stock") is True and product.get("stock_quantity") == 0
            and product.get("backorders") == "yes" and not product.get("regular_price") and not product.get("sale_price"))


def approved_languages(contents: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({str(c.get("content_language") or "").lower() for c in contents
                   if c.get("content_status") == "APPROVED" and c.get("review_required") is False})


def plan_book(row: lpm.LanguageProduct, product: Mapping[str, Any] | None, contents: Sequence[Mapping[str, Any]]) -> BookPlan:
    approved = approved_languages(contents)
    plan = BookPlan(row.product_code, row.woocommerce_product_id, (product or {}).get("status"), approved)
    if product is None or product.get("status") == "trash":
        plan.blocker = VI_NOT_READ
    elif product.get("status") != "draft":
        plan.blocker = VI_PUBLISHED
    elif not set(wtg.TRANSLATION_LANGUAGES) <= set(approved):
        plan.blocker = TRANSLATIONS_NOT_APPROVED
    elif not is_preorder_shape(product):
        plan.blocker = VI_NOT_PREORDER
    return plan


def summarize(plans: Sequence[BookPlan]) -> dict[str, Any]:
    by_blocker = collections.Counter(p.blocker for p in plans)
    return {"books": len(plans), "by_blocker": {b: by_blocker.get(b, 0) for b in BLOCKERS},
            "all_languages_approved": sum(1 for p in plans if set(wtg.SUPPORTED_LANGUAGES) <= set(p.approved_languages)),
            "creates_possible_today": 0}
