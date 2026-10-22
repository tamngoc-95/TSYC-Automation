"""One logical book = one WooCommerce translation group (VI source + EN/DE).

Background (read-only production checks, 2026-10-22): free Polylang 3.8.4
already translates the `product` post type. Production holds ~874 VI,
554 EN and 558 DE products, created as independent copies (no Polylang for
WooCommerce). Some TSYC SKUs exist two or three times (VI + EN + DE). Each
copy has its own price and stock. Those can drift, and an EN/DE copy can be
published and sellable while its VI source is still a draft.

This module models a translation group in both modes, so TSYC can audit
today's manual copies and later drive Polylang for WooCommerce
(docs/POLYLANG_WOOCOMMERCE_PLAN.md stage 5) with the same rules:

  * add-on mode: the product carries `lang` and `translations` REST fields
    (Polylang for WooCommerce). The link is explicit.
  * manual mode: no such fields. The language is inferred from the
    permalink prefix (`/en/`, `/de/`; VI is the unprefixed default). A
    shared SKU groups the copies; the storefront's hreflang links show
    whether Polylang also links them (evaluate_storefront_links).

Nothing in the group may be guessed. An unknown language, two members in
one language, or a missing VI source is an ERROR, and callers must stop.

Pure functions only; no network, no database.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlparse

SOURCE_LANGUAGE = "vi"
TRANSLATION_LANGUAGES = ("en", "de")
SUPPORTED_LANGUAGES = (SOURCE_LANGUAGE,) + TRANSLATION_LANGUAGES

# Fields that must be identical across one logical book. Polylang for
# WooCommerce states it synchronises these (to be proven on staging,
# plan tests T4-T6); in manual mode TSYC can only detect drift.
SHARED_FIELDS = (
    "sku",
    "regular_price",
    "sale_price",
    "manage_stock",
    "stock_quantity",
    "backorders",
    "stock_status",
)

# Only these differ per language, and only from APPROVED product_contents.
TRANSLATED_FIELDS = ("name", "description", "short_description")

# A translation payload never carries these: price is owner-only
# (CLAUDE.md 2.5), stock/backorders/SKU come from the add-on's sync of the
# VI source, and status is always forced to "draft" by the builder.
FORBIDDEN_TRANSLATION_FIELDS = frozenset({
    "regular_price",
    "sale_price",
    "price",
    "manage_stock",
    "stock_quantity",
    "backorders",
    "stock_status",
    "sku",
})

BASIS_ADDON = "ADDON_LANG_FIELD"
BASIS_PERMALINK_PREFIX = "PERMALINK_PREFIX"
BASIS_PERMALINK_DEFAULT = "PERMALINK_DEFAULT"
BASIS_TSYC_CREATED = "TSYC_CREATED"

# TSYC's ownership meta (create_woocommerce_draft.py). TSYC creates products
# only through wc/v3, where free Polylang assigns the default language (vi).
# A future TSYC translation draft records its language explicitly.
TSYC_OWNER_META_KEY = "_tsyc_internal_product_id"
TSYC_LANGUAGE_META_KEY = "_tsyc_language"

MODE_ADDON = "ADDON"
MODE_MANUAL = "MANUAL"
MODE_MIXED = "MIXED"

ERROR = "ERROR"
WARNING = "WARNING"

# Issue codes.
LANGUAGE_UNKNOWN = "LANGUAGE_UNKNOWN"
LANGUAGE_UNSUPPORTED = "LANGUAGE_UNSUPPORTED"
DUPLICATE_LANGUAGE = "DUPLICATE_LANGUAGE"
NO_SOURCE = "NO_VI_SOURCE"
SKU_MISMATCH = "SKU_MISMATCH"
MIXED_MODE = "MIXED_LINK_MODE"
TRANSLATION_MAP_MISMATCH = "TRANSLATION_MAP_MISMATCH"
SHARED_FIELD_DRIFT = "SHARED_FIELD_DRIFT"
SELLABLE_WITHOUT_SOURCE = "TRANSLATION_SELLABLE_WHILE_SOURCE_NOT"
NOT_ADDON_MANAGED = "NOT_ADDON_MANAGED"

_LANG_PREFIX = re.compile(r"^/(" + "|".join(TRANSLATION_LANGUAGES) + r")(/|$)")


def product_language(product: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(language, basis). (None, None) when the language cannot be shown."""
    if "lang" in product:
        lang = str(product.get("lang") or "").strip().lower()
        return (lang or None), (BASIS_ADDON if lang else None)

    permalink = str(product.get("permalink") or "")
    if not permalink:
        return None, None
    parsed = urlparse(permalink)
    path = parsed.path or "/"
    match = _LANG_PREFIX.match(path)
    if match:
        return match.group(1), BASIS_PERMALINK_PREFIX
    if "p=" in parsed.query and path.strip("/") == "":
        # A draft's "?post_type=product&p=N" link never carries a language
        # prefix, whatever its language. Only TSYC's own products are known:
        # by their recorded language meta, else vi (REST-created source).
        if is_tsyc_created(product):
            recorded = meta_value(product, TSYC_LANGUAGE_META_KEY)
            return (str(recorded).lower() if recorded else SOURCE_LANGUAGE), BASIS_TSYC_CREATED
        return None, None
    # Polylang hides the default-language (vi) prefix on this site.
    return SOURCE_LANGUAGE, BASIS_PERMALINK_DEFAULT


def meta_value(product: Mapping[str, Any], key: str) -> Any:
    for meta in product.get("meta_data") or []:
        if isinstance(meta, Mapping) and meta.get("key") == key:
            return meta.get("value")
    return None


def is_tsyc_created(product: Mapping[str, Any]) -> bool:
    return bool(meta_value(product, TSYC_OWNER_META_KEY))


@dataclass(frozen=True)
class GroupIssue:
    severity: str
    code: str
    detail: str


@dataclass
class TranslationGroup:
    sku: str
    mode: str
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[GroupIssue] = field(default_factory=list)

    @property
    def source(self) -> dict[str, Any] | None:
        return self.members.get(SOURCE_LANGUAGE)

    @property
    def errors(self) -> list[GroupIssue]:
        return [issue for issue in self.issues if issue.severity == ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def translation_map(self) -> dict[str, int]:
        return {lang: int(product["id"]) for lang, product in self.members.items()}


def _norm(field_name: str, value: Any) -> Any:
    if field_name in ("regular_price", "sale_price"):
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return f"{float(text):.2f}"
        except ValueError:
            return text
    if field_name == "stock_quantity":
        return None if value in (None, "") else int(value)
    return value


normalise_field_value = _norm


def evaluate_translation_group(sku: str, matches: Sequence[Mapping[str, Any]]) -> TranslationGroup:
    """Group exact-SKU matches into one logical book and report every
    inconsistency. Never picks a winner among conflicting members."""
    has_lang_field = ["lang" in product for product in matches]
    if all(has_lang_field):
        mode = MODE_ADDON
    elif not any(has_lang_field):
        mode = MODE_MANUAL
    else:
        mode = MODE_MIXED
    group = TranslationGroup(sku=sku, mode=mode)
    if mode == MODE_MIXED:
        group.issues.append(GroupIssue(ERROR, MIXED_MODE, "Some members have Polylang for WooCommerce fields, some do not."))

    for product in matches:
        pid = product.get("id")
        if str(product.get("sku") or "") != sku:
            group.issues.append(GroupIssue(ERROR, SKU_MISMATCH, f"#{pid} has SKU {product.get('sku')!r}."))
            continue
        lang, _basis = product_language(product)
        if lang is None:
            group.issues.append(GroupIssue(ERROR, LANGUAGE_UNKNOWN, f"#{pid}: language cannot be determined."))
            continue
        if lang not in SUPPORTED_LANGUAGES:
            group.issues.append(GroupIssue(ERROR, LANGUAGE_UNSUPPORTED, f"#{pid}: language {lang!r}."))
            continue
        if lang in group.members:
            group.issues.append(GroupIssue(
                ERROR, DUPLICATE_LANGUAGE,
                f"#{group.members[lang]['id']} and #{pid} are both {lang!r}."))
            continue
        group.members[lang] = dict(product)

    if matches and group.source is None:
        group.issues.append(GroupIssue(ERROR, NO_SOURCE, "No Vietnamese source product carries this SKU."))

    if mode == MODE_ADDON and group.members:
        expected = group.translation_map()
        for lang, product in group.members.items():
            linked = {str(k): int(v) for k, v in (product.get("translations") or {}).items()}
            if linked != expected:
                group.issues.append(GroupIssue(
                    ERROR, TRANSLATION_MAP_MISMATCH,
                    f"#{product['id']} ({lang}) links {linked}, group is {expected}."))

    if len(group.members) > 1:
        for field_name in SHARED_FIELDS:
            values = {lang: _norm(field_name, p.get(field_name)) for lang, p in group.members.items()}
            if len(set(values.values())) > 1:
                severity = ERROR if mode == MODE_ADDON else WARNING
                group.issues.append(GroupIssue(severity, SHARED_FIELD_DRIFT, f"{field_name}: {values}"))

        source = group.source
        if source is not None and source.get("status") != "publish":
            for lang, product in group.members.items():
                if lang != SOURCE_LANGUAGE and product.get("status") == "publish":
                    group.issues.append(GroupIssue(
                        WARNING, SELLABLE_WITHOUT_SOURCE,
                        f"#{product['id']} ({lang}) is published while VI #{source['id']} is {source.get('status')!r}."))

        if mode == MODE_MANUAL:
            group.issues.append(GroupIssue(
                WARNING, NOT_ADDON_MANAGED,
                "Independent per-language products: price and stock are not synchronised."))

    return group


def select_source_product(sku: str, matches: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The VI product of a clean group (None when the SKU is unused).
    Raises when the group is inconsistent: never guess (CLAUDE.md 5.2)."""
    if not matches:
        return None
    group = evaluate_translation_group(sku, matches)
    if not group.ok:
        raise ValueError(f"Translation group for {sku} is inconsistent: {[i.code for i in group.errors]}")
    return group.source


# --- Polylang links on the public storefront (manual mode) ---------------
#
# Free Polylang exposes no translation links over REST (`wc/v3` and
# `wp/v2/product` carry no `lang`/`translations`). It does print one
# `<link rel="alternate" hreflang>` per PUBLISHED translation on each
# product page. That is the only read-only evidence that manual copies are
# linked. It matters because Polylang for WooCommerce starts synchronising
# price and stock across linked products once installed (plan section 3a).
# A draft member has no public page and never appears in its siblings'
# links, so its link cannot be verified this way.

LINK_FOREIGN = "TRANSLATION_LINK_FOREIGN"
LINK_MISSING = "TRANSLATION_LINK_MISSING"
LINK_OUTSIDE_GROUP = "TRANSLATION_LINK_OUTSIDE_SKU_GROUP"
LINK_UNVERIFIABLE = "TRANSLATION_LINK_UNVERIFIABLE"
LINKED_BY_POLYLANG = "POLYLANG_LINKED_MANUAL_COPIES"

_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_TAG_ATTRIBUTE = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*("([^"]*)"|'([^']*)')""")


def parse_hreflang_links(page_html: str) -> dict[str, str]:
    """{language: normalised URL} from a page's rel=alternate hreflang tags."""
    links: dict[str, str] = {}
    for tag in _LINK_TAG.findall(page_html or ""):
        attributes = {
            match.group(1).lower(): html.unescape(match.group(3) if match.group(3) is not None else match.group(4))
            for match in _TAG_ATTRIBUTE.finditer(tag)
        }
        if "alternate" not in attributes.get("rel", "").lower().split():
            continue
        lang = attributes.get("hreflang", "").strip().lower().split("-")[0]
        href = attributes.get("href", "").strip()
        if lang and lang != "x" and href:
            links[lang] = normalise_url(href)
    return links


def normalise_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = unquote(parsed.path or "/").rstrip("/") or "/"
    return f"{parsed.netloc.lower()}{path}" + (f"?{parsed.query}" if parsed.query else "")


def evaluate_storefront_links(
    group: TranslationGroup, page_links: Mapping[str, Mapping[str, str] | None]
) -> list[GroupIssue]:
    """Compare each published member's hreflang links with the SKU group.

    page_links: {language: parse_hreflang_links(...) of that member's page,
    or None when the page could not be read}. Never guesses: a link to a
    product outside the group is reported, not followed."""
    issues: list[GroupIssue] = []
    if len(group.members) < 2:
        return issues
    published = {lang: p for lang, p in group.members.items() if p.get("status") == "publish"}
    expected = {lang: normalise_url(str(p.get("permalink") or "")) for lang, p in published.items()}

    for lang, product in group.members.items():
        if lang not in published:
            issues.append(GroupIssue(
                WARNING, LINK_UNVERIFIABLE,
                f"#{product['id']} ({lang}) is {product.get('status')!r}: Polylang shows no public link to it."))

    seen_links = False
    for lang, product in published.items():
        links = page_links.get(lang)
        if links is None:
            issues.append(GroupIssue(
                WARNING, LINK_UNVERIFIABLE, f"#{product['id']} ({lang}): storefront page could not be read."))
            continue
        for other_lang, other_url in expected.items():
            if other_lang == lang:
                continue
            got = links.get(other_lang)
            if got is None:
                issues.append(GroupIssue(
                    WARNING, LINK_MISSING,
                    f"#{product['id']} ({lang}) does not link {other_lang} #{published[other_lang]['id']}."))
            elif got != other_url:
                issues.append(GroupIssue(
                    ERROR, LINK_FOREIGN,
                    f"#{product['id']} ({lang}) links {other_lang} to {got}, not group member "
                    f"#{published[other_lang]['id']}."))
            else:
                seen_links = True
        for other_lang, got in links.items():
            if other_lang not in group.members and other_lang in SUPPORTED_LANGUAGES:
                issues.append(GroupIssue(
                    WARNING, LINK_OUTSIDE_GROUP,
                    f"#{product['id']} ({lang}) links {other_lang} to {got}, which does not carry this SKU."))

    if seen_links and group.mode == MODE_MANUAL:
        issues.append(GroupIssue(
            WARNING, LINKED_BY_POLYLANG,
            "Copies are linked as Polylang translations: once Polylang for WooCommerce is installed it "
            "synchronises price and stock from whichever copy is saved next."))
    return issues


# --- Stage 5 planning (add-on mode only; not wired to any writer yet) ---

ACTION_CREATE = "CREATE"
ACTION_UPDATE_TEXT = "UPDATE_TEXT"
ACTION_NO_OP = "NO_OP"
ACTION_SKIP_OWNER_MAINTAINED = "SKIP_OWNER_MAINTAINED"
ACTION_SKIP_NO_APPROVED_CONTENT = "SKIP_NO_APPROVED_CONTENT"
ACTION_RECONCILE_FIRST = "RECONCILE_FIRST"
ACTION_REFUSE = "REFUSE"


# render(content) -> (description_html, short_description_html). The writer
# passes create_woocommerce_draft's build_description/build_short_description
# so EN/DE are rendered exactly like the VI draft.
Renderer = Callable[[Mapping[str, Any]], tuple[str, str]]


def build_translation_payload(
    source: Mapping[str, Any], lang: str, content: Mapping[str, Any], render: Renderer
) -> dict[str, Any]:
    """POST body for one linked EN/DE translation of a VI draft.

    Draft only, text from APPROVED content only, no price/stock/SKU fields
    (the add-on copies them from the source). Raises on any violation."""
    if lang not in TRANSLATION_LANGUAGES:
        raise ValueError(f"Not a translation language: {lang!r}")
    source_lang, _ = product_language(source)
    if source_lang != SOURCE_LANGUAGE:
        raise ValueError(f"Source product #{source.get('id')} is not the VI product ({source_lang!r}).")
    if source.get("status") != "draft":
        raise ValueError(f"Source product #{source.get('id')} is {source.get('status')!r}, not an automation draft.")
    if content.get("content_status") != "APPROVED":
        raise ValueError(f"{lang} content is {content.get('content_status')!r}, not APPROVED.")
    if content.get("review_required") is not False:
        raise ValueError(f"{lang} content still requires review.")
    if str(content.get("content_language") or "").lower() != lang:
        raise ValueError(f"Content language {content.get('content_language')!r} does not match {lang!r}.")
    name = str(content.get("product_name") or "").strip()
    if not name:
        raise ValueError(f"{lang} content has no product name.")

    links = {SOURCE_LANGUAGE: int(source["id"])}
    for other_lang, other_id in (source.get("translations") or {}).items():
        if other_lang not in (SOURCE_LANGUAGE, lang):
            links[str(other_lang)] = int(other_id)

    description, short_description = render(content)
    payload = {
        "name": name,
        "description": description,
        "short_description": short_description,
        "status": "draft",
        "lang": lang,
        "translations": links,
    }
    if FORBIDDEN_TRANSLATION_FIELDS & payload.keys():
        raise ValueError("Translation payload carries a price/stock/SKU field.")
    return payload


def plan_translation_actions(
    group: TranslationGroup,
    approved_contents: Mapping[str, Mapping[str, Any]],
    render: Renderer,
    uncertain_languages: Sequence[str] = (),
) -> dict[str, str]:
    """Per-language action for one group. Idempotent: an existing, matching
    translation is NO_OP; a published one is never touched."""
    if group.mode != MODE_ADDON or not group.ok or group.source is None:
        return {lang: ACTION_REFUSE for lang in TRANSLATION_LANGUAGES}
    if group.source.get("status") != "draft":
        return {lang: ACTION_SKIP_OWNER_MAINTAINED for lang in TRANSLATION_LANGUAGES}

    actions: dict[str, str] = {}
    for lang in TRANSLATION_LANGUAGES:
        content = approved_contents.get(lang)
        existing = group.members.get(lang)
        if lang in uncertain_languages:
            actions[lang] = ACTION_RECONCILE_FIRST
        elif existing is not None and existing.get("status") != "draft":
            actions[lang] = ACTION_SKIP_OWNER_MAINTAINED
        elif content is None or content.get("content_status") != "APPROVED":
            actions[lang] = ACTION_SKIP_NO_APPROVED_CONTENT
        elif existing is None:
            actions[lang] = ACTION_CREATE
        else:
            wanted = build_translation_payload(group.source, lang, content, render)
            same = all(str(existing.get(f) or "") == wanted[f] for f in TRANSLATED_FIELDS)
            actions[lang] = ACTION_NO_OP if same else ACTION_UPDATE_TEXT
    return actions


def verify_translation_readback(
    source: Mapping[str, Any], translation: Mapping[str, Any], lang: str
) -> list[str]:
    """Problems found after creating/updating a translation (empty = verified)."""
    problems: list[str] = []
    if translation.get("status") != "draft":
        problems.append(f"status is {translation.get('status')!r}, expected 'draft'")
    if translation.get("lang") != lang:
        problems.append(f"lang is {translation.get('lang')!r}, expected {lang!r}")
    linked = {str(k): int(v) for k, v in (translation.get("translations") or {}).items()}
    if linked.get(SOURCE_LANGUAGE) != int(source["id"]):
        problems.append(f"not linked to VI #{source['id']} ({linked})")
    for field_name in SHARED_FIELDS:
        if _norm(field_name, translation.get(field_name)) != _norm(field_name, source.get(field_name)):
            problems.append(f"{field_name} not synchronised from source")
    return problems
