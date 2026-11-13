"""Read-only campaign history preflight, no API or production database writes.

Uses a disposable SQLite ledger to exercise the same reservation import
that is invoked for every new overnight sublot.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_produtos import OUT, import_prior_reservations


def main() -> int:
    history_root = Path(sys.argv[1]) if len(sys.argv) == 2 else OUT
    if not history_root.is_dir():
        raise RuntimeError(f"HISTORY_ROOT_NOT_FOUND: {history_root}")
    with tempfile.TemporaryDirectory(prefix="overnight_history_no_api_") as tmp:
        destination = Path(tmp) / "reservations.sqlite"
        result = import_prior_reservations(destination, history_root)
        print(json.dumps({
            "status": "PASS",
            "mode": "OFFLINE_HISTORY_PREFLIGHT",
            "history_root": str(history_root.resolve()),
            **result,
            "search_calls": 0,
            "llm_calls": 0,
            "original_history_modified": False,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"OFFLINE_HISTORY_PREFLIGHT_FAIL: {error}", file=sys.stderr)
        raise SystemExit(2)
