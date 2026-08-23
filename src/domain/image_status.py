"""Canonical image-status vocabularies.

Two distinct enums live in two different tables and must not be
conflated -- they share a column name but not a value set:

- product_images.image_status: the status of one physical image row.
  Authoritative source: migrations/001_initial_schema.sql.
- internal_products.image_status: the aggregate image-readiness rollup
  for one internal product (derived from its images, not a 1:1 mirror of
  any single image's status). Authoritative source:
  migrations/007_create_internal_products.sql.
"""
from __future__ import annotations


class ImageStatus:
    """product_images.image_status values."""

    PENDING = "PENDING"
    DOWNLOADED = "DOWNLOADED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


ALL_IMAGE_STATUSES = frozenset(
    {
        ImageStatus.PENDING,
        ImageStatus.DOWNLOADED,
        ImageStatus.VALIDATED,
        ImageStatus.REJECTED,
        ImageStatus.FAILED,
    }
)


class InternalProductImageStatus:
    """internal_products.image_status values (aggregate rollup)."""

    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"


ALL_INTERNAL_PRODUCT_IMAGE_STATUSES = frozenset(
    {
        InternalProductImageStatus.PENDING,
        InternalProductImageStatus.AVAILABLE,
        InternalProductImageStatus.REVIEW_REQUIRED,
        InternalProductImageStatus.APPROVED,
    }
)
