"""The developer handoff build (tools/site_blackbox/build_handoff.py), offline: no real Git commit is built."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tools" / "site_blackbox"
sys.path.insert(0, str(PACKAGE))

import build_handoff as bh  # noqa: E402
import site_blackbox as bbx  # noqa: E402


def fake_git(dirty: str = "", overrides: dict[str, bytes] | None = None):
    def git(*arguments: str) -> bytes:
        if arguments[0] == "rev-parse":
            return b"abcdef1234567890abcdef1234567890abcdef12\n"
        if arguments[0] == "status":
            return dirty.encode()
        if arguments[0] == "show":
            path = arguments[1].split(":", 1)[1]
            if overrides and path in overrides:
                return overrides[path]
            return (ROOT / path).read_bytes()
        raise AssertionError(arguments)
    return git


def test_every_package_file_exists_and_the_readme_names_it() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    for name, path in bh.PACKAGE_FILES.items():
        assert (ROOT / path).is_file(), path
        assert name == "README.md" or f"`{name}`" in readme, name
    assert "build_handoff.py" not in bh.PACKAGE_FILES          # the builder is not shipped


def test_the_build_ships_the_committed_files_with_hashes_and_the_bridge_the_harness_expects(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bh, "git", fake_git())
    archive = bh.build("HEAD", tmp_path)
    folder = archive.with_suffix("")
    assert folder.name == "tsyc-blackbox-gate-a-abcdef1"
    assert sorted(p.name for p in folder.iterdir()) == sorted([*bh.PACKAGE_FILES, "MANIFEST.json", "SHA256SUMS.txt"])
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["source_commit"].startswith("abcdef1") and manifest["bridge_version"] == "1.2.0"
    # The harness, run from the unpacked package, accepts exactly this bridge file.
    assert bbx.expected_bridge_sha256(folder) == manifest["bridge_sha256_normalized"]
    sums = dict(line.split("  ")[::-1] for line in (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines())
    assert set(sums) == set(bh.PACKAGE_FILES) and all(sums[n] == manifest["files"][n]["sha256"] for n in sums)
    with zipfile.ZipFile(archive) as packed:
        assert sorted(packed.namelist()) == sorted(f"{folder.name}/{p.name}" for p in folder.iterdir())
    with pytest.raises(bh.BuildError, match="already exists"):
        bh.build("HEAD", tmp_path)


def test_the_build_refuses_uncommitted_package_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bh, "git", fake_git(dirty=" M tools/site_blackbox/site_blackbox.py"))
    with pytest.raises(bh.BuildError, match="uncommitted"):
        bh.build("HEAD", tmp_path)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("planted", [
    b"define('TSYC_BRIDGE_WRITE_TOKEN', 'a8f3k2m9q7w1e5r4t6y8u0i2');",
    b"key ck_" + b"a" * 40,
    b"secret cs_" + b"0" * 40,
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"mail owner@gmail.com",
    b"pw Ab1c Xy2z Qr3s Tu4v Wx5y Za6b",
    b"WOO_CONSUMER_SECRET=abc",
])
def test_the_build_refuses_anything_that_looks_like_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            planted: bytes) -> None:
    readme = (ROOT / "tools/site_blackbox/README.md").read_bytes() + b"\n" + planted + b"\n"
    monkeypatch.setattr(bh, "git", fake_git(overrides={"tools/site_blackbox/README.md": readme}))
    with pytest.raises(bh.BuildError, match="possible secret"):
        bh.build("HEAD", tmp_path)
    assert not any(tmp_path.iterdir())


def test_the_real_package_files_hold_no_secret() -> None:
    for name, path in bh.PACKAGE_FILES.items():
        assert bh.secret_findings(name, (ROOT / path).read_bytes()) == [], name


def test_ordinary_words_are_not_an_application_password() -> None:
    assert bh.secret_findings("x", b"only this copy will make some test data here") == []
