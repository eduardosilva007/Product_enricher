"""Read-only 940 baseline acceptance audit against the original isolated workbooks.

Usage:
 python tests/verify_baseline_940_readonly.py \
   piloto_argentina/controlled_night/20261112T190958Z_68f61e00

No Search/JEV/LLM calls. Never writes workbook, database, or canonical files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_produtos import (
    _baseline_row_accepted, _ensure_master_columns,
    _read_business_sheet, _sync_final_into_master,
    _sanitize_legacy_unresolved_taxonomy,
    business_key, find_col, load_baseline_inventory, safe,
)


def accepted_keys(frame):
    b = find_col(frame, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(frame, ["Modelo", "Model", "SKU", "Part Number"])
    return {
        business_key(safe(row.get(b)), safe(row.get(m)))
        for _, row in frame.iterrows()
        if safe(row.get(b)) and safe(row.get(m))
        and _baseline_row_accepted(row, frame)
    }


def run(run_dir: Path) -> int:
    run_dir = run_dir.resolve()
    cfg = json.loads((run_dir / "isolated_config.json").read_text(encoding="utf-8"))
    files = cfg["files"]
    master_path = Path(files["master"])
    final_path = Path(files["final"])
    baseline = load_baseline_inventory(master_path, final_path)
    expected = baseline["processed"]
    frame, _ = _read_business_sheet(master_path)
    frame, b, m = _ensure_master_columns(frame)
    states = {}
    states["original_master"] = accepted_keys(frame)
    frame, _ = _sync_final_into_master(frame, final_path, b, m)
    states["after_final_sync"] = accepted_keys(frame)
    frame, _ = _sanitize_legacy_unresolved_taxonomy(frame)
    states["after_sanitization"] = accepted_keys(frame)
    report = {
        "expected_union": len(expected),
        "final_only": len(baseline["final_accepted"] - baseline["master_accepted"]),
        "final_only_bi_ok": len(
            (baseline["final_accepted"] - baseline["master_accepted"])
            & baseline["bi_locked"]
        ),
        "phases": {
            label: {
                "accepted": len(keys),
                "missing": len(expected - keys),
                "missing_examples": sorted(expected - keys)[:20],
            }
            for label, keys in states.items()
        },
        "bi_lock_total": len(baseline["bi_locked"]),
        "api_calls": 0,
        "production_files_modified": False,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not (expected - states["after_sanitization"]) else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/verify_baseline_940_readonly.py RUN_DIR")
    raise SystemExit(run(Path(sys.argv[1])))
