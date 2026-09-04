"""Golden tests A-D for the historical-migration image-rights policy
(explicit shop-owner business authorization, 2026-09-04).

Covers src.domain.rules.image_rules.evaluate_historical_image_rights_policy():
own-Facebook-export images are automatically STORE_OWNED; images from an
already-approved reference source type are automatically authorized at
the mapped rights status; an unknown/unconfigured source is NOT
automatically approved. Pure functions, no live Supabase/network.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rights_status import RightsStatus
from src.domain.rules import image_rules as rules


# --- A. STORE_OWNED Facebook-export image is auto-usable -------------------


def test_a_own_facebook_export_image_is_auto_store_owned():
    result = rules.evaluate_historical_image_rights_policy(
        is_own_facebook_export=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.STORE_OWNED


def test_a_own_facebook_export_wins_even_if_source_type_also_given():
    """is_own_facebook_export is decisive on its own -- the shop's own
    export never needs a reference source_type to justify STORE_OWNED."""
    result = rules.evaluate_historical_image_rights_policy(
        is_own_facebook_export=True,
        reference_source_type="UNKNOWN_SITE",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.STORE_OWNED


# --- B. Approved BOOKSTORE reference image is auto-usable -------------------


def test_b_approved_bookstore_image_is_auto_supplier_approved():
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="BOOKSTORE",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.SUPPLIER_APPROVED


def test_b_approved_publisher_image_is_auto_publisher_approved():
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="PUBLISHER",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.PUBLISHER_APPROVED


def test_b_approved_authorized_supplier_image_is_auto_supplier_approved():
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="AUTHORIZED_SUPPLIER",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.SUPPLIER_APPROVED


# --- C. Approved FAHASA reference image is auto-usable ----------------------


def test_c_approved_fahasa_image_is_auto_supplier_approved():
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="FAHASA",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["rights_status"] == RightsStatus.SUPPLIER_APPROVED


# --- D. Unknown/unconfigured domain image is NOT auto-approved -------------


def test_d_unknown_domain_image_is_not_auto_approved():
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="UNKNOWN_SITE",
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.evidence["rights_status"] == RightsStatus.RIGHTS_UNKNOWN


def test_d_no_source_information_at_all_is_not_auto_approved():
    result = rules.evaluate_historical_image_rights_policy()

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.evidence["rights_status"] == RightsStatus.RIGHTS_UNKNOWN


def test_d_other_source_type_is_not_auto_approved():
    """OTHER is a real canonical source_type (CLAUDE.md 8.1) but is
    deliberately excluded from APPROVED_REFERENCE_SOURCE_RIGHTS."""
    result = rules.evaluate_historical_image_rights_policy(
        reference_source_type="OTHER",
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
