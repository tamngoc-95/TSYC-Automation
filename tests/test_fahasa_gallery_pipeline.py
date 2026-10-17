"""Regression tests for the Fahasa gallery / preview-image pipeline
(CLAUDE.md 14.8, 2026-10-17): collection with provenance and dedupe,
per-image review records (APPROVED / REVIEW_REQUIRED / REJECTED), gallery
approval that never touches the main cover, and gallery enrichment of
TSYC-owned Woo drafts that never overrides an owner-curated gallery.
Offline: fake Supabase, injected page/download/storage/media/Woo I/O."""
from __future__ import annotations

import copy
import io
from typing import Any

import pytest
import requests
from PIL import Image

import apply_fahasa_gallery_to_woo_drafts as woo_gallery
import collect_fahasa_gallery_images as collector
import review_fahasa_gallery_images as reviewer
from src.domain.rights_status import RightsStatus
from src.domain.rules import image_rules
from src.domain.rules import woo_gallery_rules
from support.fake_supabase import FakeSupabaseRepository

CODE = "FAHASA-2026-002-CAN-0009"
PRODUCT_CODE = f"TSYC-{CODE}"
CANDIDATE_ID = "fa000000-0000-0000-0000-000000000009"
REFERENCE_ID = "ref-gallery"
SOURCE_URL_ID = "su-gallery"
PAGE = "https://www.fahasa.com/co-tich-the-gioi-hay-nhat-ba-chua-tuyet.html"
CDN = "https://cdn1.fahasa.com"
COVER = f"{CDN}/media/catalog/product/c/o/ba_chua_tuyet_bia_1.jpg"
BACK = f"{CDN}/media/catalog/product/c/o/ba_chua_tuyet_bia.jpg"
PREVIEW_1 = f"{CDN}/media/flashmagazine/images/page_images/ba_chua_tuyet/2026_10_13_1-390x510.jpg"
PREVIEW_2 = f"{CDN}/media/flashmagazine/images/page_images/ba_chua_tuyet/2026_10_13_2-390x510.jpg"
PREVIEW_3 = f"{CDN}/media/flashmagazine/images/page_images/ba_chua_tuyet/2026_10_13_3-390x510.jpg"
SWATCH = f"{CDN}/media/catalog/product/c/o/co_be_lo_lem_bia.jpg"
COVER_IMAGE_ID = "img-cover"


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (800, 600)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


IMAGE_BYTES = {
    BACK: _jpeg((10, 20, 30)),
    PREVIEW_1: _jpeg((40, 50, 60)),
    PREVIEW_2: _jpeg((70, 80, 90)),
    PREVIEW_3: _jpeg((100, 110, 120)),
}


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CODE,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_VERIFIED",
        "extracted_title": "Cổ Tích Thế Giới Hay Nhất - Bà Chúa Tuyết",
        "verified_title": "Cổ Tích Thế Giới Hay Nhất - Bà Chúa Tuyết",
        "source_evidence": {
            "origin": "FAHASA_DISCOVERY",
            "supply_status": "PREORDER_PENDING_SUPPLY_CONFIRMATION",
            "fahasa_cover_review": {
                "image_url": COVER,
                "price_label_visible": False,
                "misleading_transformation": False,
                "depicts_candidate": True,
            },
        },
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "source_type": "FAHASA",
        "source_url_id": SOURCE_URL_ID,
        "source_url": PAGE,
        "match_decision": "MATCH",
        "reference_title": "Cổ Tích Thế Giới Hay Nhất - Bà Chúa Tuyết",
        "reference_image_url": COVER,
        "raw_metadata": {"isbn_source": "json-ld"},
    }
    row.update(overrides)
    return row


def _cover_image(**overrides: Any) -> dict[str, Any]:
    row = {
        "image_id": COVER_IMAGE_ID,
        "candidate_id": CANDIDATE_ID,
        "reference_id": REFERENCE_ID,
        "source_type": "FAHASA",
        "source_url": COVER,
        "storage_bucket": "product-images",
        "storage_path": "bookstore/x/cover.jpg",
        "image_hash": "hash-cover",
        "image_role": "FRONT_COVER",
        "image_status": "VALIDATED",
        "usage_rights_status": RightsStatus.SUPPLIER_APPROVED,
        "is_selected_main_image": True,
        "is_publish_eligible": True,
        "width_pixels": 600,
        "height_pixels": 600,
        "created_at": "2026-10-09T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _repository(candidate=None, images=None, references=None, extra_images=()) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate or _candidate()],
            "product_references": list(references) if references is not None else [_reference()],
            "source_urls": [
                {"source_url_id": SOURCE_URL_ID, "source_type": "FAHASA", "source_url": PAGE, "is_authorized": True}
            ],
            "product_images": list(images) if images is not None else [_cover_image(), *extra_images],
            "internal_products": [
                {
                    "internal_product_id": "ip-9",
                    "candidate_id": CANDIDATE_ID,
                    "product_code": PRODUCT_CODE,
                    "image_status": "APPROVED",
                    "woocommerce_status": "DRAFT_CREATED",
                }
            ],
        }
    )


class FakeStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def exists(self, path: str) -> bool:
        return path in self.files

    def upload(self, content: bytes, path: str, _content_type: str) -> None:
        self.files[path] = content

    def remove(self, path: str) -> None:
        self.files.pop(path, None)


def _ports(page_urls=(BACK, PREVIEW_1, PREVIEW_2, SWATCH), image_bytes=None, storage=None, failing=()):
    image_bytes = IMAGE_BYTES if image_bytes is None else image_bytes
    storage = storage or FakeStorage()
    fetched: list[str] = []

    def read_page_images(_page_url: str, cover_url: str | None) -> list[dict[str, Any]]:
        return image_rules.classify_fahasa_page_images(cover_url, [cover_url, *page_urls], [SWATCH])

    def fetch_image(url: str, referer: str) -> tuple[bytes, str]:
        assert referer == PAGE
        fetched.append(url)
        if url in failing:
            raise RuntimeError("HTTP 404")
        return image_bytes[url], "image/jpeg"

    ports = collector.CollectorPorts(
        read_page_images=read_page_images,
        fetch_image=fetch_image,
        storage_exists=storage.exists,
        storage_upload=storage.upload,
        storage_remove=storage.remove,
    )
    ports.fetched = fetched  # type: ignore[attr-defined]
    return ports


def _rows(repository, table):
    return repository.client.tables.setdefault(table, [])


def _by_url(repository) -> dict[str, dict[str, Any]]:
    return {row["source_url"]: row for row in _rows(repository, "product_images")}


def _verdict(url: str, **overrides: Any) -> dict[str, Any]:
    verdict = {
        "candidate_code": CODE,
        "image_url": url,
        "depicts_candidate": True,
        "edition_matches": True,
        "price_label_visible": False,
        "misleading_transformation": False,
        "relevant_preview": True if "flashmagazine" in url else None,
        "image_kind": "INSIDE_PAGE" if "flashmagazine" in url else "BACK_COVER",
        "reviewed_by": "Claude Code visual review",
        "reviewed_at": "2026-10-17",
    }
    verdict.update(overrides)
    return verdict


def _collected(page_urls=(BACK, PREVIEW_1, PREVIEW_2, SWATCH), **repository_kwargs):
    repository = _repository(**repository_kwargs)
    report = collector.run_collect(repository, _ports(page_urls), CODE, dry_run=False)
    assert report["result"] == collector.COLLECTED, report
    return repository


# --- page classification ------------------------------------------------------------


def test_preview_pages_are_classified_in_source_order():
    entries = image_rules.classify_fahasa_page_images(
        COVER, [COVER, BACK, PREVIEW_1, f"{CDN}/skin/frontend/x/ring_loader.gif", SWATCH], [SWATCH]
    )
    assert [(entry["position"], entry["role"]) for entry in entries] == [
        (1, image_rules.FAHASA_IMAGE_ROLE_COVER),
        (2, image_rules.FAHASA_IMAGE_ROLE_SUPPLEMENTARY),
        (3, image_rules.FAHASA_IMAGE_ROLE_PREVIEW),
        (4, image_rules.FAHASA_IMAGE_ROLE_OTHER_PRODUCT),
    ]


# --- collection ------------------------------------------------------------------------


def test_multiple_gallery_and_preview_images_are_registered_with_provenance():
    repository = _repository()
    ports = _ports()
    report = collector.run_collect(repository, ports, CODE, dry_run=False)

    statuses = {record["url"]: record["download_status"] for record in report["images"]}
    assert statuses == {
        COVER: collector.ALREADY_REGISTERED,
        BACK: collector.DOWNLOADED,
        PREVIEW_1: collector.DOWNLOADED,
        PREVIEW_2: collector.DOWNLOADED,
        SWATCH: collector.SKIPPED_OTHER_PRODUCT,
    }
    assert SWATCH not in ports.fetched and COVER not in ports.fetched  # never downloaded

    rows = _by_url(repository)
    for url in (BACK, PREVIEW_1, PREVIEW_2):
        row = rows[url]
        assert row["source_type"] == "FAHASA" and row["reference_id"] == REFERENCE_ID
        assert row["source_photo_url"] == PAGE and row["source_url_id"] == SOURCE_URL_ID
        assert row["image_hash"] and row["width_pixels"] == 800 and row["height_pixels"] == 600
        assert row["is_publish_eligible"] is False and row["is_selected_main_image"] is False
        assert "image_status" not in row and "usage_rights_status" not in row  # DB safe defaults
    assert rows[PREVIEW_1]["image_role"] == "INSIDE_PAGE"
    assert rows[BACK]["image_role"] is None

    raw_metadata = _rows(repository, "product_references")[0]["raw_metadata"]
    assert raw_metadata["isbn_source"] == "json-ld"  # other keys kept
    manifest = raw_metadata["page_images"]
    assert [record["position"] for record in manifest] == [1, 2, 3, 4, 5]
    assert all(record.get("sha256") for record in manifest if record["download_status"] == collector.DOWNLOADED)
    assert raw_metadata["page_images_collection"]["authorization"]["adopted"] == "2026-10-17"


def test_failed_download_is_recorded_and_the_rest_continue():
    repository = _repository()
    report = collector.run_collect(repository, _ports(failing=(PREVIEW_1,)), CODE, dry_run=False)
    statuses = {record["url"]: record for record in report["images"]}
    assert statuses[PREVIEW_1]["download_status"] == collector.DOWNLOAD_FAILED
    assert "HTTP 404" in statuses[PREVIEW_1]["error"]
    assert statuses[PREVIEW_2]["download_status"] == collector.DOWNLOADED
    assert PREVIEW_1 not in _by_url(repository)


def test_duplicate_content_is_registered_once_and_never_relinked():
    same = dict(IMAGE_BYTES, **{PREVIEW_2: IMAGE_BYTES[PREVIEW_1]})
    foreign = {
        "image_id": "img-foreign",
        "candidate_id": "someone-else",
        "source_url": "x",
        "image_hash": collector.downloader.calculate_sha256(IMAGE_BYTES[BACK]),
    }
    repository = _repository(extra_images=[foreign])
    report = collector.run_collect(repository, _ports(image_bytes=same), CODE, dry_run=False)
    statuses = {record["url"]: record for record in report["images"]}
    assert statuses[PREVIEW_2]["download_status"] == collector.DUPLICATE
    assert statuses[PREVIEW_2]["image_id"] == statuses[PREVIEW_1]["image_id"]
    assert statuses[BACK]["download_status"] == collector.DUPLICATE_OTHER_CANDIDATE
    assert BACK not in _by_url(repository) and PREVIEW_2 not in _by_url(repository)


def test_repeated_collection_is_idempotent():
    repository = _collected()
    count = len(_rows(repository, "product_images"))
    ports = _ports()
    report = collector.run_collect(repository, ports, CODE, dry_run=False)
    assert len(_rows(repository, "product_images")) == count
    assert ports.fetched == []
    assert {record["download_status"] for record in report["images"]} == {
        collector.ALREADY_REGISTERED,
        collector.SKIPPED_OTHER_PRODUCT,
    }


def test_dry_run_and_missing_reference_write_nothing():
    repository = _repository()
    report = collector.run_collect(repository, _ports(), CODE, dry_run=True)
    assert report["result"] == collector.DRY_RUN
    assert len(_rows(repository, "product_images")) == 1
    assert "page_images" not in _rows(repository, "product_references")[0]["raw_metadata"]

    no_match = _repository(references=[_reference(match_decision="POSSIBLE_MATCH")])
    assert collector.run_collect(no_match, _ports(), CODE, dry_run=False)["result"] == collector.LOCAL_NOT_READY


def test_page_without_gallery_images_records_only_the_cover():
    repository = _repository()
    report = collector.run_collect(repository, _ports(page_urls=()), CODE, dry_run=False)
    assert [record["download_status"] for record in report["images"]] == [collector.ALREADY_REGISTERED]


# --- review records -----------------------------------------------------------------------


def _review(repository, verdicts, dry_run=False):
    return reviewer.run_review(repository, CODE, verdicts, dry_run=dry_run)


def test_partial_gallery_applies_only_approved_images():
    repository = _collected(page_urls=(BACK, PREVIEW_1, PREVIEW_2, PREVIEW_3, SWATCH))
    report = _review(
        repository,
        [
            _verdict(BACK),
            _verdict(PREVIEW_1),
            _verdict(PREVIEW_2, price_label_visible=True),
            # PREVIEW_3: no verdict at all
        ],
    )
    assert report["outcome"] == reviewer.REVIEWED
    records = {record["image_url"]: record for record in report["records"]}
    assert records[BACK]["classification"] == image_rules.FAHASA_REVIEW_APPROVED
    assert records[PREVIEW_1]["classification"] == image_rules.FAHASA_REVIEW_APPROVED
    assert records[PREVIEW_2]["classification"] == image_rules.FAHASA_REVIEW_REJECTED
    assert "price label" in " ".join(records[PREVIEW_2]["reasons"])
    assert records[PREVIEW_3]["classification"] == image_rules.FAHASA_REVIEW_REQUIRED
    assert records[PREVIEW_3]["reasons"]  # explicit reason, never a silent pass

    rows = _by_url(repository)
    for url, role in ((BACK, "BACK_COVER"), (PREVIEW_1, "INSIDE_PAGE")):
        assert rows[url]["image_status"] == "VALIDATED"
        assert rows[url]["usage_rights_status"] == RightsStatus.SUPPLIER_APPROVED
        assert rows[url]["is_publish_eligible"] is True and rows[url]["is_selected_main_image"] is False
        assert rows[url]["image_role"] == role
    assert rows[PREVIEW_2]["image_status"] == "REJECTED" and rows[PREVIEW_2]["is_publish_eligible"] is False
    assert "image_status" not in rows[PREVIEW_3] and rows[PREVIEW_3]["is_publish_eligible"] is False
    assert rows[COVER]["is_selected_main_image"] is True  # main untouched

    evidence = _rows(repository, "product_candidates")[0]["source_evidence"]
    assert len(evidence["fahasa_image_review_records"]) == 5  # every collected image, cover included
    assert evidence["fahasa_cover_review"]["image_url"] == COVER  # never overwritten


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"edition_matches": False}, "different edition"),
        ({"depicts_candidate": False}, "does not depict"),
        ({"misleading_transformation": True}, "misleading"),
        ({"relevant_preview": False}, "not relevant"),
        ({"visual_duplicate": True}, "repeats another image"),
    ],
)
def test_confirmed_negative_verdicts_reject_the_image(overrides, reason):
    repository = _collected(page_urls=(PREVIEW_1,))
    report = _review(repository, [_verdict(PREVIEW_1, **overrides)])
    record = next(record for record in report["records"] if record["image_url"] == PREVIEW_1)
    assert record["classification"] == image_rules.FAHASA_REVIEW_REJECTED
    assert reason in " ".join(record["reasons"])


@pytest.mark.parametrize("missing", ["edition_matches", "relevant_preview", "depicts_candidate", "price_label_visible"])
def test_unknown_verdict_fields_need_review(missing):
    repository = _collected(page_urls=(PREVIEW_1,))
    report = _review(repository, [_verdict(PREVIEW_1, **{missing: None})])
    record = next(record for record in report["records"] if record["image_url"] == PREVIEW_1)
    assert record["classification"] == image_rules.FAHASA_REVIEW_REQUIRED
    assert _by_url(repository)[PREVIEW_1]["is_publish_eligible"] is False


def test_duplicate_image_rows_are_rejected():
    duplicate = _cover_image(
        image_id="img-dup",
        source_url=COVER.replace("cdn1", "cdn0"),
        is_selected_main_image=False,
        is_publish_eligible=False,
        image_status="PENDING",
        usage_rights_status="RIGHTS_UNKNOWN",
        created_at="2026-10-17T10:00:00+00:00",
    )
    assert image_rules.find_duplicate_images([_cover_image(), duplicate]) == {"img-dup": COVER_IMAGE_ID}
    record = image_rules.review_fahasa_image(duplicate, _reference(), _candidate(), duplicate_of=COVER_IMAGE_ID)
    assert record["classification"] == image_rules.FAHASA_REVIEW_REJECTED


def test_interior_or_preview_page_is_never_main_eligible():
    repository = _collected(page_urls=(PREVIEW_1,))
    reference = _rows(repository, "product_references")[0]
    candidate = _candidate()
    candidate["source_evidence"]["fahasa_image_reviews"] = [_verdict(PREVIEW_1)]
    preview_row = _by_url(repository)[PREVIEW_1]
    preview = image_rules.review_fahasa_image(preview_row, reference, candidate)
    assert preview["classification"] == image_rules.FAHASA_REVIEW_APPROVED and preview["main_eligible"] is False

    candidate["source_evidence"]["fahasa_cover_review"]["image_kind"] = "INSIDE_PAGE"
    cover = image_rules.review_fahasa_image(_cover_image(), reference, candidate)
    assert cover["main_eligible"] is False
    assert image_rules.review_fahasa_image(_cover_image(), reference, _candidate())["main_eligible"] is True


def test_main_cover_failure_isolates_the_candidate():
    candidate = _candidate()
    candidate["source_evidence"]["fahasa_cover_review"]["price_label_visible"] = True
    repository = _collected(page_urls=(BACK,), candidate=candidate)
    report = _review(repository, [_verdict(BACK)])
    assert report["outcome"] == reviewer.MAIN_COVER_FAILED
    assert report["planned_updates"] == []
    assert _by_url(repository)[BACK]["is_publish_eligible"] is False
    assert _by_url(repository)[COVER]["is_selected_main_image"] is True  # never silently demoted


def test_no_approved_main_leaves_selection_to_the_image_stage():
    repository = _collected(page_urls=(BACK,), images=[_cover_image(is_selected_main_image=False, image_status="PENDING")])
    report = _review(repository, [_verdict(BACK)])
    assert report["outcome"] == reviewer.NO_APPROVED_MAIN
    assert report["planned_updates"] == []


def test_repeated_review_is_idempotent():
    repository = _collected()
    verdicts = [_verdict(BACK), _verdict(PREVIEW_1), _verdict(PREVIEW_2)]
    assert _review(repository, verdicts)["applied_updates"] == 3
    assert _review(repository, verdicts)["planned_updates"] == []


def test_invalid_or_foreign_verdicts_are_refused():
    repository = _collected(page_urls=(BACK,))
    report = _review(
        repository,
        [
            _verdict(f"{CDN}/media/catalog/product/x/unknown.jpg"),
            _verdict(COVER),
            _verdict(BACK, depicts_candidate="yes"),
            _verdict(BACK, candidate_code="FAHASA-2026-002-CAN-0001"),  # another candidate's verdict: ignored
        ],
    )
    problems = [problem for refused in report["refused_verdicts"] for problem in refused["problems"]]
    assert any("not a registered" in problem for problem in problems)
    assert any("never overwritten" in problem for problem in problems)
    assert any("depicts_candidate" in problem for problem in problems)
    assert len(report["refused_verdicts"]) == 3


# --- Woo draft gallery enrichment -----------------------------------------------------------


class FakeWoo:
    def __init__(self, product: dict[str, Any], put_error: Exception | None = None, apply_on_error: bool = False):
        self.product = product
        self.put_error = put_error
        self.apply_on_error = apply_on_error
        self.puts: list[dict[str, Any]] = []

    def get(self, product_id):
        return copy.deepcopy(self.product) if str(product_id) == str(self.product["id"]) else None

    def update_images(self, product_id, payload):
        assert set(payload) == {"images"}
        self.puts.append(payload)
        if self.put_error is not None:
            if self.apply_on_error:
                self.product["images"] = payload["images"]
            raise self.put_error
        self.product["images"] = payload["images"]
        return copy.deepcopy(self.product)


class FakeMedia:
    def __init__(self, fail_after: int | None = None):
        self.next_id = 901
        self.uploads: list[str] = []
        self.fail_after = fail_after

    def ports(self) -> woo_gallery.MediaPorts:
        return woo_gallery.MediaPorts(verify_media=lambda _media_id: True, upload_media=self.upload)

    def upload(self, _product, image, _position):
        if self.fail_after is not None and len(self.uploads) >= self.fail_after:
            raise RuntimeError("WordPress media upload failed")
        self.uploads.append(str(image["image_id"]))
        media_id, self.next_id = self.next_id, self.next_id + 1
        return {"source_image_id": str(image["image_id"]), "wordpress_media_id": media_id}


def _remote(**overrides: Any) -> dict[str, Any]:
    product = {
        "id": 5000,
        "sku": PRODUCT_CODE,
        "status": "draft",
        "name": "Bà Chúa Tuyết",
        "description": "<p>Mô tả.</p>",
        "short_description": "<p>Ngắn.</p>",
        "regular_price": "",
        "sale_price": "",
        "price": "",
        "manage_stock": True,
        "stock_quantity": 0,
        "backorders": "yes",
        "stock_status": "onbackorder",
        "attributes": [],
        "images": [{"id": 900}],
        "meta_data": [{"key": "_tsyc_internal_product_id", "value": "ip-9"}],
    }
    product.update(overrides)
    return product


def _woo_repository(gallery_urls=(BACK, PREVIEW_1)):
    repository = _collected()
    _review(repository, [_verdict(url) for url in gallery_urls])
    _rows(repository, "woocommerce_product_syncs").append(
        {
            "sync_id": "sync-9",
            "internal_product_id": "ip-9",
            "woocommerce_product_id": 5000,
            "product_sku": PRODUCT_CODE,
            "woocommerce_status": "DRAFT_CREATED",
            "response_payload": {
                "uploaded_media": [{"source_image_id": COVER_IMAGE_ID, "wordpress_media_id": 900}],
                "media_upload_completed": True,
                "woocommerce_product": {"id": 5000},
            },
        }
    )
    return repository


def _apply(repository, client, media, dry_run=False):
    return woo_gallery.run_apply(repository, client, media.ports(), PRODUCT_CODE, dry_run=dry_run)


def test_approved_gallery_images_are_added_to_a_tsyc_draft():
    repository = _woo_repository()
    client, media = FakeWoo(_remote()), FakeMedia()
    report = _apply(repository, client, media)
    assert report["result"] == woo_gallery.UPDATED, report
    assert report["media_ids"] == [900, 901, 902]  # featured cover stays first
    assert [image["id"] for image in client.product["images"]] == [900, 901, 902]
    assert client.product["status"] == "draft" and client.product["regular_price"] == ""
    assert client.product["backorders"] == "yes"

    payload = _rows(repository, "woocommerce_product_syncs")[0]["response_payload"]
    assert payload["applied_image_media_ids"] == [900, 901, 902]
    assert payload["woocommerce_product"] == {"id": 5000}  # creation record kept
    assert {media["source_image_id"] for media in payload["uploaded_media"]} >= {COVER_IMAGE_ID}


def test_repeated_enrichment_is_a_no_op_without_new_uploads():
    repository = _woo_repository()
    client, media = FakeWoo(_remote()), FakeMedia()
    _apply(repository, client, media)
    second = FakeMedia()
    report = _apply(repository, client, second)
    assert report["result"] == woo_gallery.NO_OP
    assert second.uploads == [] and len(client.puts) == 1


@pytest.mark.parametrize(
    ("remote_overrides", "expected"),
    [
        ({"images": [{"id": 900}, {"id": 777}]}, woo_gallery_rules.OWNER_CURATED_IMAGES),
        ({"images": [{"id": 777}]}, woo_gallery_rules.OWNER_CURATED_IMAGES),
        ({"status": "publish"}, woo_gallery_rules.NOT_A_DRAFT),
        ({"status": "trash"}, woo_gallery_rules.NOT_A_DRAFT),
        ({"sku": "OTHER"}, woo_gallery_rules.REMOTE_IDENTITY_MISMATCH),
        ({"meta_data": []}, woo_gallery_rules.NOT_TSYC_PRODUCT),
    ],
)
def test_owner_maintained_or_foreign_products_are_never_written(remote_overrides, expected):
    repository = _woo_repository()
    client, media = FakeWoo(_remote(**remote_overrides)), FakeMedia()
    report = _apply(repository, client, media)
    assert report["result"] == expected
    assert client.puts == [] and media.uploads == []


def test_uncertain_put_is_never_retried():
    repository = _woo_repository()
    client, media = FakeWoo(_remote(), put_error=requests.Timeout("timed out")), FakeMedia()
    report = _apply(repository, client, media)
    assert report["result"] == woo_gallery.UNCERTAIN
    assert len(client.puts) == 1


def test_timeout_after_a_successful_put_is_reconciled_on_re_read():
    repository = _woo_repository()
    client = FakeWoo(_remote(), put_error=requests.ConnectionError("reset"), apply_on_error=True)
    report = _apply(repository, client, FakeMedia())
    assert report["result"] == woo_gallery.UPDATED
    assert len(client.puts) == 1


def test_failed_media_upload_keeps_progress_and_the_rerun_reuses_it():
    repository = _woo_repository()
    client = FakeWoo(_remote())
    first = FakeMedia(fail_after=1)
    assert _apply(repository, client, first)["result"] == woo_gallery.MEDIA_UPLOAD_FAILED
    assert client.puts == [] and len(first.uploads) == 1

    second = FakeMedia()
    second.next_id = 950
    report = _apply(repository, client, second)
    assert report["result"] == woo_gallery.UPDATED
    assert len(second.uploads) == 1  # only the image that failed before
    assert report["media_ids"][:2] == [900, 901]


def test_dry_run_and_products_without_gallery_are_not_written():
    repository = _woo_repository()
    client, media = FakeWoo(_remote()), FakeMedia()
    assert _apply(repository, client, media, dry_run=True)["result"] == woo_gallery.DRY_RUN
    assert client.puts == [] and media.uploads == []

    bare = _woo_repository(gallery_urls=())
    assert _apply(bare, FakeWoo(_remote()), FakeMedia())["result"] == woo_gallery.NO_GALLERY_IMAGES


@pytest.mark.parametrize("local_status", ["PUBLISHED", "REMOTE_REMOVED", "READY_FOR_DRAFT"])
def test_owner_published_or_removed_product_is_refused_before_any_remote_call(local_status):
    repository = _woo_repository()
    _rows(repository, "internal_products")[0]["woocommerce_status"] = local_status
    client = FakeWoo(_remote())
    client.get = lambda _product_id: pytest.fail("no remote read for a non-draft local product")
    report = _apply(repository, client, FakeMedia())
    assert report["result"] == woo_gallery.LOCAL_NOT_READY
    assert local_status in report["detail"]


def test_live_non_preorder_product_is_not_ready():
    repository = _woo_repository()
    _rows(repository, "product_candidates")[0]["source_evidence"].pop("supply_status")
    report = _apply(repository, FakeWoo(_remote()), FakeMedia())
    assert report["result"] == woo_gallery.LOCAL_NOT_READY


def test_gallery_payload_carries_only_images():
    assert woo_gallery_rules.build_gallery_payload([900, 901]) == {"images": [{"id": 900}, {"id": 901}]}
    with pytest.raises(RuntimeError):
        woo_gallery.WooGalleryClient("https://x", "wc/v3", "k", "s", 5).update_images(1, {"images": [], "status": "publish"})


# --- argument bounds --------------------------------------------------------------------------


def test_every_writer_requires_an_exact_bounded_allowlist():
    with pytest.raises(SystemExit):
        collector.parse_arguments(["--candidate-code", CODE, "--max-candidates", "11"])
    with pytest.raises(SystemExit):
        reviewer.parse_arguments(["--candidate-code", CODE, "--candidate-code", CODE, "--max-candidates", "2"])
    with pytest.raises(SystemExit):
        woo_gallery.parse_arguments(["--product-code", PRODUCT_CODE, "--product-code", "B", "--max-products", "1"])
    assert collector.parse_arguments(["--candidate-code", CODE, "--max-candidates", "1"]).confirm_collect is False
