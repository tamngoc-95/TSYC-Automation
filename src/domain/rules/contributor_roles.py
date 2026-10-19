"""Verified contributor roles (author, reteller, illustrator, ...).

2026-10-17 owner request: distinct contributor roles must never be
collapsed into one "Tác giả" field. Fahasa's author field merges them --
"Hiếu Minh, Cloud Pillow Studio" for a book whose cover credits
"Hiếu Minh kể lại | Cloud Pillow Studio minh họa" -- so the merged value
would present an illustration studio as an author.

The verified roles are stored additively in
internal_products.product_metadata.contributor_roles, with the evidence
they were read from (scripts/record_contributor_roles.py). The
internal_products.author column is never rewritten (CLAUDE.md 2.7); it
stays the identity/provenance value, and the WooCommerce attributes are
built from the roles when they exist (woo_presentation.build_attributes).

Never invents a contributor (CLAUDE.md 2.2): every name must appear in
the recorded credit text; an AUTHOR / RETELLER must already be in the
verified author field (translator, illustrator and original author may
be credited outside it -- EXTRA_ROLES); no name in the author field may
be dropped.

Pure functions only.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.domain.rules import author_rules

METADATA_KEY = "contributor_roles"

AUTHOR = "AUTHOR"
ORIGINAL_AUTHOR = "ORIGINAL_AUTHOR"
RETELLER = "RETELLER"
TRANSLATOR = "TRANSLATOR"
ILLUSTRATOR = "ILLUSTRATOR"

# Canonical order is also the WooCommerce attribute order.
ROLE_ATTRIBUTE_LABELS: dict[str, str] = {
    AUTHOR: "Tác giả",
    ORIGINAL_AUTHOR: "Nguyên tác",
    RETELLER: "Người kể lại",
    TRANSLATOR: "Người dịch",
    ILLUSTRATOR: "Minh họa",
}
ROLES = tuple(ROLE_ATTRIBUTE_LABELS)
# Roles a source credits outside its author field (Fahasa's separate
# "Người Dịch" field, an illustrator named only in the description, the
# original author of a retold tale). Still evidence-bound: the name must
# appear in the recorded credit text.
EXTRA_ROLES = frozenset({ORIGINAL_AUTHOR, TRANSLATOR, ILLUSTRATOR})


def normalize_contributors(contributors: Sequence[Mapping[str, Any]] | None) -> list[tuple[str, str]]:
    """[(name, role)] in input order; raises ValueError on an unknown role,
    an empty name or a repeated (name, role) pair."""
    pairs: list[tuple[str, str]] = []
    for item in contributors or []:
        name = " ".join(str(item.get("name") or "").split())
        role = str(item.get("role") or "").strip().upper()
        if not name:
            raise ValueError("Contributor name is empty.")
        if role not in ROLE_ATTRIBUTE_LABELS:
            raise ValueError(f"Unknown contributor role {role!r}; expected one of {list(ROLES)}.")
        if (name, role) in pairs:
            raise ValueError(f"Duplicate contributor {name!r} as {role}.")
        pairs.append((name, role))
    if not pairs:
        raise ValueError("At least one contributor is required.")
    return pairs


def check_against_evidence(
    author_field: str | None,
    contributors: Sequence[tuple[str, str]],
    credit_text: str | None,
) -> list[str]:
    """Reasons the roles are NOT supported by the evidence (empty = OK)."""
    reasons: list[str] = []
    credit_key = author_rules.person_name_key(credit_text)
    if not credit_key:
        reasons.append("No credit text was recorded as evidence.")
    author_keys = {author_rules.person_name_key(name) for name in author_rules.split_person_names(author_field)}
    covered: set[str] = set()
    for name, role in contributors:
        key = author_rules.person_name_key(name)
        covered.add(key)
        if credit_key and key not in credit_key:
            reasons.append(f"{name!r} does not appear in the recorded credit text.")
        if role not in EXTRA_ROLES and key not in author_keys:
            reasons.append(f"{name!r} ({role}) is not in the verified author field {author_field!r}.")
    for key in sorted(author_keys - covered):
        reasons.append(f"Author-field name {key!r} has no recorded role.")
    return reasons


def verified_contributors(product_metadata: Mapping[str, Any] | None) -> list[tuple[str, str]] | None:
    """The stored verified roles, or None when absent or malformed (a
    malformed record falls back to the plain author field, never a guess)."""
    record = (product_metadata or {}).get(METADATA_KEY) if isinstance(product_metadata, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    try:
        return normalize_contributors(record.get("contributors"))
    except (ValueError, AttributeError, TypeError):
        return None


def attribute_groups(contributors: Sequence[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """[(attribute label, [names])] in canonical role order."""
    groups: list[tuple[str, list[str]]] = []
    for role in ROLES:
        names = [name for name, contributor_role in contributors if contributor_role == role]
        if names:
            groups.append((ROLE_ATTRIBUTE_LABELS[role], names))
    return groups
