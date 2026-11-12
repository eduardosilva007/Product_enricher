"""Calculate how many RAW_EMPTY / prior-Serper-error SKUs are actually eligible.
Read-only against production cache, baseline and historical reports. No API calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_produtos import OUT, overnight_audit, read_json, source_paths


def main() -> int:
    cfg = read_json(ROOT / "pipeline_config.json")
    paths = source_paths(cfg)
    result = overnight_audit(
        paths["input"], paths["cache"], paths["master"], paths["final"]
    )
    prior_keys = set()
    history_files = sorted(OUT.glob("*/run_report.json"))
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
