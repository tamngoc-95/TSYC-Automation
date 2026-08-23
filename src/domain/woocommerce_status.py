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

scripts/sync_woocommerce_product_status.py's map_remote_status() maps a
remote WooCommerce product status to a confirmed, canonical pair from
these two enums for exactly two remote statuses ("draft", "publish") --
see its own docstring and SUPPORTED_REMOTE_STATUS_MAPPING for the exact
rule and reasoning. Any other remote status is deliberately left
unmapped: mark_reconciliation_anomaly() leaves both woocommerce_status
columns untouched and flags the internal product for review instead of
guessing a value. Neither column is ever written a value outside the
sets below -- tests/test_sync_woocommerce_product_status.py covers every
supported and unsupported branch.
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
