"""Client of the bridge's write routes. Staging and the local lab only.

The constructor runs require_staging_target, so the production host cannot be
addressed, whatever the caller passes. The bridge itself does not register
these routes on production either; both locks must fail for a write to land.

Every answer is sorted into exactly one kind (CLAUDE.md 2.6):

  CONFIRMED  the bridge read the link back and said so;
  REFUSED    a deterministic 4xx: nothing new was created by this call,
             except a side-effect stop, which names the draft it left behind;
  UNCERTAIN  a timeout, a broken connection, a 5xx or a malformed success:
             the product may exist. Reconcile before anything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from src.domain import multilingual_adapter as ma
from src.domain import woo_translation_group as wtg
from src.services import polylang_link_reader as plr

CONFIRMED = "CONFIRMED"
REFUSED = "REFUSED"
UNCERTAIN = "UNCERTAIN"

INVENTORY_PROFILE_FAHASA_PREORDER = "FAHASA_PREORDER"
BRIDGE_STATE_LINKED = "LINKED"
BRIDGE_BUSY = "tsyc_busy"
# Protected values moved during the operation. The bridge stopped and repaired
# nothing, so the whole book stops for a person (docs/POLYLANG_BRIDGE.md).
BRIDGE_SIDE_EFFECT = "tsyc_sibling_side_effect"


@dataclass(frozen=True)
class CreateRequest:
    source_id: int
    lang: str
    request_id: str
    authorization_id: str
    internal_product_id: str
    product_code: str
    expected_source_modified_gmt: str
    content: Mapping[str, str]
    inventory_profile: str = INVENTORY_PROFILE_FAHASA_PREORDER

    def as_body(self) -> dict[str, Any]:
        ma.assert_text_only(self.content)
        unknown = set(self.content) - set(wtg.TRANSLATED_FIELDS)
        if unknown:
            raise ValueError(f"Content carries fields beyond the text: {sorted(unknown)}")
        return {
            "source_id": int(self.source_id),
            "lang": self.lang,
            "request_id": self.request_id,
            "authorization_id": self.authorization_id,
            "internal_product_id": self.internal_product_id,
            "product_code": self.product_code,
            "expected_source_modified_gmt": self.expected_source_modified_gmt,
            "inventory_profile": self.inventory_profile,
            "content": {name: str(self.content[name]) for name in wtg.TRANSLATED_FIELDS},
        }


@dataclass(frozen=True)
class CreateOutcome:
    kind: str
    request_id: str
    code: str | None = None
    detail: str | None = None
    http_status: int | None = None
    product_id: int | None = None
    modified_gmt: str | None = None
    text: Mapping[str, str] | None = None
    translations: Mapping[str, int] = field(default_factory=dict)
    replayed: bool = False
    changes: tuple[Mapping[str, Any], ...] = ()


def _refusal_data(body: Any) -> Mapping[str, Any]:
    data = body.get("data") if isinstance(body, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def _named_product(data: Mapping[str, Any]) -> int | None:
    """The product a refusal names: its own partial draft, or the one in the way."""
    pid = data.get("product_id")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def _changes(data: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    changes = data.get("changes")
    return tuple(c for c in changes if isinstance(c, Mapping)) if isinstance(changes, list) else ()


def parse_create_response(status: int | None, body: Any, request: CreateRequest) -> CreateOutcome:
    """Sort one HTTP answer. A success is believed only when it carries the
    bridge's own read-back of exactly this request, language and source."""
    rid = request.request_id
    if status in (200, 201):
        problem = _success_problem(body, request)
        if problem:
            return CreateOutcome(UNCERTAIN, rid, "MALFORMED_SUCCESS", problem, status)
        return CreateOutcome(CONFIRMED, rid, None, None, status, int(body["product_id"]), str(body["modified_gmt"]),
                             dict(body["text"]), {str(k): int(v) for k, v in body["translations"].items()},
                             body.get("replayed") is True)
    code = body.get("code") if isinstance(body, Mapping) else None
    message = body.get("message") if isinstance(body, Mapping) else None
    if code == BRIDGE_BUSY:
        # Another writer holds the lock and may be creating this very product:
        # this call changed nothing, but the outcome is not known yet.
        return CreateOutcome(UNCERTAIN, rid, code, str(message), status)
    if isinstance(status, int) and 400 <= status < 500 and isinstance(code, str) and code.startswith(("tsyc_", "rest_")):
        data = _refusal_data(body)
        return CreateOutcome(REFUSED, rid, code, str(message), status, product_id=_named_product(data), changes=_changes(data))
    # A 5xx, or a 4xx that is not the bridge's (a proxy, a WAF): unknown effect.
    return CreateOutcome(UNCERTAIN, rid, str(code) if code else "UNEXPECTED_ANSWER", str(message), status)


def _success_problem(body: Any, request: CreateRequest) -> str | None:
    if not isinstance(body, Mapping):
        return "The success answer is not an object."
    if body.get("operation_id") != request.request_id:
        return f"The answer is for operation {body.get('operation_id')!r}."
    if body.get("state") != BRIDGE_STATE_LINKED:
        return f"Bridge state {body.get('state')!r}, not linked."
    if body.get("lang") != request.lang or body.get("source_id") != request.source_id:
        return "The answer names another language or source."
    if body.get("status") != "draft":
        return f"The product is {body.get('status')!r}, not a draft."
    product_id = body.get("product_id")
    if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
        return "The answer has no product ID."
    translations = body.get("translations")
    if not isinstance(translations, Mapping) or translations.get(request.lang) != product_id \
            or translations.get(wtg.SOURCE_LANGUAGE) != request.source_id:
        return "The answer does not link the product to its source."
    read_back = body.get("read_back")
    if not isinstance(read_back, Mapping) or read_back.get("verified") is not True:
        return "The answer has no verified read-back."
    if not isinstance(body.get("text"), Mapping) or not isinstance(body.get("modified_gmt"), str):
        return "The answer has no baseline."
    return None


class BridgeDraftWriter:
    """Create-and-link through POST tsyc/v1/translations. One attempt per call:
    this class never retries. Retrying is the runner's decision, after a read."""

    def __init__(
        self,
        *,
        store_url: str,
        production_url: str,
        auth: Any,
        write_token: str,
        verified_on_staging: bool = False,
        allow_loopback_http: bool = False,
        timeout_seconds: int = 120,
        http_post: Callable[..., Any] = requests.post,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        plr.require_staging_target(store_url, production_url, allow_loopback_http=allow_loopback_http)
        if urlparse(store_url).hostname is None:
            raise plr.StagingTargetError(f"No host in {store_url!r}.")
        self.base = f"{store_url.rstrip('/')}/wp-json/{plr.ENDPOINT_NAMESPACE}"
        self.auth = auth
        self.write_token = write_token
        self.mechanism = plr.MECHANISM_NAME
        self.name = plr.MECHANISM_NAME
        self.verified_on_staging = verified_on_staging
        self.timeout = timeout_seconds
        self.http_post = http_post
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-TSYC-Write-Token": self.write_token, **self.extra_headers}

    def create_translation_draft(self, request: CreateRequest) -> CreateOutcome:
        body = request.as_body()
        try:
            response = self.http_post(f"{self.base}/translations", auth=self.auth, headers=self._headers(),
                                      json=body, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - the request may have reached the store
            return CreateOutcome(UNCERTAIN, request.request_id, "NO_RESPONSE", f"{type(error).__name__}: {error}")
        status = getattr(response, "status_code", None)
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            parsed = None
        return parse_create_response(status, parsed, request)

    def rollback(self, request_id: str, expected_modified_gmt: str) -> CreateOutcome:
        """Undo one create. Refused by the bridge once the draft changed."""
        try:
            response = self.http_post(f"{self.base}/translations/rollback", auth=self.auth, headers=self._headers(),
                                      json={"request_id": request_id, "expected_modified_gmt": expected_modified_gmt},
                                      timeout=self.timeout)
            status, parsed = response.status_code, response.json()
        except Exception as error:  # noqa: BLE001
            return CreateOutcome(UNCERTAIN, request_id, "NO_RESPONSE", f"{type(error).__name__}: {error}")
        if status == 200 and isinstance(parsed, Mapping) and parsed.get("rolled_back") is True:
            return CreateOutcome(CONFIRMED, request_id, None, None, status, parsed.get("product_id"))
        code = parsed.get("code") if isinstance(parsed, Mapping) else None
        message = parsed.get("message") if isinstance(parsed, Mapping) else None
        kind = REFUSED if 400 <= status < 500 else UNCERTAIN
        data = _refusal_data(parsed)
        return CreateOutcome(kind, request_id, str(code), str(message), status, product_id=_named_product(data), changes=_changes(data))
