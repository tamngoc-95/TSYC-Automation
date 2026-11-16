"""Read-back verification of one pre-order VI/EN/DE draft trio (no I/O).

Input is what was read after the creates: each member's WooCommerce product
(`context=edit`), the Polylang group as read from every member, and the text
the approved packages rendered to. Output is a list of problems per Gate A
check (src/domain/integration_gates.py A05-A10); an empty list means that
check holds. Nothing here writes or repairs.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.domain import woo_translation_group as wtg
from src.domain.rules import preorder_inventory_rules as pir

CHECK_IDS = ("A05", "A06", "A07", "A08", "A09", "A10")
PHYSICAL_FIELDS = ("weight", "dimensions", "type", "catalog_visibility")


def _norm(text: Any) -> str:
    return str(text or "").strip()


def _image_ids(product: Mapping[str, Any]) -> list[int]:
    return [int(image["id"]) for image in product.get("images") or [] if isinstance(image, Mapping) and image.get("id")]


def _attribute_values(product: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Option values per attribute, in position order. Labels are localized
    on a copy; the values always come from the VI source."""
    attributes = sorted(product.get("attributes") or [], key=lambda a: int(a.get("position") or 0))
    return [tuple(str(option) for option in attribute.get("options") or []) for attribute in attributes]


def verify_trio(
    *,
    internal_product_id: str,
    product_code: str,
    products: Mapping[str, Mapping[str, Any]],
    group_views: Mapping[str, Mapping[str, int]],
    expected_text: Mapping[str, Mapping[str, str]],
) -> dict[str, list[str]]:
    """
    products: language -> product. group_views: language -> the translation
    map Polylang returned when asked about that member. expected_text:
    language -> {"name", "description", "short_description"}.
    """
    problems: dict[str, list[str]] = {check: [] for check in CHECK_IDS}
    languages = wtg.SUPPORTED_LANGUAGES
    missing = [lang for lang in languages if lang not in products]
    if missing:
        for check in CHECK_IDS:
            problems[check].append(f"No product read for {missing}.")
        return problems
    ids = {lang: int(products[lang]["id"]) for lang in languages}

    # A05: the authoritative group, the same from every member.
    if len(set(ids.values())) != len(languages):
        problems["A05"].append(f"The three languages are not three products: {ids}.")
    for lang in languages:
        view = {str(k): int(v) for k, v in (group_views.get(lang) or {}).items()}
        if view != ids:
            problems["A05"].append(f"Group read from {lang} #{ids[lang]} is {view}, expected {ids}.")

    # A06: one sellable book.
    vi = products[wtg.SOURCE_LANGUAGE]
    if _norm(vi.get("sku")) != product_code:
        problems["A06"].append(f"VI SKU is {vi.get('sku')!r}, expected {product_code!r}.")
    for lang in languages:
        product = products[lang]
        owner = wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY)
        if str(owner) != str(internal_product_id):
            problems["A06"].append(f"{lang} #{ids[lang]} carries internal product {owner!r}.")
        if lang != wtg.SOURCE_LANGUAGE:
            recorded = wtg.meta_value(product, wtg.TSYC_LANGUAGE_META_KEY)
            if str(recorded or "").lower() != lang:
                problems["A06"].append(f"{lang} #{ids[lang]} language meta is {recorded!r}.")
            # No SKU, or the VI SKU a site hook copied: one logical book either way.
            if _norm(product.get("sku")) not in ("", product_code):
                problems["A06"].append(f"{lang} #{ids[lang]} has a different SKU {product.get('sku')!r}.")
        if product.get("type") != vi.get("type"):
            problems["A06"].append(f"{lang} #{ids[lang]} is a {product.get('type')!r} product.")

    for lang in languages:
        product = products[lang]
        label = f"{lang} #{ids[lang]}"
        # A07: pre-order configuration.
        if product.get("status") != "draft":
            problems["A07"].append(f"{label} status is {product.get('status')!r}.")
        if not pir.inventory_matches_policy(product):
            problems["A07"].append(
                f"{label} inventory is manage_stock={product.get('manage_stock')!r}, "
                f"stock_quantity={product.get('stock_quantity')!r}, backorders={product.get('backorders')!r}.")
        if product.get("stock_status") != pir.EXPECTED_STOCK_STATUS:
            problems["A07"].append(f"{label} stock_status is {product.get('stock_status')!r}.")
        # A08: no price of any kind.
        for name in pir.PRICE_FIELDS:
            if _norm(product.get(name)):
                problems["A08"].append(f"{label} {name} is {product.get(name)!r}.")
        if product.get("purchasable") is True:
            problems["A08"].append(f"{label} is purchasable.")
        # A09: the approved package, exactly.
        wanted = expected_text.get(lang)
        if wanted is None:
            problems["A09"].append(f"No approved text given for {lang}.")
        else:
            for name in wtg.TRANSLATED_FIELDS:
                if _norm(product.get(name)) != _norm(wanted.get(name)):
                    problems["A09"].append(f"{label} {name} differs from the approved {lang} package.")

    # A10: images, contributors and physical metadata follow the VI source.
    if not _image_ids(vi):
        problems["A10"].append("The VI draft has no image.")
    if not _attribute_values(vi):
        problems["A10"].append("The VI draft has no attributes (contributors).")
    for lang in wtg.TRANSLATION_LANGUAGES:
        product = products[lang]
        label = f"{lang} #{ids[lang]}"
        if _image_ids(product) != _image_ids(vi):
            problems["A10"].append(f"{label} images {_image_ids(product)} differ from VI {_image_ids(vi)}.")
        if _attribute_values(product) != _attribute_values(vi):
            problems["A10"].append(f"{label} attribute values differ from the VI source.")
        for name in PHYSICAL_FIELDS:
            if product.get(name) != vi.get(name):
                problems["A10"].append(f"{label} {name} is {product.get(name)!r}, VI has {vi.get(name)!r}.")
    return problems
