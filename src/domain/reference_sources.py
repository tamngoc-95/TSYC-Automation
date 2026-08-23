"""Canonical identity-reference source vocabulary.

Scope: product_references.source_type -- the source-type vocabulary used
for book-identity reference collection/registration (register_reference_
source.py, manual_create_product_reference.py, collect_reference_
metadata.py). Authoritative source: migrations/001_initial_schema.sql
(product_references source_type CHECK constraint).

This is deliberately narrower than source_urls.source_type (which also
covers purchase-price document types like PURCHASE_INVOICE) and
product_images.source_type (which also covers STORE_OWNED for shop-
photographed images). Those are different concepts tracked in different
columns and are not centralized here.

Source priority mirrors CLAUDE.md section 8.1's book-identity source
order and manual_create_product_reference.py's exact numeric values (the
canonical automated-matching implementation). collect_reference_
metadata.py defines its own narrower priority map (no FACEBOOK entry,
OTHER=5 instead of 9) -- found during the inventory for this
centralization but deliberately NOT unified here, since doing so would
change what priority integer that script writes for OTHER-type
references, a runtime-behavior change out of scope for this refactor.
"""
from __future__ import annotations

from types import MappingProxyType


class SourceType:
    """product_references.source_type values."""

    PUBLISHER = "PUBLISHER"
    AUTHORIZED_SUPPLIER = "AUTHORIZED_SUPPLIER"
    BOOKSTORE = "BOOKSTORE"
    FAHASA = "FAHASA"
    FACEBOOK = "FACEBOOK"
    OTHER = "OTHER"


# Ordered to match the existing SOURCE_TYPES tuples in
# register_reference_source.py and manual_create_product_reference.py
# (used as argparse `choices=`, where order affects displayed order).
REFERENCE_SOURCE_TYPES = (
    SourceType.PUBLISHER,
    SourceType.AUTHORIZED_SUPPLIER,
    SourceType.BOOKSTORE,
    SourceType.FAHASA,
    SourceType.FACEBOOK,
    SourceType.OTHER,
)

ALL_REFERENCE_SOURCE_TYPES = frozenset(REFERENCE_SOURCE_TYPES)

# Lower number = higher priority. Exact values from
# manual_create_product_reference.py's SOURCE_PRIORITIES.
REFERENCE_SOURCE_PRIORITY = MappingProxyType(
    {
        SourceType.PUBLISHER: 1,
        SourceType.AUTHORIZED_SUPPLIER: 2,
        SourceType.BOOKSTORE: 3,
        SourceType.FAHASA: 4,
        SourceType.FACEBOOK: 5,
        SourceType.OTHER: 9,
    }
)
