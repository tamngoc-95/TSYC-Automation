"""Client of the TSYC Polylang bridge's read routes.

Free Polylang exposes no REST read of a product's language or links
(verified 2026-10-22). The bridge
(wordpress/tsyc-polylang-bridge/, docs/POLYLANG_BRIDGE.md) answers

    GET {store}/wp-json/tsyc/v1/translations/{id}
    -> {"id": 123, "lang": "vi", "sku": "...", "status": "draft",
        "translations": {"vi": 123, "en": 456}, "members": {...},
        "consistent": true, "issues": []}

and wraps only pll_get_post_translations / pll_get_post_language. It is NOT
installed on the live store. This module is GET only, parses strictly and
fails closed. Any read failure or malformed answer raises; it never means
"no translations". A group the bridge itself reports as inconsistent raises
LinkConflictError. The result is only marked verified_on_staging when the
operator attests a recorded run for this exact mechanism version; the adapter
ignores an unverified map.

The writer lives in src/services/polylang_bridge_writer.py and refuses
production through require_staging_target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import requests

from src.domain import multilingual_adapter as ma
from src.domain import woo_translation_group as wtg

ENDPOINT_NAMESPACE = "tsyc/v1"
MECHANISM_NAME = "tsyc/v1 translations endpoint"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class LinkReadError(RuntimeError):
    """The link map could not be read or proven. Never 'no translations'."""


class LinkConflictError(LinkReadError):
    """The bridge read Polylang and reports the group as inconsistent."""

    def __init__(self, message: str, issues: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.issues = list(issues)


class StagingTargetError(RuntimeError):
    """A staging-only operation was pointed at production or an unsafe URL."""


class PolylangLinkReader(Protocol):
    mechanism: str

    def read_translations(self, product_id: int) -> ma.PolylangLinkMap: ...


@dataclass(frozen=True)
class GroupRead:
    """One product's authoritative Polylang group, as the bridge read it."""

    product_id: int
    lang: str
    status: str
    sku: str
    translations: Mapping[str, int]
    members: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestRead:
    """What the store knows about one create request_id."""

    request_id: str
    found: bool
    product_id: int | None = None
    status: str | None = None
    lang: str | None = None
    state: str | None = None
    source_id: int | None = None
    translations: Mapping[str, int] = field(default_factory=dict)
    consistent: bool = False


def parse_group_response(body: Any, product_id: int) -> GroupRead:
    """Strict parse of one bridge answer, for a product of any language.
    An answer without an explicit `consistent: true` is never accepted."""
    if not isinstance(body, Mapping):
        raise LinkReadError(f"#{product_id}: the answer is not an object.")
    if _int(body.get("id")) != product_id:
        raise LinkReadError(f"#{product_id}: the answer is for product {body.get('id')!r}.")
    lang = str(body.get("lang") or "").lower()
    if lang not in wtg.SUPPORTED_LANGUAGES:
        raise LinkReadError(f"#{product_id}: language {body.get('lang')!r} is unknown or unsupported.")
    consistent = body.get("consistent")
    if not isinstance(consistent, bool):
        raise LinkReadError(f"#{product_id}: the answer has no consistency verdict.")
    issues = body.get("issues")
    if not isinstance(issues, list):
        raise LinkReadError(f"#{product_id}: the answer has no issue list.")
    if not consistent or issues:
        codes = ", ".join(str(i.get("code")) if isinstance(i, Mapping) else repr(i) for i in issues) or "unspecified"
        raise LinkConflictError(f"#{product_id}: Polylang group is inconsistent ({codes}).",
                                [i for i in issues if isinstance(i, Mapping)])
    translations = _parse_translations(body.get("translations"), product_id, lang)
    members = body.get("members")
    return GroupRead(product_id, lang, str(body.get("status") or ""), str(body.get("sku") or ""), translations,
                     dict(members) if isinstance(members, Mapping) else {})


def parse_translation_response(
    body: Any, product_id: int, *, mechanism: str, verified_on_staging: bool
) -> ma.PolylangLinkMap:
    """Strict parse of one endpoint answer for a VI product."""
    group = parse_group_response(body, product_id)
    if group.lang != wtg.SOURCE_LANGUAGE:
        raise LinkReadError(f"#{product_id} is {group.lang}, not a VI source.")
    return ma.PolylangLinkMap(source_id=product_id, translations=dict(group.translations),
                              mechanism=mechanism, verified_on_staging=verified_on_staging)


def _parse_translations(raw: Any, product_id: int, lang: str) -> dict[str, int]:
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
    return translations


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def is_loopback_http(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


class EndpointLinkReader:
    """GET-only client of the tsyc/v1 endpoint."""

    def __init__(
        self,
        *,
        store_url: str,
        auth: Any,
        verified_on_staging: bool = False,
        mechanism: str = MECHANISM_NAME,
        timeout_seconds: int = 30,
        http_get: Callable[..., Any] = requests.get,
        allow_loopback_http: bool = False,
    ) -> None:
        parsed = urlparse(store_url)
        loopback_ok = allow_loopback_http and is_loopback_http(store_url)
        if (parsed.scheme != "https" and not loopback_ok) or not parsed.netloc:
            raise StagingTargetError(f"Refusing a non-https store URL: {store_url!r}.")
        self.base = f"{store_url.rstrip('/')}/wp-json/{ENDPOINT_NAMESPACE}"
        self.auth = auth
        self.mechanism = mechanism
        self.verified_on_staging = verified_on_staging
        self.timeout = timeout_seconds
        self.http_get = http_get

    def _get(self, path: str, label: str) -> Any:
        try:
            response = self.http_get(f"{self.base}/{path}", auth=self.auth,
                                     headers={"Accept": "application/json"}, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - any failure means unknown links
            raise LinkReadError(f"{label}: {type(error).__name__}: {error}") from error
        status = getattr(response, "status_code", None)
        if status != 200:
            # A 404 is an unknown product or a missing endpoint, never "no links".
            raise LinkReadError(f"{label}: HTTP {status}.")
        try:
            return response.json()
        except Exception as error:  # noqa: BLE001
            raise LinkReadError(f"{label}: unreadable body ({type(error).__name__}).") from error

    def read_translations(self, product_id: int) -> ma.PolylangLinkMap:
        body = self._get(f"translations/{int(product_id)}", f"#{product_id}")
        return parse_translation_response(body, int(product_id), mechanism=self.mechanism,
                                          verified_on_staging=self.verified_on_staging)

    def read_group(self, product_id: int) -> GroupRead:
        """The group of a product of any language."""
        return parse_group_response(self._get(f"translations/{int(product_id)}", f"#{product_id}"), int(product_id))

    def read_request(self, request_id: str) -> RequestRead:
        """Reconciliation read. found=False is an explicit answer from the
        store; any failed read raises and never means "absent"."""
        body = self._get(f"translations/by-request/{request_id}", f"request {request_id}")
        if not isinstance(body, Mapping) or body.get("request_id") != request_id:
            raise LinkReadError(f"request {request_id}: the answer is for another request.")
        found = body.get("found")
        if found is False:
            return RequestRead(request_id, False)
        if found is not True or _int(body.get("product_id")) is None:
            raise LinkReadError(f"request {request_id}: malformed answer.")
        group = body.get("group")
        translations: dict[str, int] = {}
        consistent = False
        if isinstance(group, Mapping) and isinstance(group.get("translations"), Mapping):
            consistent = group.get("consistent") is True
            translations = {str(k).lower(): int(v) for k, v in group["translations"].items() if _int(v) is not None}
        return RequestRead(request_id, True, _int(body.get("product_id")), str(body.get("status") or ""),
                           str(body.get("lang") or "").lower(), str(body.get("state") or ""),
                           _int(body.get("source_id")), translations, consistent)


def require_staging_target(target_url: str, production_url: str, *, allow_loopback_http: bool = False) -> None:
    """A staging pilot step may run only against a different https host
    than production (POLYLANG_WOOCOMMERCE_PLAN.md section 4a), or, when the
    caller opts in, the plain-http loopback address of the local lab."""
    target, production = urlparse(target_url), urlparse(production_url)
    if allow_loopback_http and is_loopback_http(target_url):
        return
    if target.scheme != "https" or not target.hostname:
        raise StagingTargetError(f"Staging URL must be https with a host: {target_url!r}.")
    if not production.hostname:
        raise StagingTargetError("The production URL is unknown; cannot prove the target is not production.")
    if target.hostname.lower().removeprefix("www.") == production.hostname.lower().removeprefix("www."):
        raise StagingTargetError(f"{target.hostname} is the production host.")
