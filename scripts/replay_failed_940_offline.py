"""Replay an already-produced 20-SKU classification into disposable Excel copies.

NO search, LLM, semantic model, or SQLite writes. The classifier is NOT invoked.
This is a preflight tool: it never alters the campaign state or production.

python scripts/replay_failed_940_offline.py PATH_TO_FAILED_RUN

On a successful run, the patch is ready for a separate controlled recovery
commit. Success here does not mean the campaign is resumed.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

# Reuse the same exact-physical-row proof as the no-results persistence test.
# Do not rely on the normalized-key BI snapshot (collides across SKU aliases).
sys.path.insert(0, str(ROOT / "tests"))
from verify_baseline_940_isolated_write import exact_human_rows, human_examples

from pipeline_produtos import (
    Rules, _accepted_keys_from_baseline_file, _expected_cohort_accepted_after_run,
    _load_bi_lock_snapshot, _read_business_sheet, business_key, find_col,
    load_baseline_inventory, persist_baseline_excels, safe,
)


def _keys(frame):
    b = find_col(frame, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(frame, ["Modelo", "Model", "SKU", "Part Number"])
    return [business_key(safe(row.get(b)), safe(row.get(m)))
            for _, row in frame.iterrows()]


def preview(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    stages = report.get("stages", {})
    if (stages.get("collection", {}).get("status") != "PASS"
            or stages.get("classification", {}).get("status") != "FAIL"
            or stages.get("classification", {}).get("attempts") != 1):
        raise RuntimeError("REPLAY_BLOCKED_NOT_FAILED_AFTER_COLLECTION")
    cohort_keys = report.get("cohort_keys") or []
    if len(cohort_keys) != 20 or len(set(cohort_keys)) != 20:
        raise RuntimeError("REPLAY_BLOCKED_REQUIRES_EXACT_20_SKU_COHORT")

    cfg = json.loads((run_dir / "isolated_config.json").read_text(encoding="utf-8"))
    paths = {k: Path(cfg["files"][k])
             for k in ("master", "final", "input", "output", "cache")}
    if any(not p.is_file() for p in paths.values()):
        raise RuntimeError("REPLAY_BLOCKED_MISSING_ISOLATED_ARTIFACT")

    cohort = pd.read_excel(paths["input"], dtype=object, keep_default_na=False)
    results = pd.read_excel(paths["output"], sheet_name="Resultados",
                            dtype=object, keep_default_na=False)
    if (len(cohort) != 20 or _keys(cohort) != cohort_keys
            or len(results) != 20 or set(_keys(results)) != set(cohort_keys)
            or len(set(_keys(results))) != 20):
        raise RuntimeError("REPLAY_BLOCKED_INCOMPLETE_OR_DUPLICATE_CLASSIFICATION")

    baseline_before = load_baseline_inventory(paths["master"], paths["final"])
    previous_accepted = baseline_before["processed"]
    human_before = exact_human_rows(paths["final"])
    source_sha = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }
    records = results.to_dict(orient="records")

    with tempfile.TemporaryDirectory(prefix="940_replay_no_api_") as temp:
        tmp = Path(temp)
        master = tmp / paths["master"].name
        final = tmp / paths["final"].name
        source_input = tmp / paths["input"].name
        for source, target in (
            (paths["master"], master), (paths["final"], final),
            (paths["input"], source_input)
        ):
            shutil.copy2(source, target)

        # This is the REAL persistence path with the existing output rows.
        # Search/LLM are never called; only read-only cache hydration is allowed.
        isolated_cfg = dict(cfg)
        isolated_cfg["files"] = dict(cfg.get("files", {}))
        isolated_cfg["files"]["baseline_backup_dir"] = str(
            ROOT / "Backups_Baseline" / "replay_940_diagnostics"
        )
        stats = persist_baseline_excels(
            rows=records,
            master_path=master, final_path=final, input_path=source_input,
            rules=Rules(ROOT / "regras_negocio.json"),
            cache_path=paths["cache"],
            cfg=isolated_cfg,
        )
        after = load_baseline_inventory(master, final)
        lost_accepted = previous_accepted - after["processed"]
        lost_from_final = previous_accepted - after["final_accepted"]
        human_after = exact_human_rows(final)
        missing_human = human_before - human_after
        added_human = human_after - human_before
        newly_accepted_outside_cohort = (
            after["processed"] - previous_accepted - set(cohort_keys)
        )
        source_changed = [
            name for name, path in paths.items()
            if hashlib.sha256(path.read_bytes()).hexdigest() != source_sha[name]
        ]
        # Verify that the cohort's accepted statuses match the actual classifier
        # output; unresolved proposals must not be silently promoted.
        expected_cohort = _expected_cohort_accepted_after_run(
            set(cohort_keys), baseline_before["final_accepted"], records
        )
        actual_cohort = after["final_accepted"] & set(cohort_keys)
        unexpected_accepted = actual_cohort - expected_cohort
        missing_accepted = expected_cohort - actual_cohort
        result = {
            "status": "PASS" if not (lost_accepted or lost_from_final
                                   or missing_human or added_human
                                   or newly_accepted_outside_cohort or source_changed
                                   or unexpected_accepted or missing_accepted) else "FAIL",
            "input_cohort": 20,
            "result_rows": len(results),
            "accepted_before": len(previous_accepted),
            "accepted_after": len(after["processed"]),
            "lost_accepted": len(lost_accepted),
            "lost_from_final": len(lost_from_final),
            "protected_human_original_rows_missing_or_modified": sum(missing_human.values()),
            "protected_human_rows_newly_marked_or_modified": sum(added_human.values()),
            "protected_missing_examples": human_examples(missing_human),
            "protected_added_examples": human_examples(added_human),
            "unexpected_noncohort_accepted": len(newly_accepted_outside_cohort),
            "unexpected_noncohort_examples": sorted(newly_accepted_outside_cohort)[:20],
            "unexpected_cohort_accepted": len(unexpected_accepted),
            "missing_cohort_accepted": len(missing_accepted),
            "lost_examples": sorted(lost_accepted)[:20],
            "source_modified": source_changed,
            "persist_stats": stats,
            "search_calls": 0,
            "llm_calls": 0,
            "production_modified": False,
            "isolated_run_modified": False,
        }
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/replay_failed_940_offline.py RUN_DIR")
    report = preview(Path(sys.argv[1]))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    raise SystemExit(0 if report["status"] == "PASS" else 2)
