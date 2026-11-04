#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from validate_v42_acceptance import validate


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--result", required=True)
    p.add_argument("--gold", default="acceptance/v42_gladiator_10.csv")
    p.add_argument("--sheet", default="")
    p.add_argument("--min-pass-rate", type=float, default=1.0)
    p.add_argument("--report-json", default="", help="Write overall and per-family evaluation metrics")
    args = p.parse_args()

    result = Path(args.result)
    if not result.is_absolute():
        result = ROOT / result
    gold = Path(args.gold)
    if not gold.is_absolute():
        gold = ROOT / gold

    if not 0 <= args.min_pass_rate <= 1:
        print("CLASSIFICATION EVAL: BLOCKED | invalid pass-rate threshold")
        return 2
    try:
        report = validate(result, gold, args.sheet)
    except (ValueError, KeyError, FileNotFoundError, OSError) as exc:
        print(f"CLASSIFICATION EVAL: BLOCKED | {type(exc).__name__}: {exc}")
        return 2
    if report.empty:
        print("CLASSIFICATION EVAL: BLOCKED | NO_GOLD_ROWS")
        return 2
    total = len(report)
    passed = int((report["PASS"] == "SIM").sum())
    rate = passed / max(1, total)

    print("CLASSIFICATION EVAL")
    print("=" * 86)
    print(f"Gold rows................................. {total}")
    print(f"Passed.................................... {passed}")
    print(f"Pass rate................................. {rate:.2%}")
    failed = report.loc[report["PASS"] != "SIM"]
    if len(failed):
        print("-" * 86)
        print(failed[["Marca", "Modelo", "Failure"]].to_string(index=False))
    print("=" * 86)
    families = []
    for family, group in report.groupby("Expected FAMILY", dropna=False, sort=True):
        n = len(group)
        ok = int((group["PASS"] == "SIM").sum())
        families.append({"family": str(family), "total": n, "passed": ok, "pass_rate": round(ok / n, 6)})
    print("PER FAMILY:")
    for item in families:
        print(f"  {item['family']}: {item['passed']}/{item['total']} ({item['pass_rate']:.2%})")
    if args.report_json:
        destination = Path(args.report_json)
        if not destination.is_absolute():
            destination = ROOT / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({
            "gold_path": str(gold), "result_path": str(result),
            "total": total, "passed": passed, "pass_rate": round(rate, 6),
            "min_pass_rate": args.min_pass_rate, "families": families,
            "failures": failed[["Marca", "Modelo", "Failure"]].fillna("").to_dict(orient="records"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"METRICS JSON: {destination}")
    if rate < args.min_pass_rate:
        print("STATUS: BLOCKED")
        return 2
    print("STATUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
