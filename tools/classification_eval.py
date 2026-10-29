#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    args = p.parse_args()

    result = Path(args.result)
    if not result.is_absolute():
        result = ROOT / result
    gold = Path(args.gold)
    if not gold.is_absolute():
        gold = ROOT / gold

    report = validate(result, gold, args.sheet)
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
    if rate < args.min_pass_rate:
        print("STATUS: BLOCKED")
        return 2
    print("STATUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
