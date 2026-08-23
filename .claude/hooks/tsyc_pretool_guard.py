#!/usr/bin/env python3
"""TSYC Bash/PowerShell PreToolUse guard.

Implements the CLAUDE.md "Stable Automation Policy":

- Every direct execution of a *.py file must use the repository .venv
  Python (CLAUDE.md section 22, "Python Environment"). This applies
  regardless of whether the path has a literal "scripts/" prefix (e.g.
  after `cd scripts`), so venv enforcement cannot be bypassed by cwd.
- Deterministic pipeline-stage scripts (CLAUDE.md sections 3.1, 4.2, 5.1,
  19.4) are auto-allowed so normal bounded processing does not repeatedly
  interrupt for approval. Auto-allow classification additionally requires
  the script directory component (if any) to literally be "scripts" -- a
  same-named file executed from an unrelated directory is never
  auto-allowed, only ever asked-about at worst.
- create_woocommerce_draft.py (the single required business approval,
  section 6/17) and manual_create_product_reference.py (the human path for
  provenance/identity ambiguity automation could not resolve
  deterministically, section 5.2/10) always require an explicit ask,
  regardless of the directory they are executed from.
- run_batch.py (the orchestrator, section 19) is auto-allowed for ordinary
  bounded batch runs, but asks when invoked with --allow-woo-draft -- the
  orchestrator flag for bounded Woo-draft authorization.
- Python argv semantics are approximated deliberately narrowly: only a
  literal `-m py_compile` immediately after the interpreter (module flags
  aside) is treated as a side-effect-free static check exempt from the
  ask gates. Any other `-m <module>` (cProfile, pdb, trace, ...) truly
  executes the target script and is classified exactly like a direct
  invocation -- this includes a module addressed purely by dotted/bare
  name with no separate *.py argument in argv at all (e.g. `-m
  scripts.create_woocommerce_draft`), which is mapped back to its
  equivalent path rather than silently producing no decision. Bundled
  single-letter flags (`-um`, `-mfoo.bar`, ...) are also recognized.
  `python -c ...` (inline code, no script argument at all) is left
  unclassified -- it is outside this hook scope, not a scripts/*.py
  execution.
- Commands that merely mention a script path as an argument to something
  else (grep, cat, git diff, ...) are left alone.

Known limitations: this is a heuristic command-text classifier, not a
real shell parser or a full CPython argv emulator.
- Adjacent quote-splicing that reconstructs an interpreter or script
  token from fragments with no separating whitespace (for example two
  empty quoted strings spliced into the middle of a bare word) is not
  detected -- defeating that requires full POSIX word-splitting
  semantics, which is out of proportion for a PreToolUse safety net.
  Quoted shell operator characters (e.g. -X "a;b") ARE handled correctly
  and do not fracture command segmentation.
- Only -W/-X are known to consume a value, in either the separate-token
  form (-W value) or the glued/attached form (-Wvalue, e.g. -Wignore::
  DeprecationWarning) -- both are recognized and never routed through
  the -m/-c bundling scan, however many "m"/"c" letters the value itself
  contains. A double-dash long option this hook does not otherwise know
  about (the only one in stock CPython, --check-hash-based-pycs, takes a
  following value) is treated as a plain no-value flag, so its value
  token can be misidentified as the positional script argument and the
  real invocation missed. This is a narrow, obscure case -- none of the
  scripts this hook classifies are ever invoked with such flags in
  normal TSYC operation -- and is accepted rather than hardcoding
  CPython's full flag table. Double-dash long options are otherwise
  always treated as plain flags and never mistaken for -m/-c, no matter
  what letters they contain (the bundling scan below applies only to
  genuine single-dash short-flag tokens).

No JSON output at all means "no decision from this hook" -- the normal
settings.json permission rules apply.
"""
from __future__ import annotations

import json
import re
import sys

# Deterministic pipeline-stage writers/readers (CLAUDE.md 3.1 / 4.2 / 5.1 /
# 19.4): normal bounded processing through these approved scripts must not
# repeatedly ask for approval.
AUTO_ALLOW = {
    "collect_one_facebook_post.py",
    "clean_facebook_raw_pages.py",
    "create_candidates_from_cleaned_posts.py",
    "upload_facebook_images_to_supabase.py",
    "register_reference_source.py",
    "collect_reference_metadata.py",
    "match_candidate_identity.py",
    "create_internal_product.py",
    "review_product_images.py",
    "prepare_product_content.py",
    "sync_woocommerce_product_status.py",
    "check_draft_readiness.py",
}

# Requires a human decision every invocation (CLAUDE.md 6/17 Woo draft
# creation; 5.2/10 manual provenance/identity entry). Matched regardless
# of directory -- extra caution here is always safe.
ASK_ALWAYS = {
    "create_woocommerce_draft.py",
    "manual_create_product_reference.py",
}

RUN_BATCH = "run_batch.py"
WOO_DRAFT_FLAG = "--allow-woo-draft"

# --- shell segmentation ------------------------------------------------

QUOTE_SPAN_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;&|]")
TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _mask_quotes(s: str) -> str:
    """Replace quoted spans with same-length NUL placeholders so operator
    characters inside quotes (e.g. -X "a;b") are never mistaken for
    segment separators, while preserving character offsets."""
    return QUOTE_SPAN_RE.sub(lambda m: "\x00" * len(m.group(0)), s)


def _split_segments(command: str):
    """Split into shell command segments, respecting quoted spans, and
    treating a bare newline the same as ";" (a top-level line break)."""
    for line in command.split("\n"):
        masked = _mask_quotes(line)
        last = 0
        for m in SEGMENT_SPLIT_RE.finditer(masked):
            yield line[last:m.start()]
            last = m.end()
        yield line[last:]


def _clean(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1]
    return token.replace("\\", "/")


def _tokenize(segment: str) -> list[str]:
    return [_clean(t) for t in TOKEN_RE.findall(segment)]


# --- interpreter / argv classification ----------------------------------

PY_TOKEN_RE = re.compile(r"^(python[0-9.]*|py)(\.exe)?$", re.IGNORECASE)
SCRIPT_BASENAME_RE = re.compile(r"^[a-z0-9_.-]+\.py$", re.IGNORECASE)
VALUE_FLAGS = {"-W", "-X"}  # consume the following token as a value


def _classify_tail(tail: list[str]):
    """Approximate CPython argv parsing for the tokens after the
    interpreter. Returns (kind, target, rest):
      kind == "module":      target is the module name after -m
      kind == "inline_code": -c was used (arbitrary code, no script arg)
      kind == "script":      target is the positional script path
      kind is None:          nothing recognizable (e.g. python --version)

    Handles bundled single-letter flags (e.g. -um == -u -m, -mfoo.bar ==
    -m with an attached value): no other real CPython *short* flag letter
    is "m" or "c", so any single-dash bundle containing one of those
    letters is treated as invoking -m/-c at that point, deliberately
    erring toward recognizing module/inline-code mode rather than missing
    it. This scan is deliberately restricted to genuine single-dash
    tokens -- a double-dash long option (e.g. --check-hash-based-pycs,
    which contains "c") is a single flag, never a short-flag bundle, and
    must not be misread as -c/-m.
    """
    idx = 0
    while idx < len(tail):
        t = tail[idx]
        if t in VALUE_FLAGS:
            idx += 2
            continue
        if t[:2] in VALUE_FLAGS and len(t) > 2:
            # Glued/attached value, e.g. -Wignore::DeprecationWarning --
            # the value is already part of this token, so no further
            # token is consumed. Checked before the bundling scan below
            # so a "c" or "m" inside the attached value (e.g. the "c" in
            # "DeprecationWarning") is never misread as -c/-m.
            idx += 1
            continue
        if t.startswith("--"):
            idx += 1
            continue
        if t.startswith("-") and t != "-":
            body = t[1:]
            positions = [p for p in (body.find("m"), body.find("c")) if p != -1]
            if positions:
                special_pos = min(positions)
                letter = body[special_pos]
                attached = body[special_pos + 1:]
                kind = "module" if letter == "m" else "inline_code"
                if attached:
                    return kind, attached, tail[idx + 1:]
                target = tail[idx + 1] if idx + 1 < len(tail) else None
                return kind, target, tail[idx + 2:]
            idx += 1
            continue
        return "script", t, tail[idx + 1:]
    return None, None, []


def _first_py_file(tokens: list[str]) -> str | None:
    """First token that looks like a *.py path, or None."""
    for t in tokens:
        basename = t.rsplit("/", 1)[-1]
        if SCRIPT_BASENAME_RE.match(basename):
            return t
    return None


def _module_as_path(module_name: str) -> str:
    """Derive a pseudo file path for a module addressed by name (e.g.
    `-m scripts.create_woocommerce_draft` or a bare `-m
    create_internal_product` run from inside scripts/), so it is subject
    to the same venv/ask/allow classification as a direct `scripts/x.py`
    invocation instead of silently yielding no decision at all."""
    return module_name.replace(".", "/") + ".py"


def _qualifies_for_auto_allow(path_token: str) -> bool:
    """A bare basename (relies on cwd, e.g. after cd scripts) or a path
    whose immediate parent directory is literally "scripts" qualifies.
    A same-named file executed from an unrelated directory does not --
    it is never auto-allowed, only ever asked-about at worst."""
    parts = path_token.split("/")
    if len(parts) == 1:
        return True
    return parts[-2].lower() == "scripts"


def _find_invocations(command: str):
    """Yield dicts describing every direct python/script execution found
    in `command`. Segments with no python-like executable token
    (grep/cat/git/etc.) are skipped entirely -- never gated by this hook."""
    for segment in _split_segments(command):
        tokens = _tokenize(segment)
        py_idx = next(
            (i for i, t in enumerate(tokens)
             if PY_TOKEN_RE.match(t.rsplit("/", 1)[-1])),
            None,
        )
        if py_idx is None:
            continue
        py_token = tokens[py_idx]
        tail = tokens[py_idx + 1:]
        kind, target, rest = _classify_tail(tail)

        if kind == "inline_code" or kind is None:
            # python -c ... (arbitrary inline code) or nothing
            # recognizable (python --version): not a scripts/*.py
            # execution, and out of this hook scope either way.
            continue

        if kind == "module" and target and target.lower() == "py_compile":
            yield {
                "py_token": py_token,
                "exempt": True,
                "script": None,
                "qualifies": False,
                "tail": tail,
            }
            continue

        if kind == "module":
            # Any other module (-m pdb, -m cProfile, -m trace, ...) truly
            # executes its target -- classify exactly like a direct
            # invocation, never exempt. Prefer an explicit *.py argument
            # (the wrapper-module pattern, e.g. `-m cProfile x.py`); if
            # there isn't one, the module itself is being addressed by
            # name (`-m scripts.x` / a bare `-m x` run from inside
            # scripts/) -- derive its equivalent path instead of silently
            # yielding no decision at all.
            path_token = _first_py_file(rest)
            if path_token is None and target:
                path_token = _module_as_path(target)
        else:  # kind == "script"
            path_token = target

        if path_token is None:
            continue
        basename = path_token.rsplit("/", 1)[-1]
        if not SCRIPT_BASENAME_RE.match(basename):
            continue

        yield {
            "py_token": py_token,
            "exempt": False,
            "script": basename.lower(),
            "qualifies": _qualifies_for_auto_allow(path_token),
            "tail": tail,
        }


def _venv_ok(py_token: str) -> bool:
    parts = py_token.lower().split("/")
    return parts[-3:] == [".venv", "scripts", "python.exe"]


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))


def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        emit("deny", "TSYC guard could not parse request.")
        return
    if event.get("tool_name") not in {"Bash", "PowerShell"}:
        return
    command = str((event.get("tool_input") or {}).get("command", ""))
    invocations = list(_find_invocations(command))
    if not invocations:
        return

    ask_reasons = []
    classifiable = True
    for inv in invocations:
        if not _venv_ok(inv["py_token"]):
            emit("deny", "Use repository .venv/Scripts/python.exe only.")
            return
        if inv["exempt"]:
            continue
        script = inv["script"]
        if script in ASK_ALWAYS:
            ask_reasons.append(script)
        elif script == RUN_BATCH and inv["qualifies"]:
            if WOO_DRAFT_FLAG in (t.lower() for t in inv["tail"]):
                ask_reasons.append(f"{RUN_BATCH} {WOO_DRAFT_FLAG}")
        elif script in AUTO_ALLOW and inv["qualifies"]:
            pass
        else:
            # Unrecognized script, or a recognized auto-allow basename
            # executed from a directory other than scripts/: leave
            # unclassified rather than guessing.
            classifiable = False

    if ask_reasons:
        emit(
            "ask",
            "TSYC business-approval gate requires human authorization: "
            + ", ".join(sorted(set(ask_reasons))),
        )
        return

    if classifiable:
        emit(
            "allow",
            "TSYC deterministic pipeline stage auto-allowed under the "
            "Stable Automation Policy (CLAUDE.md).",
        )
        return

    # Defer to normal settings.json permission rules rather than guessing.


if __name__ == "__main__":
    main()
