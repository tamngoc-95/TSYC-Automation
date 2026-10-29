"""Staging-ready interface for reading Polylang's own translation links.

Free Polylang exposes no REST read of a product's language or links
(verified 2026-10-22). The proposed developer endpoint
(data/output/audit_20261026/TSYC_MULTILINGUAL_INTEGRATION_AUDIT.md section
6, option B) is

    GET {store}/wp-json/tsyc/v1/translations/{id}
    -> {"id": 123, "lang": "vi", "sku": "...", "status": "draft",
        "translations": {"vi": 123, "en": 456}}

and must wrap only pll_get_post_translations / pll_get_post_language.
Nothing here exists on the live store yet. This module is the client side:
GET only, strict parsing, and fail-closed. Any read failure or malformed
answer raises; it never means "no translations". The result is only marked
verified_on_staging when the operator attests a recorded staging run for
this exact mechanism version; the adapter ignores an unverified map.

A writer (create + link) is deliberately not implemented: it needs the
endpoint, the hook source review and the staging matrix first
(docs/POLYLANG_EXISTING_ARCHITECTURE.md section 7). TranslationDraftWriter
only fixes its contract, and require_staging_target guards a staging pilot
against pointing at production.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import requests

from src.domain import multilingual_adapter as ma
from src.domain import woo_translation_group as wtg

ENDPOINT_NAMESPACE = "tsyc/v1"
MECHANISM_NAME = "tsyc/v1 translations endpoint"


class LinkReadError(RuntimeError):
    """The link map could not be read or proven. Never 'no translations'."""


class StagingTargetError(RuntimeError):
    """A staging-only operation was pointed at production or an unsafe URL."""


class PolylangLinkReader(Protocol):
    mechanism: str

    def read_translations(self, product_id: int) -> ma.PolylangLinkMap: ...


class TranslationDraftWriter(Protocol):
    """Contract for the future create-and-link mechanism. An
    implementation must create a *draft* in `lang` (never publish, never a
    price, stock, backorders or SKU field), link it to the VI source
    through Polylang's public API, record `_tsyc_language` and the
    request ID, and be idempotent per request_id: a repeated request
    returns the existing product instead of creating a second one."""

    mechanism: str
    verified_on_staging: bool

    def create_translation_draft(self, *, source_id: int, lang: str, payload: Mapping[str, Any],
                                 request_id: str) -> int: ...


def parse_translation_response(
    body: Any, product_id: int, *, mechanism: str, verified_on_staging: bool
) -> ma.PolylangLinkMap:
    """Strict parse of one endpoint answer for a VI product."""
    if not isinstance(body, Mapping):
        raise LinkReadError(f"#{product_id}: the answer is not an object.")
    if _int(body.get("id")) != product_id:
        raise LinkReadError(f"#{product_id}: the answer is for product {body.get('id')!r}.")
    lang = str(body.get("lang") or "").lower()
    if lang not in wtg.SUPPORTED_LANGUAGES:
        raise LinkReadError(f"#{product_id}: language {body.get('lang')!r} is unknown or unsupported.")
    raw = body.get("translations")
    if not isinstance(raw, Mapping):
        raise LinkReadError(f"#{product_id}: no translations object.")
    translations: dict[str, int] = {}
    for key, value in raw.items():
        member_lang = str(key).lower()
        member_id = _int(value)
        if member_lang not in wtg.SUPPORTED_LANGUAGES:
            raise LinkReadError(f"#{product_id}: unsupported linked language {key!r}.")
        if member_id is None or member_id <= 0:
            raise LinkReadError(f"#{product_id}: linked {key!r} has no valid product ID ({value!r}).")
        if member_lang in translations:
            raise LinkReadError(f"#{product_id}: language {key!r} is listed twice.")
        translations[member_lang] = member_id
    if translations.get(lang) != product_id:
        raise LinkReadError(f"#{product_id}: its own language {lang} does not point back at it.")
    if len(set(translations.values())) != len(translations):
        raise LinkReadError(f"#{product_id}: one product is linked under two languages.")
    if lang != wtg.SOURCE_LANGUAGE:
        raise LinkReadError(f"#{product_id} is {lang}, not a VI source.")
    return ma.PolylangLinkMap(source_id=product_id, translations=translations,
                              mechanism=mechanism, verified_on_staging=verified_on_staging)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class EndpointLinkReader:
    """GET-only client of the proposed tsyc/v1 endpoint."""

    def __init__(
        self,
        *,
        store_url: str,
        auth: Any,
        verified_on_staging: bool = False,
        mechanism: str = MECHANISM_NAME,
        timeout_seconds: int = 30,
        http_get: Callable[..., Any] = requests.get,
    ) -> None:
        parsed = urlparse(store_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise StagingTargetError(f"Refusing a non-https store URL: {store_url!r}.")
        self.base = f"{store_url.rstrip('/')}/wp-json/{ENDPOINT_NAMESPACE}"
        self.auth = auth
        self.mechanism = mechanism
        self.verified_on_staging = verified_on_staging
        self.timeout = timeout_seconds
        self.http_get = http_get

    def read_translations(self, product_id: int) -> ma.PolylangLinkMap:
        try:
            response = self.http_get(f"{self.base}/translations/{int(product_id)}", auth=self.auth,
                                     headers={"Accept": "application/json"}, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - any failure means unknown links
            raise LinkReadError(f"#{product_id}: {type(error).__name__}: {error}") from error
        status = getattr(response, "status_code", None)
        if status != 200:
            # A 404 is an unknown product or a missing endpoint, never "no links".
            raise LinkReadError(f"#{product_id}: HTTP {status}.")
        try:
            body = response.json()
        except Exception as error:  # noqa: BLE001
            raise LinkReadError(f"#{product_id}: unreadable body ({type(error).__name__}).") from error
        return parse_translation_response(body, int(product_id), mechanism=self.mechanism,
                                          verified_on_staging=self.verified_on_staging)


def require_staging_target(target_url: str, production_url: str) -> None:
    """A staging pilot step may run only against a different https host
    than production (POLYLANG_WOOCOMMERCE_PLAN.md section 4a)."""
    target, production = urlparse(target_url), urlparse(production_url)
    if target.scheme != "https" or not target.hostname:
        raise StagingTargetError(f"Staging URL must be https with a host: {target_url!r}.")
    if not production.hostname:
        raise StagingTargetError("The production URL is unknown; cannot prove the target is not production.")
    if target.hostname.lower().removeprefix("www.") == production.hostname.lower().removeprefix("www."):
        raise StagingTargetError(f"{target.hostname} is the production host.")
