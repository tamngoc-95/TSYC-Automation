"""Mandatory catalog identity check before every WooCommerce create (no I/O).

Production finding 2026-10-22: about 1,896 of 1,986 live products have no
SKU, so an exact-SKU search can never prove that a book is new. Historical
candidate FB-HIST-2026-IMG-004-CAN-0031 ("Blockchain - Khởi Nguồn Cho Một
Nền Kinh Tế Mới", Melanie Swan) was one create away from duplicating the
owner's published #1422.

This module decides, for one product about to be created, whether the book
is already represented anywhere TSYC can see:

  * every WooCommerce product in every status, trash included;
  * TSYC sync records, including owner-removed products (removal markers
    and legacy remote-loss records whose Woo ID was cleared);
  * other internal products, candidates and MATCH references;
  * Polylang translation groups, so linked VI/EN/DE copies count as ONE
    logical product, never as unrelated duplicates.

Comparison reuses src.domain.rules.catalog_identity_index (valid ISBN,
normalized title without edition suffix, volume, combo vs single, format,
author; owner names "Title – Author"). A publisher barcode (893…) is kept
separate from ISBN (CLAUDE.md 2.3): equal barcodes are a strong signal but
never an ISBN match.

Only NEW_CONFIRMED may be created. Everything else, including an incomplete
catalog read, stops that candidate with its evidence (fail closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from src.domain import woo_translation_group as wtg
from src.domain.rules import catalog_identity_index as idx
from src.domain.rules import identity_rules

CHECK_VERSION = "1.0.0"

EXISTING_PRODUCT = "EXISTING_PRODUCT"
LINKED_TRANSLATION = "LINKED_TRANSLATION"
NEW_CONFIRMED = "NEW_CONFIRMED"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
DISTINCT_EDITION = "DISTINCT_EDITION"
REMOTE_REMOVED = "REMOTE_REMOVED"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
CATALOG_UNAVAILABLE = "CATALOG_UNAVAILABLE"

CREATE_ALLOWED = frozenset({NEW_CONFIRMED})

# A confirmed existing or owner-removed product ends the candidate's create
# path (terminal, never recreated); every other block needs a person.
TERMINAL_CLASSIFICATIONS = frozenset({EXISTING_PRODUCT, REMOTE_REMOVED})

MARKER_KEY = "catalog_identity_check"


@dataclass(frozen=True)
class CatalogSnapshot:
    """Everything the check reads, loaded by the caller.

    complete=False (any page failed, totals disagree, a status was not
    read) makes the check fail closed."""

    woo_products: Sequence[Mapping[str, Any]]
    complete: bool
    incomplete_reason: str | None = None
    local_entries: Sequence[idx.IndexEntry] = ()
    removed_entries: Sequence[idx.IndexEntry] = ()
    translation_group_of: Mapping[int, int] = field(default_factory=dict)
    barcodes: Mapping[str, str] = field(default_factory=dict)  # entry ref -> barcode


@dataclass(frozen=True)
class CreateCheck:
    classification: str
    reason: str
    evidence: tuple[dict[str, Any], ...] = ()
    logical_products: tuple[tuple[int, ...], ...] = ()

    @property
    def create_allowed(self) -> bool:
        return self.classification in CREATE_ALLOWED

    def marker(self, *, product_code: str, checked_at: str, checked_by: str) -> dict[str, Any]:
        """The evidence record stored on internal_products.product_metadata."""
        return {
            "classification": self.classification,
            "reason": self.reason,
            "product_code": product_code,
            "evidence": list(self.evidence)[:20],
            "logical_products": [list(ids) for ids in self.logical_products],
            "terminal": self.classification in TERMINAL_CLASSIFICATIONS,
            "checked_at": checked_at,
            "checked_by": checked_by,
            "check_version": CHECK_VERSION,
        }


def woo_entries(products: Iterable[Mapping[str, Any]]) -> list[idx.IndexEntry]:
    return [
        idx.IndexEntry(
            origin=idx.ORIGIN_WOO,
            ref=str(product.get("id")),
            title=str(product.get("name") or ""),
            remote_removed=product.get("status") == "trash",
            status=product.get("status"),
            url=product.get("permalink"),
        )
        for product in products
    ]


def _logical_key(woo_id: int, group_of: Mapping[int, int]) -> tuple[str, int]:
    group = group_of.get(woo_id)
    return ("group", group) if group is not None else ("product", woo_id)


def check_before_create(
    listing: idx.Listing,
    *,
    product_code: str,
    snapshot: CatalogSnapshot,
    barcode: str | None = None,
    reviewed_distinct: frozenset[tuple[str, str, str]] = frozenset(),
) -> CreateCheck:
    """Classify one product that is about to be created.

    ``reviewed_distinct`` (see reviewed_distinct_keys) only ever turns a
    POSSIBLE_DUPLICATE whose every match was reviewed as a different book
    into NEW_CONFIRMED; certain, edition, ISBN and barcode matches are
    never affected."""
    check = _check_before_create(listing, product_code=product_code, snapshot=snapshot, barcode=barcode)
    return apply_distinct_review(check, reviewed_distinct)


def _check_before_create(
    listing: idx.Listing,
    *,
    product_code: str,
    snapshot: CatalogSnapshot,
    barcode: str | None,
) -> CreateCheck:
    if not snapshot.complete:
        return CreateCheck(
            CATALOG_UNAVAILABLE,
            f"The catalog could not be read completely ({snapshot.incomplete_reason}); failing closed.",
        )
    if not idx.folded_title_key(listing.title):
        return CreateCheck(IDENTITY_CONFLICT, "The product has no usable title; identity cannot be checked.")

    products_by_id = {int(p["id"]): p for p in snapshot.woo_products if p.get("id") is not None}

    own_sku = [p for p in snapshot.woo_products if str(p.get("sku") or "") == product_code]
    if own_sku:
        live = [p for p in own_sku if p.get("status") != "trash"]
        classification = EXISTING_PRODUCT if live else REMOTE_REMOVED
        return CreateCheck(
            classification,
            f"SKU {product_code} is already in the catalog (ids {[p.get('id') for p in own_sku]}).",
            tuple({"origin": idx.ORIGIN_WOO, "ref": str(p.get("id")), "status": p.get("status"),
                   "reason": "Same SKU."} for p in own_sku),
            _logical(own_sku, snapshot.translation_group_of),
        )

    entries = woo_entries(snapshot.woo_products) + list(snapshot.removed_entries) + list(snapshot.local_entries)

    conflict = _isbn_conflict(listing, entries)
    if conflict is not None:
        return conflict

    if barcode:
        same_barcode = [ref for ref, value in snapshot.barcodes.items() if value == barcode]
        if same_barcode:
            return CreateCheck(
                POSSIBLE_DUPLICATE,
                f"Publisher barcode {barcode} is already recorded for {same_barcode} (barcode, not ISBN).",
                tuple({"origin": "BARCODE", "ref": ref, "reason": "Equal publisher barcode."} for ref in same_barcode),
            )

    result = idx.classify_listing(listing, entries, facebook_posts=[])
    evidence = tuple(result.evidence())
    matched_woo = [
        products_by_id[int(m.entry.ref)] for m in result.matches
        if m.entry.origin == idx.ORIGIN_WOO and m.entry.ref.isdigit() and int(m.entry.ref) in products_by_id
    ]
    logical = _logical(matched_woo, snapshot.translation_group_of)

    if result.classification == idx.EXISTING_PRODUCT:
        certain_woo = [
            products_by_id[int(m.entry.ref)] for m in result.matches
            if m.strength == idx.MATCH_CERTAIN and m.entry.origin == idx.ORIGIN_WOO and not m.entry.remote_removed
            and m.entry.ref.isdigit() and int(m.entry.ref) in products_by_id
        ]
        languages = {wtg.product_language(p)[0] for p in certain_woo}
        if certain_woo and wtg.SOURCE_LANGUAGE not in languages:
            return CreateCheck(
                LINKED_TRANSLATION,
                "The book already exists as a translated copy "
                f"({sorted(str(p['id']) for p in certain_woo)}, languages {sorted(l or '?' for l in languages)}); "
                "a new VI product would need a translation-link decision.",
                evidence, logical)
        return CreateCheck(EXISTING_PRODUCT, result.reason, evidence, logical)

    mapping = {
        idx.REMOTE_REMOVED: REMOTE_REMOVED,
        idx.DISTINCT_EDITION: DISTINCT_EDITION,
        idx.POSSIBLE_DUPLICATE: POSSIBLE_DUPLICATE,
        idx.NEW_CONFIRMED: NEW_CONFIRMED,
    }
    classification = mapping.get(result.classification)
    if classification is None:
        # FACEBOOK_COVERAGE_UNKNOWN cannot occur with facebook_posts=[];
        # anything unexpected fails closed.
        return CreateCheck(IDENTITY_CONFLICT, f"Unexpected classification {result.classification}.", evidence, logical)
    return CreateCheck(classification, result.reason, evidence, logical)


def _logical(products: Sequence[Mapping[str, Any]], group_of: Mapping[int, int]) -> tuple[tuple[int, ...], ...]:
    """Matched Woo products collapsed into logical products: one Polylang
    translation group counts once."""
    buckets: dict[tuple[str, int], set[int]] = {}
    for product in products:
        pid = int(product["id"])
        key = _logical_key(pid, group_of)
        members = {other for other, group in group_of.items() if key[0] == "group" and group == key[1]} or {pid}
        buckets.setdefault(key, set()).update(members)
    return tuple(tuple(sorted(ids)) for ids in sorted(buckets.values(), key=min))


def _isbn_conflict(listing: idx.Listing, entries: Sequence[idx.IndexEntry]) -> CreateCheck | None:
    """Equal valid ISBN on a different title: the identity data disagree."""
    if not listing.isbn or not identity_rules.looks_like_valid_isbn(listing.isbn):
        return None
    isbn = identity_rules.normalize_isbn(listing.isbn)
    listing_key = idx.folded_title_key(listing.title)
    for entry in entries:
        if not entry.isbn or not identity_rules.looks_like_valid_isbn(entry.isbn):
            continue
        if identity_rules.normalize_isbn(entry.isbn) != isbn:
            continue
        entry_key = entry.title_key
        if entry_key and entry_key != listing_key and not idx._titles_contain(listing_key, entry_key):
            return CreateCheck(
                IDENTITY_CONFLICT,
                f"ISBN {isbn} is recorded for a different title ({entry.origin} {entry.ref}: {entry.title!r}).",
                ({"origin": entry.origin, "ref": entry.ref, "title": entry.title, "reason": "Same ISBN, other title."},),
            )
    return None


def blocking_marker(internal_product: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The persisted blocking check result of an internal product, if any."""
    metadata = (internal_product or {}).get("product_metadata")
    marker = metadata.get(MARKER_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(marker, Mapping) or marker.get("classification") in (None, NEW_CONFIRMED, CATALOG_UNAVAILABLE):
        return None
    if marker.get("product_code") != (internal_product or {}).get("product_code"):
        return None
    return marker


# Evidence-based resolution of a CATALOG_DUPLICATE_REVIEW
# (scripts/resolve_catalog_duplicate_review.py), 2026-10-23.
REVIEW_KEY = "catalog_identity_review"
REVIEW_DISTINCT = "DISTINCT"
REVIEW_SAME_AS_EXISTING = "SAME_AS_EXISTING"


def review_entry_key(origin: str, ref: str, title: str) -> tuple[str, str, str]:
    """A reviewed match is bound to its exact origin, ref AND folded title:
    if the other record is renamed, the review no longer applies."""
    return (str(origin), str(ref), idx.folded_title_key(title))


def reviewed_distinct_keys(internal_product: Mapping[str, Any] | None) -> frozenset[tuple[str, str, str]]:
    """The (origin, ref, title key) matches recorded as different books for
    this exact product code; empty without a valid DISTINCT review."""
    metadata = (internal_product or {}).get("product_metadata")
    review = metadata.get(REVIEW_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(review, Mapping) or review.get("decision") != REVIEW_DISTINCT:
        return frozenset()
    if review.get("product_code") != (internal_product or {}).get("product_code"):
        return frozenset()
    keys = set()
    for item in review.get("distinct_from") or ():
        if isinstance(item, Mapping) and item.get("origin") and item.get("ref") and item.get("title"):
            keys.add(review_entry_key(item["origin"], item["ref"], item["title"]))
    return frozenset(keys)


def apply_distinct_review(check: CreateCheck, reviewed: frozenset[tuple[str, str, str]]) -> CreateCheck:
    """NEW_CONFIRMED only when the check is POSSIBLE_DUPLICATE and EVERY
    piece of evidence is a POSSIBLE-strength match reviewed as distinct.
    Any unreviewed, renamed, certain, edition or barcode match keeps the
    block. The reviewed matches stay in the evidence."""
    if not reviewed or check.classification != POSSIBLE_DUPLICATE or not check.evidence:
        return check
    for item in check.evidence:
        if item.get("strength") != idx.MATCH_POSSIBLE:
            return check
        key = review_entry_key(item.get("origin") or "", item.get("ref") or "", item.get("title") or "")
        if key not in reviewed:
            return check
    return CreateCheck(
        NEW_CONFIRMED,
        f"Only possible matches remain and each was reviewed as a different book ({check.reason}).",
        tuple({**item, "reviewed_distinct": True} for item in check.evidence),
        check.logical_products,
    )


# Resolution of an uncertain historical create (recover_historical_woo_create.py).
RESOLUTION_KEY = "create_outcome_resolution"
RESOLVED_NOT_RECREATED_EXISTING = "NOT_RECREATED_EXISTING_PRODUCT"


def create_outcome_resolved(sync: Mapping[str, Any] | None, internal_product: Mapping[str, Any] | None) -> bool:
    """True only when a FAILED, ID-less sync carries a resolution record for
    its own SKU AND the product carries a terminal catalog marker. The
    original failure evidence stays on the row; nothing claims a create
    succeeded."""
    if not sync or sync.get("woocommerce_product_id"):
        return False
    payload = sync.get("response_payload")
    resolution = payload.get(RESOLUTION_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(resolution, Mapping) or resolution.get("state") != RESOLVED_NOT_RECREATED_EXISTING:
        return False
    if resolution.get("sku") != sync.get("product_sku"):
        return False
    marker = blocking_marker(internal_product)
    return bool(marker and marker.get("terminal") and marker.get("classification") == EXISTING_PRODUCT)


def legacy_removed_entries(syncs: Iterable[Mapping[str, Any]], titles: Mapping[str, str]) -> list[idx.IndexEntry]:
    """Owner-removed products whose sync row lost its Woo ID: the
    remote-loss recovery of 2026-09-19 cleared woocommerce_product_id but
    kept previous_woocommerce_product_id in response_payload."""
    entries = []
    for sync in syncs:
        payload = sync.get("response_payload") if isinstance(sync.get("response_payload"), Mapping) else {}
        loss = payload.get("remote_loss_confirmed") if isinstance(payload, Mapping) else None
        if sync.get("woocommerce_product_id") or not isinstance(loss, Mapping):
            continue
        previous = loss.get("previous_woocommerce_product_id")
        if not previous:
            continue
        entries.append(idx.IndexEntry(
            origin=idx.ORIGIN_WOO_SYNC,
            ref=f"{sync.get('product_sku')} (removed Woo #{previous})",
            title=titles.get(str(sync.get("internal_product_id"))) or str(sync.get("product_name") or ""),
            remote_removed=True,
            status="REMOTE_LOSS_CONFIRMED",
        ))
    return entries
