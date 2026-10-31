"""EN/DE translation discovery for TSYC's recorded VI products (no I/O).

For each RECORDED VI mapping (woocommerce_language_products), this module
lists every existing EN/DE product that could be its translation and
classifies each one from read-only evidence:

  - exact-SKU copies (later copies keep the VI SKU as raw meta);
  - the storefront hreflang group (Polylang's own output, published pages
    only; src/domain/polylang_link_graph.py);
  - products carrying TSYC's ownership meta for the book (copied meta);
  - EN/DE products sharing the VI featured image (a lead only: the v2
    bulk-link plugin matches on it, which is not identity);
  - optionally a Polylang link map (multilingual_adapter.PolylangLinkMap).
    Only a map from a staging-proven mechanism counts.

Classes:

  VERIFIED_LINK      a staging-proven Polylang map links it, and the adapter
                     resolves the whole group (RESOLVED). This is the only
                     class that may become a mapping row. Today there is no
                     such map (free Polylang has no REST read of links).
  POTENTIAL_LINK     the published VI and this published copy list each other
                     in a consistent hreflang group, and the copy carries the
                     exact SKU. Strong, but hreflang is supplementary: it
                     cannot see drafts and is not Polylang's link table.
  OWNER_MAINTAINED   the copy carries the exact SKU, but both pages were read
                     and Polylang shows no link to the VI product. It is a
                     product the owner keeps outside TSYC's VI group. TSYC
                     never links or adopts it.
  UNVERIFIABLE       the link cannot be observed: the VI or the copy is not
                     published (no page), a page was unread, a hreflang link
                     without the SKU (bulk-link slug or image match), or a
                     shared featured image only.
  IDENTITY_CONFLICT  the evidence disagrees: copied TSYC meta, a non-EN/DE
                     product sharing the SKU, a linked copy with another SKU,
                     a conflicted hreflang group, SKU copies split over groups,
                     or two candidates for one language.

Nothing here infers a link from a SKU, title or image alone, and nothing is
written. A mapping row may be planned only for VERIFIED_LINK.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import polylang_link_graph as plg
from src.domain import woo_translation_group as wtg

VERIFIED_LINK = "VERIFIED_LINK"
POTENTIAL_LINK = "POTENTIAL_LINK"
OWNER_MAINTAINED = "OWNER_MAINTAINED"
UNVERIFIABLE = "UNVERIFIABLE"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
CLASSES = (VERIFIED_LINK, POTENTIAL_LINK, OWNER_MAINTAINED, UNVERIFIABLE, IDENTITY_CONFLICT)

# Evidence bases of one candidate.
BASIS_SKU = "SKU"
BASIS_HREFLANG = "HREFLANG"
BASIS_COPIED_META = "COPIED_META"
BASIS_IMAGE = "FEATURED_IMAGE"
BASIS_LINK_MAP = "POLYLANG_LINK_MAP"

# Book-level outcome when no candidate exists at all.
NO_CANDIDATE = "NO_CANDIDATE"


@dataclass
class Candidate:
    woo_id: int
    language: str | None
    status: str | None
    sku: str
    name: str
    bases: list[str] = field(default_factory=list)
    classification: str = UNVERIFIABLE
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"woo_id": self.woo_id, "language": self.language, "status": self.status, "sku": self.sku,
                "name": self.name, "bases": list(self.bases), "classification": self.classification,
                "reasons": list(self.reasons)}


@dataclass
class BookDiscovery:
    product_code: str
    internal_product_id: str
    vi_woo_id: int
    vi_status: str | None
    adapter_status: str
    adapter_reasons: list[str]
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def mapping_insert_allowed(self) -> dict[str, int]:
        """{language: Woo ID} a writer could record. VERIFIED_LINK only."""
        return {c.language: c.woo_id for c in self.candidates
                if c.classification == VERIFIED_LINK and c.language in wtg.TRANSLATION_LANGUAGES}

    @property
    def outcome(self) -> str:
        if not self.candidates:
            return NO_CANDIDATE
        for cls in (IDENTITY_CONFLICT, UNVERIFIABLE, OWNER_MAINTAINED, POTENTIAL_LINK, VERIFIED_LINK):
            if any(c.classification == cls for c in self.candidates):
                return cls
        return UNVERIFIABLE

    def as_dict(self) -> dict[str, Any]:
        return {"product_code": self.product_code, "internal_product_id": self.internal_product_id,
                "vi_woo_id": self.vi_woo_id, "vi_status": self.vi_status, "outcome": self.outcome,
                "adapter_status": self.adapter_status, "adapter_reasons": list(self.adapter_reasons),
                "mapping_insert_allowed": self.mapping_insert_allowed,
                "candidates": [c.as_dict() for c in self.candidates]}


def _group_of(groups: Sequence[plg.CatalogGroup], pid: int) -> plg.CatalogGroup | None:
    return next((g for g in groups if pid in g.members.values()), None)


def discover_book(
    row: lpm.LanguageProduct,
    catalog: Mapping[int, Mapping[str, Any]],
    groups: Sequence[plg.CatalogGroup],
    page_links: Mapping[int, Mapping[str, str] | None],
    link_map: ma.PolylangLinkMap | None = None,
) -> BookDiscovery:
    """Candidates and their classes for one RECORDED VI mapping."""
    vi_id = int(row.woocommerce_product_id or 0)
    vi = catalog.get(vi_id)
    code = row.product_code
    sku_matches = [p for p in catalog.values() if str(p.get("sku") or "") == code and p.get("status") != "trash"]
    inputs = lpm.adapter_inputs([row])
    resolution = ma.resolve_group(product_code=code, source_id=vi_id, products_by_id=catalog,
                                  sku_matches=sku_matches, storefront_groups=groups,
                                  tsyc_records=inputs.tsyc_records, link_map=link_map)
    book = BookDiscovery(code, row.internal_product_id, vi_id, (vi or {}).get("status"),
                         resolution.status, list(resolution.reasons))
    if vi is None:
        return book

    found: dict[int, Candidate] = {}

    def add(pid: int, basis: str) -> None:
        product = catalog.get(pid)
        if product is None or pid == vi_id:
            return
        if pid not in found:
            found[pid] = Candidate(pid, wtg.product_language(product)[0], product.get("status"),
                                   str(product.get("sku") or ""), str(product.get("name") or ""))
        if basis not in found[pid].bases:
            found[pid].bases.append(basis)

    for product in sku_matches:
        add(int(product["id"]), BASIS_SKU)
    vi_group = _group_of(groups, vi_id)
    copy_groups = {id(g): g for g in (_group_of(groups, int(p["id"])) for p in sku_matches) if g is not None}
    for group in ([vi_group] if vi_group else []) + list(copy_groups.values()):
        for pid in group.members.values():
            add(pid, BASIS_HREFLANG)
    for pid, product in catalog.items():
        if str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY) or "") == row.internal_product_id:
            add(pid, BASIS_COPIED_META)
    if link_map is not None:
        for pid in link_map.translations.values():
            add(int(pid), BASIS_LINK_MAP)
    vi_image = plg.image_id(vi)
    if vi_image is not None:
        for pid, product in catalog.items():
            if (pid not in found and product.get("status") != "trash"
                    and wtg.product_language(product)[0] in wtg.TRANSLATION_LANGUAGES
                    and plg.image_id(product) == vi_image):
                add(pid, BASIS_IMAGE)

    groups_with_copies = {id(g) for g in copy_groups.values()} | ({id(vi_group)} if vi_group else set())
    for candidate in found.values():
        _classify(candidate, row, vi, vi_group, groups, page_links, resolution, len(groups_with_copies))

    # Two strong candidates for one language contradict each other.
    strong = collections.Counter(c.language for c in found.values()
                                 if c.classification in (VERIFIED_LINK, POTENTIAL_LINK, OWNER_MAINTAINED))
    for candidate in found.values():
        if candidate.classification in (VERIFIED_LINK, POTENTIAL_LINK, OWNER_MAINTAINED) \
                and strong[candidate.language] > 1:
            candidate.classification = IDENTITY_CONFLICT
            candidate.reasons.append(f"More than one {candidate.language} candidate carries the SKU or a link.")
    book.candidates = sorted(found.values(), key=lambda c: (c.language or "~", c.woo_id))
    return book


def _classify(
    candidate: Candidate,
    row: lpm.LanguageProduct,
    vi: Mapping[str, Any],
    vi_group: plg.CatalogGroup | None,
    groups: Sequence[plg.CatalogGroup],
    page_links: Mapping[int, Mapping[str, str] | None],
    resolution: ma.GroupResolution,
    group_count: int,
) -> None:
    code = row.product_code
    reasons = candidate.reasons
    own_group = _group_of(groups, candidate.woo_id)
    linked_to_vi = vi_group is not None and own_group is vi_group

    def conflict(reason: str) -> None:
        candidate.classification = IDENTITY_CONFLICT
        reasons.append(reason)

    if BASIS_COPIED_META in candidate.bases:
        return conflict(f"#{candidate.woo_id} carries TSYC's ownership meta for this book but is not TSYC's "
                        "record (copied metadata).")
    if not {BASIS_SKU, BASIS_HREFLANG, BASIS_LINK_MAP} & set(candidate.bases):
        candidate.classification = UNVERIFIABLE
        reasons.append("Shares the VI featured image only: a lead, never identity.")
        return None
    if candidate.language not in wtg.TRANSLATION_LANGUAGES:
        if BASIS_SKU in candidate.bases or linked_to_vi:
            return conflict(f"#{candidate.woo_id} reads as {candidate.language!r}, not EN/DE, and shares the "
                            "SKU or the link group.")
        candidate.classification = UNVERIFIABLE
        reasons.append("Language cannot be shown from the permalink.")
        return None
    if own_group is not None and own_group.status != plg.GROUP_OK:
        return conflict("Its hreflang group is conflicted: " + "; ".join(f.code for f in own_group.findings))
    if group_count > 1:
        return conflict("The SKU copies fall into more than one hreflang group.")
    if candidate.sku and candidate.sku != code and (linked_to_vi or BASIS_LINK_MAP in candidate.bases):
        return conflict(f"Linked to the VI product but carries SKU {candidate.sku!r}: another book.")

    if resolution.status == ma.RESOLVED and resolution.members.get(candidate.language) == candidate.woo_id:
        candidate.classification = VERIFIED_LINK
        reasons.append("A staging-proven Polylang map, the SKU and hreflang agree.")
        return None
    if resolution.status == ma.CONFLICT and (BASIS_SKU in candidate.bases or linked_to_vi):
        return conflict("Adapter: " + "; ".join(resolution.reasons))

    if linked_to_vi and candidate.sku == code:
        candidate.classification = POTENTIAL_LINK
        reasons.append("Published VI and this copy list each other (hreflang) and share the SKU; no "
                       "authoritative Polylang map has confirmed the link.")
        return None
    if linked_to_vi:
        candidate.classification = UNVERIFIABLE
        reasons.append("Linked on the storefront but carries no SKU: the link may be a bulk-link slug or image "
                       "match, so its book identity is unproven.")
        return None
    vi_read = vi.get("status") == "publish" and page_links.get(int(vi["id"])) is not None
    copy_read = candidate.status == "publish" and page_links.get(candidate.woo_id) is not None
    if BASIS_SKU in candidate.bases and vi_read and copy_read:
        candidate.classification = OWNER_MAINTAINED
        reasons.append("Carries the SKU, but Polylang shows no link to the VI product on either page: an "
                       "owner copy outside TSYC's group. Never linked or adopted by TSYC.")
        return None
    candidate.classification = UNVERIFIABLE
    reasons.append("The VI or the copy has no readable public page (draft or unread), so a link cannot be "
                   "observed." if BASIS_SKU in candidate.bases else
                   "Linked only to the SKU copies' group, without the SKU: identity unproven.")
    return None


def summarize(books: Sequence[BookDiscovery]) -> dict[str, Any]:
    by_class = collections.Counter(c.classification for b in books for c in b.candidates)
    by_lang = collections.Counter(f"{c.language} {c.classification}" for b in books for c in b.candidates)
    return {
        "books": len(books),
        "books_by_outcome": dict(collections.Counter(b.outcome for b in books)),
        "adapter_status": dict(collections.Counter(b.adapter_status for b in books)),
        "candidates": sum(len(b.candidates) for b in books),
        "candidates_by_class": {cls: by_class.get(cls, 0) for cls in CLASSES},
        "candidates_by_language_class": dict(sorted(by_lang.items())),
        "mapping_inserts_allowed": sum(len(b.mapping_insert_allowed) for b in books),
    }
