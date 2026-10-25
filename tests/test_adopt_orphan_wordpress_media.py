"""Regression tests for orphan WordPress media adoption
(scripts/adopt_orphan_wordpress_media.py, 2026-10-25).

Production case: FAHASA-2026-003-CAN-0006, whose 2026-10-24 create failed
after uploading media #4999..#5003 without recording them on the sync row.
A retry must reuse them, never upload duplicates -- but only media proven
to be this product's images is adopted. No network, no Supabase, no Woo.
"""
from __future__ import annotations

import pytest

import adopt_orphan_wordpress_media as adopt
import create_woocommerce_draft as cwd
from src.domain.woocommerce_status import WooCommerceSyncStatus
from support.fake_supabase import FakeSupabaseRepository

CODE = "TSYC-FAHASA-2026-003-CAN-0006"
PRODUCT = {"internal_product_id": "ip-1", "candidate_id": "cand-1", "product_code": CODE}
COVER = {"image_id": "img-cover", "image_hash": "a" * 64, "image_role": "FRONT_COVER",
         "is_selected_main_image": True, "storage_bucket": "b", "storage_path": "p/cover.webp", "mime_type": "image/webp"}
PAGE1 = {"image_id": "img-p1", "image_hash": "b" * 64, "image_role": "INSIDE_PAGE",
         "is_selected_main_image": False, "storage_bucket": "b", "storage_path": "p/1.webp", "mime_type": "image/webp"}
PAGE2 = {"image_id": "img-p2", "image_hash": "c" * 64, "image_role": "INSIDE_PAGE",
         "is_selected_main_image": False, "storage_bucket": "b", "storage_path": "p/2.webp", "mime_type": "image/webp"}
IMAGES = [COVER, PAGE1, PAGE2]


def sync(**overrides):
    row = {"sync_id": "s-1", "internal_product_id": "ip-1", "woocommerce_status": WooCommerceSyncStatus.FAILED,
           "woocommerce_product_id": None, "updated_at": "2026-10-24T14:58:35+00:00",
           "error_code": "SUPABASE_IMAGE_DOWNLOAD_FAILED",
           "response_payload": {"uploaded_media": [], "error": {"exception_message": "Server disconnected"}}}
    row.update(overrides)
    return row


def cover_media(**overrides):
    item = adopt.MediaEvidence(media_id=4999, http_status=200, post=None,
                               file="2026/10/tsyc-fahasa-2026-003-can-0006-front_cover.webp",
                               source_url="https://x/cover.webp", mime_type="image/webp", sha256="a" * 64)
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def page_media(media_id=5000, scores=None, **overrides):
    item = adopt.MediaEvidence(media_id=media_id, http_status=200, post=0,
                               file=f"2026/10/tsyc-fahasa-2026-003-can-0006-inside_page-{media_id}.webp",
                               sha256="f" * 64,
                               pixel_scores=scores if scores is not None else {"img-p1": 1.1, "img-p2": 36.8, "img-cover": 80.0})
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def plan(media, syncs=None, remote=()):
    return adopt.plan_adoption(product=PRODUCT, syncs=[sync()] if syncs is None else syncs, images=IMAGES,
                               media=media, remote_sku_products=None if remote is None else list(remote))


def test_exact_hash_and_unambiguous_pixel_matches_are_adopted():
    result = plan([cover_media(), page_media()])
    assert result.ok, result.reason
    assert [(m["wordpress_media_id"], m["source_image_id"], m["method"]) for m in result.matches] == [
        (4999, "img-cover", "EXACT"), (5000, "img-p1", "PIXEL")]
    cover = result.media_records[0]
    assert cover["is_selected_main_image"] is True and cover["storage_path"] == "p/cover.webp"


def test_adopted_records_are_what_the_creator_reuses_by_source_image_id():
    result = plan([cover_media(), page_media()])
    saved = cwd.extract_uploaded_media({"response_payload": {"uploaded_media": result.media_records}})
    assert cwd.find_saved_media_for_image(saved, "img-p1")["wordpress_media_id"] == 5000
    assert cwd.find_saved_media_for_image(saved, "img-p2") is None


@pytest.mark.parametrize("scores, fragment", [
    ({"img-p1": 4.5, "img-p2": 40.0}, "best pixel difference"),
    ({"img-p1": 1.0, "img-p2": 9.0}, "ambiguous"),
    ({}, "no exact hash match"),
])
def test_unproven_pixel_identity_is_refused(scores, fragment):
    result = plan([page_media(scores=scores)])
    assert not result.ok and fragment in result.reason


def test_media_attached_to_a_post_is_never_adopted():
    result = plan([cover_media(post=4881)])
    assert not result.ok and "attached" in result.reason


def test_media_named_for_another_product_is_refused():
    result = plan([cover_media(file="2026/10/tsyc-fahasa-2026-003-can-0007-front_cover.webp")])
    assert not result.ok and "not named for" in result.reason


def test_role_in_file_name_must_match_the_matched_image():
    result = plan([page_media(file="2026/10/tsyc-fahasa-2026-003-can-0006-front_cover.webp")])
    assert not result.ok and "role" in result.reason


def test_two_media_matching_one_image_is_refused():
    result = plan([page_media(5000), page_media(5001)])
    assert not result.ok and "both match" in result.reason


def test_unreadable_media_is_refused():
    result = plan([cover_media(http_status=404)])
    assert not result.ok and "HTTP 404" in result.reason


@pytest.mark.parametrize("syncs, fragment", [
    ([], "exactly one sync"),
    ([sync(), sync(sync_id="s-2")], "exactly one sync"),
    ([sync(woocommerce_product_id=5200)], "already has WooCommerce product"),
    ([sync(woocommerce_status=WooCommerceSyncStatus.PENDING)], "not FAILED"),
    ([sync(response_payload={"uploaded_media": [{"source_image_id": "img-cover", "wordpress_media_id": 1}]})],
     "already records uploaded media"),
])
def test_sync_preconditions(syncs, fragment):
    result = plan([cover_media()], syncs=syncs)
    assert not result.ok and fragment in result.reason


def test_remote_product_with_the_sku_blocks_adoption():
    result = plan([cover_media()], remote=[{"id": 5200}])
    assert not result.ok and "already has SKU" in result.reason


def test_uncertain_remote_lookup_blocks_adoption():
    result = plan([cover_media()], remote=None)
    assert not result.ok and "unproven" in result.reason


def test_apply_records_media_keeps_failure_evidence_and_stays_failed():
    row = sync()
    repository = FakeSupabaseRepository({"woocommerce_product_syncs": [row], "process_logs": []})
    result = plan([cover_media(), page_media()], syncs=[dict(row)])
    adopt.apply_plan(repository, PRODUCT, dict(row), result, "2026-10-25T10:00:00+00:00")
    stored = repository.client.tables["woocommerce_product_syncs"][0]
    assert stored["woocommerce_status"] == WooCommerceSyncStatus.FAILED
    assert stored["woocommerce_product_id"] is None
    assert [m["wordpress_media_id"] for m in stored["response_payload"]["uploaded_media"]] == [4999, 5000]
    assert stored["response_payload"]["error"] == {"exception_message": "Server disconnected"}
    assert stored["response_payload"]["orphan_media_adoption"]["matches"][1]["method"] == "PIXEL"
    assert len(repository.client.tables["process_logs"]) == 1


def test_apply_refuses_a_sync_changed_since_it_was_read():
    row = sync()
    repository = FakeSupabaseRepository({"woocommerce_product_syncs": [dict(row, updated_at="2026-10-25T09:00:00+00:00")]})
    result = plan([cover_media()], syncs=[row])
    with pytest.raises(RuntimeError, match="changed since it was read"):
        adopt.apply_plan(repository, PRODUCT, row, result, "2026-10-25T10:00:00+00:00")
