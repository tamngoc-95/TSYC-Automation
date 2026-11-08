"""Static inventory of WordPress hooks in PHP source (no I/O, nothing executed).

Built for the developer's flatsome-child and bulk-link source, which TSYC has not
seen yet. It reads text only: PHP is never run, included or passed to an interpreter.

It records, for every add_action / add_filter:
  hook, callback, priority, file and line, product fields read and written,
  trigger conditions, whether it can reach sibling translations, recursion and
  conflicts, and whether the hook really fires during a bridge create (from the
  lab's measured events).

Everything derived is a lead for a person, never a result: `direction` is a
heuristic over call names. Only the lab run with the real code (Stage D in
docs/POLYLANG_BRIDGE.md) can prove behaviour.

Credentials: string values are never copied into the report. A hook name or meta
key appears only if it looks like an identifier; conditions print other strings
as '...'; suspected secrets are reported by file, line and kind, without the value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

TOOL_VERSION = "1.0.0"

# ---------------------------------------------------------------- lexer


@dataclass(frozen=True)
class Token:
    kind: str  # ID, VAR, STR, NUM, OP
    text: str
    line: int
    value: str | None = None  # Decoded value of a STR with no interpolation.


_ID = re.compile(r"[A-Za-z_\x80-\U0010ffff\\][A-Za-z0-9_\x80-\U0010ffff\\]*")
_VAR = re.compile(r"\$+[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*")
_NUM = re.compile(r"\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|0[xX][0-9a-fA-F]+")
_HEREDOC = re.compile(r"<<<[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1\r?\n")
_OPS = ("<=>", "===", "!==", "**=", "...", "<<=", ">>=", "??=", "?->", "::", "->", "=>", "==", "!=", "<>", "<=", ">=",
        "&&", "||", "??", "++", "--", "+=", "-=", "*=", "/=", ".=", "%=", "&=", "|=", "^=", "<<", ">>", "**")


def lex(source: str) -> list[Token]:
    """PHP tokens with inline HTML, comments and whitespace removed. Unterminated
    constructs end the file quietly: broken source must not stop an inventory."""
    tokens: list[Token] = []
    i, line, end = 0, 1, len(source)

    def advance(to: int) -> None:
        nonlocal i, line
        line += source.count("\n", i, to)
        i = to

    def next_open() -> None:
        found = [p for p in (source.find("<?php", i), source.find("<?=", i)) if p >= 0]
        if not found:
            advance(end)
            return
        at = min(found)
        advance(at + (5 if source.startswith("<?php", at) else 3))

    next_open()
    while i < end:
        ch = source[i]
        if ch in " \t\r\n":
            advance(i + 1)
        elif source.startswith("?>", i):
            advance(i + 2)
            next_open()
        elif source.startswith("//", i) or (ch == "#" and not source.startswith("#[", i)):
            stop = len(source)
            for marker in ("\n", "?>"):
                at = source.find(marker, i)
                if at >= 0:
                    stop = min(stop, at)
            advance(stop)
        elif source.startswith("/*", i):
            at = source.find("*/", i + 2)
            advance(at + 2 if at >= 0 else end)
        elif ch == "'" or ch == '"':
            start_line = line
            j = i + 1
            while j < end and source[j] != ch:
                j += 2 if source[j] == "\\" else 1
            raw = source[i + 1:min(j, end)]
            advance(min(j + 1, end))
            dynamic = ch == '"' and re.search(r"(?<!\\)\$[A-Za-z_{]|\{\$", raw) is not None
            value = None if dynamic else re.sub(r"\\(.)", r"\1", raw) if ch == '"' else raw.replace("\\'", "'").replace("\\\\", "\\")
            tokens.append(Token("STR", raw, start_line, value))
        elif (heredoc := _HEREDOC.match(source, i)) is not None:
            start_line = line
            quote, label = heredoc.group(1), heredoc.group(2)
            close = re.compile(rf"^[ \t]*{label}\b", re.M).search(source, heredoc.end())
            stop = close.start() if close else end
            raw = source[heredoc.end():stop]
            advance(close.end() if close else end)
            dynamic = quote != "'" and re.search(r"\$[A-Za-z_{]|\{\$", raw) is not None
            tokens.append(Token("STR", raw, start_line, None if dynamic else raw))
        elif (var := _VAR.match(source, i)) is not None:
            tokens.append(Token("VAR", var.group(0), line))
            advance(var.end())
        elif (identifier := _ID.match(source, i)) is not None:
            tokens.append(Token("ID", identifier.group(0), line))
            advance(identifier.end())
        elif (number := _NUM.match(source, i)) is not None:
            tokens.append(Token("NUM", number.group(0), line))
            advance(number.end())
        else:
            op = next((o for o in _OPS if source.startswith(o, i)), ch)
            tokens.append(Token("OP", op, line))
            advance(i + len(op))
    return tokens


def _matching(tokens: Sequence[Token], start: int) -> int:
    """Index of the bracket closing the one at start; len(tokens) when unclosed."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[tokens[start].text]]
    for k in range(start + 1, len(tokens)):
        token = tokens[k]
        if token.kind != "OP":
            continue
        if token.text in pairs:
            stack.append(pairs[token.text])
        elif stack and token.text == stack[-1]:
            stack.pop()
            if not stack:
                return k
    return len(tokens)


def _split_args(tokens: Sequence[Token], open_at: int) -> tuple[list[list[Token]], int]:
    close = _matching(tokens, open_at)
    args: list[list[Token]] = [[]]
    depth = 0
    for token in tokens[open_at + 1:close]:
        if token.kind == "OP":
            if token.text in "([{":
                depth += 1
            elif token.text in ")]}":
                depth -= 1
            elif token.text == "," and depth == 0:
                args.append([])
                continue
        args[-1].append(token)
    if args and not args[-1]:
        args.pop()
    return args, close


# ------------------------------------------------------- what the calls mean

META_WRITERS = {"update_post_meta": 1, "add_post_meta": 1, "delete_post_meta": 1,
                "update_metadata": 2, "add_metadata": 2, "delete_metadata": 2}
META_READERS = {"get_post_meta": 1, "get_metadata": 2, "metadata_exists": 2}
META_FIELD = {"_stock": "stock_quantity", "_manage_stock": "manage_stock", "_backorders": "backorders", "_stock_status": "stock_status",
              "_regular_price": "regular_price", "_sale_price": "sale_price", "_price": "price", "_sku": "sku",
              "_thumbnail_id": "image_id", "_product_image_gallery": "gallery_image_ids", "_weight": "weight", "_length": "length",
              "_width": "width", "_height": "height", "_product_attributes": "attributes"}
PRODUCT_PROPS = frozenset({"stock_quantity", "manage_stock", "backorders", "stock_status", "regular_price", "sale_price", "price", "sku",
                           "status", "name", "description", "short_description", "image_id", "gallery_image_ids", "weight", "length",
                           "width", "height", "attributes", "category_ids", "tag_ids", "date_on_sale_from", "date_on_sale_to", "slug",
                           "parent_id", "catalog_visibility", "featured", "virtual", "downloadable", "tax_status", "tax_class"})
# The fields whose movement between languages is the open question.
SYNC_FIELDS = frozenset({"stock_quantity", "manage_stock", "backorders", "stock_status", "regular_price", "sale_price", "price", "sku"})
FUNCTION_WRITES = {"wc_update_product_stock": ("stock_quantity", "stock_status"), "wc_update_product_stock_status": ("stock_status",),
                   "wp_update_post": ("post",), "wp_insert_post": ("post",), "wp_delete_post": ("post",), "wp_trash_post": ("post",),
                   "wp_set_object_terms": ("terms",), "wp_set_post_terms": ("terms",), "wp_add_object_terms": ("terms",),
                   "pll_save_post_translations": ("polylang_link",), "pll_set_post_language": ("polylang_language",)}
SIBLING_APIS = frozenset({"pll_get_post_translations", "pll_get_post", "pll_save_post_translations", "pll_set_post_language", "pll_languages_list"})
LANGUAGE_CHECKS = frozenset({"pll_default_language", "pll_current_language", "pll_get_post_language"})
# What a call fires, for the recursion check. Measured on the lab where a scenario covers it.
FIRES = {"product_save": ("woocommerce_update_product", "woocommerce_new_product", "woocommerce_before_product_object_save",
                          "woocommerce_after_product_object_save", "woocommerce_product_object_updated_props", "save_post",
                          "save_post_product", "wp_insert_post", "post_updated", "edit_post"),
         "wp_update_post": ("pre_post_update", "post_updated", "edit_post", "save_post", "save_post_product", "wp_insert_post", "transition_post_status"),
         "wp_insert_post": ("save_post", "save_post_product", "wp_insert_post", "transition_post_status"),
         "wp_trash_post": ("wp_trash_post", "trashed_post", "transition_post_status", "save_post", "save_post_product", "wp_insert_post"),
         "wp_delete_post": ("before_delete_post", "delete_post", "deleted_post"),
         "update_post_meta": ("update_post_meta", "updated_post_meta", "added_post_meta", "updated_postmeta", "update_post_metadata"),
         "add_post_meta": ("add_post_meta", "added_post_meta", "add_post_metadata"),
         "delete_post_meta": ("delete_post_meta", "deleted_post_meta", "delete_post_metadata"),
         "wc_update_product_stock": ("woocommerce_product_set_stock", "woocommerce_updated_product_stock", "woocommerce_product_set_stock_status"),
         "wc_update_product_stock_status": ("woocommerce_product_set_stock_status",),
         "wp_set_object_terms": ("set_object_terms",), "wp_set_post_terms": ("set_object_terms",), "wp_add_object_terms": ("set_object_terms",),
         "pll_save_post_translations": ("set_object_terms",), "pll_set_post_language": ("set_object_terms",)}
GUARD_NAMES = {"wp_is_post_revision": "NOT_REVISION", "wp_is_post_autosave": "NOT_AUTOSAVE", "is_admin": "ADMIN_ONLY",
               "current_user_can": "CAPABILITY", "doing_action": "DOING_ACTION", "did_action": "DID_ACTION", "doing_filter": "DOING_ACTION",
               "wp_doing_ajax": "AJAX", "wp_doing_cron": "CRON", "check_admin_referer": "NONCE", "wp_verify_nonce": "NONCE",
               "get_post_type": "POST_TYPE", "is_product": "PRODUCT_PAGE", "remove_action": "REMOVES_A_HOOK", "remove_filter": "REMOVES_A_HOOK",
               "pll_default_language": "DEFAULT_LANGUAGE_CHECK", "pll_get_post_language": "LANGUAGE_CHECK", "pll_current_language": "LANGUAGE_CHECK"}
RECURSION_GUARDS = frozenset({"REMOVES_A_HOOK", "STATIC_FLAG", "DOING_ACTION", "DID_ACTION", "GLOBAL_FLAG"})
PRODUCT_HOOK = re.compile(r"woocommerce|wc_|product|stock|save_post|wp_insert_post|edit_post|post_updated|post_meta|postmeta|transition_post_status"
                          r"|pll_|set_object_terms|trash|duplicate|rest_")
REGISTRARS = {"add_action": "action", "add_filter": "filter"}
DEREGISTRARS = ("remove_action", "remove_filter", "remove_all_actions", "remove_all_filters")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-:./\\]{1,100}$")
_SECRET_NAME = re.compile(r"(pass(word|wd)?|secret|token|api_?key|auth_?key|private_?key|credential|consumer_(key|secret)|app_?password|bearer|\bsalt\b)", re.I)
_SECRET_VALUES = (("WOOCOMMERCE_KEY", re.compile(r"^c[ks]_[0-9a-f]{40}$")), ("PAYMENT_KEY", re.compile(r"^(sk|rk)_live_")),
                  ("AWS_KEY", re.compile(r"^AKIA[0-9A-Z]{16}$")), ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
                  ("BEARER_TOKEN", re.compile(r"^Bearer\s+\S{16,}")), ("JWT", re.compile(r"^eyJ[\w-]{10,}\.[\w-]{10,}\.")),
                  ("URL_WITH_PASSWORD", re.compile(r"://[^/\s:]+:[^/\s@]+@")))
SENSITIVE_FILENAMES = re.compile(r"(^|/)(wp-config[^/]*\.php|\.env[^/]*|.*\.(pem|key|p12|pfx|sql|sql\.gz|dump|bak)|auth\.json|\.htpasswd)$", re.I)


def _shows_safely(value: str) -> bool:
    """Identifier-shaped, not a known secret format and not a long opaque token."""
    if not _SAFE_NAME.match(value) or _SECRET_NAME.search(value) or any(pattern.search(value) for _, pattern in _SECRET_VALUES):
        return False
    opaque = len(value) >= 24 and sum(c.isdigit() for c in value) >= len(value) / 5
    return not opaque


def safe_name(value: str | None) -> str:
    """A string from the source, only if it can be shown without risk."""
    if value is None:
        return "<dynamic>"
    return value if _shows_safely(value) else "<redacted>"


def _render(tokens: Sequence[Token], limit: int = 160) -> str:
    parts: list[str] = []
    for token in tokens:
        if token.kind == "STR":
            shown = token.value if token.value is not None and _shows_safely(token.value) else "..."
            parts.append(f"'{shown}'")
        else:
            parts.append(token.text)
    text = " ".join(parts)
    for op in ("( ", "[ "):
        text = text.replace(op, op.strip())
    for op in (" )", " ]", " ,", " ->", "-> ", " ::", ":: "):
        text = text.replace(op, op.strip())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# -------------------------------------------------------------- structure


@dataclass
class Unit:
    path: str
    tokens: list[Token]
    classes: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Body:
    unit: Unit
    start: int
    end: int


def _short(name: str) -> str:
    return name.rsplit("\\", 1)[-1].lower()


class Index:
    def __init__(self) -> None:
        self.units: list[Unit] = []
        self.functions: dict[str, tuple[Body, int]] = {}
        self.methods_by_name: dict[str, list[str]] = {}

    def add(self, path: str, source: str) -> None:
        unit = Unit(path, lex(source))
        self.units.append(unit)
        tokens = unit.tokens
        for k, token in enumerate(tokens):
            if token.kind != "ID":
                continue
            word = token.text.lower()
            previous = tokens[k - 1].text if k else ""
            if word in ("class", "trait", "interface", "enum") and previous not in ("::", "->", "new") \
                    and k + 1 < len(tokens) and tokens[k + 1].kind == "ID":
                brace = _next_op(tokens, k, "{")
                if brace is not None:
                    unit.classes.append((_short(tokens[k + 1].text), brace, _matching(tokens, brace)))
        for k, token in enumerate(tokens):
            if token.kind != "ID" or token.text.lower() != "function" or (k and tokens[k - 1].text in ("::", "->")):
                continue
            name_at = k + 1
            if name_at < len(tokens) and tokens[name_at].text == "&":
                name_at += 1
            if name_at >= len(tokens) or tokens[name_at].kind != "ID":
                continue  # A closure: read where it is registered.
            brace = _body_brace(tokens, name_at + 1)
            if brace is None:
                continue
            owner = _class_at(unit, k)
            short = _short(tokens[name_at].text)
            key = f"{owner}::{short}" if owner else short
            self.functions.setdefault(key, (Body(unit, brace + 1, _matching(tokens, brace)), tokens[name_at].line))
            if owner:
                self.methods_by_name.setdefault(short, []).append(key)


def _next_op(tokens: Sequence[Token], start: int, op: str) -> int | None:
    for k in range(start, min(start + 200, len(tokens))):
        if tokens[k].kind == "OP" and tokens[k].text == op:
            return k
        if tokens[k].kind == "OP" and tokens[k].text == ";":
            return None
    return None


def _body_brace(tokens: Sequence[Token], paren_from: int) -> int | None:
    if paren_from >= len(tokens) or tokens[paren_from].text != "(":
        return None
    return _next_op(tokens, _matching(tokens, paren_from), "{")


def _class_at(unit: Unit, index: int) -> str | None:
    inside = [c for c in unit.classes if c[1] < index < c[2]]
    return max(inside, key=lambda c: c[1])[0] if inside else None


# --------------------------------------------------------------- analysis


@dataclass
class Effects:
    writes: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    sibling_apis: set[str] = field(default_factory=set)
    language_checks: set[str] = field(default_factory=set)
    fires: set[str] = field(default_factory=set)
    custom_fires: set[str] = field(default_factory=set)
    guards: set[str] = field(default_factory=set)
    conditions: list[str] = field(default_factory=list)
    calls: set[str] = field(default_factory=set)
    raw_sql: bool = False
    dynamic_meta: set[str] = field(default_factory=set)
    meta_mentioned: set[str] = field(default_factory=set)
    unresolved_calls: set[str] = field(default_factory=set)

    def merge(self, other: "Effects") -> None:
        for name in ("writes", "reads", "sibling_apis", "language_checks", "fires", "custom_fires", "guards", "calls",
                     "dynamic_meta", "meta_mentioned", "unresolved_calls"):
            getattr(self, name).update(getattr(other, name))
        self.raw_sql = self.raw_sql or other.raw_sql
        for condition in other.conditions:
            if condition not in self.conditions:
                self.conditions.append(condition)


def _meta_field(key: str) -> str:
    return META_FIELD.get(key, key)


def _literal(arg: Sequence[Token]) -> str | None:
    return arg[0].value if len(arg) == 1 and arg[0].kind == "STR" else None


def _dynamic_pattern(arg: Sequence[Token]) -> str | None:
    """A hook name built by concatenation, with the unknown parts shown as {}."""
    parts: list[str] = []
    for token in arg:
        if token.kind == "STR" and token.value is not None:
            parts.append(safe_name(token.value))
        elif token.kind == "OP" and token.text == ".":
            continue
        elif not parts or parts[-1] != "{}":
            parts.append("{}")
    joined = "".join(parts)
    return joined if joined.strip("{}") else None


class Analyzer:
    def __init__(self, index: Index) -> None:
        self.index = index
        self.memo: dict[str, Effects] = {}
        self.active: set[str] = set()

    def of_function(self, key: str) -> Effects:
        if key in self.memo:
            return self.memo[key]
        if key in self.active or key not in self.index.functions:
            return Effects()
        self.active.add(key)
        result = self.of_body(self.index.functions[key][0])
        self.active.discard(key)
        self.memo[key] = result
        return result

    def of_body(self, body: Body) -> Effects:
        effects = Effects()
        tokens = body.unit.tokens
        owner = _class_at(body.unit, body.start)
        k = body.start
        while k < body.end:
            token = tokens[k]
            if token.kind == "STR" and token.value and re.fullmatch(r"_[a-z][a-z0-9_]{2,60}", token.value):
                effects.meta_mentioned.add(_meta_field(token.value))
            if token.kind == "VAR" and token.text in ("$_POST", "$_GET", "$_REQUEST"):
                effects.reads.add(token.text)
            if token.kind == "VAR" and token.text == "$wpdb" and k + 2 < body.end and tokens[k + 1].text == "->" \
                    and tokens[k + 2].text.lower() in ("query", "update", "insert", "replace", "delete"):
                effects.raw_sql = True
            if token.kind == "ID":
                word = token.text.lower()
                if word == "if" and k + 1 < body.end and tokens[k + 1].text == "(":
                    close = _matching(tokens, k + 1)
                    condition = _render(tokens[k + 2:close])
                    if condition and condition not in effects.conditions:
                        effects.conditions.append(condition)
                    if "doing_autosave" in condition.lower():
                        effects.guards.add("NOT_AUTOSAVE")
                elif word == "static" and k + 1 < body.end and tokens[k + 1].kind == "VAR":
                    effects.guards.add("STATIC_FLAG")
                elif word == "global" and k + 1 < body.end and tokens[k + 1].kind == "VAR" and tokens[k + 1].text != "$wpdb":
                    effects.guards.add("GLOBAL_FLAG")
                elif k + 1 < body.end and tokens[k + 1].text == "(":
                    self._call(body, k, owner, effects)
            k += 1
        return effects

    def _call(self, body: Body, k: int, owner: str | None, effects: Effects) -> None:
        tokens = body.unit.tokens
        name = _short(tokens[k].text)
        previous = tokens[k - 1].text if k else ""
        args, _close = _split_args(tokens, k + 1)
        if previous in ("->", "?->"):
            self._method_call(tokens[k - 2] if k >= 2 else None, name, args, owner, effects)
            return
        if previous == "::":
            target = tokens[k - 2].text.lower() if k >= 2 else ""
            cls = owner if target in ("self", "static", "parent") else _short(target)
            self._follow(f"{cls}::{name}", name, effects)
            return
        if previous == "function" or name in ("if", "foreach", "for", "while", "switch", "array", "isset", "empty", "catch", "return", "list", "match"):
            return
        if name in META_WRITERS:
            position = META_WRITERS[name]
            key = _literal(args[position]) if position < len(args) else None
            if key is not None:
                effects.writes.add(_meta_field(key))
            else:
                effects.dynamic_meta.add(name)
            effects.fires.update(FIRES.get(name, FIRES["update_post_meta"]))
        elif name in META_READERS:
            position = META_READERS[name]
            key = _literal(args[position]) if position < len(args) else None
            if key is not None:
                effects.reads.add(_meta_field(key))
        elif name in FUNCTION_WRITES:
            effects.writes.update(FUNCTION_WRITES[name])
            effects.fires.update(FIRES.get(name, ()))
        if name in SIBLING_APIS:
            effects.sibling_apis.add(name)
        if name in LANGUAGE_CHECKS:
            effects.language_checks.add(name)
        if name in GUARD_NAMES:
            effects.guards.add(GUARD_NAMES[name])
        if name == "pll":
            effects.sibling_apis.add("PLL()")
        if name in ("do_action", "apply_filters", "do_action_ref_array", "apply_filters_ref_array") and args:
            custom = _literal(args[0])
            effects.custom_fires.add(custom if custom is not None else "<dynamic>")
        if name in self.index.functions:
            effects.calls.add(name)
            effects.merge(self.of_function(name))

    def _method_call(self, receiver: Token | None, name: str, args: Sequence[Sequence[Token]], owner: str | None, effects: Effects) -> None:
        if name.startswith("set_") and name[4:] in PRODUCT_PROPS:
            effects.writes.add(name[4:])
        elif name.startswith("get_") and name[4:] in PRODUCT_PROPS:
            effects.reads.add(name[4:])
        elif name in ("update_meta_data", "add_meta_data", "delete_meta_data"):
            key = _literal(args[0]) if args else None
            if key is not None:
                effects.writes.add(_meta_field(key))
            else:
                effects.dynamic_meta.add(name)
        elif name == "get_meta":
            key = _literal(args[0]) if args else None
            if key is not None:
                effects.reads.add(_meta_field(key))
        elif name == "save" and args == []:
            effects.writes.add("product_save")
            effects.fires.update(FIRES["product_save"])
        elif receiver is not None and receiver.text == "$this" and owner:
            self._follow(f"{owner}::{name}", name, effects)
        else:
            self._follow(None, name, effects)

    def _follow(self, key: str | None, name: str, effects: Effects) -> None:
        if key is not None and key in self.index.functions:
            effects.calls.add(key)
            effects.merge(self.of_function(key))
            return
        candidates = self.index.methods_by_name.get(name, [])
        if len(candidates) == 1:
            # The class of the receiver is unknown; a single method of that name is the only lead.
            effects.calls.add(candidates[0] + " (by name)")
            effects.merge(self.of_function(candidates[0]))
        elif len(candidates) > 1:
            effects.unresolved_calls.add(f"{name} ({len(candidates)} candidates)")


# ----------------------------------------------------------- registrations


@dataclass
class Registration:
    kind: str
    hook: str
    hook_dynamic: bool
    callback: str
    callback_resolved: bool
    priority: Any
    accepted_args: Any
    file: str
    line: int
    callback_file: str | None
    callback_line: int | None
    registered_in: str
    indirect: bool
    effects: Effects


def _resolve_callback(analyzer: Analyzer, unit: Unit, at: int, arg: Sequence[Token]) -> tuple[str, bool, Effects, str | None, int | None]:
    """(label, resolved, effects, file, line) of one callback argument."""
    index = analyzer.index
    owner = _class_at(unit, at)

    def named(key: str, label: str) -> tuple[str, bool, Effects, str | None, int | None]:
        if key in index.functions:
            body, line = index.functions[key]
            return label, True, analyzer.of_function(key), body.unit.path, line
        return label, False, Effects(), None, None

    def method(name: str, cls: str | None, label: str) -> tuple[str, bool, Effects, str | None, int | None]:
        if cls and f"{cls}::{name}" in index.functions:
            return named(f"{cls}::{name}", label)
        candidates = index.methods_by_name.get(name, [])
        return named(candidates[0], label + " (by name)") if len(candidates) == 1 else (label, False, Effects(), None, None)

    if not arg:
        return "<missing>", False, Effects(), None, None
    if len(arg) == 1 and arg[0].kind == "STR":
        value = arg[0].value
        if value is None:
            return "<dynamic>", False, Effects(), None, None
        if "::" in value:
            cls, _, name = value.partition("::")
            return method(_short(name), _short(cls), safe_name(value))
        return named(_short(value), safe_name(value))
    head = arg[0].text.lower()
    closure_at = next((k for k, t in enumerate(arg[:2]) if t.kind == "ID" and t.text.lower() in ("function", "fn")), None)
    if closure_at is not None:
        start = next((k for k, t in enumerate(unit.tokens) if t is arg[closure_at]), None)
        if start is not None:
            if arg[closure_at].text.lower() == "function":
                brace = _body_brace(unit.tokens, start + 1)
                if brace is not None:
                    return "{closure}", True, analyzer.of_body(Body(unit, brace + 1, _matching(unit.tokens, brace))), unit.path, arg[closure_at].line
            else:
                arrow = _next_op(unit.tokens, start, "=>")
                if arrow is not None:
                    end = next((k for k, t in enumerate(unit.tokens) if t is arg[-1]), arrow) + 1
                    return "{arrow fn}", True, analyzer.of_body(Body(unit, arrow + 1, end)), unit.path, arg[closure_at].line
        return "{closure}", False, Effects(), unit.path, arg[closure_at].line
    if head in ("array",) or arg[0].text == "[":
        inner_open = 1 if head == "array" else 0
        inner, _ = _split_args(list(arg), inner_open)
        if len(inner) == 2:
            name = _literal(inner[1])
            if name is None:
                return "<dynamic>", False, Effects(), None, None
            target = inner[0]
            first = target[0].text.lower() if target else ""
            if first in ("$this", "self", "static", "__class__"):
                return method(_short(name), owner, f"{owner or '?'}::{safe_name(name)}")
            literal_class = _literal(target)
            if literal_class is not None:
                return method(_short(name), _short(literal_class), f"{safe_name(literal_class)}::{safe_name(name)}")
            if target and target[0].kind == "ID":
                return method(_short(name), _short(target[0].text), f"{_short(target[0].text)}::{safe_name(name)}")
            return method(_short(name), None, f"{_render(target, 40)}->{safe_name(name)}")
    # First-class callable syntax: foo(...), $this->foo(...), Foo::bar(...)
    if len(arg) >= 4 and arg[-1].text == ")" and arg[-2].text == "..." and arg[-3].text == "(":
        name = _short(arg[-4].text)
        if len(arg) == 4:
            return named(name, name)
        joiner = arg[-5].text
        if joiner == "::":
            first = arg[-6].text.lower()
            cls = owner if first in ("self", "static") else _short(first)
            return method(name, cls, f"{cls}::{name}")
        if arg[-6].text == "$this":
            return method(name, owner, f"{owner or '?'}::{name}")
        return method(name, None, f"->{name}")
    return f"<unresolved {_render(arg, 40)}>", False, Effects(), None, None


def _parse_int_arg(args: Sequence[Sequence[Token]], position: int, default: int) -> Any:
    if position >= len(args):
        return default
    arg = args[position]
    if len(arg) == 1 and arg[0].kind == "NUM":
        return int(float(arg[0].text.replace("_", "")))
    if len(arg) == 2 and arg[0].text == "-" and arg[1].kind == "NUM":
        return -int(float(arg[1].text.replace("_", "")))
    return "<dynamic>" if _render(arg) not in ("PHP_INT_MAX", "PHP_INT_MIN") else _render(arg)


def _enclosing(index: Index, unit: Unit, at: int) -> str:
    best = None
    for key, (body, _line) in index.functions.items():
        if body.unit is unit and body.start <= at <= body.end and (best is None or body.start > best[1]):
            best = (key, body.start)
    return best[0] if best else "file scope"


def find_registrations(index: Index, analyzer: Analyzer) -> tuple[list[Registration], list[dict[str, Any]]]:
    registrations: list[Registration] = []
    removals: list[dict[str, Any]] = []
    for unit in index.units:
        tokens = unit.tokens
        for k, token in enumerate(tokens):
            if token.kind != "ID" or k + 1 >= len(tokens) or tokens[k + 1].text != "(":
                continue
            name = _short(token.text)
            previous = tokens[k - 1].text if k else ""
            if previous == "function":
                continue
            if name in DEREGISTRARS and previous not in ("->", "?->", "::"):
                args, _ = _split_args(tokens, k + 1)
                removals.append({"kind": name, "hook": safe_name(_literal(args[0]) if args else None), "file": unit.path, "line": token.line,
                                 "registered_in": _enclosing(index, unit, k)})
                continue
            if name not in REGISTRARS:
                continue
            args, _ = _split_args(tokens, k + 1)
            if len(args) < 2:
                continue
            literal = _literal(args[0])
            hook = safe_name(literal) if literal is not None else (_dynamic_pattern(args[0]) or "<dynamic>")
            label, resolved, effects, cb_file, cb_line = _resolve_callback(analyzer, unit, k, args[1])
            registrations.append(Registration(
                REGISTRARS[name], hook, literal is None, label, resolved, _parse_int_arg(args, 2, 10), _parse_int_arg(args, 3, 1),
                unit.path, token.line, cb_file, cb_line, _enclosing(index, unit, k), previous in ("->", "?->", "::"), effects))
    return registrations, removals


# ---------------------------------------------------------------- derived


def direction_of(effects: Effects) -> str:
    """A heuristic over call names; never proof of the direction."""
    if not effects.writes and not effects.dynamic_meta and not effects.raw_sql:
        return "NO_WRITE"
    if not effects.sibling_apis:
        return "SAME_PRODUCT_OR_UNKNOWN"
    if "DEFAULT_LANGUAGE_CHECK" in effects.guards:
        return "SIBLINGS_LANGUAGE_CONDITIONAL_CHECK_DEFAULT"
    if effects.language_checks:
        return "SIBLINGS_LANGUAGE_CONDITIONAL"
    return "SIBLINGS_ANY_LANGUAGE"


def _touches_sync(effects: Effects) -> set[str]:
    touched = (effects.writes | effects.meta_mentioned) & SYNC_FIELDS
    return set(touched)


def is_relevant(reg: Registration) -> bool:
    effects = reg.effects
    return bool(PRODUCT_HOOK.search(reg.hook) or effects.writes & (SYNC_FIELDS | {"product_save", "post", "polylang_link", "polylang_language", "terms"})
                or effects.sibling_apis or _touches_sync(effects) or effects.raw_sql)


def recursion_findings(registrations: Sequence[Registration]) -> list[dict[str, Any]]:
    """Hook H -> a callback on H fires H2 -> a callback on H2 ... back to H."""
    by_hook: dict[str, list[Registration]] = {}
    for reg in registrations:
        by_hook.setdefault(reg.hook, []).append(reg)
    edges: dict[str, set[str]] = {}
    for reg in registrations:
        fired = (reg.effects.fires | reg.effects.custom_fires) & set(by_hook)
        if fired:
            edges.setdefault(reg.hook, set()).update(fired)
    cycles: set[tuple[str, ...]] = set()

    def walk(start: str, node: str, path: list[str]) -> None:
        for nxt in sorted(edges.get(node, ())):
            if nxt == start:
                ring = tuple(path)
                pivot = ring.index(min(ring))
                cycles.add(ring[pivot:] + ring[:pivot])
            elif nxt not in path and len(path) < 6:
                walk(start, nxt, path + [nxt])

    for hook in sorted(edges):
        walk(hook, hook, [hook])
    findings = []
    for ring in sorted(cycles):
        members = [reg for hook in ring for reg in by_hook[hook] if (reg.effects.fires | reg.effects.custom_fires) & set(ring)]
        guarded = all(reg.effects.guards & RECURSION_GUARDS for reg in members)
        findings.append({"code": "RECURSION_GUARD_SEEN" if guarded else "RECURSION_RISK", "cycle": list(ring),
                         "callbacks": [f"{r.callback} ({r.file}:{r.line})" for r in members],
                         "note": "Static: a guard was seen in the code, not shown to work." if guarded else
                                 "No recursion guard seen on at least one callback in this cycle."})
    return findings


def conflict_findings(registrations: Sequence[Registration], removals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    writers: dict[tuple[str, str], list[Registration]] = {}
    field_writers: dict[str, list[Registration]] = {}
    seen: dict[tuple[str, str], list[Registration]] = {}
    for reg in registrations:
        seen.setdefault((reg.hook, reg.callback), []).append(reg)
        for written in reg.effects.writes & SYNC_FIELDS:
            writers.setdefault((reg.hook, written), []).append(reg)
            field_writers.setdefault(written, []).append(reg)
    for (hook, written), regs in sorted(writers.items()):
        if len(regs) > 1:
            findings.append({"code": "SAME_FIELD_SAME_HOOK", "hook": hook, "field": written,
                             "callbacks": [f"{r.callback} p{r.priority} ({r.file}:{r.line})" for r in regs],
                             "note": "The higher priority number runs last and wins."})
    for written, regs in sorted(field_writers.items()):
        if len({r.hook for r in regs}) > 1:
            findings.append({"code": "FIELD_WRITTEN_FROM_SEVERAL_HOOKS", "field": written, "hooks": sorted({r.hook for r in regs}),
                             "callbacks": [f"{r.callback} ({r.file}:{r.line})" for r in regs]})
    for (hook, callback), regs in sorted(seen.items()):
        if len(regs) > 1 and callback not in ("{closure}", "{arrow fn}"):
            findings.append({"code": "REGISTERED_TWICE", "hook": hook, "callback": callback, "at": [f"{r.file}:{r.line}" for r in regs]})
    for removal in removals:
        if removal["hook"] in {r.hook for r in registrations} and removal["registered_in"] == "file scope":
            findings.append({"code": "HOOK_REMOVED_AT_FILE_SCOPE", **removal})
    return findings


def bridge_events(measured: Mapping[str, Any] | None) -> tuple[set[str], set[str], set[str]]:
    """(fires during a bridge create, fires during a rollback, reached the source
    during a create) from the lab's measured events."""
    if not measured:
        return set(), set(), set()
    scenarios = measured.get("scenarios", {})
    create, rollback, source = set(), set(), set()
    for name, events in scenarios.items():
        for key, entry in events.items():
            hook = key.split(":", 1)[0]
            if name.startswith("bridge_create"):
                create.add(hook)
                if {"SOURCE", "SIBLING_DRAFT"} & set(entry.get("roles", ())):
                    source.add(hook)
            elif name == "bridge_rollback":
                rollback.add(hook)
    return create, rollback, source


def risk_of(reg: Registration, create: set[str], rollback: set[str], touches_source: set[str]) -> tuple[str, list[str]]:
    effects = reg.effects
    reasons: list[str] = []
    sync = _touches_sync(effects)
    reaches_siblings = bool(effects.sibling_apis)
    on_bridge = reg.hook in create or reg.hook in rollback
    if reaches_siblings and sync and on_bridge:
        reasons.append(f"Can reach sibling products, touches {sorted(sync)}, on a hook a bridge operation fires.")
    if reg.hook in touches_source and (effects.writes or effects.dynamic_meta or effects.raw_sql):
        reasons.append("Writes on a hook measured firing on the VI source or a sibling during a bridge create.")
    if reasons:
        return "HIGH", reasons
    if reg.hook_dynamic and (effects.writes or effects.dynamic_meta or effects.raw_sql):
        reasons.append("Hook name is built at runtime, so a person must work out when it fires.")
    if not reg.callback_resolved and PRODUCT_HOOK.search(reg.hook):
        reasons.append("The callback could not be read statically.")
    if reaches_siblings and (sync or effects.dynamic_meta):
        reasons.append(f"Can reach sibling products and touches {sorted(sync) or 'dynamic meta'}.")
    if effects.raw_sql:
        reasons.append("Writes with raw SQL, which the bridge's write watcher cannot see.")
    if on_bridge and (effects.writes & SYNC_FIELDS or effects.dynamic_meta):
        reasons.append("Writes product fields on a hook a bridge operation fires.")
    if reasons:
        return "MEDIUM", reasons
    if sync or effects.writes:
        return "LOW", [f"Writes {sorted(effects.writes)} but shows no route to siblings."] if effects.writes else [f"Mentions {sorted(sync)}."]
    return "NONE", []


def secret_findings(unit: Unit) -> list[dict[str, Any]]:
    """Suspected credentials by file, line and kind. Never the value."""
    findings: list[dict[str, Any]] = []
    tokens = unit.tokens
    for k, token in enumerate(tokens):
        if token.kind != "STR" or token.value is None or not token.value.strip():
            continue
        for kind, pattern in _SECRET_VALUES:
            if pattern.search(token.value):
                findings.append({"file": unit.path, "line": token.line, "kind": kind})
                break
        else:
            # define('X_PASSWORD', 'value')  $x_token = 'value'  'api_key' => 'value'
            before = tokens[k - 1].text if k else ""
            name = None
            if before == "=" and k >= 2 and tokens[k - 2].kind == "VAR":
                name = tokens[k - 2].text
            elif before == "=>" and k >= 2 and tokens[k - 2].kind == "STR":
                name = tokens[k - 2].value
            elif before == "," and k >= 3 and tokens[k - 2].kind == "STR" and tokens[k - 3].text == "(" and k >= 4 and tokens[k - 4].text.lower() == "define":
                name = tokens[k - 2].value
            if name and _SECRET_NAME.search(name) and len(token.value) >= 8 and " " not in token.value:
                findings.append({"file": unit.path, "line": token.line, "kind": "NAMED_CREDENTIAL"})
    return findings


# ------------------------------------------------------------------ report


def _sorted_fields(values: Iterable[str]) -> list[str]:
    return sorted(safe_name(v) if not v.startswith("$_") else v for v in values)


def build_inventory(sources: Mapping[str, str], *, measured_events: Mapping[str, Any] | None = None,
                    skipped: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """sources: {relative path: PHP text}. Pure: reads nothing and runs nothing."""
    index = Index()
    for path in sorted(sources):
        index.add(path, sources[path])
    analyzer = Analyzer(index)
    registrations, removals = find_registrations(index, analyzer)
    create, rollback, touches_source = bridge_events(measured_events)
    rows: list[dict[str, Any]] = []
    for reg in sorted(registrations, key=lambda r: (r.file, r.line)):
        risk, reasons = risk_of(reg, create, rollback, touches_source)
        effects = reg.effects
        rows.append({
            "kind": reg.kind, "hook": reg.hook, "hook_is_dynamic": reg.hook_dynamic, "callback": reg.callback,
            "callback_resolved": reg.callback_resolved, "priority": reg.priority, "accepted_args": reg.accepted_args,
            "file": reg.file, "line": reg.line, "callback_file": reg.callback_file, "callback_line": reg.callback_line,
            "registered_in": reg.registered_in, "indirect": reg.indirect, "relevant": is_relevant(reg),
            "reads": _sorted_fields(effects.reads), "writes": _sorted_fields(effects.writes),
            "dynamic_meta_writes": sorted(effects.dynamic_meta), "fields_mentioned": _sorted_fields(effects.meta_mentioned & SYNC_FIELDS),
            "raw_sql_write": effects.raw_sql, "trigger_conditions": effects.conditions[:12], "guards": sorted(effects.guards),
            "sibling_apis": sorted(effects.sibling_apis), "language_checks": sorted(effects.language_checks),
            "fires": sorted(effects.fires), "fires_custom": _sorted_fields(effects.custom_fires),
            "calls": _sorted_fields(c.split(" ")[0] for c in effects.calls), "unresolved_calls": sorted(effects.unresolved_calls),
            "direction_guess": direction_of(effects), "fires_on_bridge_create": reg.hook in create,
            "fires_on_bridge_rollback": reg.hook in rollback, "measured_on_source_or_sibling": reg.hook in touches_source,
            "risk": risk, "risk_reasons": reasons,
        })
    secrets = [finding for unit in index.units for finding in secret_findings(unit)]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["risk"]] = counts.get(row["risk"], 0) + 1
    return {
        "tool_version": TOOL_VERSION, "status": "INVENTORIED", "php_files": len(sources),
        "registrations": len(rows), "relevant": sum(1 for r in rows if r["relevant"]), "risk_counts": counts,
        "unresolved_callbacks": sum(1 for r in rows if not r["callback_resolved"]),
        "measured_events_used": bool(measured_events), "items": rows, "removals": list(removals),
        "recursion": recursion_findings(registrations), "conflicts": conflict_findings(registrations, removals),
        "suspected_secrets": secrets, "skipped": list(skipped),
        "reading_note": "Static leads for a person. direction_guess is a heuristic; nothing here proves behaviour. Run the lab with the real code.",
    }


def to_markdown(report: Mapping[str, Any], *, relevant_only: bool = True) -> str:
    lines = [f"# PHP hook inventory ({report.get('status')})", "", str(report.get("reading_note", "")), ""]
    if report.get("status") != "INVENTORIED":
        lines.append(str(report.get("detail", "")))
        return "\n".join(lines) + "\n"
    lines += [f"- Files: {report['php_files']}. Registrations: {report['registrations']}, relevant {report['relevant']}, "
              f"unresolved callbacks {report['unresolved_callbacks']}.",
              f"- Risk: {report['risk_counts']}. Measured bridge events used: {report['measured_events_used']}.",
              f"- Suspected secrets: {len(report['suspected_secrets'])}. Skipped: {len(report['skipped'])}.", ""]
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}
    items = [i for i in report["items"] if i["relevant"] or not relevant_only]
    lines += ["| Risk | Hook | Callback | Prio | Where | Reads | Writes | Direction (guess) | Bridge fires it |", "|---|---|---|---|---|---|---|---|---|"]
    for item in sorted(items, key=lambda i: (order[i["risk"]], i["file"], i["line"])):
        reads = ", ".join(i for i in item["reads"] if i in SYNC_FIELDS or i.startswith("$_")) or "-"
        writes = ", ".join(item["writes"]) or ("dynamic meta" if item["dynamic_meta_writes"] else "-")
        bridge = "create" if item["fires_on_bridge_create"] else ("rollback" if item["fires_on_bridge_rollback"] else "-")
        lines.append(f"| {item['risk']} | `{item['hook']}` | `{item['callback']}` | {item['priority']} | {item['file']}:{item['line']} | "
                     f"{reads} | {writes} | {item['direction_guess']} | {bridge} |")
    high = [i for i in items if i["risk"] == "HIGH"]
    if high:
        lines += ["", "## HIGH: read these first", ""]
        for item in high:
            lines += [f"### `{item['hook']}` → `{item['callback']}` ({item['file']}:{item['line']})", ""]
            lines += [f"- {reason}" for reason in item["risk_reasons"]]
            lines += [f"- Sibling APIs: {item['sibling_apis']}. Guards: {item['guards']}."]
            if item["trigger_conditions"]:
                lines += ["- Conditions:"] + [f"  - `{condition}`" for condition in item["trigger_conditions"]]
            lines.append("")
    for title, key in (("Recursion", "recursion"), ("Conflicts", "conflicts")):
        if report[key]:
            lines += ["", f"## {title}", ""] + [f"- {finding}" for finding in report[key]]
    if report["suspected_secrets"]:
        lines += ["", "## Suspected credentials (values deliberately not shown)", ""]
        lines += [f"- {s['file']}:{s['line']} {s['kind']}" for s in report["suspected_secrets"]]
    return "\n".join(lines) + "\n"
