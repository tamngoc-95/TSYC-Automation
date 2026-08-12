#!/usr/bin/env python3
"""
TSYC Claude Code PreToolUse guard.

Purpose:
- Require the repository virtual environment for Python scripts under scripts/.
- Force explicit user approval for production-write pipeline scripts.
- Leave read-only commands to the normal Claude Code permission flow.

This hook does not access Supabase, WooCommerce, secrets, browser profiles,
or external services.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


PRODUCTION_WRITE_SCRIPTS = {
    "collect_one_facebook_post.py",
    "clean_facebook_raw_pages.py",
    "create_candidates_from_cleaned_posts.py",
    "upload_facebook_images_to_supabase.py",
    "register_reference_source.py",
    "collect_reference_metadata.py",
    "manual_create_product_reference.py",
    "match_candidate_identity.py",
    "create_internal_product.py",
    "review_product_images.py",
    "prepare_product_content.py",
    "create_woocommerce_draft.py",
    "sync_woocommerce_product_status.py",
}


def emit_decision(decision: str, reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload, ensure_ascii=False))


def normalize_command(command: str) -> str:
    return command.replace("\\", "/").lower()


def script_names_in_command(command: str) -> set[str]:
    normalized = normalize_command(command)
    matches = re.findall(
        r"(?:^|[\s\"'])scripts/([a-z0-9_.-]+\.py)(?:$|[\s\"'])",
        normalized,
    )
    return set(matches)


def main() -> int:
    try:
        raw = sys.stdin.read()
        event: dict[str, Any] = json.loads(raw)
    except Exception:
        emit_decision(
            "deny",
            "TSYC guard could not parse the PreToolUse input. The tool call is blocked.",
        )
        return 0

    tool_name = str(event.get("tool_name", ""))
    if tool_name not in {"Bash", "PowerShell"}:
        return 0

    tool_input = event.get("tool_input") or {}
    command = str(tool_input.get("command", ""))
    normalized = normalize_command(command)

    scripts = script_names_in_command(command)
    if not scripts:
        return 0

    # Any repository Python script under scripts/ must run through the repo venv.
    if ".venv/scripts/python.exe" not in normalized:
        emit_decision(
            "deny",
            "TSYC policy requires repository Python scripts to run with "
            ".venv/Scripts/python.exe. System Python is not allowed.",
        )
        return 0

    protected = sorted(scripts & PRODUCTION_WRITE_SCRIPTS)
    if protected:
        emit_decision(
            "ask",
            "TSYC production-write gate: explicit user approval is required before running "
            + ", ".join(protected)
            + ".",
        )
        return 0

    # No hook decision: continue through normal Claude Code permissions.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
