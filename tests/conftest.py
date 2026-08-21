"""Shared pytest setup for the TSYC automated test suite.

Puts the project root and scripts/ directory on sys.path so tests can import
`src.*` and the individual `scripts/*.py` entrypoints as plain modules, the
same way the scripts import each other and src.* at runtime.

This file must stay free of live credentials, network access, and browser
dependencies -- it is imported for every test collected under tests/.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

for path in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
