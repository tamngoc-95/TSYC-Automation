"""Create the missing EN/DE drafts of one logical book, one language at a time.

Order of one language:

  1. A pending operation (REQUEST_SENT / UNCERTAIN) is reconciled first by
     request ID. Nothing else happens for that language until the store has
     answered: found, or explicitly absent. A failed read leaves it pending.
  2. The group is resolved from TSYC's records plus Polylang's map, and the
     adapter plans the language. Only CREATE_AND_LINK proceeds.
  3. REQUEST_SENT is written to disk, then one POST is sent.
  4. The result is stored. An UNCERTAIN result is never retried in this run.

A failure in one language or book never stops the others. Nothing here writes
to Supabase: confirmed creates are returned as proposed mapping rows, for a
separately authorized writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import polylang_link_graph as plg
from src.domain import translation_create_state as tcs
from src.domain import woo_translation_group as wtg
from src.services import polylang_bridge_writer as pbw
from src.services import polylang_link_reader as plr

# Result of one language in one run.
RESULT_CREATED = "CREATED"
RESULT_RECONCILED = "RECONCILED"
RESULT_ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
RESULT_UNCERTAIN = "UNCERTAIN"
RESULT_REFUSED = "REFUSED"
RESULT_SKIPPED = "SKIPPED"
RESULT_ERROR = "ERROR"


class CatalogReader(Protocol):
    def __call__(self, product_ids: Sequence[int], product_code: str) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]: ...


@dataclass(frozen=True)
class BookInput:
    internal_product_id: str
    product_code: str
    vi_row: lpm.LanguageProduct
    contents: Mapping[str, Mapping[str, Any]]
    authorization_id: str


@dataclass
class LanguageResult:
    lang: str
    result: str
    action: str | None = None
    product_id: int | None = None
    code: str | None = None
    detail: str | None = None


@dataclass
class BookResult:
    product_code: str
    resolution_status: str | None = None
    resolution_reasons: list[str] = field(default_factory=list)
    languages: dict[str, LanguageResult] = field(default_factory=dict)
    proposed_rows: list[lpm.LanguageProduct] = field(default_factory=list)


def _baseline_row(operation: tcs.LanguageOperation) -> lpm.LanguageProduct:
    return lpm.LanguageProduct(
        internal_product_id=operation.internal_product_id,
        product_code=operation.product_code,
        language_code=operation.lang,
        woocommerce_product_id=operation.product_id,
        origin=lpm.ORIGIN_TSYC_CREATED,
        mapping_status=lpm.RECORDED,
        product_content_id=operation.product_content_id,
        request_id=operation.request_id,
        baseline_source=lpm.BASELINE_CREATE_RESPONSE,
        baseline_modified_gmt=operation.modified_gmt,
        baseline_text=operation.baseline_text,
        link_mechanism=plr.MECHANISM_NAME,
    )


class TranslationDraftRunner:
    def __init__(
        self,
        *,
        reader: plr.EndpointLinkReader,
        writer: pbw.BridgeDraftWriter,
        store: tcs.CheckpointStore,
        read_catalog: CatalogReader,
        render: wtg.Renderer,
        storefront_groups: Callable[[], Sequence[plg.CatalogGroup]] = lambda: (),
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.store = store
        self.read_catalog = read_catalog
        self.render = render
        self.storefront_groups = storefront_groups

    # ------------------------------------------------------------- one book

    def run_book(self, book: BookInput, *, create_authorized: bool) -> BookResult:
        result = BookResult(book.product_code)
        for lang in wtg.TRANSLATION_LANGUAGES:
            try:
                result.languages[lang] = self._run_language(book, lang, create_authorized, result)
            except Exception as error:  # noqa: BLE001 - isolate one language from the rest
                result.languages[lang] = LanguageResult(lang, RESULT_ERROR, code=type(error).__name__, detail=str(error))
        result.proposed_rows = [_baseline_row(op) for lang in wtg.TRANSLATION_LANGUAGES
                                if (op := self.store.get(book.internal_product_id, lang)) is not None
                                and op.state == tcs.CONFIRMED]
        return result

    def _rows(self, book: BookInput) -> list[lpm.LanguageProduct]:
        rows = [book.vi_row]
        for lang in wtg.TRANSLATION_LANGUAGES:
            operation = self.store.get(book.internal_product_id, lang)
            if operation is not None and operation.state == tcs.CONFIRMED:
                rows.append(_baseline_row(operation))
        return rows

    def resolve(self, book: BookInput, link_map: ma.PolylangLinkMap) -> tuple[ma.GroupResolution, dict[int, dict[str, Any]], lpm.AdapterInputs]:
        source_id = int(book.vi_row.woocommerce_product_id or 0)
        inputs = lpm.adapter_inputs(self._rows(book))
        wanted = sorted({source_id, *link_map.translations.values(), *(r.woo_id for r in inputs.tsyc_records.values())})
        products, sku_matches = self.read_catalog(wanted, book.product_code)
        resolution = ma.resolve_group(
            product_code=book.product_code, source_id=source_id, products_by_id=products, sku_matches=sku_matches,
            storefront_groups=list(self.storefront_groups()), tsyc_records=inputs.tsyc_records, link_map=link_map)
        return resolution, products, inputs

    # --------------------------------------------------------- one language

    def _run_language(self, book: BookInput, lang: str, create_authorized: bool, result: BookResult) -> LanguageResult:
        operation = self.store.get(book.internal_product_id, lang)
        if operation is not None and operation.state in tcs.NEEDS_RECONCILE:
            step = self._reconcile(book, operation)
            if step is not None:
                return step
            # The store said "absent": planning continues and the same request ID may be sent.
        if operation is not None and operation.state == tcs.CONFIRMED:
            return self._verify_confirmed(operation)
        if operation is not None and operation.state in (tcs.REFUSED, tcs.ROLLED_BACK):
            return LanguageResult(lang, RESULT_REFUSED, code=operation.reason_code,
                                  detail=f"Stays {operation.state}: {operation.reason or 'see the checkpoint'}.",
                                  product_id=operation.product_id)

        link_map = self.reader.read_translations(int(book.vi_row.woocommerce_product_id or 0))
        resolution, products, inputs = self.resolve(book, link_map)
        result.resolution_status = resolution.status
        result.resolution_reasons = list(resolution.reasons)
        plan = ma.plan_language_actions(
            resolution, internal_product_id=book.internal_product_id, products_by_id=products,
            approved_contents=book.contents, render=self.render, tsyc_records=inputs.tsyc_records,
            create_authorized=create_authorized, link_mechanism=self.writer,
            uncertain_languages=inputs.uncertain_languages, blocked_languages=inputs.blocked_languages)
        action = plan[lang]
        if action != ma.CREATE_AND_LINK:
            return LanguageResult(lang, RESULT_SKIPPED, action=action,
                                  detail="; ".join(resolution.reasons) or None,
                                  product_id=resolution.members.get(lang))
        return self._send(book, lang, products, operation)

    def _send(self, book: BookInput, lang: str, products: Mapping[int, Mapping[str, Any]],
              operation: tcs.LanguageOperation | None) -> LanguageResult:
        content = book.contents[lang]
        source_id = int(book.vi_row.woocommerce_product_id or 0)
        content_id = str(content.get("product_content_id") or "")
        if not content_id:
            return LanguageResult(lang, RESULT_REFUSED, code="NO_CONTENT_ID", detail="The approved content has no ID.")
        if operation is None:
            operation = tcs.LanguageOperation(
                request_id=tcs.make_request_id(book.internal_product_id, lang, content_id),
                internal_product_id=book.internal_product_id, product_code=book.product_code, lang=lang,
                source_id=source_id, product_content_id=content_id)
        payload = ma.build_text_payload(content, lang, self.render)
        # The expectation is TSYC's recorded baseline, not whatever the store
        # says now: a source edited after TSYC's write must be refused.
        operation.expected_source_modified_gmt = str(book.vi_row.baseline_modified_gmt or "")
        request = pbw.CreateRequest(
            source_id=source_id, lang=lang, request_id=operation.request_id,
            authorization_id=book.authorization_id, internal_product_id=book.internal_product_id,
            product_code=book.product_code,
            expected_source_modified_gmt=operation.expected_source_modified_gmt,
            content=payload)
        operation.move(tcs.REQUEST_SENT)
        self.store.put(operation)  # On disk before the request leaves.
        outcome = self.writer.create_translation_draft(request)
        return self._apply(operation, outcome, RESULT_CREATED)

    def _apply(self, operation: tcs.LanguageOperation, outcome: pbw.CreateOutcome, success: str) -> LanguageResult:
        if outcome.kind == pbw.CONFIRMED:
            operation.product_id = outcome.product_id
            operation.modified_gmt = outcome.modified_gmt
            operation.baseline_text = dict(outcome.text or {})
            operation.translations = dict(outcome.translations)
            operation.move(tcs.CONFIRMED)
            self.store.put(operation)
            return LanguageResult(operation.lang, success, product_id=outcome.product_id)
        if outcome.kind == pbw.REFUSED:
            operation.move(tcs.REFUSED, code=outcome.code, reason=outcome.detail)
            self.store.put(operation)
            return LanguageResult(operation.lang, RESULT_REFUSED, code=outcome.code, detail=outcome.detail)
        operation.move(tcs.UNCERTAIN, code=outcome.code, reason=outcome.detail)
        self.store.put(operation)
        return LanguageResult(operation.lang, RESULT_UNCERTAIN, code=outcome.code, detail=outcome.detail)

    def _reconcile(self, book: BookInput, operation: tcs.LanguageOperation) -> LanguageResult | None:
        """None means the same request ID may be sent. Any other return stops
        this language for this run."""
        try:
            found = self.reader.read_request(operation.request_id)
        except plr.LinkReadError as error:
            operation.move(tcs.UNCERTAIN, code="RECONCILE_READ_FAILED", reason=str(error))
            self.store.put(operation)
            return LanguageResult(operation.lang, RESULT_UNCERTAIN, code="RECONCILE_READ_FAILED", detail=str(error))
        if not found.found:
            return None
        if found.lang != operation.lang or found.source_id != operation.source_id:
            operation.move(tcs.REFUSED, code="RECONCILE_IDENTITY_MISMATCH",
                           reason=f"Request {operation.request_id} landed on #{found.product_id} "
                                  f"({found.lang}, source {found.source_id}).")
            self.store.put(operation)
            return LanguageResult(operation.lang, RESULT_REFUSED, code="RECONCILE_IDENTITY_MISMATCH",
                                  detail=operation.reason, product_id=found.product_id)
        # Partial or complete, replaying the same request cannot create a second
        # product: the bridge finds it by request ID and finishes the remaining
        # steps, then answers with the read-back and baseline.
        content = book.contents[operation.lang]
        request = pbw.CreateRequest(
            source_id=operation.source_id, lang=operation.lang, request_id=operation.request_id,
            authorization_id=book.authorization_id, internal_product_id=operation.internal_product_id,
            product_code=operation.product_code,
            expected_source_modified_gmt=operation.expected_source_modified_gmt or "",
            content=ma.build_text_payload(content, operation.lang, self.render))
        outcome = self.writer.create_translation_draft(request)
        return self._apply(operation, outcome, RESULT_RECONCILED)

    def _verify_confirmed(self, operation: tcs.LanguageOperation) -> LanguageResult:
        """A finished operation is re-read, never recreated. If the store
        disagrees the operation is reported, not repaired."""
        group = self.reader.read_group(int(operation.product_id or 0))
        if group.translations.get(operation.lang) != operation.product_id \
                or group.translations.get(wtg.SOURCE_LANGUAGE) != operation.source_id:
            return LanguageResult(operation.lang, RESULT_REFUSED, code="CONFIRMED_LINK_CHANGED",
                                  detail=f"The store now links {dict(group.translations)}.",
                                  product_id=operation.product_id)
        return LanguageResult(operation.lang, RESULT_ALREADY_CONFIRMED, product_id=operation.product_id)
