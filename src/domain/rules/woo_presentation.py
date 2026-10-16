"""How verified product metadata is presented on a WooCommerce product.

2026-10-16 owner request: the author must not be repeated in the product
description. It stays in internal_products.author / product_details and is
shown once, as the WooCommerce "Tác giả" product attribute ("Additional
information" tab). The other verified details lines ("Nhà xuất bản: X",
"Số trang: 56", ...) become attributes the same way, so the description is
prose only -- no author/publisher/ISBN metadata section.

Conservative fallback: a details value that is not a plain "Label: value"
list (e.g. a combo's numbered list of volumes) cannot be split into
attributes without guessing, so it stays in the description as a list --
minus its author line, which is still an attribute.

Pure functions only; used by scripts/create_woocommerce_draft.py and
scripts/update_woocommerce_draft_content.py.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from src.domain.rules import author_rules, storefront_text

AUTHOR_ATTRIBUTE_NAME = "Tác giả"
_AUTHOR_LABELS = frozenset({"tác giả", "đồng tác giả", "author", "authors", "autor", "autorin", "autoren"})
_DETAIL_LINE_RE = re.compile(r"^\s*([^:：\n]{1,40}?)\s*[:：]\s*(\S.*?)\s*$")
_MAX_ATTRIBUTE_VALUE_LENGTH = 200


def _detail_lines(product_details: str | None) -> list[str]:
    if not product_details:
        return []
    text = storefront_text.decode_html_entities(str(product_details))
    return [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def parse_detail_line(line: str) -> tuple[str, str] | None:
    """("Label", "value") for a plain "Label: value" line, else None."""
    match = _DETAIL_LINE_RE.match(line)
    if not match or len(match.group(2)) > _MAX_ATTRIBUTE_VALUE_LENGTH:
        return None
    label = " ".join(match.group(1).split())
    if not label or label[0].isdigit():
        return None
    return label, match.group(2)


def is_author_detail_line(line: str) -> bool:
    parsed = parse_detail_line(line)
    return parsed is not None and parsed[0].casefold() in _AUTHOR_LABELS


def details_are_attribute_list(product_details: str | None) -> bool:
    """True when every details line is a plain "Label: value" pair."""
    lines = _detail_lines(product_details)
    return bool(lines) and all(parse_detail_line(line) for line in lines)


def description_detail_lines(product_details: str | None) -> list[str]:
    """Details lines still rendered in the description: none when the
    whole list becomes attributes; otherwise every line except the
    author line (never shown twice)."""
    if details_are_attribute_list(product_details):
        return []
    return [line for line in _detail_lines(product_details) if not is_author_detail_line(line)]


def author_names(author: str | None) -> list[str]:
    """Verified author names, roles/honorifics removed (author_rules)."""
    return [name for name in author_rules.split_person_names(author) if name]


def build_attributes(
    product: Mapping[str, Any],
    content: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """
    WooCommerce product attributes (custom, visible, not for variations):
    "Tác giả" from the verified internal_products.author, then -- only
    when the details value is a plain attribute list -- every other
    details line in its stored order. Never invents a value: an absent
    author or detail simply produces no attribute.
    """
    attributes: list[dict[str, Any]] = []

    def add(name: str, options: list[str]) -> None:
        attributes.append(
            {"name": name, "position": len(attributes), "visible": True, "variation": False, "options": options}
        )

    names = author_names(product.get("author"))
    if names:
        add(AUTHOR_ATTRIBUTE_NAME, names)

    details = content.get("product_details")
    if details_are_attribute_list(details):
        seen = {AUTHOR_ATTRIBUTE_NAME.casefold()}
        for line in _detail_lines(details):
            label, value = parse_detail_line(line)  # type: ignore[misc]
            if label.casefold() in _AUTHOR_LABELS:
                if not names:
                    # No verified structured author: the details line is the
                    # only (already-approved) statement of it.
                    add(AUTHOR_ATTRIBUTE_NAME, [value])
                    names = [value]
                continue
            if label.casefold() in seen:
                continue
            seen.add(label.casefold())
            add(label, [value])
    return attributes


def comparable_attributes(attributes: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Order-preserving (name, options) pairs for comparing what TSYC
    sent with what WooCommerce stored (ids/positions/slugs ignored)."""
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for attribute in attributes or []:
        if not isinstance(attribute, Mapping):
            continue
        pairs.append(
            (
                " ".join(str(attribute.get("name") or "").split()),
                tuple(" ".join(str(option).split()) for option in attribute.get("options") or []),
            )
        )
    return pairs
