"""Regression tests for create_woocommerce_draft.py's
reconcile_local_state_with_existing_remote_product().

Covers the production recovery case: a Woo draft create's remote result
became uncertain (e.g. a client-side timeout hid the response), a rerun's
built-in remote-SKU search (find_product_by_sku) confirmed the WooCommerce
product already exists, and local state (woocommerce_product_syncs /
internal_products) must converge to reflect that confirmed remote product
-- without ever attempting a second create.

Fully offline: FakeSupabaseRepository only, no live Supabase, no network,
no WooCommerce/WordPress calls. reconcile_local_state_with_existing_remote_
product() itself never calls create_woocommerce_product() or any WordPress
media upload function -- it only reads/writes through the fake repository.
"""

from __future__ import annotations

import pytest

import create_woocommerce_draft as woo
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus
from support.fake_supabase import FakeSupabaseRepository

INTERNAL_PRODUCT_ID = "ip-1"
PRODUCT_CODE = "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0021"
PRODUCT_NAME = "Không Tự Khinh Bỉ Không Tự Phí Hoài"

REMOTE_PRODUCT_DRAFT = {
    "id": 3708,
    "sku": PRODUCT_CODE,
    "status": "draft",
    "permalink": "https://tiemsachyeucon.com/?post_type=product&p=3708",
}

UPLOADED_MEDIA = [
    {
        "source_image_id": "img-1",
        "wordpress_media_id": 3707,
        "wordpress_source_url": (
            "https://tiemsachyeucon.com/wp-content/uploads/2026/09/"
            "tsyc-fb-hist-2026-autoimport-can-0021-front_cover.jpg"
        ),
        "is_selected_main_image": True,
    }
]


def make_product() -> dict:
    return {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "product_code": PRODUCT_CODE,
        "product_metadata": {},
        "woocommerce_status": WooCommerceStatus.READY_FOR_DRAFT,
    }


def make_repository_with_uncertain_sync() -> FakeSupabaseRepository:
    """
    Mirrors the exact production incident: media was uploaded and
    recorded, the sync was left IN_PROGRESS with no woocommerce_product_id
    because the create response was never observed locally.
    """
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_product()],
            "woocommerce_product_syncs": [
                {
                    "sync_id": "sync-1",
                    "internal_product_id": INTERNAL_PRODUCT_ID,
                    "woocommerce_status": WooCommerceSyncStatus.IN_PROGRESS,
                    "woocommerce_product_id": None,
                    "product_sku": PRODUCT_CODE,
                    "product_name": PRODUCT_NAME,
                    "product_permalink": None,
                    "response_payload": {
                        "uploaded_media": UPLOADED_MEDIA,
                        "media_upload_completed": True,
                    },
                    "sync_attempt_count": 1,
                }
            ],
        }
    )
    return repository


def get_sync_row(repository: FakeSupabaseRepository) -> dict:
    rows = repository.client.tables["woocommerce_product_syncs"]
    assert len(rows) == 1
    return rows[0]


def get_internal_product_row(repository: FakeSupabaseRepository) -> dict:
    rows = repository.client.tables["internal_products"]
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# 1. Uncertain create -> rerun finds existing remote -> local state reconciled
# ---------------------------------------------------------------------------


def test_uncertain_create_reconciles_to_existing_remote_product():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    sync_row = get_sync_row(repository)
    assert sync_row["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED
    assert sync_row["woocommerce_product_id"] == 3708
    assert sync_row["product_permalink"] == REMOTE_PRODUCT_DRAFT["permalink"]

    internal_row = get_internal_product_row(repository)
    assert internal_row["woocommerce_status"] == WooCommerceStatus.DRAFT_CREATED
    assert internal_row["product_metadata"]["woocommerce_draft"]["woocommerce_product_id"] == 3708


# ---------------------------------------------------------------------------
# 2. Already-existing remote product -> no second create is ever attempted
# ---------------------------------------------------------------------------


def test_reconciliation_never_calls_create_woocommerce_product(monkeypatch):
    def _fail_if_called(**_kwargs):
        raise AssertionError(
            "create_woocommerce_product() must never be called during "
            "reconciliation of an already-existing remote product."
        )

    monkeypatch.setattr(woo, "create_woocommerce_product", _fail_if_called)

    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    # Does not raise -- proves the reconciliation path never reaches the
    # patched (failing) create function.
    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )


def test_reconciliation_never_calls_wordpress_media_upload(monkeypatch):
    def _fail_if_called(**_kwargs):
        raise AssertionError(
            "upload_or_reuse_product_images() must never be called during "
            "reconciliation of an already-existing remote product -- no "
            "blind re-upload of media."
        )

    monkeypatch.setattr(woo, "upload_or_reuse_product_images", _fail_if_called)

    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )


# ---------------------------------------------------------------------------
# 3. Sync row transitions to the successful terminal status
# ---------------------------------------------------------------------------


def test_sync_row_transitions_to_draft_created_status():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    assert existing_sync["woocommerce_status"] == WooCommerceSyncStatus.IN_PROGRESS

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    assert get_sync_row(repository)["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED


# ---------------------------------------------------------------------------
# 4. Woo product id stored locally
# ---------------------------------------------------------------------------


def test_woo_product_id_stored_on_sync_row():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    assert existing_sync["woocommerce_product_id"] is None

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    assert get_sync_row(repository)["woocommerce_product_id"] == 3708


# ---------------------------------------------------------------------------
# 5. Internal product updated
# ---------------------------------------------------------------------------


def test_internal_product_status_updated():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    assert get_internal_product_row(repository)["woocommerce_status"] == (
        WooCommerceStatus.READY_FOR_DRAFT
    )

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    assert get_internal_product_row(repository)["woocommerce_status"] == (
        WooCommerceStatus.DRAFT_CREATED
    )


# ---------------------------------------------------------------------------
# 6. Idempotent rerun: calling reconciliation twice is safe and stable
# ---------------------------------------------------------------------------


def test_reconciliation_is_idempotent_on_rerun():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    first_sync_state = dict(get_sync_row(repository))
    first_internal_state = dict(get_internal_product_row(repository))

    # Rerun with the now-reconciled sync row, exactly as main() would pass
    # whatever get_existing_sync() reads back on a second invocation.
    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=get_sync_row(repository),
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    assert len(repository.client.tables["woocommerce_product_syncs"]) == 1
    assert len(repository.client.tables["internal_products"]) == 1
    assert get_sync_row(repository) == first_sync_state
    assert get_internal_product_row(repository) == first_internal_state


# ---------------------------------------------------------------------------
# 7. No local sync record existed yet (edge case): one is created, then
#    marked succeeded -- never left half-written.
# ---------------------------------------------------------------------------


def test_creates_sync_record_when_none_existed():
    repository = FakeSupabaseRepository(
        tables={"internal_products": [make_product()]}
    )
    product = make_product()

    assert repository.client.tables.get("woocommerce_product_syncs", []) == []

    woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=None,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    sync_rows = repository.client.tables["woocommerce_product_syncs"]
    assert len(sync_rows) == 1
    assert sync_rows[0]["woocommerce_product_id"] == 3708
    assert sync_rows[0]["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED


# ---------------------------------------------------------------------------
# 8. Non-draft remote status refuses to reconcile (local/remote conflict) --
#    CLAUDE.md draft-only boundary, never silently overwritten.
# ---------------------------------------------------------------------------


def test_refuses_to_reconcile_non_draft_remote_status():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    published_product = {**REMOTE_PRODUCT_DRAFT, "status": "publish"}

    with pytest.raises(RuntimeError, match="not 'draft'"):
        woo.reconcile_local_state_with_existing_remote_product(
            repository=repository,
            product=product,
            product_name=PRODUCT_NAME,
            existing_sync=existing_sync,
            product_response=published_product,
        )

    # No write happened -- the sync row is untouched.
    assert get_sync_row(repository)["woocommerce_status"] == WooCommerceSyncStatus.IN_PROGRESS
    assert get_sync_row(repository)["woocommerce_product_id"] is None
    assert get_internal_product_row(repository)["woocommerce_status"] == (
        WooCommerceStatus.READY_FOR_DRAFT
    )


# ---------------------------------------------------------------------------
# 9. Uploaded media is reused from the existing sync record, never dropped
#    or re-invented.
# ---------------------------------------------------------------------------


def test_reuses_already_recorded_uploaded_media():
    repository = make_repository_with_uncertain_sync()
    product = make_product()
    existing_sync = get_sync_row(repository)

    result = woo.reconcile_local_state_with_existing_remote_product(
        repository=repository,
        product=product,
        product_name=PRODUCT_NAME,
        existing_sync=existing_sync,
        product_response=REMOTE_PRODUCT_DRAFT,
    )

    assert result["uploaded_media"] == UPLOADED_MEDIA

    sync_row = get_sync_row(repository)
    assert sync_row["response_payload"]["uploaded_media"] == UPLOADED_MEDIA
