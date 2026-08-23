"""Canonical `product_images.usage_rights_status` vocabulary.

Authoritative source: migrations/001_initial_schema.sql (the column's
CHECK constraint) and migrations/009_add_product_image_review_guards.sql
(product_images_publish_eligibility_check, which restricts
is_publish_eligible = true to exactly the three PUBLISHABLE values below).

Do not add a value here that the database does not already accept, and do
not remove a value the database still accepts -- keep this file and the
migrations in lockstep. tests/test_domain_constants.py checks this.
"""
from __future__ import annotations


class RightsStatus:
    """Individual usage_rights_status values, as plain strings so they
    drop in wherever a raw string literal was compared/written before."""

    STORE_OWNED = "STORE_OWNED"
    PUBLISHER_APPROVED = "PUBLISHER_APPROVED"
    SUPPLIER_APPROVED = "SUPPLIER_APPROVED"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    RIGHTS_UNKNOWN = "RIGHTS_UNKNOWN"


# Every value the product_images.usage_rights_status CHECK constraint
# accepts (migrations/001_initial_schema.sql).
ALL_RIGHTS_STATUSES = frozenset(
    {
        RightsStatus.STORE_OWNED,
        RightsStatus.PUBLISHER_APPROVED,
        RightsStatus.SUPPLIER_APPROVED,
        RightsStatus.REFERENCE_ONLY,
        RightsStatus.RIGHTS_UNKNOWN,
    }
)

# The subset that product_images_publish_eligibility_check
# (migrations/009_add_product_image_review_guards.sql) allows
# is_publish_eligible = true for. REFERENCE_ONLY and RIGHTS_UNKNOWN are
# valid rights statuses but are never publish eligible.
PUBLISHABLE_RIGHTS_STATUSES = frozenset(
    {
        RightsStatus.STORE_OWNED,
        RightsStatus.PUBLISHER_APPROVED,
        RightsStatus.SUPPLIER_APPROVED,
    }
)
