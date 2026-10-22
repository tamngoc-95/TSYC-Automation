"""Polylang translation groups of the whole WooCommerce catalog (no I/O).

The live store links VI/EN/DE product copies with free Polylang. Each
language has its own WooCommerce product ID. The links were made by the
pll-bulk-link-translations plugins (v1: slug root; v2: shared featured
image, then slug), and Flatsome Child hooks copy metadata and stock
between linked copies (developer statement, 2026-10-22; the hook source is
not readable over HTTP).

Free Polylang exposes no product links over REST, so the only read-only
evidence of a link is the storefront: each published product page prints
one `<link rel="alternate" hreflang>` per published translation, itself
included. This module turns those per-page link maps into translation
groups and reports every inconsistency. It never infers a link from a
slug, title or image: a product without hreflang evidence stays unlinked.

Groups are built only from pages that agree with each other. A page that
lists a different set than its siblings, a link to an unknown URL, a link
whose language disagrees with the target's permalink, or two members in
one language all make the group CONFLICTED, and nothing downstream may act
on it.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.domain import woo_translation_group as wtg

# Fields that are one value per logical book when the copies are in sync.
SYNC_FIELDS = (
    "regular_price",
    "sale_price",
    "manage_stock",
    "stock_quantity",
    "backorders",
    "stock_status",
    "sku",
    "image_id",
)

GROUP_OK = "OK"
GROUP_CONFLICTED = "CONFLICTED"

UNRESOLVED_LINK = "UNRESOLVED_LINK"
LANGUAGE_MISMATCH = "LINK_LANGUAGE_MISMATCH"
ASYMMETRIC_LINKS = "ASYMMETRIC_LINKS"
DUPLICATE_LANGUAGE = "DUPLICATE_LANGUAGE"
MISSING_LANGUAGE = "MISSING_LANGUAGE"
FIELD_DRIFT = "FIELD_DRIFT"
SKU_ON_SOME_MEMBERS = "SKU_ON_SOME_MEMBERS"
PAGE_UNREAD = "PAGE_UNREAD"


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str


@dataclass
class CatalogGroup:
    """One Polylang translation group as seen on the storefront."""

    members: dict[str, int]  # language -> Woo product id
    status: str = GROUP_OK
    findings: list[Finding] = field(default_factory=list)

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.members.values()))


def image_id(product: Mapping[str, Any]) -> int | None:
    images = product.get("images") or []
    if images and isinstance(images[0], Mapping) and images[0].get("id"):
        return int(images[0]["id"])
    return None


def sync_value(product: Mapping[str, Any], field_name: str) -> Any:
    if field_name == "image_id":
        return image_id(product)
    if field_name in ("regular_price", "sale_price"):
        return wtg.normalise_field_value(field_name, product.get(field_name))
    if field_name == "stock_quantity":
        return wtg.normalise_field_value(field_name, product.get(field_name))
    if field_name == "sku":
        return str(product.get("sku") or "")
    return product.get(field_name)


def build_groups(
    products: Sequence[Mapping[str, Any]],
    page_links: Mapping[int, Mapping[str, str] | None],
) -> tuple[list[CatalogGroup], list[int]]:
    """(groups, unlinked published product ids).

    page_links: {product id: parse_hreflang_links(page) or None when the
    page could not be read}. Only published products have pages."""
    by_id = {int(p["id"]): p for p in products}
    by_url = {
        wtg.normalise_url(str(p.get("permalink") or "")): int(p["id"])
        for p in products
        if p.get("status") == "publish" and p.get("permalink")
    }

    seen: set[int] = set()
    groups: list[CatalogGroup] = []
    unlinked: list[int] = []

    for pid in sorted(page_links):
        if pid in seen:
            continue
        links = page_links[pid]
        if links is None:
            continue
        if not links:
            unlinked.append(pid)
            seen.add(pid)
            continue

        # Members named by this page; then every member page must agree.
        findings: list[Finding] = []
        members: dict[str, int] = {}
        named_ids: set[int] = set()
        for lang, url in sorted(links.items()):
            target = by_url.get(url)
            if target is None:
                findings.append(Finding(UNRESOLVED_LINK, f"#{pid} links {lang} to {url}, which is no published product."))
                continue
            named_ids.add(target)
            target_lang, _ = wtg.product_language(by_id[target])
            if target_lang != lang:
                findings.append(Finding(
                    LANGUAGE_MISMATCH, f"#{pid} links {lang} to #{target}, whose permalink language is {target_lang!r}."))
            if lang in members and members[lang] != target:
                findings.append(Finding(DUPLICATE_LANGUAGE, f"{lang}: #{members[lang]} and #{target}."))
                continue
            members[lang] = target
        if pid not in named_ids:
            findings.append(Finding(ASYMMETRIC_LINKS, f"#{pid}'s own page does not list itself."))
        named_ids.add(pid)

        for member in sorted(named_ids - {pid}):
            other = page_links.get(member)
            if other is None:
                findings.append(Finding(PAGE_UNREAD, f"#{member}'s page could not be read."))
                continue
            other_ids = {by_url.get(url) for url in other.values()}
            if other_ids != named_ids:
                findings.append(Finding(
                    ASYMMETRIC_LINKS,
                    f"#{pid} lists {sorted(named_ids)}, #{member} lists {sorted(i for i in other_ids if i is not None)}."))

        group = CatalogGroup(members=members, findings=findings)
        if any(f.code in (UNRESOLVED_LINK, LANGUAGE_MISMATCH, ASYMMETRIC_LINKS, DUPLICATE_LANGUAGE, PAGE_UNREAD)
               for f in findings):
            group.status = GROUP_CONFLICTED
        seen.update(named_ids)
        groups.append(group)

    for group in groups:
        if group.status != GROUP_OK:
            continue
        evaluate_group_fields(group, by_id)
    return groups, unlinked


def evaluate_group_fields(group: CatalogGroup, by_id: Mapping[int, Mapping[str, Any]]) -> None:
    """Report missing languages and per-field drift inside one OK group."""
    for lang in wtg.SUPPORTED_LANGUAGES:
        if lang not in group.members:
            group.findings.append(Finding(MISSING_LANGUAGE, f"No published {lang} member."))
    if len(group.members) < 2:
        return
    for field_name in SYNC_FIELDS:
        values = {lang: sync_value(by_id[pid], field_name) for lang, pid in group.members.items()}
        if len(set(values.values())) <= 1:
            continue
        if field_name == "sku" and len({v for v in values.values() if v}) <= 1:
            group.findings.append(Finding(SKU_ON_SOME_MEMBERS, f"sku: {values}"))
        else:
            group.findings.append(Finding(FIELD_DRIFT, f"{field_name}: {values}"))


def sibling_save_coincidences(
    groups: Sequence[CatalogGroup], by_id: Mapping[int, Mapping[str, Any]]
) -> list[tuple[int, ...]]:
    """Groups in which two or more members carry the identical
    date_modified_gmt (to the second). One editor save rarely touches two
    posts in the same second, so this is behavioural evidence of a hook
    that writes to sibling translations on save. Evidence only, not proof."""
    hits = []
    for group in groups:
        stamps = collections.Counter(
            by_id[pid].get("date_modified_gmt") for pid in group.members.values() if by_id[pid].get("date_modified_gmt"))
        if any(count > 1 for count in stamps.values()):
            hits.append(group.ids)
    return hits


def summarize(
    groups: Sequence[CatalogGroup],
    unlinked: Sequence[int],
    products: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_id = {int(p["id"]): p for p in products}
    ok = [g for g in groups if g.status == GROUP_OK]
    drift = collections.Counter()
    in_sync = collections.Counter()
    for group in ok:
        if len(group.members) < 2:
            continue
        drifted = {f.detail.split(":", 1)[0] for f in group.findings if f.code == FIELD_DRIFT}
        for field_name in SYNC_FIELDS:
            (drift if field_name in drifted else in_sync)[field_name] += 1
    shapes = collections.Counter("+".join(sorted(g.members)) for g in ok)
    unlinked_by_lang = collections.Counter(wtg.product_language(by_id[pid])[0] or "unknown" for pid in unlinked)
    return {
        "groups": len(groups),
        "groups_ok": len(ok),
        "groups_conflicted": len(groups) - len(ok),
        "group_shapes": dict(shapes),
        "unlinked_published_by_language": dict(unlinked_by_lang),
        "field_in_sync_groups": dict(in_sync),
        "field_drift_groups": dict(drift),
        "sibling_save_coincidence_groups": len(sibling_save_coincidences(ok, by_id)),
    }
