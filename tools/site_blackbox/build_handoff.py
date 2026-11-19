"""Build the developer's black-box handoff package from one Git commit.

Every file is taken from the commit (`git show REF:path`), never from the
working tree, so the package is exactly what was reviewed at that commit and
the bridge the developer installs is the bridge the harness expects. A package
file with uncommitted changes refuses the build.

Output (gitignored): data/output/blackbox_handoff/tsyc-blackbox-gate-a-<sha>/
and the same folder as a .zip. It contains the package files, MANIFEST.json
and SHA256SUMS.txt. The build refuses if any file looks like it holds a
credential, a private key or a real e-mail address.

  .venv/Scripts/python.exe tools/site_blackbox/build_handoff.py [--ref HEAD]

This script is not part of the package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "tsyc-blackbox-gate-a"

# Published name -> repository path.
PACKAGE_FILES = {
    "README.md": "tools/site_blackbox/README.md",
    "site_blackbox.py": "tools/site_blackbox/site_blackbox.py",
    "tsyc-isolation.php": "tools/site_blackbox/tsyc-isolation.php",
    "tsyc-site-snapshot.php": "tools/site_blackbox/tsyc-site-snapshot.php",
    "tsyc-bridge-trio-driver.php": "tools/site_blackbox/tsyc-bridge-trio-driver.php",
    "tsyc-polylang-bridge.php": "wordpress/tsyc-polylang-bridge/tsyc-polylang-bridge.php",
}

# Versions TSYC's lab Gate A passed on (docs/PREORDER_MULTILINGUAL_WORKFLOW.md section 3).
TESTED_WITH = {"wordpress": "7.1.4", "woocommerce": "11.2.1", "polylang": "3.9.1", "php_lab": "8.3",
               "php_minimum": "7.4", "wp_cli": "2.12.0", "python_minimum": "3.9", "databases": ["MariaDB 11.4.5", "SQLite"]}

SECRET_PATTERNS = {
    "woocommerce consumer key": re.compile(rb"\bck_[0-9a-f]{40}\b"),
    "woocommerce consumer secret": re.compile(rb"\bcs_[0-9a-f]{40}\b"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "application password": re.compile(rb"\b[A-Za-z0-9]{4}( [A-Za-z0-9]{4}){5}\b"),
    "e-mail address": re.compile(rb"[A-Za-z0-9._%+-]+@(?!example\.(?:com|test|invalid)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "token value": re.compile(rb"WRITE_TOKEN',\s*'(?!PASTE-A-NEW-RANDOM-VALUE')[^']{16,}'"),
    "dotenv": re.compile(rb"^(SUPABASE|WOO|WC|WP)_[A-Z_]*(KEY|SECRET|PASSWORD|TOKEN)\s*=", re.M),
}


class BuildError(RuntimeError):
    pass


def git(*arguments: str) -> bytes:
    done = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, timeout=60)
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(arguments)}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def normalized_sha256(data: bytes) -> str:
    """The same rule as site_blackbox.normalized_sha256 and tsyc-isolation.php."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _looks_random(text: bytes) -> bool:
    """Generated secrets mix lower case, upper case and digits; six ordinary words do not."""
    return bool(re.search(rb"[a-z]", text) and re.search(rb"[A-Z]", text) and re.search(rb"[0-9]", text))


def secret_findings(name: str, data: bytes) -> list[str]:
    found = []
    for label, pattern in SECRET_PATTERNS.items():
        matches = [m.group(0) for m in pattern.finditer(data)]
        if label == "application password":
            matches = [m for m in matches if _looks_random(m)]
        if matches:
            found.append(f"{name}: looks like a {label}")
    return found


def bridge_version(data: bytes) -> str:
    found = re.search(rb"const TSYC_BRIDGE_VERSION = '([^']+)'", data)
    return found.group(1).decode() if found else ""


def build(ref: str = "HEAD", out_root: Path | None = None) -> Path:
    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    dirty = git("status", "--porcelain", "--", *PACKAGE_FILES.values()).decode().strip()
    if dirty and ref == "HEAD":
        raise BuildError(f"Package files have uncommitted changes; commit them first:\n{dirty}")
    contents = {name: git("show", f"{commit}:{path}") for name, path in PACKAGE_FILES.items()}
    findings = [f for name, data in contents.items() for f in secret_findings(name, data)]
    if findings:
        raise BuildError("Refused, possible secret in the package:\n" + "\n".join(findings))

    out_root = out_root or ROOT / "data" / "output" / "blackbox_handoff"
    folder = out_root / f"{NAME}-{commit[:7]}"
    if folder.exists() and any(folder.iterdir()):
        raise BuildError(f"{folder} already exists. Remove it to rebuild this commit.")
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (folder / name).write_bytes(data)

    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    manifest = {
        "package": NAME,
        "purpose": "Gate A (draft creation safety) on an isolated copy of the real site. Read README.md.",
        "source_commit": commit,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bridge_version": bridge_version(contents["tsyc-polylang-bridge.php"]),
        "bridge_sha256_normalized": normalized_sha256(contents["tsyc-polylang-bridge.php"]),
        "tested_with": TESTED_WITH,
        "files": {name: {"sha256": hashes[name], "bytes": len(contents[name])} for name in contents},
        "writes": "Only tsyc-bridge-trio-driver.php writes, only test products with SKU prefix TSYC-BBX-, only after "
                  "every isolation check passes. No price, no publish, no order, no customer data.",
    }
    (folder / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    (folder / "SHA256SUMS.txt").write_text("".join(f"{hashes[n]}  {n}\n" for n in sorted(hashes)), encoding="utf-8",
                                          newline="\n")

    archive = folder.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as packed:
        for path in sorted(folder.iterdir()):
            packed.write(path, f"{folder.name}/{path.name}")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="HEAD", help="Commit to build from (default HEAD).")
    parser.add_argument("--out", type=Path, help="Output folder (default data/output/blackbox_handoff).")
    args = parser.parse_args(argv)
    try:
        archive = build(args.ref, args.out)
    except BuildError as error:
        print(f"BUILD REFUSED: {error}", file=sys.stderr)
        return 1
    print(f"Package: {archive}")
    print(f"Folder:  {archive.with_suffix('')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
