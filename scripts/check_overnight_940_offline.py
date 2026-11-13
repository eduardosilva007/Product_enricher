"""One-command, no-API readiness diagnosis for the interrupted 940-SKU night.

Run this script from the EXISTING working branch. It never checks out, merges,
creates worktrees, classifies, collects, promotes, or alters canonical files.
Reports live under Backups_Baseline. A PASS is an offline preflight only:
the interrupted 20-SKU campaign checkpoint still needs separate recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(chunk)
    return hash_.hexdigest()


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
    # Fingerprint every original campaign artifact before and after every
    # offline test. A test must not edit isolated or official workbooks.
    cfg = json.loads((run_dir / "isolated_config.json").read_text(encoding="utf-8"))
    sources = {
        "run_report": run_dir / "run_report.json",
        "isolated_config": run_dir / "isolated_config.json",
    }
    for name in ("master", "final", "cache", "classification_db", "input", "output"):
        value = (cfg.get("files") or {}).get(name)
        if value and Path(value).is_file():
            sources["isolated_" + name] = Path(value)
    originals_file = run_dir / "original_sources.json"
    if originals_file.is_file():
        sources["original_manifest"] = originals_file
        originals = json.loads(originals_file.read_text(encoding="utf-8"))
        for name, meta in originals.items():
            path = Path(meta.get("path", ""))
            if path.is_file():
                sources["canonical_" + name] = path
    before = {name: fingerprint(path) for name, path in sources.items()}
    summary = {
        "status": "RUNNING", "run_dir": str(run_dir),
        "mode": "OFFLINE_NO_APIS", "tests": [],
        "source_hashes_checked": len(sources),
        "original_campaign_unchanged": None,
        "overnight_approved": False,
    }
    failures = []
    try:
        # Run ALL independent checks. Never stop at the first failure:
        # one forensic pass should produce complete diagnostics.
        for label, base in CHECKS:
            if label == "historical_reservation_ledger":
                command = base + [str(run_dir.parent)]
            elif label in ("remaining_eligible_skus", "failed_20_sku_output_replay") or label.startswith("accepted_baseline"):
                command = base + [str(run_dir)]
            else:
                command = list(base)
            print(f"\\n[940 OFFLINE] START {label}", flush=True)
            process = subprocess.run(
                command, cwd=ROOT, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
            log_path = folder / (summary_path.stem + "_" + label + ".log")
            output = (process.stdout or "") + (process.stderr or "")
            log_path.write_text(output, encoding="utf-8")
            item = {
                "name": label, "exit_code": process.returncode,
                "status": "PASS" if process.returncode == 0 else "FAIL",
                "log": str(log_path),
            }
            summary["tests"].append(item)
            if process.returncode:
                failures.append(label)
                print(f"[940 OFFLINE] FAIL {label}. Last 20 output lines:", flush=True)
                print("\\n".join(output.splitlines()[-20:]), flush=True)
            else:
                print(f"[940 OFFLINE] PASS {label}", flush=True)

        after = {}
        for name, path in sources.items():
            after[name] = fingerprint(path) if path.is_file() else "MISSING"
        changed = [name for name in sources if after[name] != before[name]]
        summary["original_campaign_unchanged"] = not changed
        summary["modified_source_files"] = changed
        if changed:
            failures.append("ORIGINAL_SOURCE_CHANGED")
        summary["failed_checks"] = failures
        summary["status"] = (
            "OFFLINE_PASS_RECOVERY_PENDING" if not failures else "BLOCKED"
        )
        print(f"\\n[940 OFFLINE] RESULT: {summary['status']}", flush=True)
        print(f"[940 OFFLINE] Failed: {failures}; source_modified: {changed}", flush=True)
        return 0 if not failures else 2
    except Exception as exc:
        summary["status"] = "BLOCKED"
        summary["reason"] = str(exc)
        print(f"\\n[940 OFFLINE] BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[940 OFFLINE] REPORT: {summary_path}", flush=True)

if __name__ == "__main__":
    raise SystemExit(main())
