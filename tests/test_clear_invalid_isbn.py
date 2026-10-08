"""scripts/clear_invalid_isbn.py: bounded, logged removal of non-ISBN values."""
from __future__ import annotations

import pytest

import clear_invalid_isbn as cli

from support.fake_supabase import FakeSupabaseRepository

RETAILER_CODE = "2421762043452"
REAL_ISBN = "9786041234567"


def _repository() -> FakeSupabaseRepository:
    return FakeSupabaseRepository(tables={
        "internal_products": [
            {"internal_product_id": "p1", "candidate_id": "c1", "product_code": "TSYC-A",
             "isbn": RETAILER_CODE, "woocommerce_status": "DRAFT_CREATED"},
            {"internal_product_id": "p2", "candidate_id": "c2", "product_code": "TSYC-B",
             "isbn": REAL_ISBN, "woocommerce_status": "READY_FOR_DRAFT"},
        ],
        "product_references": [
            {"reference_id": "r1", "candidate_id": "c1", "reference_isbn": RETAILER_CODE, "source_type": "BOOKSTORE"},
            {"reference_id": "r2", "candidate_id": "c2", "reference_isbn": REAL_ISBN, "source_type": "FAHASA"},
            {"reference_id": "r3", "candidate_id": "c3", "reference_isbn": "8935086855171", "source_type": "BOOKSTORE"},
        ],
        "product_candidates": [
            {"candidate_id": "c1", "candidate_code": "A", "verified_isbn": None},
            {"candidate_id": "c2", "candidate_code": "B", "verified_isbn": REAL_ISBN},
        ],
        "process_logs": [],
    })


def _tables(repository):
    return repository.client.tables


def test_dry_run_writes_nothing():
    repository = _repository()
    plans = cli.main(["--all-invalid", "--max-products", "5"], repository)
    assert len(plans) == 2  # product A + product-less reference r3
    assert _tables(repository)["internal_products"][0]["isbn"] == RETAILER_CODE
    assert _tables(repository)["process_logs"] == []


def test_confirm_clears_only_invalid_values_and_logs_previous_values():
    repository = _repository()
    cli.main(["--all-invalid", "--max-products", "5", "--confirm-clear"], repository)
    products = {p["product_code"]: p for p in _tables(repository)["internal_products"]}
    references = {r["reference_id"]: r for r in _tables(repository)["product_references"]}

    assert products["TSYC-A"]["isbn"] is None
    assert products["TSYC-B"]["isbn"] == REAL_ISBN  # a real ISBN is never touched
    assert references["r1"]["reference_isbn"] is None
    assert references["r2"]["reference_isbn"] == REAL_ISBN
    assert references["r3"]["reference_isbn"] is None  # 893 barcode
    logged = [log["error_details"] for log in _tables(repository)["process_logs"]]
    assert {"previous_internal_products_isbn": RETAILER_CODE}.items() <= logged[0].items()
    assert logged[0]["previous_reference_isbn"] == {"r1": RETAILER_CODE}


def test_bound_is_enforced_before_any_write():
    repository = _repository()
    _tables(repository)["internal_products"].append(
        {"internal_product_id": "p3", "candidate_id": "c3", "product_code": "TSYC-C",
         "isbn": "6471542393457", "woocommerce_status": "FAILED"}
    )
    with pytest.raises(RuntimeError, match="--max-products"):
        cli.main(["--all-invalid", "--max-products", "1", "--confirm-clear"], repository)
    assert _tables(repository)["internal_products"][0]["isbn"] == RETAILER_CODE


def test_exact_product_code_targets_only_that_product():
    repository = _repository()
    cli.main(["--product-code", "TSYC-A", "--max-products", "1", "--confirm-clear"], repository)
    references = {r["reference_id"]: r for r in _tables(repository)["product_references"]}
    assert references["r3"]["reference_isbn"] == "8935086855171"  # not targeted
