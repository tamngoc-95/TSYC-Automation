"""Canonical content-status vocabularies.

Two distinct enums live in two different tables and must not be
conflated -- they share a column name but not a value set:

- product_contents.content_status: the customer-facing content row's own
  lifecycle. Authoritative source: migrations/008_create_product_contents.sql
  (product_contents_status_check). Workflow: DRAFTED -> (REVIEW_REQUIRED) ->
  APPROVED / REJECTED. REVISE is a prepare_product_content.py --action, not
  a content_status value.
- internal_products.content_status: the internal product's own content
  readiness field, a narrower/different set (no REJECTED, but has PENDING
  as its starting point). Authoritative source:
  migrations/007_create_internal_products.sql
  (internal_products_content_status_check).
"""
from __future__ import annotations


class ContentStatus:
    """product_contents.content_status values."""

    DRAFTED = "DRAFTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


ALL_CONTENT_STATUSES = frozenset(
    {
        ContentStatus.DRAFTED,
        ContentStatus.REVIEW_REQUIRED,
        ContentStatus.APPROVED,
        ContentStatus.REJECTED,
    }
)


class InternalProductContentStatus:
    """internal_products.content_status values."""

    PENDING = "PENDING"
    DRAFTED = "DRAFTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"


ALL_INTERNAL_PRODUCT_CONTENT_STATUSES = frozenset(
    {
        InternalProductContentStatus.PENDING,
        InternalProductContentStatus.DRAFTED,
        InternalProductContentStatus.REVIEW_REQUIRED,
        InternalProductContentStatus.APPROVED,
    }
)
