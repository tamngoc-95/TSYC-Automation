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
did, and it is not proof of identity).

Evidence, fail-closed:
  * the link authority is Polylang's own translation map, read through
    an authenticated mechanism proven on staging (PolylangLinkMap).
    Without it a group is at most UNVERIFIABLE: hreflang is printed for
    published pages only, can be cached, and is supplementary;
  * a member's book identity comes from the exact SKU or TSYC's own
    local record of that Woo ID, never from the link alone (a bulk-link
    match on slug or image is a link, not an identity);
  * ownership comes from TSYC's local creation record and its write
    baseline, not from `_tsyc_internal_product_id` alone: a metadata-copy
    hook (Flatsome Child) or a duplicate plugin can copy that meta onto a
    product TSYC never created.

Every plan is draft-only and
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

# Ownership of one group member (ownership_of).
OWN_TSYC_DRAFT_UNCHANGED = "TSYC_DRAFT_UNCHANGED"  # the only writable state
OWN_TSYC_DRAFT_EDITED = "TSYC_DRAFT_EDITED"        # TSYC-created, changed since TSYC's baseline
OWN_LINKED_TRANSLATION = "LINKED_TRANSLATION"      # a copy made inside WordPress
OWN_PUBLISHED = "PUBLISHED"                        # published: owner territory
OWN_UNKNOWN = "UNKNOWN"                            # cannot be proven either way
OWN_CONFLICT = "CONFLICT"                          # records and the product disagree

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
class PolylangLinkMap:
    """Polylang's own translations of one product ({lang: Woo ID}, what
    pll_get_post_translations returns), read through an authenticated,
    supported mechanism. Free Polylang has no such read over REST
    (verified 2026-10-22), so today nobody can supply one; a map from a
    mechanism not proven on staging is ignored."""

    source_id: int
    translations: Mapping[str, int]
    mechanism: str
    verified_on_staging: bool


@dataclass(frozen=True)
class TsycProductRecord:
    """TSYC's local record of a product it created itself (a sync row):
    the language, Woo ID and book, plus the baseline seen right after
    TSYC's last confirmed write (modified time and translated text)."""

    lang: str
    woo_id: int
    internal_product_id: str
    baseline_modified_gmt: str | None = None
    baseline_text: Mapping[str, str] | None = None


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
    Necessary, never sufficient: copies made inside WordPress carried no
    `_tsyc_*` meta on 2026-10-22, but a metadata-copy hook can copy it, so
    ownership is decided by ownership_of against TSYC's local records."""
    return str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY)) == str(internal_product_id)


def ownership_of(
    product: Mapping[str, Any],
    lang: str,
    *,
    internal_product_id: str,
    record: TsycProductRecord | None,
) -> str:
    """Who owns one group member. Only OWN_TSYC_DRAFT_UNCHANGED may be
    written: TSYC recorded creating exactly this Woo ID in this language
    for this book, the product still carries matching meta, is a draft,
    and nothing changed since TSYC's own last confirmed write."""
    pid = int(product.get("id") or 0)
    owner_meta = str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY) or "")
    lang_meta = str(wtg.meta_value(product, wtg.TSYC_LANGUAGE_META_KEY) or "").lower()
    if record is not None and (record.woo_id != pid or record.lang != lang
                               or record.internal_product_id != internal_product_id):
        return OWN_CONFLICT
    if owner_meta and owner_meta != internal_product_id:
        return OWN_CONFLICT
    if record is None:
        # TSYC meta on a product TSYC never recorded creating: copied meta
        # (theme hook, Duplicate Post) or an unreconciled TSYC create.
        if owner_meta:
            return OWN_CONFLICT
        return OWN_LINKED_TRANSLATION if product.get("status") == "draft" else OWN_PUBLISHED
    if not owner_meta:
        return OWN_UNKNOWN
    if lang != wtg.SOURCE_LANGUAGE and lang_meta != lang:
        return OWN_CONFLICT if lang_meta else OWN_UNKNOWN
    if product.get("status") != "draft":
        return OWN_PUBLISHED
    if record.baseline_modified_gmt is None or record.baseline_text is None:
        return OWN_UNKNOWN
    changed = str(product.get("date_modified_gmt") or "") != record.baseline_modified_gmt or any(
        str(product.get(f) or "") != str(record.baseline_text.get(f) or "") for f in wtg.TRANSLATED_FIELDS)
    return OWN_TSYC_DRAFT_EDITED if changed else OWN_TSYC_DRAFT_UNCHANGED


def _record_conflicts(
    records: Mapping[str, TsycProductRecord],
    products_by_id: Mapping[int, Mapping[str, Any]],
    sku_matches: Sequence[Mapping[str, Any]],
    internal_product_id: str,
) -> list[str]:
    """TSYC's records must be self-consistent and every product carrying
    TSYC's meta for this book must be one TSYC recorded."""
    reasons: list[str] = []
    recorded: dict[int, str] = {}
    for lang, record in records.items():
        if record.lang != lang:
            reasons.append(f"TSYC record filed under {lang} is for {record.lang}.")
        if record.internal_product_id != internal_product_id:
            reasons.append(f"TSYC {lang} record #{record.woo_id} belongs to another internal product.")
        if record.woo_id in recorded:
            reasons.append(f"TSYC records #{record.woo_id} for both {recorded[record.woo_id]} and {lang}.")
        recorded[record.woo_id] = lang
        product = products_by_id.get(record.woo_id)
        if product is None:
            reasons.append(f"TSYC recorded {lang} #{record.woo_id}, which was not read: reconcile first.")
            continue
        lang_meta = str(wtg.meta_value(product, wtg.TSYC_LANGUAGE_META_KEY) or "").lower()
        if lang_meta and lang_meta != lang:
            reasons.append(f"TSYC recorded #{record.woo_id} as {lang}, its meta says {lang_meta}.")
    candidates = list(products_by_id.values()) + list(sku_matches)
    for pid in sorted({int(p.get("id") or 0) for p in candidates if is_tsyc_owned(p, internal_product_id)}):
        if pid not in recorded:
            reasons.append(
                f"#{pid} carries TSYC's ownership meta for this book, but TSYC never recorded creating it "
                "(copied metadata, or an unreconciled TSYC create).")
    return reasons


def resolve_group(
    *,
    product_code: str,
    source_id: int,
    products_by_id: Mapping[int, Mapping[str, Any]],
    sku_matches: Sequence[Mapping[str, Any]],
    storefront_groups: Sequence[plg.CatalogGroup],
    tsyc_records: Mapping[str, TsycProductRecord],
    link_map: PolylangLinkMap | None = None,
) -> GroupResolution:
    """Translation group of TSYC's VI product. Every source must agree:
    TSYC's own records, the exact-SKU matches (copies keep the SKU), the
    storefront hreflang group (published members only, supplementary) and
    Polylang's translation map, the only link authority. Without that map
    the result is at most UNVERIFIABLE; links are never invented."""
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
    source_record = tsyc_records.get(wtg.SOURCE_LANGUAGE)
    if (source_record is None or source_record.woo_id != source_id
            or not is_tsyc_owned(source, source_record.internal_product_id)):
        resolution.status = SOURCE_INVALID
        resolution.reasons.append(f"#{source_id} is not TSYC's recorded VI product for {product_code}.")
        return resolution
    record_reasons = _record_conflicts(tsyc_records, products_by_id, sku_matches,
                                       source_record.internal_product_id)
    if record_reasons:
        resolution.reasons.extend(record_reasons)
        return resolution
    resolution.evidence.append(Evidence(
        "TSYC_RECORD", f"recorded {dict(sorted((l, r.woo_id) for l, r in tsyc_records.items()))}"))

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

    # Members are known for read-only reporting even when unproven.
    resolution.members = dict(sku_members)
    if link_map is None or not link_map.verified_on_staging:
        resolution.status = UNVERIFIABLE
        if link_map is not None:
            resolution.reasons.append(f"Link map from {link_map.mechanism!r} ignored: not proven on staging.")
        what = "that no translation exists" if len(sku_members) == 1 else "the translation links"
        resolution.reasons.append(
            f"No Polylang translation map from a staging-proven mechanism: {what} cannot be proven "
            "(hreflang and SKU are supplementary).")
        return resolution

    links = {str(lang).lower(): int(pid) for lang, pid in link_map.translations.items()}
    reasons = _link_map_conflicts(links, link_map, source_id, sku_members, storefront, tsyc_records, products_by_id)
    if reasons:
        resolution.reasons.extend(reasons)
        return resolution
    resolution.evidence.append(Evidence("POLYLANG_LINK", f"{link_map.mechanism}: {dict(sorted(links.items()))}"))
    resolution.members = dict(links)

    # Identity of each translation: the exact SKU or TSYC's own record.
    # A Polylang link alone may come from a bulk-link slug/image match.
    unproven: list[str] = []
    for lang, pid in sorted(links.items()):
        if lang == wtg.SOURCE_LANGUAGE:
            continue
        sku = str(products_by_id[pid].get("sku") or "")
        record = tsyc_records.get(lang)
        if sku == product_code or (record is not None and record.woo_id == pid):
            continue
        if sku:
            resolution.reasons.append(f"{lang} #{pid} is linked by Polylang but carries SKU {sku!r}: another book.")
        else:
            unproven.append(f"{lang} #{pid} is linked by Polylang but carries neither the SKU nor a TSYC record: "
                            "its book identity is unproven.")
    if resolution.reasons:
        return resolution
    if unproven:
        resolution.status = UNVERIFIABLE
        resolution.reasons.extend(unproven)
        return resolution
    resolution.status = SOURCE_ONLY if len(links) == 1 else RESOLVED
    return resolution


def _link_map_conflicts(
    links: Mapping[str, int],
    link_map: PolylangLinkMap,
    source_id: int,
    members: Mapping[str, int],
    storefront: plg.CatalogGroup | None,
    records: Mapping[str, TsycProductRecord],
    products_by_id: Mapping[int, Mapping[str, Any]],
) -> list[str]:
    """Where Polylang's map disagrees with the SKU, hreflang or TSYC's records."""
    reasons: list[str] = []
    if link_map.source_id != source_id or links.get(wtg.SOURCE_LANGUAGE) != source_id:
        return [f"The Polylang map is not that of VI #{source_id} (map of #{link_map.source_id}: {dict(links)})."]
    unsupported = set(links) - {wtg.SOURCE_LANGUAGE, *wtg.TRANSLATION_LANGUAGES}
    if unsupported:
        reasons.append(f"Polylang links unsupported languages {sorted(unsupported)}.")
    for lang, pid in sorted(links.items()):
        product = products_by_id.get(pid)
        if product is None:
            reasons.append(f"{lang} #{pid} is linked by Polylang but was not read.")
            continue
        shown = wtg.product_language(product)[0]
        if shown not in (None, lang):
            reasons.append(f"{lang} #{pid} is linked as {lang} but reads as {shown}.")
        if members.get(lang) not in (None, pid):
            reasons.append(f"{lang}: Polylang links #{pid}, SKU or hreflang say #{members[lang]}.")
    for lang, pid in sorted(members.items()):
        if lang not in links:
            reasons.append(f"{lang} #{pid} carries the SKU or a hreflang link, but Polylang does not link it.")
    for lang, record in sorted(records.items()):
        if links.get(lang) != record.woo_id:
            reasons.append(f"TSYC recorded {lang} #{record.woo_id}, Polylang links {links.get(lang)}.")
    published = {lang: pid for lang, pid in links.items()
                 if products_by_id.get(pid, {}).get("status") == "publish"}
    # Polylang prints hreflang only between published translations, so
    # every published linked member must show there (none if alone).
    shown_links = dict(storefront.members) if storefront is not None else {}
    for lang, pid in sorted(published.items()):
        if len(published) >= 2 and shown_links.get(lang) != pid:
            reasons.append(f"{lang} #{pid} is published and linked by Polylang, but hreflang shows "
                           f"{shown_links.get(lang)}.")
    return reasons


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
    tsyc_records: Mapping[str, TsycProductRecord],
    create_authorized: bool = False,
    link_mechanism: LinkMechanism | None = None,
    uncertain_languages: Sequence[str] = (),
) -> dict[str, str]:
    """Per-language action for one logical book. Idempotent: an existing
    draft whose text already matches is NO_OP. Only an unchanged
    TSYC-created draft is ever updated; uncertain ownership is REFUSE."""
    if not resolution.actionable:
        return {lang: REFUSE for lang in wtg.TRANSLATION_LANGUAGES}

    actions: dict[str, str] = {}
    for lang in wtg.TRANSLATION_LANGUAGES:
        content = approved_contents.get(lang)
        approved = content is not None and content.get("content_status") == "APPROVED"
        member_id = resolution.members.get(lang)
        member = products_by_id.get(member_id) if member_id is not None else None
        if lang in uncertain_languages:
            actions[lang] = RECONCILE_FIRST
        elif member is not None:
            owner = ownership_of(member, lang, internal_product_id=internal_product_id,
                                 record=tsyc_records.get(lang))
            if owner in (OWN_CONFLICT, OWN_UNKNOWN):
                actions[lang] = REFUSE
            elif owner == OWN_TSYC_DRAFT_UNCHANGED:
                if not approved:
                    actions[lang] = SKIP_NO_APPROVED_CONTENT
                else:
                    actions[lang] = NO_OP if _text_matches(member, build_text_payload(content, lang, render)) \
                        else UPDATE_TEXT_DRAFT
            elif (owner == OWN_TSYC_DRAFT_EDITED and approved
                  and _text_matches(member, build_text_payload(content, lang, render))):
                # E.g. TSYC's own write landed but its answer was lost:
                # nothing to write; reconciliation records the new baseline.
                actions[lang] = NO_OP
            else:
                actions[lang] = SKIP_OWNER_MAINTAINED
        elif lang in tsyc_records:
            # TSYC recorded a product the resolved group does not contain.
            actions[lang] = REFUSE
        elif not approved:
            actions[lang] = SKIP_NO_APPROVED_CONTENT
        elif not create_authorized:
            actions[lang] = CREATE_NOT_AUTHORIZED
        elif link_mechanism is None or not link_mechanism.verified_on_staging:
            actions[lang] = CREATE_BLOCKED_NO_LINK_MECHANISM
        else:
            actions[lang] = CREATE_AND_LINK
    return actions


def _text_matches(product: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    return all(str(product.get(f) or "") == wanted[f] for f in wtg.TRANSLATED_FIELDS)


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
