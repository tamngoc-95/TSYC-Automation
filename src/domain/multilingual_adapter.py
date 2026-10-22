"""TSYC adapter for the store's existing multilingual architecture (no I/O).

Verified architecture (docs/POLYLANG_EXISTING_ARCHITECTURE.md):
  * free Polylang links VI/EN/DE product copies; every language has its
    own WooCommerce product ID, price field and stock field;
  * the links were made by the one-time pll-bulk-link-translations plugins
    (v1 slug root, v2 shared featured image then slug); both are still
    active and must stay;
  * Flatsome Child hooks copy metadata and stock between linked copies
    (developer statement; hook source not yet inspected);
  * free Polylang has no REST API for languages or links: a product
    created through wc/v3 gets the default language (vi) and no links.

One logical book is the VI product TSYC owns (its sync row's Woo ID and
SKU). This module resolves that book's translation group from independent
evidence, then plans what TSYC may do per language. It never infers a link
from a slug, title or image (that is exactly what the bulk-link plugins
did, and it is not proof of identity). Every plan is draft-only and
text-only: price, stock, backorders, SKU and status of an existing product
are never part of a TSYC payload, and the VI product stays the only price
and inventory authority.

Creating or linking a missing translation needs a link mechanism that has
been proven on an isolated staging copy. No such mechanism exists yet, so
those actions are planned as blocked, never improvised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from src.domain import polylang_link_graph as plg
from src.domain import woo_translation_group as wtg

# Group resolution.
RESOLVED = "RESOLVED"              # every member proven by agreeing evidence
SOURCE_ONLY = "SOURCE_ONLY"        # the VI product has no translation at all
UNVERIFIABLE = "UNVERIFIABLE"      # members exist but their link cannot be proven
CONFLICT = "CONFLICT"              # evidence disagrees; nobody may act
SOURCE_INVALID = "SOURCE_INVALID"  # the VI product is not TSYC's verified source

# Per-language plan.
NO_OP = "NO_OP"
UPDATE_TEXT_DRAFT = "UPDATE_TEXT_DRAFT"
SKIP_OWNER_MAINTAINED = "SKIP_OWNER_MAINTAINED"
SKIP_NO_APPROVED_CONTENT = "SKIP_NO_APPROVED_CONTENT"
CREATE_NOT_AUTHORIZED = "CREATE_NOT_AUTHORIZED"
CREATE_BLOCKED_NO_LINK_MECHANISM = "CREATE_BLOCKED_NO_LINK_MECHANISM"
CREATE_AND_LINK = "CREATE_AND_LINK"
RECONCILE_FIRST = "RECONCILE_FIRST"
REFUSE = "REFUSE"

# Never in any TSYC multilingual payload: price is owner-only (CLAUDE.md
# 2.5) and stock/backorders/SKU/status belong to the VI authority and the
# store's own synchronisation.
FORBIDDEN_PAYLOAD_FIELDS = wtg.FORBIDDEN_TRANSLATION_FIELDS | {"status", "price", "date_on_sale_from", "date_on_sale_to"}


class LinkMechanism(Protocol):
    """A way to create a translation and link it to its VI source.

    Implementations wrap something the live architecture really supports
    (e.g. a developer-provided, authenticated endpoint around
    pll_set_post_language / pll_save_post_translations). The adapter plans
    CREATE_AND_LINK only for a mechanism proven on staging."""

    name: str
    verified_on_staging: bool


@dataclass(frozen=True)
class Evidence:
    source: str
    detail: str


@dataclass
class GroupResolution:
    status: str
    source_id: int | None
    members: dict[str, int] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.status in (RESOLVED, SOURCE_ONLY)


def is_tsyc_owned(product: Mapping[str, Any], internal_product_id: str) -> bool:
    """The product carries TSYC's own ownership meta for this book.
    Copies made inside WordPress do not carry it (checked 2026-10-22)."""
    return str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY)) == str(internal_product_id)


def resolve_group(
    *,
    product_code: str,
    source_id: int,
    products_by_id: Mapping[int, Mapping[str, Any]],
    sku_matches: Sequence[Mapping[str, Any]],
    storefront_groups: Sequence[plg.CatalogGroup],
) -> GroupResolution:
    """Translation group of TSYC's VI product, from two independent
    sources that must agree: the exact-SKU matches (copies keep the SKU)
    and the storefront hreflang group (published members only)."""
    resolution = GroupResolution(status=CONFLICT, source_id=source_id)
    source = products_by_id.get(source_id)
    if source is None:
        resolution.status = SOURCE_INVALID
        resolution.reasons.append(f"VI product #{source_id} was not read.")
        return resolution
    if str(source.get("sku") or "") != product_code:
        resolution.status = SOURCE_INVALID
        resolution.reasons.append(f"#{source_id} carries SKU {source.get('sku')!r}, not {product_code}.")
        return resolution
    if wtg.product_language(source)[0] != wtg.SOURCE_LANGUAGE:
        resolution.status = SOURCE_INVALID
        resolution.reasons.append(f"#{source_id} is not a Vietnamese product.")
        return resolution

    sku_group = wtg.evaluate_translation_group(product_code, sku_matches)
    if not sku_group.ok:
        resolution.reasons.extend(f"SKU group: {i.code} {i.detail}" for i in sku_group.errors)
        return resolution
    if sku_group.source is None or int(sku_group.source["id"]) != source_id:
        resolution.reasons.append(
            f"The VI product carrying {product_code} is not #{source_id}; never reconcile onto another product.")
        return resolution
    sku_members = {lang: int(p["id"]) for lang, p in sku_group.members.items()}
    resolution.evidence.append(Evidence("SKU", f"exact-SKU members {sku_members}"))

    linked = [g for g in storefront_groups if set(g.members.values()) & set(sku_members.values())]
    if len(linked) > 1:
        resolution.reasons.append(f"SKU members fall into {len(linked)} different storefront groups.")
        return resolution
    storefront = linked[0] if linked else None
    if storefront is not None:
        if storefront.status != plg.GROUP_OK:
            resolution.reasons.append("Storefront group is conflicted: " + "; ".join(f.code for f in storefront.findings))
            return resolution
        resolution.evidence.append(Evidence("HREFLANG", f"storefront group {storefront.members}"))
        for lang, pid in storefront.members.items():
            if sku_members.get(lang) not in (None, pid):
                resolution.reasons.append(f"{lang}: SKU says #{sku_members[lang]}, storefront says #{pid}.")
            elif lang not in sku_members:
                # The hreflang link is Polylang's own data; a copy made
                # without the SKU (10 live groups, 2026-10-22) is still the
                # linked translation. A non-empty different SKU is not.
                other_sku = str(products_by_id.get(pid, {}).get("sku") or "")
                if other_sku:
                    resolution.reasons.append(
                        f"{lang} #{pid} is linked on the storefront but carries SKU {other_sku!r}.")
                else:
                    sku_members[lang] = pid
                    resolution.evidence.append(Evidence("HREFLANG_ONLY", f"{lang} #{pid} is linked but has no SKU"))
        if resolution.reasons:
            return resolution

    published = {lang: pid for lang, pid in sku_members.items()
                 if products_by_id.get(pid, {}).get("status") == "publish"}
    proven = set(storefront.members.values()) if storefront else set()
    unproven = {lang: pid for lang, pid in sku_members.items() if pid not in proven}

    resolution.members = dict(sku_members)
    if len(sku_members) == 1:
        resolution.status = SOURCE_ONLY
        return resolution
    if not unproven:
        resolution.status = RESOLVED
        return resolution
    # A draft member has no public page; a published member missing from
    # the storefront group is unlinked. Either way the link is unproven.
    resolution.status = UNVERIFIABLE
    for lang, pid in sorted(unproven.items()):
        state = products_by_id.get(pid, {}).get("status")
        why = "has no public page" if lang not in published else "is not in the storefront group"
        resolution.reasons.append(f"{lang} #{pid} ({state}) {why}: its translation link cannot be proven.")
    return resolution


def build_text_payload(content: Mapping[str, Any], lang: str, render: wtg.Renderer) -> dict[str, Any]:
    """Text-only update body for one existing translation draft."""
    if content.get("content_status") != "APPROVED" or content.get("review_required") is not False:
        raise ValueError(f"{lang} content is not APPROVED and final.")
    if str(content.get("content_language") or "").lower() != lang:
        raise ValueError(f"Content language {content.get('content_language')!r} is not {lang!r}.")
    name = str(content.get("product_name") or "").strip()
    if not name:
        raise ValueError(f"{lang} content has no product name.")
    description, short_description = render(content)
    payload = {"name": name, "description": description, "short_description": short_description}
    assert_text_only(payload)
    return payload


def assert_text_only(payload: Mapping[str, Any]) -> None:
    leaked = FORBIDDEN_PAYLOAD_FIELDS & set(payload)
    if leaked:
        raise ValueError(f"Multilingual payload carries forbidden fields: {sorted(leaked)}")


def plan_language_actions(
    resolution: GroupResolution,
    *,
    internal_product_id: str,
    products_by_id: Mapping[int, Mapping[str, Any]],
    approved_contents: Mapping[str, Mapping[str, Any]],
    render: wtg.Renderer,
    create_authorized: bool = False,
    link_mechanism: LinkMechanism | None = None,
    uncertain_languages: Sequence[str] = (),
) -> dict[str, str]:
    """Per-language action for one logical book. Idempotent: an existing
    draft whose text already matches is NO_OP."""
    if not resolution.actionable:
        return {lang: REFUSE for lang in wtg.TRANSLATION_LANGUAGES}

    actions: dict[str, str] = {}
    for lang in wtg.TRANSLATION_LANGUAGES:
        content = approved_contents.get(lang)
        member_id = resolution.members.get(lang)
        member = products_by_id.get(member_id) if member_id is not None else None
        if lang in uncertain_languages:
            actions[lang] = RECONCILE_FIRST
        elif member is not None:
            if member.get("status") != "draft" or not is_tsyc_owned(member, internal_product_id):
                actions[lang] = SKIP_OWNER_MAINTAINED
            elif content is None or content.get("content_status") != "APPROVED":
                actions[lang] = SKIP_NO_APPROVED_CONTENT
            else:
                wanted = build_text_payload(content, lang, render)
                same = all(str(member.get(f) or "") == wanted[f] for f in wtg.TRANSLATED_FIELDS)
                actions[lang] = NO_OP if same else UPDATE_TEXT_DRAFT
        elif content is None or content.get("content_status") != "APPROVED":
            actions[lang] = SKIP_NO_APPROVED_CONTENT
        elif not create_authorized:
            actions[lang] = CREATE_NOT_AUTHORIZED
        elif link_mechanism is None or not link_mechanism.verified_on_staging:
            actions[lang] = CREATE_BLOCKED_NO_LINK_MECHANISM
        else:
            actions[lang] = CREATE_AND_LINK
    return actions


def inventory_authority_findings(
    resolution: GroupResolution, products_by_id: Mapping[int, Mapping[str, Any]]
) -> list[str]:
    """Where a translation's price or stock differs from the VI authority.
    Reported, never corrected: changing a linked copy's stock or price
    would go through the store's own sync hooks, whose behaviour must be
    proven on staging first (and price is always owner-only)."""
    findings: list[str] = []
    source = products_by_id.get(resolution.source_id or -1)
    if source is None:
        return findings
    for lang, pid in resolution.members.items():
        if lang == wtg.SOURCE_LANGUAGE:
            continue
        member = products_by_id.get(pid, {})
        for field_name in ("regular_price", "sale_price", "manage_stock", "stock_quantity", "backorders"):
            if plg.sync_value(member, field_name) != plg.sync_value(source, field_name):
                findings.append(
                    f"{lang} #{pid} {field_name}={member.get(field_name)!r} differs from VI "
                    f"#{source['id']} ({source.get(field_name)!r}).")
    return findings
