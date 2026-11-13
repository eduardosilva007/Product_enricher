"""Calculate how many RAW_EMPTY / prior-Serper-error SKUs are actually eligible.
Read-only against production cache, baseline and historical reports. No API calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_produtos import overnight_audit, read_json


def main() -> int:
    # When executed from git-archive preflight, ROOT is ONLY the code snapshot.
    # Source workbooks and campaign ledgers must come from the user's REAL repo.
    if len(sys.argv) != 2:
        raise RuntimeError("USAGE: audit_overnight_940_eligible.py FAILED_RUN_DIR")
    run_dir = Path(sys.argv[1]).resolve()
    if not (run_dir / "isolated_config.json").is_file():
        raise RuntimeError("ORIGINAL_FAILED_RUN_NOT_FOUND")
    repo_root = run_dir.parents[2]
    config_file = repo_root / "pipeline_config.json"
    cfg = read_json(config_file)
    paths = {
        k: (repo_root / str(cfg["files"][k])).resolve()
        for k in ("input", "cache", "master", "final")
    }
    if any(not p.is_file() for p in paths.values()):
        raise RuntimeError("ORIGINAL_CANONICAL_INPUT_CACHE_OR_BASELINE_MISSING")
    result = overnight_audit(
        paths["input"], paths["cache"], paths["master"], paths["final"]
    )
    prior_keys = set()
    history_files = sorted(run_dir.parent.glob("*/run_report.json"))
    for file in history_files:
        report = json.loads(file.read_text(encoding="utf-8"))
        if report.get("stages", {}).get("collection", {}).get("attempts", 0):
            prior_keys.update(report.get("cohort_keys") or [])

    normal = {
        row["business_key"] for row in result.get("candidate_rows", ())
        if row["business_key"] not in prior_keys
    }
    serper_rescues = {
        row["business_key"] for row in result.get("error_rows", ())
        if row["business_key"] not in prior_keys
        and str(row.get("provider") or "").casefold() == "serper"
        and row.get("prior_state") == "REQUEST_ATTEMPTED_NO_AUTO_RETRY"
        and "not enough credits" in str(row.get("previous_error") or "").casefold()
    }
    eligible = normal | serper_rescues
    print(json.dumps({
        "mode": "READONLY_ELIGIBLE_CAMPAIGN_AUDIT",
        "original_repo_root": str(repo_root),
        "normal_new_candidates": len(normal),
        "serper_quota_error_rescue": len(serper_rescues),
        "eligible_unique_skus": len(eligible),
        "prior_attempted_unique_skus": len(prior_keys),
        "history_reports": len(history_files),
        "expected_940_skus_remaining_after_pilots": len(eligible),
        "search_calls": 0,
        "llm_calls": 0,
        "canonical_files_modified": False,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ELIGIBILITY_AUDIT_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
