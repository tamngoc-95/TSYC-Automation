"""Which multilingual workflow one logical book belongs to (no I/O).

Two independent workflows, decided from TSYC's own records plus one remote read:

  NEW_PREORDER_PRODUCT        a genuinely new Fahasa pre-order book whose VI
                              product is still TSYC's own unpriced draft. The
                              only workflow in which automation may create
                              EN/DE drafts, and only where a bridge is allowed.
  EXISTING_PUBLISHED_PRODUCT  the VI product is live or otherwise out of draft.
                              Owner territory: automation creates, links and
                              edits nothing (docs/POLYLANG_BRIDGE.md section 9,
                              owner item 1). It is handled separately and never
                              blocks the pre-order workflow.

A book in neither is OUT_OF_SCOPE (for example a removed product, which is
terminal and never recreated, or a draft that is not a pre-order).

`eligible` only says the records allow the pre-order workflow. It never
authorizes a write: the gates (src/domain/integration_gates.py), the bridge's
own guards and the owner's authorization decide that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.domain import language_product_mapping as lpm
from src.domain import translation_draft_plan as tdp
from src.domain import woo_translation_group as wtg
from src.domain.rules import preorder_inventory_rules as pir

NEW_PREORDER_PRODUCT = "NEW_PREORDER_PRODUCT"
EXISTING_PUBLISHED_PRODUCT = "EXISTING_PUBLISHED_PRODUCT"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
WORKFLOWS = (NEW_PREORDER_PRODUCT, EXISTING_PUBLISHED_PRODUCT, OUT_OF_SCOPE)

# Why a book is not (yet) eligible for the pre-order workflow.
OWNER_REMOVED_IDENTITY = "OWNER_REMOVED_IDENTITY"            # terminal
VI_MAPPING_NOT_RECORDED = "VI_MAPPING_NOT_RECORDED"
VI_NOT_READ = "VI_NOT_READ"                                  # a failed read is never "new"
EXISTING_PRODUCT_PROTECTED = "EXISTING_PRODUCT_PROTECTED"
NOT_PREORDER_CANDIDATE = "NOT_PREORDER_CANDIDATE"
VI_NOT_AUTOMATION_OWNED = "VI_NOT_AUTOMATION_OWNED"
VI_SKU_MISMATCH = "VI_SKU_MISMATCH"
OWNER_PRICED = "OWNER_PRICED"
VI_NOT_PREORDER_SHAPE = "VI_NOT_PREORDER_SHAPE"
CONTENT_NOT_APPROVED = "CONTENT_NOT_APPROVED"
CATALOG_IDENTITY_CONFLICT = "CATALOG_IDENTITY_CONFLICT"
CATALOG_CHECK_MISSING = "CATALOG_CHECK_MISSING"

# Statuses in which a product is live, scheduled or awaiting the owner.
OWNER_TERRITORY_STATUSES = frozenset({"publish", "private", "pending", "future"})


@dataclass(frozen=True)
class WorkflowDecision:
    product_code: str
    workflow: str
    blockers: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        """Records allow the pre-order EN/DE draft workflow. Not a write authorization."""
        return self.workflow == NEW_PREORDER_PRODUCT and not self.blockers

    @property
    def automation_may_write(self) -> bool:
        """False for every existing product, whatever else is true."""
        return self.eligible

    def as_dict(self) -> dict[str, Any]:
        return {"product_code": self.product_code, "workflow": self.workflow, "eligible": self.eligible,
                "blockers": list(self.blockers), "detail": dict(self.detail)}


def _meta(product: Mapping[str, Any], key: str) -> str | None:
    for item in product.get("meta_data") or []:
        if isinstance(item, Mapping) and item.get("key") == key:
            return str(item.get("value"))
    return None


def catalog_conflict_from_marker(internal_product: Mapping[str, Any] | None) -> bool | None:
    """The stored result of the mandatory catalog identity check
    (woo_catalog_identity): False only for a NEW_CONFIRMED marker of this
    exact product code, True for a blocking marker, None when no usable
    check is recorded."""
    from src.domain import woo_catalog_identity as wci  # local: keeps this module light to import

    if wci.blocking_marker(internal_product) is not None:
        return True
    metadata = (internal_product or {}).get("product_metadata")
    marker = metadata.get(wci.MARKER_KEY) if isinstance(metadata, Mapping) else None
    if (isinstance(marker, Mapping) and marker.get("classification") == wci.NEW_CONFIRMED
            and marker.get("product_code") == (internal_product or {}).get("product_code")):
        return False
    return None


def classify(
    row: lpm.LanguageProduct,
    product: Mapping[str, Any] | None,
    contents: Sequence[Mapping[str, Any]],
    *,
    candidate: Mapping[str, Any] | None,
    catalog_conflict: bool | None,
) -> WorkflowDecision:
    """
    row: the book's stored VI mapping row. product: its Woo product as read
    now (None when the read did not return it). contents: every
    product_contents row of the book. candidate: its product_candidates row.
    catalog_conflict: the mandatory catalog identity check's answer -- True a
    conflicting Woo/Facebook product exists, False none, None not checked
    (never read as "none").
    """
    code = row.product_code
    status = (product or {}).get("status")
    detail = {"vi_woo_id": row.woocommerce_product_id, "vi_status": status, "mapping_status": row.mapping_status}

    if row.mapping_status == lpm.REMOTE_REMOVED or status == "trash":
        return WorkflowDecision(code, OUT_OF_SCOPE, (OWNER_REMOVED_IDENTITY,), detail)
    if row.mapping_status != lpm.RECORDED or row.language_code != wtg.SOURCE_LANGUAGE:
        return WorkflowDecision(code, OUT_OF_SCOPE, (VI_MAPPING_NOT_RECORDED,), detail)
    if product is None:
        return WorkflowDecision(code, OUT_OF_SCOPE, (VI_NOT_READ,), detail)
    if status in OWNER_TERRITORY_STATUSES or status != "draft":
        return WorkflowDecision(code, EXISTING_PUBLISHED_PRODUCT, (EXISTING_PRODUCT_PROTECTED,), detail)
    if not pir.is_fahasa_preorder_candidate(candidate):
        return WorkflowDecision(code, OUT_OF_SCOPE, (NOT_PREORDER_CANDIDATE,), detail)

    blockers: list[str] = []
    if _meta(product, wtg.TSYC_OWNER_META_KEY) != str(row.internal_product_id):
        blockers.append(VI_NOT_AUTOMATION_OWNED)
    if str(product.get("sku") or "") != code:
        blockers.append(VI_SKU_MISMATCH)
    if any(str(product.get(name) or "").strip() for name in ("regular_price", "sale_price")):
        blockers.append(OWNER_PRICED)
    elif not tdp.is_preorder_shape(product):
        blockers.append(VI_NOT_PREORDER_SHAPE)
    missing = sorted(set(wtg.SUPPORTED_LANGUAGES) - set(tdp.approved_languages(contents)))
    if missing:
        blockers.append(CONTENT_NOT_APPROVED)
        detail["languages_not_approved"] = missing
    if catalog_conflict is None:
        blockers.append(CATALOG_CHECK_MISSING)
    elif catalog_conflict:
        blockers.append(CATALOG_IDENTITY_CONFLICT)
    return WorkflowDecision(code, NEW_PREORDER_PRODUCT, tuple(blockers), detail)


def summarize(decisions: Sequence[WorkflowDecision]) -> dict[str, Any]:
    by_workflow = {name: 0 for name in WORKFLOWS}
    blockers: dict[str, int] = {}
    for decision in decisions:
        by_workflow[decision.workflow] += 1
        for blocker in decision.blockers:
            blockers[blocker] = blockers.get(blocker, 0) + 1
    return {"by_workflow": by_workflow, "preorder_eligible": sum(1 for d in decisions if d.eligible),
            "blockers": dict(sorted(blockers.items()))}
