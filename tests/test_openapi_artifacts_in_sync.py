"""Phase-0 guardrail (R10/D1): checked-in OpenAPI artifacts must not be stale.

The single source of truth is the FastAPI app's OpenAPI schema;
``frontend/public/openapi.json`` and ``frontend/src/api/schema.ts`` are
generated artifacts. ``scripts/generate_openapi.py --check`` is the gate used
by pre-commit and CI — this test ensures the gate keeps working and that the
artifacts are currently in sync (no manual edits / forgotten regeneration).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_openapi_artifacts_in_sync():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_openapi.py"), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "OpenAPI artifacts are stale or the --check gate failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
