"""One-command, no-API readiness diagnosis for the interrupted 940-SKU night.

Run this script from the EXISTING working branch. It never checks out, merges,
creates worktrees, classifies, collects, promotes, or alters canonical files.
Reports live under Backups_Baseline. A PASS is an offline preflight only:
the interrupted 20-SKU campaign checkpoint still needs separate recovery.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("regression_suite",
     [sys.executable, "-m", "unittest", "discover", "-s", "tests",
      "-p", "test_*.py", "-v"]),
    ("historical_reservation_ledger",
     [sys.executable, "scripts/audit_overnight_history_offline.py"]),
    ("remaining_eligible_skus",
     [sys.executable, "scripts/audit_overnight_940_eligible.py"]),
    ("accepted_baseline_readonly",
     [sys.executable, "tests/verify_baseline_940_readonly.py"]),
    ("accepted_baseline_isolated_write",
     [sys.executable, "tests/verify_baseline_940_isolated_write.py"]),
    ("failed_20_sku_output_replay",
     [sys.executable, "scripts/replay_failed_940_offline.py"]),
)


def main() -> int:
    p = argparse.ArgumentParser(description="940 offline readiness; no API calls")
    p.add_argument("run_dir", type=Path)
    options = p.parse_args()
    run_dir = options.run_dir.resolve()
    required = ("run_report.json", "isolated_config.json")
    if any(not (run_dir / name).is_file() for name in required):
        raise RuntimeError(f"RUN_NOT_FOUND_OR_INCOMPLETE: {run_dir}")

    report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    if len(report.get("cohort_keys", [])) != 20:
        raise RuntimeError("EXPECTED_FAILED_20_SKU_RUN")
    folder = ROOT / "Backups_Baseline" / "readiness_940"
    folder.mkdir(parents=True, exist_ok=True)
    summary_path = folder / (
        "offline_readiness_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    summary = {"status": "RUNNING", "run_dir": str(run_dir),
               "mode": "OFFLINE_NO_APIS", "tests": [],
               "original_campaign_unchanged": True,
               "overnight_approved": False}
    try:
        for label, base in CHECKS:
            command = base + ([str(run_dir)] if label.startswith("accepted_baseline")
                              or label == "failed_20_sku_output_replay" else [])
            print(f"\n[940 OFFLINE] START {label}", flush=True)
            result = subprocess.run(command, cwd=ROOT, check=False)
            summary["tests"].append({
                "name": label, "exit_code": result.returncode,
                "status": "PASS" if result.returncode == 0 else "FAIL"
            })
            if result.returncode:
                raise RuntimeError(f"{label} failed: exit_code={result.returncode}")
            print(f"[940 OFFLINE] PASS {label}", flush=True)
        summary["status"] = "OFFLINE_PASS_CHECKPOINT_RECOVERY_PENDING"
        print("\n[940 OFFLINE] ALL CHECKS PASS; DO NOT START LIVE UNTIL RECOVERY")
        return 0
    except Exception as exc:
        summary["status"] = "BLOCKED"
        summary["reason"] = str(exc)
        print(f"\n[940 OFFLINE] BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[940 OFFLINE] REPORT: {summary_path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
