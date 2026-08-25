"""An in-memory fake of the small slice of the Supabase Python client used
by the TSYC pipeline scripts.

This is a test double, not a mock of behavior we assume -- it re-implements
the tiny subset of the Postgrest fluent query builder that
scripts/*.py actually call (`.table().select().eq().not_.is_().order()
.limit().execute()`, plus `.insert()`, `.update()`, `.delete()`), backed by
plain Python dict rows held in memory.

No network access, no credentials, no live Supabase project. Safe to import
and use from any pytest test.
"""

from __future__ import annotations

import uuid
from typing import Any


# Primary-key column used per table when a fake `insert()` needs to invent an
# ID that the payload did not already supply (mirrors Postgres identity /
# default columns for the tables the pipeline scripts touch).
PRIMARY_KEY_COLUMNS: dict[str, str] = {
    "batches": "batch_id",
    "source_urls": "source_url_id",
    "raw_pages": "raw_page_id",
    "product_candidates": "candidate_id",
    "product_references": "reference_id",
    "product_images": "image_id",
    "internal_products": "internal_product_id",
    "product_contents": "product_content_id",
    "woocommerce_product_syncs": "sync_id",
    "process_logs": "process_log_id",
}


class FakeResponse:
    """Mimic the `.data` attribute of a postgrest APIResponse."""

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _NotProxy:
    """Mimic the `.not_` accessor, which currently only needs `.is_()`."""

    def __init__(self, builder: "FakeQueryBuilder") -> None:
        self._builder = builder

    def is_(self, column: str, value: str) -> "FakeQueryBuilder":
        if value != "null":
            raise NotImplementedError(
                "FakeQueryBuilder.not_.is_() only supports the 'null' "
                f"sentinel used by the pipeline scripts, got: {value!r}"
            )

        self._builder._not_null_filters.append(column)
        return self._builder


class FakeQueryBuilder:
    """A minimal, chainable stand-in for a Postgrest table query."""

    def __init__(self, store: list[dict[str, Any]], table_name: str) -> None:
        self._store = store
        self._table_name = table_name
        self._op: str | None = None
        self._payload: Any = None
        self._eq_filters: list[tuple[str, Any]] = []
        self._not_null_filters: list[str] = []
        self._null_filters: list[str] = []
        self._orders: list[tuple[str, bool]] = []
        self._limit_n: int | None = None

    # -- operation selection -------------------------------------------------

    def select(self, _columns: str = "*") -> "FakeQueryBuilder":
        self._op = "select"
        return self

    def insert(self, payload: Any) -> "FakeQueryBuilder":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> "FakeQueryBuilder":
        self._op = "update"
        self._payload = payload
        return self

    def delete(self) -> "FakeQueryBuilder":
        self._op = "delete"
        return self

    # -- filters ---------------------------------------------------------

    def eq(self, column: str, value: Any) -> "FakeQueryBuilder":
        self._eq_filters.append((column, value))
        return self

    def is_(self, column: str, value: str) -> "FakeQueryBuilder":
        """Mimic the bare `.is_(column, "null")` IS NULL filter (distinct
        from `.not_.is_(column, "null")`'s IS NOT NULL, above)."""
        if value != "null":
            raise NotImplementedError(
                "FakeQueryBuilder.is_() only supports the 'null' sentinel "
                f"used by the pipeline scripts, got: {value!r}"
            )

        self._null_filters.append(column)
        return self

    @property
    def not_(self) -> _NotProxy:
        return _NotProxy(self)

    def order(self, column: str, desc: bool = False) -> "FakeQueryBuilder":
        self._orders.append((column, desc))
        return self

    def limit(self, count: int) -> "FakeQueryBuilder":
        self._limit_n = count
        return self

    # -- execution ---------------------------------------------------------

    def _passes_filters(self, row: dict[str, Any]) -> bool:
        for column, value in self._eq_filters:
            if row.get(column) != value:
                return False

        for column in self._not_null_filters:
            if row.get(column) is None:
                return False

        for column in self._null_filters:
            if row.get(column) is not None:
                return False

        return True

    def _apply_orders(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = list(rows)

        # Stable-sort from the last `.order()` call to the first so that the
        # first call remains the dominant (primary) sort key, matching
        # Postgrest's left-to-right ordering semantics.
        for column, desc in reversed(self._orders):
            result.sort(
                key=lambda row, c=column: (
                    row.get(c) is None,
                    row.get(c),
                ),
                reverse=desc,
            )

        return result

    def execute(self) -> FakeResponse:
        if self._op in (None, "select"):
            rows = [row for row in self._store if self._passes_filters(row)]
            rows = self._apply_orders(rows)

            if self._limit_n is not None:
                rows = rows[: self._limit_n]

            return FakeResponse(list(rows))

        if self._op == "insert":
            payloads = (
                self._payload
                if isinstance(self._payload, list)
                else [self._payload]
            )

            inserted: list[dict[str, Any]] = []
            primary_key = PRIMARY_KEY_COLUMNS.get(self._table_name)

            for payload in payloads:
                row = dict(payload)

                if primary_key and not row.get(primary_key):
                    row[primary_key] = str(uuid.uuid4())

                self._store.append(row)
                inserted.append(row)

            return FakeResponse(inserted)

        if self._op == "update":
            rows = [row for row in self._store if self._passes_filters(row)]

            for row in rows:
                row.update(self._payload)

            return FakeResponse(list(rows))

        if self._op == "delete":
            rows = [row for row in self._store if self._passes_filters(row)]

            for row in rows:
                self._store.remove(row)

            return FakeResponse(list(rows))

        raise NotImplementedError(
            f"FakeQueryBuilder does not support operation: {self._op}"
        )


class FakeSupabaseClient:
    """A minimal stand-in for `supabase.Client` backed by in-memory tables."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = tables or {}

    def table(self, name: str) -> FakeQueryBuilder:
        store = self.tables.setdefault(name, [])
        return FakeQueryBuilder(store, name)


class FakeSupabaseRepository:
    """Duck-types `SupabaseRepository` for gate/payload tests.

    Production code only ever touches `repository.client...`, so this fake
    never needs to subclass the real `SupabaseRepository` (which requires
    live SUPABASE_URL/SUPABASE_KEY credentials to construct).
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.client = FakeSupabaseClient(tables)

    def get_batch_by_code(
        self,
        batch_code: str,
    ) -> dict[str, Any] | None:
        """Mirror SupabaseRepository.get_batch_by_code() for offline tests."""
        rows = (
            self.client.table("batches")
            .select("*")
            .eq("batch_code", batch_code)
            .limit(1)
            .execute()
            .data
            or []
        )

        return rows[0] if rows else None

    def save_source_url(
        self,
        batch_id: str,
        source_url: str,
        selection_reason: str | None = None,
        active: bool = True,
        source_type: str = "FACEBOOK_POST",
    ) -> dict[str, Any]:
        """Mirror SupabaseRepository.save_source_url() for offline tests.

        The real method upserts on (batch_id, source_type, source_url);
        FakeQueryBuilder has no .upsert(), so this replicates the same
        insert-or-update-on-conflict semantics using only its already-
        supported select/insert/update operations.
        """
        payload = {
            "batch_id": batch_id,
            "source_type": source_type,
            "source_url": source_url,
            "source_name": selection_reason,
            "is_authorized": active,
            "crawl_status": "PENDING" if active else "SKIPPED",
        }

        existing_rows = (
            self.client.table("source_urls")
            .select("*")
            .eq("batch_id", batch_id)
            .eq("source_type", source_type)
            .eq("source_url", source_url)
            .execute()
            .data
            or []
        )

        if existing_rows:
            response = (
                self.client.table("source_urls")
                .update(payload)
                .eq("batch_id", batch_id)
                .eq("source_type", source_type)
                .eq("source_url", source_url)
                .execute()
            )
        else:
            response = (
                self.client.table("source_urls")
                .insert(payload)
                .execute()
            )

        if not response.data:
            raise RuntimeError(f"Source URL upsert returned no data: {source_url}")

        return response.data[0]

    def write_process_log(
        self,
        message: str,
        process_name: str,
        batch_id: str | None = None,
        candidate_id: str | None = None,
        process_step: str | None = None,
        log_level: str = "INFO",
        status: str | None = None,
        error_code: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mirror SupabaseRepository.write_process_log() for offline tests."""
        payload = {
            "batch_id": batch_id,
            "candidate_id": candidate_id,
            "process_name": process_name,
            "process_step": process_step,
            "log_level": log_level,
            "status": status,
            "message": message,
            "error_code": error_code,
            "error_details": error_details,
        }

        response = (
            self.client.table("process_logs")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                "Process log insertion returned no data."
            )

        return response.data[0]
