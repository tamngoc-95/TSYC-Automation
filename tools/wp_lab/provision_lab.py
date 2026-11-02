"""Provision the isolated local WordPress lab (no Docker, no system install).

Everything lands in one folder outside the repository:

  portable PHP (windows.php.net, sha256 checked)
  WordPress + WooCommerce + Polylang + SQLite integration (wordpress.org,
  sha1 / per-file checksums checked), wp-cli (sha512 checked)

Isolation: SQLite file database with synthetic data only, loopback only,
outbound HTTP blocked (WP_HTTP_BLOCK_EXTERNAL), mail disabled, no production
key, no .env value is ever read. Passwords and tokens are generated here and
stay in <lab>/lab_secrets.json.

Usage:
  .venv/Scripts/python.exe tools/wp_lab/provision_lab.py [--lab-dir PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
BRIDGE_SOURCE = PROJECT_ROOT / "wordpress" / "tsyc-polylang-bridge" / "tsyc-polylang-bridge.php"

# The versions production runs where they are known
# (docs/POLYLANG_EXISTING_ARCHITECTURE.md section 1).
PHP_BRANCH = "8.3"
WORDPRESS_VERSION = "7.1.4"
PLUGINS = {"woocommerce": "10.7.0", "polylang": "3.8.4", "sqlite-database-integration": "3.1.2"}
PORT = 8765
TIMEOUT = 300


def default_lab_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TSYC" / "wp-lab"


def download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    print(f"download {url}")
    with requests.get(url, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        partial = target.with_suffix(target.suffix + ".part")
        with partial.open("wb") as handle:
            for chunk in response.iter_content(1024 * 256):
                handle.write(chunk)
    partial.rename(target)
    return target


def digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def require_digest(path: Path, algorithm: str, expected: str) -> None:
    actual = digest(path, algorithm)
    if actual.lower() != expected.strip().lower():
        path.unlink(missing_ok=True)
        raise SystemExit(f"Checksum mismatch for {path.name}: {algorithm} {actual} != {expected}")


def unzip(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)


def provision_php(lab: Path) -> Path:
    php_dir = lab / "php"
    php = php_dir / "php.exe"
    if not php.exists():
        releases = requests.get("https://windows.php.net/downloads/releases/releases.json", timeout=TIMEOUT).json()
        build = releases[PHP_BRANCH]["nts-vs16-x64"]["zip"]
        archive = download(f"https://windows.php.net/downloads/releases/{build['path']}",
                           lab / "downloads" / "php" / build["path"])
        require_digest(archive, "sha256", build["sha256"])
        unzip(archive, php_dir)
    extensions = ["curl", "fileinfo", "gd", "intl", "mbstring", "openssl", "pdo_sqlite", "sqlite3", "zip", "exif"]
    ini = [f'extension_dir="{(php_dir / "ext").as_posix()}"'] + [f"extension={e}" for e in extensions] + [
        "memory_limit=1024M", "max_execution_time=300", "display_errors=stderr", "error_reporting=E_ALL & ~E_DEPRECATED",
        f'error_log="{(lab / "php_errors.log").as_posix()}"', "log_errors=On", "date.timezone=UTC",
    ]
    (php_dir / "php.ini").write_text("\n".join(ini) + "\n", encoding="utf-8")
    return php


def provision_wordpress(lab: Path) -> Path:
    site = lab / "site"
    if not (site / "wp-settings.php").exists():
        name = f"wordpress-{WORDPRESS_VERSION}.zip"
        url = f"https://downloads.wordpress.org/release/{name}"
        archive = download(url, lab / "downloads" / "wordpress" / name)
        require_digest(archive, "sha1", requests.get(url + ".sha1", timeout=TIMEOUT).text)
        unpacked = lab / "downloads" / "wordpress" / "unpacked"
        unzip(archive, unpacked)
        shutil.move(str(unpacked / "wordpress"), str(site))
    return site


def provision_plugin(lab: Path, site: Path, slug: str, version: str) -> None:
    target = site / "wp-content" / "plugins" / slug
    if target.exists():
        return
    name = f"{slug}.{version}.zip"
    archive = download(f"https://downloads.wordpress.org/plugin/{name}", lab / "downloads" / slug / name)
    unpacked = lab / "downloads" / slug / "unpacked"
    unzip(archive, unpacked)
    sums = requests.get(f"https://downloads.wordpress.org/plugin-checksums/{slug}/{version}.json", timeout=TIMEOUT)
    sums.raise_for_status()
    for relative, entry in sums.json()["files"].items():
        wanted = entry["md5"]
        wanted = wanted if isinstance(wanted, list) else [wanted]
        if digest(unpacked / slug / relative, "md5") not in wanted:
            raise SystemExit(f"{slug} {relative}: checksum mismatch; the package is not installed.")
    shutil.move(str(unpacked / slug), str(target))


def write_config(site: Path, secrets_data: dict[str, str]) -> None:
    salts = "\n".join(
        f"define('{key}', '{secrets.token_hex(32)}');"
        for key in ("AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
                    "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT"))
    (site / "wp-config.php").write_text(f"""<?php
// TSYC local lab. Synthetic data only; generated by tools/wp_lab/provision_lab.py.
define('DB_NAME', 'lab');
define('DB_USER', '');
define('DB_PASSWORD', '');
define('DB_HOST', '');
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');
define('DB_ENGINE', 'sqlite');
{salts}
$table_prefix = 'wp_';
define('WP_ENVIRONMENT_TYPE', 'local');
define('WP_HOME', 'http://127.0.0.1:{PORT}');
define('WP_SITEURL', 'http://127.0.0.1:{PORT}');
define('WP_HTTP_BLOCK_EXTERNAL', true);
define('WP_ACCESSIBLE_HOSTS', '127.0.0.1');
define('DISABLE_WP_CRON', true);
define('AUTOMATIC_UPDATER_DISABLED', true);
define('WP_AUTO_UPDATE_CORE', false);
define('WP_DEBUG', false);
define('TSYC_BRIDGE_ENABLE_WRITES', getenv('TSYC_LAB_WRITES_OFF') ? false : true);
define('TSYC_BRIDGE_WRITE_TOKEN', '{secrets_data["write_token"]}');
if (!defined('ABSPATH')) {{ define('ABSPATH', __DIR__ . '/'); }}
require_once ABSPATH . 'wp-settings.php';
""", encoding="utf-8")


def install_drop_in(site: Path) -> None:
    source = site / "wp-content" / "plugins" / "sqlite-database-integration"
    text = (source / "db.copy").read_text(encoding="utf-8")
    plugin_path = "WP_CONTENT_DIR . '/plugins/sqlite-database-integration'"
    text = text.replace("'{SQLITE_IMPLEMENTATION_FOLDER_PATH}'", plugin_path).replace(
        "{SQLITE_PLUGIN}", "sqlite-database-integration/load.php")
    (site / "wp-content" / "db.php").write_text(text, encoding="utf-8")


def install_mu_plugins(site: Path) -> None:
    mu = site / "wp-content" / "mu-plugins"
    mu.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BRIDGE_SOURCE, mu / BRIDGE_SOURCE.name)
    shutil.copy2(HERE / "tsyc-lab-guards.php", mu / "tsyc-lab-guards.php")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", type=Path, default=default_lab_dir())
    args = parser.parse_args(argv)
    lab: Path = args.lab_dir.resolve()
    if PROJECT_ROOT in lab.parents or lab == PROJECT_ROOT:
        raise SystemExit("The lab must live outside the repository.")
    lab.mkdir(parents=True, exist_ok=True)

    php = provision_php(lab)
    site = provision_wordpress(lab)
    for slug, version in PLUGINS.items():
        provision_plugin(lab, site, slug, version)
    release = "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"
    wp_cli = download(release, lab / "wp-cli" / "wp-cli.phar")
    require_digest(wp_cli, "sha512", requests.get(release + ".sha512", timeout=TIMEOUT).text.split()[0])

    secrets_path = lab / "lab_secrets.json"
    if secrets_path.exists():
        secrets_data = json.loads(secrets_path.read_text(encoding="utf-8"))
    else:
        secrets_data = {"admin_password": secrets.token_urlsafe(24), "write_token": secrets.token_hex(32)}
        secrets_path.write_text(json.dumps(secrets_data), encoding="utf-8")
    write_config(site, secrets_data)
    install_drop_in(site)
    install_mu_plugins(site)
    shutil.copy2(HERE / "router.php", lab / "router.php")

    def wp(*arguments: str) -> str:
        command = [str(php), "-c", str(php.parent / "php.ini"), str(wp_cli), f"--path={site}", *arguments]
        done = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
        if done.returncode != 0:
            raise SystemExit(f"wp {' '.join(arguments)} failed:\n{done.stdout}\n{done.stderr}")
        return done.stdout.strip()

    if subprocess.run([str(php), "-c", str(php.parent / "php.ini"), str(wp_cli), f"--path={site}", "core", "is-installed"],
                      capture_output=True, timeout=TIMEOUT).returncode != 0:
        wp("core", "install", f"--url=http://127.0.0.1:{PORT}", "--title=TSYC lab", "--admin_user=lab-admin",
           f"--admin_password={secrets_data['admin_password']}", "--admin_email=lab@example.invalid", "--skip-email")
    wp("plugin", "activate", "woocommerce", "polylang")
    wp("rewrite", "structure", "/%postname%/")
    print(wp("eval-file", str(HERE / "configure_lab.php")))
    print(f"Lab ready at {lab}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
