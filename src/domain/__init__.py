"""TSYC domain constants: the canonical source of truth for the status,
rights, and source-type vocabularies used across the pipeline.

Each submodule documents, in its own docstring, exactly which table and
column its constants describe and which migrations/*.sql file is
authoritative. Where two tables share a column name but not a value set
(e.g. image_status, content_status, woocommerce_status), this package
keeps them as two distinct, clearly-named constants rather than one
conflated set -- see the submodule docstrings for the exact mapping.

Production scripts may import either from here (`from src.domain import
RightsStatus`) or directly from the submodule (`from src.domain.
rights_status import RightsStatus`); both resolve to the same objects.
"""
from __future__ import annotations

from src.domain.content_status import (
    ALL_CONTENT_STATUSES,
    ALL_INTERNAL_PRODUCT_CONTENT_STATUSES,
    ContentStatus,
    InternalProductContentStatus,
)
from src.domain.identity_status import (
    ALL_IDENTITY_STATUSES,
    ALL_MATCH_DECISIONS,
    IdentityStatus,
    MatchDecision,
)
from src.domain.image_status import (
    ALL_IMAGE_STATUSES,
    ALL_INTERNAL_PRODUCT_IMAGE_STATUSES,
    ImageStatus,
    InternalProductImageStatus,
)
from src.domain.reference_sources import (
    ALL_REFERENCE_SOURCE_TYPES,
    REFERENCE_SOURCE_PRIORITY,
    REFERENCE_SOURCE_TYPES,
    SourceType,
)
from src.domain.rights_status import (
    ALL_RIGHTS_STATUSES,
    PUBLISHABLE_RIGHTS_STATUSES,
    RightsStatus,
)
from src.domain.woocommerce_status import (
    ALL_WOOCOMMERCE_STATUSES,
    ALL_WOOCOMMERCE_SYNC_STATUSES,
    WooCommerceStatus,
    WooCommerceSyncStatus,
)

__all__ = [
    "ALL_CONTENT_STATUSES",
    "ALL_IDENTITY_STATUSES",
    "ALL_IMAGE_STATUSES",
    "ALL_INTERNAL_PRODUCT_CONTENT_STATUSES",
    "ALL_INTERNAL_PRODUCT_IMAGE_STATUSES",
    "ALL_MATCH_DECISIONS",
    "ALL_REFERENCE_SOURCE_TYPES",
    "ALL_RIGHTS_STATUSES",
    "ALL_WOOCOMMERCE_STATUSES",
    "ALL_WOOCOMMERCE_SYNC_STATUSES",
    "ContentStatus",
    "IdentityStatus",
    "ImageStatus",
    "InternalProductContentStatus",
    "InternalProductImageStatus",
    "MatchDecision",
    "PUBLISHABLE_RIGHTS_STATUSES",
    "REFERENCE_SOURCE_PRIORITY",
    "REFERENCE_SOURCE_TYPES",
    "RightsStatus",
    "SourceType",
    "WooCommerceStatus",
    "WooCommerceSyncStatus",
]
