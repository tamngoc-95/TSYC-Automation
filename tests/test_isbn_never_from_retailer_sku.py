"""Regression: a retailer SKU/barcode must never become an "ISBN".

2026-10-06 Fast Track Batch 10: NetaBooks pages expose their store code
(e.g. "2421762043452") in JSON-LD "sku"/"gtin13". extract_isbn() accepted
any 10/13-digit value, so 26 of 27 BOOKSTORE reference_isbn values were
not ISBNs; they flowed into internal_products.isbn and into the
storefront product_details line "ISBN: 2421762043452" (CLAUDE.md 2.2/2.3).
"""
from __future__ import annotations

import pytest

import collect_reference_metadata as collector
import create_internal_product as cip
import prepare_product_content as ppc
from src.domain.rules import content_rules, identity_rules, storefront_text

RETAILER_SKU = "2421762043452"
BARCODE_893 = "8935086855171"
REAL_ISBN = "9786041234567"


@pytest.mark.parametrize("value", [RETAILER_SKU, BARCODE_893, "6471542393457"])
def test_extract_isbn_rejects_retailer_codes_and_barcodes(value):
    assert collector.extract_isbn("", {"sku": value, "gtin13": value}) is None
    assert collector.extract_isbn(f"Mã ISBN: {value}\n", None) is None


def test_extract_isbn_still_accepts_a_real_isbn_after_a_sku():
    json_ld = {"sku": RETAILER_SKU, "gtin13": REAL_ISBN}
    assert collector.extract_isbn("", json_ld) == REAL_ISBN


def test_first_valid_isbn_skips_non_isbn_values():
    assert identity_rules.first_valid_isbn(None, RETAILER_SKU, BARCODE_893) is None
    assert identity_rules.first_valid_isbn(RETAILER_SKU, REAL_ISBN) == REAL_ISBN


def test_internal_product_isbn_never_takes_a_retailer_code():
    assert cip.first_valid_isbn(None, RETAILER_SKU, None) is None


def test_product_details_never_label_a_retailer_code_as_isbn():
    details = ppc.build_product_details({"author": "Chin-Ning Chu", "isbn": RETAILER_SKU, "page_count": 172})
    assert "ISBN" not in details
    details = ppc.build_product_details({"author": "Chin-Ning Chu", "isbn": REAL_ISBN})
    assert f"ISBN: {REAL_ISBN}" in details


@pytest.mark.parametrize(
    "value",
    [RETAILER_SKU, BARCODE_893, REAL_ISBN, "978-604-1-23456-7", "604123456X", "6041234567", "12345"],
)
def test_storefront_isbn_check_agrees_with_identity_rules(value):
    flagged = storefront_text.INVALID_ISBN in storefront_text.find_text_defects(f"ISBN: {value}")
    plausible = len(value.replace("-", "")) in (10, 13)
    if plausible:
        assert flagged is (not identity_rules.looks_like_valid_isbn(value))


def test_repair_drops_an_invalid_isbn_line_from_approved_details():
    existing = {
        "product_name": "Mặt Dày Tâm Đen",
        "short_description": "Có một bí quyết ẩn giấu dưới mọi thành công.",
        "long_description": "Có một bí quyết ẩn giấu dưới mọi thành công. Các vị tướng từ thời cổ đại biết bí quyết đó.",
        "author_summary": "Tác giả của ấn phẩm là Chin-Ning Chu.",
        "product_details": f"Tác giả: Chin-Ning Chu\nSố trang: 172\nISBN: {RETAILER_SKU}",
        "seo_title": "Mặt Dày Tâm Đen – Chin-Ning Chu",
        "seo_description": "Có một bí quyết ẩn giấu dưới mọi thành công.",
    }
    assert storefront_text.INVALID_ISBN in storefront_text.find_content_defects(
        existing, content_rules.CUSTOMER_FACING_FIELDS
    )["product_details"]

    plan = ppc.plan_storefront_repair(existing, {"title": "Mặt Dày Tâm Đen", "author": "Chin-Ning Chu"}, {}, [])

    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE, plan["reason"]
    assert plan["category"] == ppc.REPAIR_CATEGORY_INVALID_ISBN
    assert plan["content"]["product_details"] == "Tác giả: Chin-Ning Chu\nSố trang: 172"
