"""Read-only inventory of WordPress hooks in the developer's PHP source.

Reads folders or .zip files as text. Nothing is executed or extracted, and PHP is
never involved. With no source available it reports BLOCKED_MISSING_SOURCE and
exits 3: it never reports an empty inventory as a clean one.

The developer's source and this report stay out of Git: a source or output path
inside the repository is refused unless it is under the gitignored data/ folder.

Usage:
  .venv/Scripts/python.exe scripts/inventory_php_hooks.py --source PATH [--source PATH ...]
      [--out DIR] [--include-irrelevant] [--measured-events FILE]

Exit codes: 0 inventoried, no HIGH; 1 inventoried, HIGH or a suspected secret to
read first; 2 bad arguments; 3 BLOCKED_MISSING_SOURCE.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import php_hook_inventory as phi  # noqa: E402

DEFAULT_SOURCE = PROJECT_ROOT / "data" / "raw" / "developer_source"
DEFAULT_OUT = PROJECT_ROOT / "data" / "output" / "hook_inventory"
MEASURED_EVENTS = PROJECT_ROOT / "tests" / "fixtures" / "php_hooks" / "measured_hook_events.json"
PHP_SUFFIXES = (".php", ".inc", ".phtml")
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 20000
EXIT_OK, EXIT_LEADS, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3


class PathRefused(ValueError):
    """A path that would put private source or a report at risk of being committed."""


def inside_repository(path: Path) -> bool:
    return path.resolve() == PROJECT_ROOT or PROJECT_ROOT in path.resolve().parents


def require_private(path: Path, what: str, *, allow_repo_path: bool = False) -> None:
    if allow_repo_path or not inside_repository(path):
        return
    if (PROJECT_ROOT / "data") not in path.resolve().parents:
        raise PathRefused(f"{what} {path} is inside the repository but outside the gitignored data/ folder.")


def _skip(skipped: list[dict[str, Any]], path: str, reason: str) -> None:
    skipped.append({"path": phi.safe_name(path) if len(path) <= 100 else "<long path>", "reason": reason})


def read_folder(root: Path, label: str, skipped: list[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    total = 0
    for folder, subfolders, names in os.walk(root, followlinks=False):
        subfolders[:] = sorted(s for s in subfolders if not (Path(folder) / s).is_symlink() and s not in (".git", "node_modules"))
        for name in sorted(names):
            path = Path(folder) / name
            relative = f"{label}/{path.relative_to(root).as_posix()}"
            if phi.SENSITIVE_FILENAMES.search(relative):
                _skip(skipped, relative, "SENSITIVE_FILE_NOT_READ")
            elif not name.lower().endswith(PHP_SUFFIXES):
                continue
            elif path.is_symlink():
                _skip(skipped, relative, "SYMLINK_NOT_FOLLOWED")
            elif path.stat().st_size > MAX_FILE_BYTES:
                _skip(skipped, relative, "TOO_LARGE")
            elif total + path.stat().st_size > MAX_TOTAL_BYTES:
                _skip(skipped, relative, "TOTAL_SIZE_LIMIT")
            else:
                total += path.stat().st_size
                yield relative, path.read_bytes().decode("utf-8", errors="replace")


def read_zip(archive: Path, label: str, skipped: list[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    """Each member is read into memory; nothing is written to disk, so a
    traversal path or a planted file has nowhere to land."""
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            relative = f"{label}/{name}"
            if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                _skip(skipped, name, "UNSAFE_PATH_NOT_READ")
            elif phi.SENSITIVE_FILENAMES.search(name):
                _skip(skipped, relative, "SENSITIVE_FILE_NOT_READ")
            elif name.lower().endswith((".zip", ".tar", ".gz", ".phar")):
                _skip(skipped, relative, "NESTED_ARCHIVE_NOT_OPENED")
            elif not name.lower().endswith(PHP_SUFFIXES):
                continue
            elif stat.S_ISLNK(member.external_attr >> 16):
                _skip(skipped, relative, "SYMLINK_NOT_FOLLOWED")
            elif member.flag_bits & 0x1:
                _skip(skipped, relative, "ENCRYPTED_NOT_READ")
            elif member.file_size > MAX_FILE_BYTES:
                _skip(skipped, relative, "TOO_LARGE")
            elif total + member.file_size > MAX_TOTAL_BYTES:
                _skip(skipped, relative, "TOTAL_SIZE_LIMIT")
            else:
                total += member.file_size
                with bundle.open(member) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:  # The header lied about the size.
                    _skip(skipped, relative, "TOO_LARGE")
                else:
                    yield relative, data.decode("utf-8", errors="replace")


def load_sources(paths: Sequence[Path]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    sources: dict[str, str] = {}
    skipped: list[dict[str, Any]] = []
    for path in paths:
        label = phi.safe_name(path.stem if path.is_file() else path.name) or "source"
        reader = read_zip(path, label, skipped) if path.is_file() else read_folder(path, label, skipped)
        for relative, text in reader:
            if len(sources) >= MAX_FILES:
                _skip(skipped, relative, "FILE_COUNT_LIMIT")
                break
            sources[relative] = text
    return sources, skipped


def blocked_report(detail: str) -> dict[str, Any]:
    return {"tool_version": phi.TOOL_VERSION, "status": "BLOCKED_MISSING_SOURCE", "detail": detail,
            "reading_note": "No PHP was read, so nothing is known about the hooks. This is not a clean result."}


def write_report(report: dict[str, Any], out: Path, relevant_only: bool) -> Path:
    run = out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    (run / "hook_inventory.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (run / "hook_inventory.md").write_text(phi.to_markdown(report, relevant_only=relevant_only), encoding="utf-8")
    return run


def main(argv: list[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", action="append", type=Path, default=[], help=f"Folder or .zip (default: {DEFAULT_SOURCE} if present).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--include-irrelevant", action="store_true", help="Also list hooks unrelated to products in the Markdown.")
    parser.add_argument("--measured-events", type=Path, default=MEASURED_EVENTS)
    parser.add_argument("--allow-repo-path", action="store_true", help="For TSYC's own files only, such as the bridge. Never for developer source.")
    args = parser.parse_args(argv)

    try:
        require_private(args.out, "--out", allow_repo_path=False)
        paths = args.source or ([DEFAULT_SOURCE] if DEFAULT_SOURCE.exists() else [])
        for path in paths:
            require_private(path, "--source", allow_repo_path=args.allow_repo_path)
    except PathRefused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return EXIT_USAGE

    missing = [str(p) for p in paths if not p.exists()]
    if not paths or missing:
        detail = f"Not found: {missing}" if missing else f"No --source given and {DEFAULT_SOURCE} does not exist."
        run = write_report(blocked_report(detail), args.out, True)
        print(f"BLOCKED_MISSING_SOURCE: {detail}\nReport: {run}")
        return EXIT_BLOCKED

    try:
        sources, skipped = load_sources(paths)
    except zipfile.BadZipFile as error:
        print(f"REFUSED: not a readable zip: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not sources:
        run = write_report({**blocked_report(f"No PHP files in {[p.name for p in paths]}."), "skipped": skipped}, args.out, True)
        print(f"BLOCKED_MISSING_SOURCE: no PHP files found.\nReport: {run}")
        return EXIT_BLOCKED

    measured = json.loads(args.measured_events.read_text(encoding="utf-8")) if args.measured_events.exists() else None
    report = phi.build_inventory(sources, measured_events=measured, skipped=skipped)
    run = write_report(report, args.out, not args.include_irrelevant)
    print(f"{report['status']}: {report['php_files']} PHP files, {report['registrations']} registrations, {report['relevant']} relevant, "
          f"{report['unresolved_callbacks']} unresolved callbacks")
    print(f"Risk: {report['risk_counts']}. Recursion findings: {len(report['recursion'])}. Conflicts: {len(report['conflicts'])}.")
    print(f"Suspected secrets: {len(report['suspected_secrets'])} (values not shown). Skipped: {len(report['skipped'])}.")
    print(f"Report: {run}")
    needs_reading = report["risk_counts"].get("HIGH") or report["suspected_secrets"]
    return EXIT_LEADS if needs_reading else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
