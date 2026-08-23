"""Canonical WooCommerce-status vocabularies.

Two distinct enums live in two different tables and must not be
conflated -- they share a column name but not a value set:

- internal_products.woocommerce_status: one internal product's overall
  WooCommerce lifecycle stage (used for draft readiness and by
  check_draft_readiness.py / run_batch.py). Authoritative source:
  migrations/007_create_internal_products.sql
  (internal_products_woocommerce_status_check).
- woocommerce_product_syncs.woocommerce_status: the status of one sync
  *attempt* against the remote WooCommerce API (used for recovery/no-
  blind-retry logic in create_woocommerce_draft.py). Authoritative
  source: migrations/010_create_woocommerce_product_syncs.sql
  (woocommerce_product_syncs_status_check).

Known drift (found during the inventory for this centralization, not
introduced by it, and deliberately not fixed here -- fixing it would
change runtime behavior / require a schema decision, both out of scope):
scripts/sync_woocommerce_product_status.py's map_remote_status() writes
PENDING_REVIEW, PRIVATE, TRASHED, and a REVIEW_REQUIRED fallback into
these columns for non-"draft" remote statuses, and mark_product_missing()
writes MISSING. None of those five values are members of either set
below. tests/test_domain_constants.py documents this as a known,
unresolved gap rather than silently certifying it as canonical.
"""
from __future__ import annotations


class WooCommerceStatus:
    """internal_products.woocommerce_status values."""

    NOT_CREATED = "NOT_CREATED"
    READY_FOR_DRAFT = "READY_FOR_DRAFT"
    DRAFT_CREATED = "DRAFT_CREATED"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


ALL_WOOCOMMERCE_STATUSES = frozenset(
    {
        WooCommerceStatus.NOT_CREATED,
        WooCommerceStatus.READY_FOR_DRAFT,
        WooCommerceStatus.DRAFT_CREATED,
        WooCommerceStatus.READY_TO_PUBLISH,
        WooCommerceStatus.PUBLISHED,
        WooCommerceStatus.FAILED,
    }
)


class WooCommerceSyncStatus:
    """woocommerce_product_syncs.woocommerce_status values."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DRAFT_CREATED = "DRAFT_CREATED"
    FAILED = "FAILED"


ALL_WOOCOMMERCE_SYNC_STATUSES = frozenset(
    {
        WooCommerceSyncStatus.PENDING,
        WooCommerceSyncStatus.IN_PROGRESS,
        WooCommerceSyncStatus.DRAFT_CREATED,
        WooCommerceSyncStatus.FAILED,
    }
)
