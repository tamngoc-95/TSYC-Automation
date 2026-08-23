"""Regression tests for .claude/hooks/tsyc_pretool_guard.py.

Protects the CLAUDE.md Stable Automation Policy at the Claude Code
permission-enforcement layer:

- Deterministic pipeline-stage scripts must be auto-allowed (no repeated
  approval prompts for normal bounded processing).
- create_woocommerce_draft.py and manual_create_product_reference.py must
  always require an explicit human ask.
- run_batch.py must ask only when invoked with --allow-woo-draft.
- Every direct scripts/*.py execution must go through the repository
  .venv Python.
- Commands that merely reference a script path (grep/cat/git diff) or run
  it in module mode (`python -m py_compile ...`) must never be gated.

Invokes the hook as a subprocess, exactly as Claude Code's PreToolUse
hook runner does, feeding it JSON on stdin and reading its JSON stdout.
Building the request via json.dumps (rather than a shell string) avoids
any shell-quoting ambiguity around Windows backslash paths.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = PROJECT_ROOT / ".claude" / "hooks" / "tsyc_pretool_guard.py"
VENV_PYTHON = ".venv/Scripts/python.exe"


def run_guard(command: str, tool_name: str = "Bash") -> dict | None:
    """Run the hook exactly as Claude Code would and return the parsed
    hookSpecificOutput dict, or None if the hook produced no output
    (i.e. "no decision -- defer to normal permission rules")."""
    event = {"tool_name": tool_name, "tool_input": {"command": command}}
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    return json.loads(stdout)["hookSpecificOutput"]


def decision(command: str, tool_name: str = "Bash") -> str | None:
    out = run_guard(command, tool_name)
    return out["permissionDecision"] if out else None


AUTO_ALLOW_SCRIPTS = [
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
]

ASK_ALWAYS_SCRIPTS = [
    "create_woocommerce_draft.py",
    "manual_create_product_reference.py",
]


# --- Non-Bash/PowerShell tools are never gated -----------------------------


def test_non_shell_tool_is_ignored():
    event = {
        "tool_name": "Read",
        "tool_input": {"file_path": "scripts/create_woocommerce_draft.py"},
    }
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.stdout.strip() == ""


def test_malformed_json_denies_safely():
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    out = json.loads(result.stdout.strip())["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


# --- Deterministic pipeline stages are auto-allowed -------------------------


@pytest.mark.parametrize("script", AUTO_ALLOW_SCRIPTS)
def test_deterministic_pipeline_script_auto_allowed(script):
    cmd = f'{VENV_PYTHON} scripts/{script} --non-interactive'
    assert decision(cmd) == "allow"


@pytest.mark.parametrize("script", AUTO_ALLOW_SCRIPTS)
def test_deterministic_pipeline_script_auto_allowed_powershell(script):
    cmd = f'& "{VENV_PYTHON}" "scripts/{script}" --non-interactive'
    assert decision(cmd, tool_name="PowerShell") == "allow"


# --- Business-approval gates always ask -------------------------------------


@pytest.mark.parametrize("script", ASK_ALWAYS_SCRIPTS)
def test_business_approval_script_always_asks(script):
    cmd = f'{VENV_PYTHON} scripts/{script} --non-interactive --confirm-create'
    out = run_guard(cmd)
    assert out["permissionDecision"] == "ask"
    assert script in out["permissionDecisionReason"]


def test_woocommerce_draft_creation_asks_even_with_other_flags():
    cmd = (
        f'{VENV_PYTHON} scripts/create_woocommerce_draft.py '
        "--product-code CAN-1 --confirm-create --non-interactive"
    )
    assert decision(cmd) == "ask"


# --- run_batch.py: allow normally, ask only with --allow-woo-draft ---------


def test_run_batch_without_woo_flag_is_auto_allowed():
    cmd = (
        f'{VENV_PYTHON} scripts/run_batch.py --candidate-code CAN-1 '
        "--max-candidates 5 --non-interactive"
    )
    assert decision(cmd) == "allow"


def test_run_batch_with_woo_flag_asks():
    cmd = (
        f'{VENV_PYTHON} scripts/run_batch.py --candidate-code CAN-1 '
        "--max-candidates 5 --non-interactive --allow-woo-draft"
    )
    out = run_guard(cmd)
    assert out["permissionDecision"] == "ask"
    assert "--allow-woo-draft" in out["permissionDecisionReason"]


def test_run_batch_with_woo_flag_asks_powershell_backslash_paths():
    cmd = (
        '& ".venv\\Scripts\\python.exe" "scripts\\run_batch.py" '
        "--candidate-code CAN-1 --max-candidates 5 --allow-woo-draft"
    )
    assert decision(cmd, tool_name="PowerShell") == "ask"


def test_run_batch_without_woo_flag_allowed_powershell_backslash_paths():
    cmd = (
        '& ".venv\\Scripts\\python.exe" "scripts\\run_batch.py" '
        "--candidate-code CAN-1 --max-candidates 5"
    )
    assert decision(cmd, tool_name="PowerShell") == "allow"


# --- venv enforcement --------------------------------------------------------


def test_non_venv_python_is_denied():
    cmd = "python scripts/collect_one_facebook_post.py --non-interactive"
    assert decision(cmd) == "deny"


def test_system_py_launcher_is_denied():
    cmd = "py scripts/create_internal_product.py --non-interactive"
    assert decision(cmd) == "deny"


def test_venv_enforcement_applies_even_to_ask_always_scripts():
    cmd = "python scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "deny"


# --- Commands that merely reference a script are never gated ---------------


def test_grep_referencing_script_path_is_not_gated():
    cmd = "grep -n foo scripts/create_woocommerce_draft.py"
    assert decision(cmd) is None


def test_git_diff_on_script_path_is_not_gated():
    cmd = "git diff --check scripts/manual_create_product_reference.py"
    assert decision(cmd) is None


def test_cat_of_script_path_is_not_gated():
    cmd = "cat scripts/prepare_product_content.py"
    assert decision(cmd) is None


# --- Module-mode invocations (static checks) are always allowed, even for
# the ask-always scripts: a syntax check has no pipeline side effects and
# must not trigger the create_woocommerce_draft.py / manual reference
# business-approval gate.


@pytest.mark.parametrize("script", ["create_woocommerce_draft.py", "collect_one_facebook_post.py"])
def test_py_compile_module_invocation_is_allowed(script):
    cmd = f"{VENV_PYTHON} -m py_compile scripts/{script}"
    assert decision(cmd) == "allow"


def test_py_compile_still_requires_venv():
    cmd = "python -m py_compile scripts/create_woocommerce_draft.py"
    assert decision(cmd) == "deny"


# --- Unrecognized scripts fall through to default settings.json rules ------


def test_unrecognized_script_is_not_classified():
    cmd = f"{VENV_PYTHON} scripts/list_pending_facebook_urls.py"
    assert decision(cmd) is None


# --- Chained commands ---------------------------------------------------


def test_chained_command_ask_wins_over_allow():
    cmd = (
        f"{VENV_PYTHON} scripts/collect_one_facebook_post.py --non-interactive "
        f"&& {VENV_PYTHON} scripts/create_woocommerce_draft.py --product-code CAN-1"
    )
    assert decision(cmd) == "ask"


def test_chained_command_with_unrecognized_script_not_classified():
    cmd = (
        f"{VENV_PYTHON} scripts/collect_one_facebook_post.py --non-interactive "
        f"&& {VENV_PYTHON} scripts/list_pending_facebook_urls.py"
    )
    assert decision(cmd) is None


def test_chained_command_non_venv_segment_still_denied():
    cmd = (
        f"{VENV_PYTHON} scripts/collect_one_facebook_post.py --non-interactive "
        "&& python scripts/create_internal_product.py --non-interactive"
    )
    assert decision(cmd) == "deny"


# --- Regression coverage for code-review findings ---------------------------
#
# `-m <module>` must only be exempt from the ask gates for the literal,
# side-effect-free `-m py_compile` form. Any other module target really
# executes its script argument and must be classified exactly like a
# direct invocation -- in particular it must NOT bypass the
# create_woocommerce_draft.py / manual_create_product_reference.py
# business-approval gate.


@pytest.mark.parametrize("module", ["cProfile", "trace", "pdb", "timeit"])
def test_non_py_compile_module_invocation_of_ask_always_script_still_asks(module):
    cmd = (
        f"{VENV_PYTHON} -m {module} scripts/create_woocommerce_draft.py "
        "--product-code CAN-1 --confirm-create"
    )
    assert decision(cmd) == "ask"


def test_trailing_dash_m_argument_does_not_exempt_ask_always_script():
    # A stray "-m" appearing *after* the script path is just a normal CLI
    # argument being handed to the script, not python's own -m flag (the
    # positional script argument has already been consumed by then).
    cmd = (
        f"{VENV_PYTHON} scripts/create_woocommerce_draft.py "
        "--product-code CAN-1 --confirm-create -m"
    )
    assert decision(cmd) == "ask"


def test_non_py_compile_module_invocation_of_auto_allow_script_still_allowed():
    cmd = f"{VENV_PYTHON} -m cProfile scripts/collect_one_facebook_post.py --non-interactive"
    assert decision(cmd) == "allow"


# --- Regression coverage: venv/classification must not depend on a literal
# "scripts/" path prefix (e.g. after `cd scripts`) --------------------------


def test_non_venv_python_denied_without_scripts_prefix():
    cmd = "cd scripts && python create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "deny"


def test_non_venv_python_denied_for_auto_allow_script_without_prefix():
    cmd = "cd scripts && python collect_one_facebook_post.py --non-interactive"
    assert decision(cmd) == "deny"


def test_venv_python_without_scripts_prefix_is_still_classified():
    cmd = "cd scripts && .venv/Scripts/python.exe collect_one_facebook_post.py --non-interactive"
    assert decision(cmd) == "allow"


def test_venv_python_without_scripts_prefix_still_asks_for_woo_draft():
    cmd = "cd scripts && .venv/Scripts/python.exe create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_same_named_script_outside_scripts_dir_is_not_auto_allowed():
    # A same-named file executed from an unrelated directory must never be
    # auto-allowed -- only ever asked-about (for the ask-always names) or
    # left unclassified (for the auto-allow names), never silently allowed.
    cmd = f"{VENV_PYTHON} evil/collect_one_facebook_post.py"
    assert decision(cmd) is None


def test_same_named_ask_always_script_outside_scripts_dir_still_asks():
    cmd = f"{VENV_PYTHON} evil/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


# --- Regression coverage: quoted shell-operator characters must not
# fracture command segmentation ----------------------------------------


def test_quoted_semicolon_in_argument_does_not_split_segment():
    cmd = f'{VENV_PYTHON} -X "a;b" scripts/create_woocommerce_draft.py --product-code CAN-1'
    assert decision(cmd) == "ask"


def test_quoted_ampersand_in_argument_does_not_split_segment():
    cmd = f'{VENV_PYTHON} scripts/prepare_product_content.py --product-code "CAN-1 & CAN-2"'
    assert decision(cmd) == "allow"


# --- python -c (inline code) is out of this hook's scope -------------------


def test_inline_code_execution_is_not_classified():
    cmd = f'{VENV_PYTHON} -c "print(1)"'
    assert decision(cmd) is None


# --- venv path check is anchored, not a loose substring match --------------


def test_venv_path_check_is_anchored_not_substring():
    # A path that merely *contains* the ".venv/Scripts/python.exe" suffix
    # as a substring of a differently-named directory must not pass.
    cmd = "fake.venv/Scripts/python.exe scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "deny"


# --- Regression coverage: `-m <module>` addressed purely by dotted/bare
# name, with no separate *.py argument anywhere in argv, must still be
# classified -- not silently fall through with no decision at all. This
# is the idiomatic way `-m` is actually used (e.g. `python -m pytest`,
# `python -m pip`), so it must not be a blind spot for the ask-always
# scripts. -------------------------------------------------------------


def test_dotted_module_addressing_of_ask_always_script_still_asks():
    cmd = f"{VENV_PYTHON} -m scripts.create_woocommerce_draft --product-code CAN-1 --confirm-create"
    assert decision(cmd) == "ask"


def test_dotted_module_addressing_of_ask_always_script_requires_venv():
    cmd = "python -m scripts.create_woocommerce_draft --product-code CAN-1"
    assert decision(cmd) == "deny"


def test_bare_module_addressing_relies_on_cwd_like_bare_filename():
    cmd = f"{VENV_PYTHON} -m create_woocommerce_draft --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_dotted_module_addressing_of_auto_allow_script_still_allowed():
    cmd = f"{VENV_PYTHON} -m scripts.collect_one_facebook_post --non-interactive"
    assert decision(cmd) == "allow"


def test_dotted_module_addressing_of_run_batch_with_woo_flag_asks():
    cmd = (
        f"{VENV_PYTHON} -m scripts.run_batch --candidate-code CAN-1 "
        "--max-candidates 5 --allow-woo-draft"
    )
    out = run_guard(cmd)
    assert out["permissionDecision"] == "ask"
    assert "--allow-woo-draft" in out["permissionDecisionReason"]


def test_unrelated_dotted_module_is_not_classified():
    cmd = f"{VENV_PYTHON} -m json.tool somefile.json"
    assert decision(cmd) is None


# --- Regression coverage: bundled single-letter flags (-um == -u -m,
# -mfoo == -m with an attached value) must still be recognized rather
# than mis-parsing the module/script token that follows. -------------------


def test_bundled_um_flag_wrapping_ask_always_script_still_asks():
    cmd = f"{VENV_PYTHON} -um cProfile scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_bundled_m_with_attached_dotted_module_still_asks():
    cmd = f"{VENV_PYTHON} -mscripts.create_woocommerce_draft --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_long_double_dash_flag_containing_m_and_c_is_not_misread_as_module_flag():
    # A double-dash long option is a single flag, never a short-flag
    # bundle -- even if its name happens to contain the letters "m"/"c"
    # (which trigger the bundling scan for single-dash tokens), it must
    # not be misclassified as -m/-c and must not swallow the real script
    # path that follows it.
    cmd = f"{VENV_PYTHON} --classify-mode scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_glued_w_flag_value_containing_c_is_not_misread_as_inline_code():
    # -Wvalue (no space) is the glued/attached form of -W. Its value can
    # contain "c" (e.g. "DeprecationWarning") which must not be
    # misclassified as -c by the bundling scan -- that would silently
    # skip the real script argument that follows.
    cmd = f"{VENV_PYTHON} -Wignore::DeprecationWarning scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_glued_x_flag_value_is_not_misread():
    cmd = f"{VENV_PYTHON} -Ximporttime scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_glued_w_flag_for_auto_allow_script_still_allowed():
    cmd = f"{VENV_PYTHON} -Wignore::DeprecationWarning scripts/collect_one_facebook_post.py --non-interactive"
    assert decision(cmd) == "allow"


def test_separate_token_w_flag_still_works():
    cmd = f"{VENV_PYTHON} -W ignore::DeprecationWarning scripts/create_woocommerce_draft.py --product-code CAN-1"
    assert decision(cmd) == "ask"


def test_unknown_value_taking_long_flag_is_a_documented_limitation():
    # Known, documented limitation (see the hook's module docstring): the
    # hook only knows -W/-X consume a following value. An unrecognized
    # long option that also consumes a value (the only one in stock
    # CPython, --check-hash-based-pycs) can cause its value token to be
    # misidentified as the positional script argument, so the real
    # invocation is missed. This asserts the *current, accepted* behavior
    # rather than silently allowing it to regress further unnoticed.
    cmd = (
        f"{VENV_PYTHON} --check-hash-based-pycs default "
        "scripts/create_woocommerce_draft.py --product-code CAN-1"
    )
    assert decision(cmd) is None
