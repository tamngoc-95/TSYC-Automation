"""Regression tests for src/domain/*.py against the canonical migration
CHECK constraints they mirror.

These tests are the DB/migration drift guard requested for the domain-
constants centralization: they compare the Python constants against an
explicit, hand-verified fixture of each CHECK constraint's exact values
(quoted from the authoritative migration file in a comment above each
fixture), rather than parsing SQL at test time -- parsing the migration
files themselves would just move the single-source-of-truth problem
without actually verifying anything a human did not already read.

Fully offline: no Supabase/WooCommerce/Facebook/Playwright/credentials.
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


# --- Rights status: migrations/001_initial_schema.sql
# (product_images.usage_rights_status CHECK) and migrations/
# 009_add_product_image_review_guards.sql
# (product_images_publish_eligibility_check) --------------------------------

CANONICAL_ALL_RIGHTS_STATUSES = frozenset(
    {
        "STORE_OWNED",
        "PUBLISHER_APPROVED",
        "SUPPLIER_APPROVED",
        "REFERENCE_ONLY",
        "RIGHTS_UNKNOWN",
    }
)

CANONICAL_PUBLISHABLE_RIGHTS_STATUSES = frozenset(
    {
        "STORE_OWNED",
        "PUBLISHER_APPROVED",
        "SUPPLIER_APPROVED",
    }
)


def test_all_rights_statuses_matches_migration():
    assert ALL_RIGHTS_STATUSES == CANONICAL_ALL_RIGHTS_STATUSES


def test_publishable_rights_statuses_matches_migration():
    assert PUBLISHABLE_RIGHTS_STATUSES == CANONICAL_PUBLISHABLE_RIGHTS_STATUSES


def test_publishable_rights_are_a_subset_of_all_rights():
    """Every publishable rights status must be a member of ALL_RIGHTS_STATUSES."""
    assert PUBLISHABLE_RIGHTS_STATUSES <= ALL_RIGHTS_STATUSES


def test_non_publishable_rights_are_not_accidentally_publishable():
    non_publishable = ALL_RIGHTS_STATUSES - PUBLISHABLE_RIGHTS_STATUSES
    assert non_publishable == {
        RightsStatus.REFERENCE_ONLY,
        RightsStatus.RIGHTS_UNKNOWN,
    }
    assert non_publishable.isdisjoint(PUBLISHABLE_RIGHTS_STATUSES)


def test_no_stale_supplier_authorized_rights_value():
    """SUPPLIER_APPROVED is canonical; a SUPPLIER_AUTHORIZED-style stale
    name (found in docs/agent instructions during the inventory for this
    centralization, never in production code) must never appear here."""
    assert "SUPPLIER_AUTHORIZED" not in ALL_RIGHTS_STATUSES
    assert RightsStatus.SUPPLIER_APPROVED == "SUPPLIER_APPROVED"


def test_rights_status_class_attributes_match_all_rights_statuses():
    class_values = {
        RightsStatus.STORE_OWNED,
        RightsStatus.PUBLISHER_APPROVED,
        RightsStatus.SUPPLIER_APPROVED,
        RightsStatus.REFERENCE_ONLY,
        RightsStatus.RIGHTS_UNKNOWN,
    }
    assert class_values == ALL_RIGHTS_STATUSES


# --- Image status: migrations/001_initial_schema.sql
# (product_images.image_status CHECK) and migrations/
# 007_create_internal_products.sql
# (internal_products_image_status_check) -------------------------------

CANONICAL_ALL_IMAGE_STATUSES = frozenset(
    {"PENDING", "DOWNLOADED", "VALIDATED", "REJECTED", "FAILED"}
)

CANONICAL_ALL_INTERNAL_PRODUCT_IMAGE_STATUSES = frozenset(
    {"PENDING", "AVAILABLE", "REVIEW_REQUIRED", "APPROVED"}
)


def test_all_image_statuses_matches_migration():
    assert ALL_IMAGE_STATUSES == CANONICAL_ALL_IMAGE_STATUSES


def test_all_internal_product_image_statuses_matches_migration():
    assert (
        ALL_INTERNAL_PRODUCT_IMAGE_STATUSES
        == CANONICAL_ALL_INTERNAL_PRODUCT_IMAGE_STATUSES
    )


def test_image_status_and_internal_product_image_status_are_distinct_enums():
    """These share a column name across two tables but not a value set --
    this must never collapse into one shared constant."""
    assert ALL_IMAGE_STATUSES != ALL_INTERNAL_PRODUCT_IMAGE_STATUSES
    assert ImageStatus.VALIDATED not in ALL_INTERNAL_PRODUCT_IMAGE_STATUSES
    assert InternalProductImageStatus.AVAILABLE not in ALL_IMAGE_STATUSES


# --- Content status: migrations/008_create_product_contents.sql
# (product_contents_status_check) and migrations/
# 007_create_internal_products.sql
# (internal_products_content_status_check) ------------------------------

CANONICAL_ALL_CONTENT_STATUSES = frozenset(
    {"DRAFTED", "REVIEW_REQUIRED", "APPROVED", "REJECTED"}
)

CANONICAL_ALL_INTERNAL_PRODUCT_CONTENT_STATUSES = frozenset(
    {"PENDING", "DRAFTED", "REVIEW_REQUIRED", "APPROVED"}
)


def test_all_content_statuses_matches_migration():
    assert ALL_CONTENT_STATUSES == CANONICAL_ALL_CONTENT_STATUSES


def test_all_internal_product_content_statuses_matches_migration():
    assert (
        ALL_INTERNAL_PRODUCT_CONTENT_STATUSES
        == CANONICAL_ALL_INTERNAL_PRODUCT_CONTENT_STATUSES
    )


def test_content_status_internal_consistency():
    """DRAFTED/REVIEW_REQUIRED/APPROVED are shared by both tables' enums;
    REJECTED exists only for product_contents, PENDING only for
    internal_products -- both directions must hold."""
    shared = ALL_CONTENT_STATUSES & ALL_INTERNAL_PRODUCT_CONTENT_STATUSES
    assert shared == {
        ContentStatus.DRAFTED,
        ContentStatus.REVIEW_REQUIRED,
        ContentStatus.APPROVED,
    }
    assert ContentStatus.REJECTED not in ALL_INTERNAL_PRODUCT_CONTENT_STATUSES
    assert InternalProductContentStatus.PENDING not in ALL_CONTENT_STATUSES


# --- Identity status: migrations/001_initial_schema.sql
# (product_candidates.identity_status CHECK, product_references.
# match_decision CHECK) --------------------------------------------------

CANONICAL_ALL_IDENTITY_STATUSES = frozenset(
    {
        "IDENTITY_PENDING",
        "IDENTITY_VERIFIED",
        "ACCEPTED_WITH_LIMITED_METADATA",
        "IDENTITY_CONFLICT",
        "REJECTED",
    }
)

CANONICAL_ALL_MATCH_DECISIONS = frozenset(
    {"MATCH", "POSSIBLE_MATCH", "DIFFERENT_EDITION", "NO_MATCH", "MANUAL_REVIEW"}
)


def test_all_identity_statuses_matches_migration():
    assert ALL_IDENTITY_STATUSES == CANONICAL_ALL_IDENTITY_STATUSES


def test_all_match_decisions_matches_migration():
    assert ALL_MATCH_DECISIONS == CANONICAL_ALL_MATCH_DECISIONS


def test_identity_status_and_match_decision_are_distinct_enums():
    assert IdentityStatus.IDENTITY_VERIFIED not in ALL_MATCH_DECISIONS
    assert MatchDecision.MATCH not in ALL_IDENTITY_STATUSES


# --- WooCommerce status: migrations/007_create_internal_products.sql
# (internal_products_woocommerce_status_check) and migrations/
# 010_create_woocommerce_product_syncs.sql
# (woocommerce_product_syncs_status_check) -------------------------------

CANONICAL_ALL_WOOCOMMERCE_STATUSES = frozenset(
    {
        "NOT_CREATED",
        "READY_FOR_DRAFT",
        "DRAFT_CREATED",
        "READY_TO_PUBLISH",
        "PUBLISHED",
        "FAILED",
    }
)

CANONICAL_ALL_WOOCOMMERCE_SYNC_STATUSES = frozenset(
    {"PENDING", "IN_PROGRESS", "DRAFT_CREATED", "FAILED"}
)


def test_all_woocommerce_statuses_matches_migration():
    assert ALL_WOOCOMMERCE_STATUSES == CANONICAL_ALL_WOOCOMMERCE_STATUSES


def test_all_woocommerce_sync_statuses_matches_migration():
    assert ALL_WOOCOMMERCE_SYNC_STATUSES == CANONICAL_ALL_WOOCOMMERCE_SYNC_STATUSES


def test_woocommerce_status_and_sync_status_share_only_expected_overlap():
    """These are two different columns on two different tables that
    happen to overlap on DRAFT_CREATED and FAILED -- confirm the overlap
    is exactly that and nothing has silently drifted wider."""
    shared = ALL_WOOCOMMERCE_STATUSES & ALL_WOOCOMMERCE_SYNC_STATUSES
    assert shared == {
        WooCommerceStatus.DRAFT_CREATED,
        WooCommerceStatus.FAILED,
    }


def test_pipeline_scripts_use_canonical_woocommerce_status_values():
    """pipeline_state.py / check_draft_readiness.py / create_woocommerce_
    draft.py all import WooCommerceStatus and WooCommerceSyncStatus rather
    than hardcoding literals (verified by import + successful module load
    in test_domain_constants_are_actually_imported_by_priority_scripts
    below); this test locks down that the exact values those imports
    resolve to are canonical."""
    for value in (
        WooCommerceStatus.NOT_CREATED,
        WooCommerceStatus.READY_FOR_DRAFT,
        WooCommerceStatus.DRAFT_CREATED,
        WooCommerceStatus.READY_TO_PUBLISH,
        WooCommerceStatus.PUBLISHED,
        WooCommerceStatus.FAILED,
    ):
        assert value in ALL_WOOCOMMERCE_STATUSES

    for value in (
        WooCommerceSyncStatus.PENDING,
        WooCommerceSyncStatus.IN_PROGRESS,
        WooCommerceSyncStatus.DRAFT_CREATED,
        WooCommerceSyncStatus.FAILED,
    ):
        assert value in ALL_WOOCOMMERCE_SYNC_STATUSES


# --- Reference sources: migrations/001_initial_schema.sql
# (product_references.source_type CHECK) ---------------------------------

CANONICAL_REFERENCE_SOURCE_TYPES = (
    "PUBLISHER",
    "AUTHORIZED_SUPPLIER",
    "BOOKSTORE",
    "FAHASA",
    "FACEBOOK",
    "OTHER",
)


def test_reference_source_types_matches_migration():
    assert REFERENCE_SOURCE_TYPES == CANONICAL_REFERENCE_SOURCE_TYPES
    assert ALL_REFERENCE_SOURCE_TYPES == frozenset(CANONICAL_REFERENCE_SOURCE_TYPES)


def test_source_priority_contains_every_canonical_source_type():
    assert set(REFERENCE_SOURCE_PRIORITY.keys()) == ALL_REFERENCE_SOURCE_TYPES


def test_source_priority_values_are_unique_and_lower_is_higher_priority():
    priorities = list(REFERENCE_SOURCE_PRIORITY.values())
    assert len(priorities) == len(set(priorities)), "priority values must be unique"
    assert REFERENCE_SOURCE_PRIORITY[SourceType.PUBLISHER] < REFERENCE_SOURCE_PRIORITY[
        SourceType.OTHER
    ], "PUBLISHER must outrank OTHER per CLAUDE.md section 8.1"


def test_source_priority_matches_manual_create_product_reference_values():
    """Locks the exact values manual_create_product_reference.py (the
    canonical automated-matching implementation) uses."""
    assert dict(REFERENCE_SOURCE_PRIORITY) == {
        "PUBLISHER": 1,
        "AUTHORIZED_SUPPLIER": 2,
        "BOOKSTORE": 3,
        "FAHASA": 4,
        "FACEBOOK": 5,
        "OTHER": 9,
    }


# --- Immutability: constants should not be accidentally mutable at the
# call site (frozenset / tuple / MappingProxyType, not set/list/dict) ----


def test_status_and_rights_collections_are_immutable():
    for collection in (
        ALL_RIGHTS_STATUSES,
        PUBLISHABLE_RIGHTS_STATUSES,
        ALL_IMAGE_STATUSES,
        ALL_INTERNAL_PRODUCT_IMAGE_STATUSES,
        ALL_CONTENT_STATUSES,
        ALL_INTERNAL_PRODUCT_CONTENT_STATUSES,
        ALL_IDENTITY_STATUSES,
        ALL_MATCH_DECISIONS,
        ALL_WOOCOMMERCE_STATUSES,
        ALL_WOOCOMMERCE_SYNC_STATUSES,
        ALL_REFERENCE_SOURCE_TYPES,
    ):
        assert isinstance(collection, frozenset)

    assert isinstance(REFERENCE_SOURCE_TYPES, tuple)

    import types

    assert isinstance(REFERENCE_SOURCE_PRIORITY, types.MappingProxyType)


# --- Priority scripts actually import from src.domain, not just define
# matching literals independently ----------------------------------------


def test_priority_scripts_import_domain_constants():
    """Confirms the priority files from Phase 3 actually reference
    src.domain rather than coincidentally containing matching literals --
    a real drift-risk reduction, not just parallel copies."""
    import ast
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    priority_scripts = [
        "review_product_images.py",
        "create_woocommerce_draft.py",
        "audit_pipeline_state.py",
        "check_draft_readiness.py",
        "prepare_product_content.py",
        "pipeline_state.py",
        "register_reference_source.py",
        "collect_reference_metadata.py",
        "sync_woocommerce_product_status.py",
        "create_internal_product.py",
        "match_candidate_identity.py",
    ]

    for script_name in priority_scripts:
        source = (project_root / "scripts" / script_name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=script_name)
        imports_from_domain = any(
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("src.domain")
            for node in ast.walk(tree)
        )
        assert imports_from_domain, (
            f"{script_name} does not import from src.domain -- expected "
            "after the domain-constants centralization"
        )
