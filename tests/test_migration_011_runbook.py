"""The owner runbook's copy of migration 011 must be the migration file,
byte for byte (line endings aside): the owner pastes the runbook copy."""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "011_create_woocommerce_language_products.sql"
RUNBOOK = PROJECT_ROOT / "docs" / "MIGRATION_011_OWNER_RUNBOOK.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_runbook_sql_is_the_migration_file_unchanged():
    match = re.search(r"<!-- migration-011:begin -->\n```sql\n(.*?)```\n<!-- migration-011:end -->",
                      _text(RUNBOOK), re.DOTALL)
    assert match, "migration block markers missing"
    assert match.group(1).strip() == _text(MIGRATION).strip()


def test_migration_011_is_additive_only():
    sql = _text(MIGRATION).lower()
    for statement in ("drop", "truncate", "delete", "update", "insert"):
        assert not re.search(rf"^\s*{statement}\b", sql, re.MULTILINE), statement
    assert "alter column" not in sql and "drop " not in sql
    assert sql.count("create table if not exists") == 1
    assert sql.count("create unique index if not exists") == 2 and sql.count("create index if not exists") == 1
