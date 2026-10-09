"""Regression tests for the owner-authorized Fahasa cover policy
(CLAUDE.md 14.8, adopted 2026-10-09) and the Fahasa-origin candidate path."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import download_bookstore_product_image as downloader
import import_fahasa_candidates as importer
import run_batch
from pipeline_state import derive_candidate_state, load_candidate_bundle
from src.domain.candidate_origin import is_fahasa_discovery_candidate_code
from src.domain.decisions import Outcome
from src.domain.rights_status import RightsStatus
from src.domain.rules import catalog_identity_index as idx
from src.domain.rules import image_rules

from support.fake_supabase import FakeSupabaseRepository

CODE = "FAHASA-2026-001-CAN-0001"
CANDIDATE_ID = "fa000000-0000-0000-0000-000000000001"
PAGE = "https://www.fahasa.com/biet-doi-cho-bay.html"
COVER = "https://cdn1.fahasa.com/media/catalog/product/b/i/biet_doi_cho_bay_bia.jpg"


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CODE,
        "raw_page_id": None,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_VERIFIED",
        "extracted_title": "Biệt Đội Chó Bay",
        "verified_title": "Biệt Đội Chó Bay",
        "source_evidence": {
            "fahasa_cover_review": {
                "image_url": COVER,
                "price_label_visible": False,
                "misleading_transformation": False,
                "depicts_candidate": True,
            }
        },
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": "ref-1",
        "candidate_id": CANDIDATE_ID,
        "source_type": "FAHASA",
        "source_url_id": "su-1",
        "source_url": PAGE,
        "match_decision": "MATCH",
        "reference_title": "Biệt Đội Chó Bay",
        "reference_image_url": COVER,
    }
    row.update(overrides)
    return row


def _image(**overrides: Any) -> dict[str, Any]:
    row = {
        "image_id": "img-1",
        "candidate_id": CANDIDATE_ID,
        "reference_id": "ref-1",
        "source_type": "FAHASA",
        "source_url": COVER,
        "storage_path": "bookstore/x.jpg",
        "image_status": "PENDING",
        "usage_rights_status": "RIGHTS_UNKNOWN",
        "width_pixels": 600,
        "height_pixels": 600,
        "created_at": "2026-10-09T10:00:00+00:00",
    }
    row.update(overrides)
    return row


# --- policy gate ----------------------------------------------------------------


def test_matching_fahasa_cover_is_authorized_with_provenance():
    decision = image_rules.evaluate_fahasa_cover_authorization(_image(), _reference(), _candidate())
    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["rights_status"] == RightsStatus.SUPPLIER_APPROVED
    assert decision.evidence["authorization"]["adopted"] == "2026-10-09"
    assert decision.evidence["reference_page_url"] == PAGE


def test_cdn_host_or_query_difference_is_still_the_same_cover():
    image = _image(source_url=COVER.replace("cdn1", "cdn0") + "?v=2")
    decision = image_rules.evaluate_fahasa_cover_authorization(image, _reference(), _candidate())
    assert decision.outcome == Outcome.AUTO_PASS


@pytest.mark.parametrize(
    ("image_overrides", "reference_overrides", "candidate_overrides", "expected"),
    [
        ({"source_type": "BOOKSTORE"}, {}, {}, "image source_type is not FAHASA"),
        ({}, {"source_type": "BOOKSTORE"}, {}, "reference source_type is not FAHASA"),
        ({}, {"match_decision": "POSSIBLE_MATCH"}, {}, "not this candidate's MATCH"),
        ({}, {"candidate_id": "someone-else"}, {}, "another candidate"),
        ({}, {"source_url": "https://www.netabooks.vn/x"}, {}, "not on fahasa.com"),
        ({"source_url": "https://evil.example/cover.jpg"}, {}, {}, "not served by Fahasa"),
        ({"source_url": "https://cdn1.fahasa.com/other.jpg"}, {}, {}, "own cover image"),
        ({"width_pixels": 300, "height_pixels": 600}, {}, {}, "too small"),
        ({"width_pixels": None}, {}, {}, "dimensions were not recorded"),
        ({"storage_path": None}, {}, {}, "not stored"),
        ({"image_status": "REJECTED"}, {}, {}, "image_status is REJECTED"),
        ({}, {"reference_title": "Biệt Đội Chó Bay - Tập 2"}, {}, "series volume differs"),
        ({}, {"reference_title": "Combo Biệt Đội Chó Bay"}, {}, "sellable unit differs"),
    ],
)
def test_each_gate_isolates_the_image(image_overrides, reference_overrides, candidate_overrides, expected):
    decision = image_rules.evaluate_fahasa_cover_authorization(
        _image(**image_overrides), _reference(**reference_overrides), _candidate(**candidate_overrides)
    )
    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert expected in decision.reason


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("price_label_visible", True, "price label"),
        ("misleading_transformation", True, "misleading image transformation"),
        ("depicts_candidate", False, "does not depict this book"),
    ],
)
def test_visual_review_failures_isolate_the_image(field, value, expected):
    review = dict(_candidate()["source_evidence"]["fahasa_cover_review"], **{field: value})
    candidate = _candidate(source_evidence={"fahasa_cover_review": review})
    decision = image_rules.evaluate_fahasa_cover_authorization(_image(), _reference(), candidate)
    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert expected in decision.reason


def test_missing_reference_is_never_authorized():
    decision = image_rules.evaluate_fahasa_cover_authorization(_image(), None, _candidate())
    assert decision.outcome == Outcome.REVIEW_REQUIRED


# --- state machine ---------------------------------------------------------------


def _product(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": "ip-1",
        "candidate_id": CANDIDATE_ID,
        "product_code": f"TSYC-{CODE}",
        "content_status": "PENDING",
        "image_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
    }
    row.update(overrides)
    return row


def _state(candidate, images=(), references=(), code=CODE):
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "internal_products": [_product(candidate_id=candidate["candidate_id"])],
            "product_images": list(images),
            "product_references": list(references),
        }
    )
    return derive_candidate_state(load_candidate_bundle(repository, code))


def test_fahasa_candidate_without_images_downloads_its_cover():
    state = _state(_candidate(), references=[_reference()])
    assert state.derived_state == "IMAGE_DOWNLOAD_PENDING_FAHASA_COVER"
    assert state.human_gate is False


def test_live_facebook_candidate_without_images_keeps_the_human_gate():
    code = "FB-2026-001-CAN-0099"
    state = _state(_candidate(candidate_code=code), references=[_reference()], code=code)
    assert state.derived_state == "IMAGE_PENDING"
    assert state.human_gate is True


def test_authorized_cover_routes_to_automatic_approval():
    state = _state(_candidate(), images=[_image()], references=[_reference()])
    assert state.derived_state == "IMAGE_APPROVAL_PENDING_FAHASA_COVER"
    assert state.auto_main_image_id == "img-1"
    assert state.auto_rights_status == RightsStatus.SUPPLIER_APPROVED


def test_failing_cover_stays_at_rights_review_with_reason():
    state = _state(_candidate(), images=[_image(width_pixels=200)], references=[_reference()])
    assert state.derived_state == "RIGHTS_REVIEW_REQUIRED"
    assert state.human_gate is True
    assert "too small" in state.human_gate_reason


def test_fahasa_candidate_ready_for_draft_keeps_the_woo_human_gate():
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [_candidate()],
            "internal_products": [_product(woocommerce_status="READY_FOR_DRAFT", content_status="APPROVED", image_status="APPROVED")],
        }
    )
    state = derive_candidate_state(load_candidate_bundle(repository, CODE))
    assert state.derived_state == "READY_FOR_DRAFT"
    assert state.human_gate is True


def test_dispatch_entries_exist_and_never_pass_price_or_publish():
    for name in ("IMAGE_DOWNLOAD_PENDING_FAHASA_COVER", "IMAGE_APPROVAL_PENDING_FAHASA_COVER"):
        assert name in run_batch.AUTOMATABLE_DISPATCH
    state = _state(_candidate(), images=[_image()], references=[_reference()])
    args = run_batch.AUTOMATABLE_DISPATCH["IMAGE_APPROVAL_PENDING_FAHASA_COVER"].build_args(state)
    assert args[args.index("--rights-status") + 1] == RightsStatus.SUPPLIER_APPROVED
    assert not any("price" in arg or "publish" in arg for arg in args)


def test_candidate_origin_prefix():
    assert is_fahasa_discovery_candidate_code(CODE)
    assert not is_fahasa_discovery_candidate_code("FB-HIST-2026-001-CAN-0001")
    assert not is_fahasa_discovery_candidate_code(None)


# --- downloader dimensions -----------------------------------------------------


def test_downloader_measures_image_dimensions():
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480)).save(buffer, format="JPEG")
    assert downloader.measure_image_dimensions(buffer.getvalue()) == {"width_pixels": 640, "height_pixels": 480}
    assert downloader.measure_image_dimensions(b"not an image") == {}


# --- importer helpers ------------------------------------------------------------


def _classification(**overrides: Any) -> dict[str, Any]:
    row = {
        "classification": idx.NEW_CONFIRMED,
        "reason": "No match in any checked source.",
        "identity_strength": idx.IDENTITY_MODERATE,
        "sellable_unit": "SINGLE_BOOK",
    }
    row.update(overrides)
    return row


def test_only_new_confirmed_single_books_with_identity_are_imported():
    assert importer.eligibility_problems(_classification()) == []
    assert importer.eligibility_problems(_classification(classification=idx.POSSIBLE_DUPLICATE))
    assert importer.eligibility_problems(_classification(classification=idx.REMOTE_REMOVED))
    assert importer.eligibility_problems(_classification(identity_strength=idx.IDENTITY_WEAK))
    assert importer.eligibility_problems(_classification(sellable_unit="BOOK_SET"))


def test_cover_review_must_be_explicit_and_for_this_cover():
    record = {"image": {"url": COVER}}
    good = {"image_url": COVER, "price_label_visible": False, "misleading_transformation": False, "depicts_candidate": True}
    assert importer.validate_cover_review(good, record) == []
    assert importer.validate_cover_review(None, record)
    assert importer.validate_cover_review(dict(good, image_url="https://cdn1.fahasa.com/x.jpg"), record)
    assert importer.validate_cover_review(dict(good, price_label_visible=None), record)


def test_candidate_payload_is_live_single_book_without_price():
    record = {"title": "Biệt Đội Chó Bay", "author": "Huỳnh Long, Mai Chi", "isbn": None, "source_url": PAGE, "image": {"url": COVER}}
    evidence = importer.build_source_evidence(record, _classification(), {"image_url": COVER}, Path("r.json"), 23, None, {})
    payload = importer.build_candidate_payload("batch-1", CODE, "su-1", record, evidence)
    assert payload["candidate_type"] == "SINGLE_BOOK"
    assert payload["source_url_id"] == "su-1"
    assert payload["source_evidence"]["supply_status"] == "PREORDER_PENDING_SUPPLY_CONFIRMATION"
    assert payload["source_evidence"]["origin"] == "FAHASA_DISCOVERY"
    assert "identity_status" not in payload  # stays the DB default IDENTITY_PENDING
    flattened = repr({k: v for k, v in payload.items() if k != "source_evidence"}) + repr(
        {k: v for k, v in evidence.items() if k != "pricing_note"}
    )
    assert "price" not in flattened.lower()


def test_register_command_selects_the_origin_page_as_authorized_fahasa_reference():
    command = importer.build_register_command(CODE, "FAHASA-2026-001", PAGE)
    assert command[command.index("--source-type") + 1] == "FAHASA"
    assert command[command.index("--source-url") + 1] == PAGE
    assert {"--authorized", "--select-for-crawl", "--non-interactive", "--confirm-register"} <= set(command)


def test_candidate_codes_are_sequential_per_batch():
    assert importer.next_candidate_code([], "FAHASA-2026-001") == "FAHASA-2026-001-CAN-0001"
    assert importer.next_candidate_code(
        ["FAHASA-2026-001-CAN-0001", "FAHASA-2026-001-CAN-0007", "OTHER"], "FAHASA-2026-001"
    ) == "FAHASA-2026-001-CAN-0008"


def test_importer_requires_fahasa_batch_and_bounded_allowlist():
    base = ["--discovery-report", "r.json", "--cover-reviews", "c.json", "--discovery-index", "1"]
    assert importer.parse_arguments(base + ["--max-candidates", "1"]).batch_code == "FAHASA-2026-001"
    with pytest.raises(SystemExit):
        importer.parse_arguments(base + ["--max-candidates", "1", "--batch-code", "FB-2026-001"])
    with pytest.raises(SystemExit):
        importer.parse_arguments(base + ["--discovery-index", "2", "--max-candidates", "1"])
    with pytest.raises(SystemExit):
        importer.parse_arguments(base + ["--max-candidates", "50"])
