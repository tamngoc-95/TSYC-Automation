"""Per-language WooCommerce product mapping of one TSYC book (no I/O).

One internal product is one physical book. Its storefront languages
(vi / en / de) are separate WooCommerce products. This module defines the
local mapping record (migrations/011_create_woocommerce_language_products.sql),
derives the VI mapping from the existing woocommerce_product_syncs row,
validates mappings, verifies them read-only against remote products, and
turns them into the multilingual adapter's inputs.

Rules:
  * ownership is TSYC's own record of what it created: the VI row comes
    from the sync row whose request_payload TSYC itself sent (draft, exact
    SKU, its own internal-product meta). Remote `_tsyc_*` meta is checked
    against the record, never used to create one: a metadata-copy hook can
    copy it onto a product TSYC never created;
  * a POLYLANG_LINKED row (an existing copy) is only ever written from a
    staging-proven link reader, and it is never TSYC-owned;
  * nothing here infers a link from a slug, title, image or shared SKU.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from src.domain import multilingual_adapter as ma
from src.domain import woo_translation_group as wtg

TABLE = "woocommerce_language_products"
LANGUAGES = wtg.SUPPORTED_LANGUAGES

ORIGIN_TSYC_CREATED = "TSYC_CREATED"
ORIGIN_POLYLANG_LINKED = "POLYLANG_LINKED"
ORIGINS = (ORIGIN_TSYC_CREATED, ORIGIN_POLYLANG_LINKED)

PENDING_CREATE = "PENDING_CREATE"   # a create was requested, its outcome is not confirmed
RECORDED = "RECORDED"
REMOTE_REMOVED = "REMOTE_REMOVED"   # terminal: never recreated
CONFLICT = "CONFLICT"
STATUSES = (PENDING_CREATE, RECORDED, REMOTE_REMOVED, CONFLICT)

BASELINE_CREATE_RESPONSE = "CREATE_RESPONSE"
BASELINE_TSYC_WRITE = "TSYC_WRITE_RESPONSE"

ERROR = "ERROR"
WARNING = "WARNING"
INFO = "INFO"

# Finding codes.
SKU_MISMATCH = "SKU_MISMATCH"
NOT_TSYC_CREATE_REQUEST = "NOT_TSYC_CREATE_REQUEST"
NO_WOO_ID = "NO_WOO_ID"
NO_PRODUCT = "NO_INTERNAL_PRODUCT"
BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
INVALID_VALUE = "INVALID_VALUE"
DUPLICATE_LANGUAGE = "DUPLICATE_LANGUAGE"
DUPLICATE_WOO_ID = "DUPLICATE_WOO_ID"
MISSING_VI = "TRANSLATION_WITHOUT_VI"
PRODUCT_CODE_MISMATCH = "PRODUCT_CODE_MISMATCH"
REQUEST_ID_MISSING = "REQUEST_ID_MISSING"
LINK_EVIDENCE_MISSING = "LINK_EVIDENCE_MISSING"
SYNC_ON_TRANSLATION = "SYNC_ON_TRANSLATION"
STORED_DIFFERS = "STORED_DIFFERS_FROM_SYNC"
STORED_MISSING = "STORED_VI_MISSING"
REMOTE_NOT_READ = "REMOTE_NOT_READ"
REMOTE_PRESENT_AFTER_REMOVAL = "REMOTE_PRESENT_AFTER_REMOVAL"
REMOTE_SKU_MISMATCH = "REMOTE_SKU_MISMATCH"
OWNER_META_MISSING = "OWNER_META_MISSING"
OWNER_META_OTHER_BOOK = "OWNER_META_OTHER_BOOK"
OWNER_META_ON_LINKED_COPY = "OWNER_META_ON_LINKED_COPY"
LANGUAGE_MISMATCH = "LANGUAGE_MISMATCH"
COPIED_OWNERSHIP_META = "COPIED_OWNERSHIP_META"
UNMAPPED_SKU_COPY = "UNMAPPED_SKU_COPY"
CHANGED_SINCE_BASELINE = "CHANGED_SINCE_BASELINE"
REMOTE_PUBLISHED = "REMOTE_PUBLISHED"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    product_code: str
    language: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "product_code": self.product_code,
                "language": self.language, "detail": self.detail}


@dataclass(frozen=True)
class LanguageProduct:
    internal_product_id: str
    product_code: str
    language_code: str
    woocommerce_product_id: int | None
    origin: str
    mapping_status: str
    sync_id: str | None = None
    product_content_id: str | None = None
    request_id: str | None = None
    baseline_source: str | None = None
    baseline_modified_gmt: str | None = None
    baseline_text: Mapping[str, str] | None = None
    link_mechanism: str | None = None
    link_verified_at: str | None = None
    link_evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Insert body for the table (product_code is not a column)."""
        return {
            "internal_product_id": self.internal_product_id,
            "language_code": self.language_code,
            "woocommerce_product_id": self.woocommerce_product_id,
            "origin": self.origin,
            "mapping_status": self.mapping_status,
            "sync_id": self.sync_id,
            "product_content_id": self.product_content_id,
            "request_id": self.request_id,
            "baseline_source": self.baseline_source,
            "baseline_modified_gmt": self.baseline_modified_gmt,
            "baseline_text": dict(self.baseline_text) if self.baseline_text is not None else None,
            "link_mechanism": self.link_mechanism,
            "link_verified_at": self.link_verified_at,
            "link_evidence": dict(self.link_evidence),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any], product_code: str) -> "LanguageProduct":
        woo_id = row.get("woocommerce_product_id")
        return cls(
            internal_product_id=str(row["internal_product_id"]),
            product_code=product_code,
            language_code=str(row.get("language_code") or ""),
            woocommerce_product_id=int(woo_id) if woo_id is not None else None,
            origin=str(row.get("origin") or ""),
            mapping_status=str(row.get("mapping_status") or ""),
            sync_id=_opt(row.get("sync_id")),
            product_content_id=_opt(row.get("product_content_id")),
            request_id=_opt(row.get("request_id")),
            baseline_source=_opt(row.get("baseline_source")),
            baseline_modified_gmt=_opt(row.get("baseline_modified_gmt")),
            baseline_text=row.get("baseline_text"),
            link_mechanism=_opt(row.get("link_mechanism")),
            link_verified_at=_opt(row.get("link_verified_at")),
            link_evidence=row.get("link_evidence") or {},
        )


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _meta(payload: Mapping[str, Any], key: str) -> Any:
    return wtg.meta_value(payload, key)


# ---------------------------------------------------------------------
# VI mapping from the existing sync row
# ---------------------------------------------------------------------


def derive_vi_mapping(
    sync: Mapping[str, Any], product: Mapping[str, Any] | None
) -> tuple[LanguageProduct | None, list[Finding]]:
    """The VI mapping implied by one woocommerce_product_syncs row.

    Only a row whose request TSYC itself sent (draft, the exact SKU, its
    own internal-product meta) and that names a Woo ID becomes a mapping.
    Remote meta is never consulted here."""
    code = str((product or {}).get("product_code") or sync.get("product_sku") or "")
    findings: list[Finding] = []

    def finding(severity: str, kind: str, detail: str) -> None:
        findings.append(Finding(severity, kind, code, wtg.SOURCE_LANGUAGE, detail))

    if product is None:
        finding(ERROR, NO_PRODUCT, f"Sync {sync.get('sync_id')} has no internal product.")
        return None, findings
    ipid = str(product["internal_product_id"])
    if str(sync.get("product_sku") or "") != code:
        finding(ERROR, SKU_MISMATCH, f"Sync SKU {sync.get('product_sku')!r} is not the product code {code}.")
        return None, findings
    request = sync.get("request_payload") or {}
    if (request.get("status") != "draft" or str(request.get("sku") or "") != code
            or str(_meta(request, wtg.TSYC_OWNER_META_KEY) or "") != ipid):
        finding(ERROR, NOT_TSYC_CREATE_REQUEST,
                "The stored request is not TSYC's own draft create for this book; no ownership record is derived.")
        return None, findings
    woo_id = sync.get("woocommerce_product_id")
    if woo_id is None:
        finding(WARNING, NO_WOO_ID, "No Woo ID recorded: the create outcome belongs to the recovery tools.")
        return None, findings
    woo_id = int(woo_id)

    response = sync.get("response_payload") or {}
    removal = response.get("remote_removal") or {}
    status = REMOTE_REMOVED if removal.get("state") else RECORDED
    if response.get("recovery_required"):
        finding(WARNING, RECOVERY_REQUIRED, "The sync row still carries recovery_required.")

    created = response.get("woocommerce_product") or {}
    baseline_source = baseline_modified = baseline_text = None
    if isinstance(created, Mapping) and created.get("id") is not None and int(created["id"]) == woo_id:
        baseline_source = BASELINE_CREATE_RESPONSE
        baseline_modified = _opt(created.get("date_modified_gmt"))
        baseline_text = {f: str(created.get(f) or "") for f in wtg.TRANSLATED_FIELDS}
    else:
        finding(INFO, BASELINE_UNAVAILABLE, f"No create response for #{woo_id}; ownership of later edits stays unknown.")

    return LanguageProduct(
        internal_product_id=ipid,
        product_code=code,
        language_code=wtg.SOURCE_LANGUAGE,
        woocommerce_product_id=woo_id,
        origin=ORIGIN_TSYC_CREATED,
        mapping_status=status,
        sync_id=_opt(sync.get("sync_id")),
        baseline_source=baseline_source,
        baseline_modified_gmt=baseline_modified,
        baseline_text=baseline_text,
    ), findings


# ---------------------------------------------------------------------
# Validation of a set of mapping rows
# ---------------------------------------------------------------------


def validate_mappings(rows: Sequence[LanguageProduct]) -> list[Finding]:
    """Structural invariants (the migration's constraints, plus the ones a
    constraint cannot express across rows)."""
    findings: list[Finding] = []
    by_language: dict[tuple[str, str], LanguageProduct] = {}
    by_woo_id: dict[int, LanguageProduct] = {}
    for row in rows:
        def finding(code: str, detail: str, severity: str = ERROR) -> None:
            findings.append(Finding(severity, code, row.product_code, row.language_code, detail))

        if row.language_code not in LANGUAGES:
            finding(INVALID_VALUE, f"Unsupported language {row.language_code!r}.")
        if row.origin not in ORIGINS:
            finding(INVALID_VALUE, f"Unknown origin {row.origin!r}.")
        if row.mapping_status not in STATUSES:
            finding(INVALID_VALUE, f"Unknown mapping status {row.mapping_status!r}.")
        if row.mapping_status != PENDING_CREATE and row.woocommerce_product_id is None:
            finding(INVALID_VALUE, f"{row.mapping_status} mapping without a Woo ID.")
        if row.sync_id and not (row.language_code == wtg.SOURCE_LANGUAGE and row.origin == ORIGIN_TSYC_CREATED):
            finding(SYNC_ON_TRANSLATION, "Only TSYC's VI row may point at the sync row.")
        if (row.origin == ORIGIN_TSYC_CREATED and row.language_code != wtg.SOURCE_LANGUAGE
                and not row.request_id):
            finding(REQUEST_ID_MISSING, "A TSYC translation needs its create request ID.")
        if row.origin == ORIGIN_POLYLANG_LINKED and not (row.link_mechanism and row.link_verified_at):
            finding(LINK_EVIDENCE_MISSING, "A linked copy needs the proven link mechanism and time.")

        key = (row.internal_product_id, row.language_code)
        if key in by_language:
            finding(DUPLICATE_LANGUAGE, f"Two {row.language_code} mappings for one book.")
        by_language[key] = row
        if row.woocommerce_product_id is not None:
            other = by_woo_id.get(row.woocommerce_product_id)
            if other is not None:
                finding(DUPLICATE_WOO_ID, f"#{row.woocommerce_product_id} is also mapped as "
                                          f"{other.product_code} {other.language_code}.")
            by_woo_id[row.woocommerce_product_id] = row

    for (ipid, lang), row in by_language.items():
        if lang == wtg.SOURCE_LANGUAGE:
            continue
        vi = by_language.get((ipid, wtg.SOURCE_LANGUAGE))
        if vi is None or vi.mapping_status != RECORDED:
            findings.append(Finding(ERROR, MISSING_VI, row.product_code, lang,
                                    "A translation mapping without a recorded VI product."))
        elif vi.product_code != row.product_code:
            findings.append(Finding(ERROR, PRODUCT_CODE_MISMATCH, row.product_code, lang,
                                    f"VI row says {vi.product_code}."))
    return findings


def compare_stored_vi(stored: Sequence[LanguageProduct], derived: Sequence[LanguageProduct]) -> list[Finding]:
    """Stored VI rows must equal what the sync rows imply (same Woo ID,
    sync row and status). A derived row not stored yet is a WARNING."""
    findings: list[Finding] = []
    stored_vi = {r.internal_product_id: r for r in stored if r.language_code == wtg.SOURCE_LANGUAGE}
    for row in derived:
        current = stored_vi.get(row.internal_product_id)
        if current is None:
            findings.append(Finding(WARNING, STORED_MISSING, row.product_code, wtg.SOURCE_LANGUAGE,
                                    "Not stored yet (backfill pending)."))
            continue
        for name in ("woocommerce_product_id", "sync_id", "origin", "mapping_status"):
            if getattr(current, name) != getattr(row, name):
                findings.append(Finding(ERROR, STORED_DIFFERS, row.product_code, wtg.SOURCE_LANGUAGE,
                                        f"{name}: stored {getattr(current, name)!r}, sync row implies "
                                        f"{getattr(row, name)!r}."))
    return findings


# ---------------------------------------------------------------------
# Read-only remote verification
# ---------------------------------------------------------------------


def verify_remote(
    rows: Sequence[LanguageProduct],
    catalog: Mapping[int, Mapping[str, Any]],
) -> list[Finding]:
    """Compare mapping rows with a read of the remote catalog (all
    products, status any, with meta_data). Reports only; never resolves a
    finding by adopting a remote product."""
    findings: list[Finding] = []
    mapped_ids = {r.woocommerce_product_id for r in rows if r.woocommerce_product_id is not None}
    books: dict[str, list[LanguageProduct]] = {}
    for row in rows:
        books.setdefault(row.internal_product_id, []).append(row)

    for row in rows:
        def finding(severity: str, code: str, detail: str) -> None:
            findings.append(Finding(severity, code, row.product_code, row.language_code, detail))

        if row.woocommerce_product_id is None:
            continue
        product = catalog.get(row.woocommerce_product_id)
        if row.mapping_status == REMOTE_REMOVED:
            if product is not None and product.get("status") not in (None, "trash"):
                finding(WARNING, REMOTE_PRESENT_AFTER_REMOVAL,
                        f"#{row.woocommerce_product_id} is recorded removed but reads {product.get('status')!r}: "
                        "owner decision (never cleared automatically).")
            continue
        if product is None:
            finding(WARNING, REMOTE_NOT_READ,
                    f"#{row.woocommerce_product_id} was not in the catalog read: see audit_woo_remote_status.py.")
            continue
        sku = str(product.get("sku") or "")
        if row.origin == ORIGIN_TSYC_CREATED and sku != row.product_code:
            finding(ERROR, REMOTE_SKU_MISMATCH, f"#{row.woocommerce_product_id} carries SKU {sku!r}.")
        owner_meta = str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY) or "")
        if row.origin == ORIGIN_TSYC_CREATED:
            if not owner_meta:
                finding(WARNING, OWNER_META_MISSING,
                        f"#{row.woocommerce_product_id} lost TSYC's meta: its ownership is UNKNOWN.")
            elif owner_meta != row.internal_product_id:
                finding(ERROR, OWNER_META_OTHER_BOOK, f"#{row.woocommerce_product_id} carries another book's meta.")
        elif owner_meta:
            finding(ERROR, OWNER_META_ON_LINKED_COPY,
                    f"Linked copy #{row.woocommerce_product_id} carries TSYC meta it was never given (copied meta).")
        shown = wtg.product_language(product)[0]
        lang_meta = str(wtg.meta_value(product, wtg.TSYC_LANGUAGE_META_KEY) or "").lower()
        if shown not in (None, row.language_code) or (lang_meta and lang_meta != row.language_code):
            finding(ERROR, LANGUAGE_MISMATCH,
                    f"#{row.woocommerce_product_id} reads as {shown or lang_meta}, mapped as {row.language_code}.")
        if product.get("status") == "publish" and row.origin == ORIGIN_TSYC_CREATED:
            finding(INFO, REMOTE_PUBLISHED, f"#{row.woocommerce_product_id} is published: owner territory.")
        if (row.baseline_modified_gmt is not None
                and str(product.get("date_modified_gmt") or "") != row.baseline_modified_gmt):
            finding(INFO, CHANGED_SINCE_BASELINE,
                    f"#{row.woocommerce_product_id} changed after TSYC's baseline (owner, hook or a later TSYC write).")

    for ipid, book_rows in books.items():
        vi = next((r for r in book_rows if r.language_code == wtg.SOURCE_LANGUAGE), None)
        code = vi.product_code if vi else book_rows[0].product_code
        for pid, product in sorted(catalog.items()):
            if pid in mapped_ids:
                continue
            if str(wtg.meta_value(product, wtg.TSYC_OWNER_META_KEY) or "") == ipid:
                findings.append(Finding(ERROR, COPIED_OWNERSHIP_META, code, None,
                                        f"#{pid} carries TSYC's meta for this book but is not mapped: copied "
                                        "metadata or an unreconciled create. Never adopted automatically."))
            elif str(product.get("sku") or "") == code and product.get("status") != "trash":
                findings.append(Finding(WARNING, UNMAPPED_SKU_COPY, code, wtg.product_language(product)[0],
                                        f"#{pid} ({product.get('status')}) carries the SKU but is not mapped. A "
                                        "shared SKU is not a link; only a proven Polylang link may map it."))
    return findings


# ---------------------------------------------------------------------
# Blocked books
# ---------------------------------------------------------------------

BLOCKED_COPIED_OWNERSHIP_META = "BLOCKED_COPIED_OWNERSHIP_META"
BLOCKED_OWNER_DECISION = "BLOCKED_OWNER_DECISION"
BLOCKED_CREATE_UNRESOLVED = "BLOCKED_CREATE_UNRESOLVED"
BLOCKED_AUDIT_ERROR = "BLOCKED_AUDIT_ERROR"
NOT_STORED = "NOT_STORED"

# First matching finding code decides the disposition.
_BLOCKING_ORDER = (
    (COPIED_OWNERSHIP_META, BLOCKED_COPIED_OWNERSHIP_META),
    (REMOTE_PRESENT_AFTER_REMOVAL, BLOCKED_OWNER_DECISION),
    (NO_WOO_ID, BLOCKED_CREATE_UNRESOLVED),
    (RECOVERY_REQUIRED, BLOCKED_CREATE_UNRESOLVED),
)


def blocked_books(
    derived: Sequence[LanguageProduct],
    stored: Sequence[LanguageProduct],
    findings: Sequence[Finding],
) -> list[dict[str, Any]]:
    """Every book without a stored VI row, with an explicit disposition and
    the finding codes behind it. This names the block; it never clears,
    downgrades or hides a finding (the ERROR stays counted)."""
    stored_keys = {(r.internal_product_id, r.language_code) for r in stored}
    codes = {r.product_code for r in derived if (r.internal_product_id, r.language_code) not in stored_keys}
    codes |= {f.product_code for f in findings if f.code in (NO_WOO_ID, RECOVERY_REQUIRED)}
    result = []
    for code in sorted(codes):
        own = [f for f in findings if f.product_code == code and f.severity in (ERROR, WARNING)
               and f.code != STORED_MISSING]
        found = {f.code for f in own}
        disposition = next((d for c, d in _BLOCKING_ORDER if c in found), None)
        if disposition is None:
            disposition = BLOCKED_AUDIT_ERROR if any(f.severity == ERROR for f in own) else NOT_STORED
        result.append({"product_code": code, "disposition": disposition,
                       "finding_codes": sorted(f"{f.severity} {f.code}" for f in own)})
    return result


# ---------------------------------------------------------------------
# Adapter inputs
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterInputs:
    tsyc_records: dict[str, ma.TsycProductRecord]
    uncertain_languages: tuple[str, ...]
    blocked_languages: tuple[str, ...]


def adapter_inputs(rows: Iterable[LanguageProduct]) -> AdapterInputs:
    """The adapter's ownership records for one book. Only RECORDED
    TSYC_CREATED rows are TSYC records. A pending create must be
    reconciled first; a removed or conflicting language is refused (a
    removed product is never recreated). A VI row that is not RECORDED
    gives no VI record, so resolve_group refuses the whole book."""
    records: dict[str, ma.TsycProductRecord] = {}
    uncertain: list[str] = []
    blocked: list[str] = []
    for row in rows:
        if row.mapping_status == PENDING_CREATE:
            uncertain.append(row.language_code)
        elif row.mapping_status in (REMOTE_REMOVED, CONFLICT):
            blocked.append(row.language_code)
        elif row.origin == ORIGIN_TSYC_CREATED and row.woocommerce_product_id is not None:
            records[row.language_code] = ma.TsycProductRecord(
                lang=row.language_code, woo_id=row.woocommerce_product_id,
                internal_product_id=row.internal_product_id,
                baseline_modified_gmt=row.baseline_modified_gmt,
                baseline_text=dict(row.baseline_text) if row.baseline_text is not None else None)
    return AdapterInputs(records, tuple(sorted(set(uncertain))), tuple(sorted(set(blocked))))
