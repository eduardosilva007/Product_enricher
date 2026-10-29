#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AREA = {
    "classification_engine_v42.py": ("classification-contracts", "HIGH"),
    "semantic_bge_v42.py": ("semantic-retrieval", "HIGH"),
    "v42_adapters.py": ("providers-extractors", "HIGH"),
    "classify_v42.py": ("runtime-entrypoint", "HIGH"),
    "regras_negocio.json": ("rules-taxonomy", "HIGH"),
    "pipeline_config.json": ("runtime-config", "HIGH"),
    "pipeline_produtos.py": ("legacy-production-core", "HIGH"),
    "test_v42_engine_contracts.py": ("tests", "LOW"),
    ".github/workflows/validate.yml": ("ci", "MEDIUM"),
}


def run_git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="origin/main")
    args = p.parse_args()

    diff = run_git("diff", "--name-only", f"{args.base}...HEAD")
    if diff.returncode != 0:
        diff = run_git("diff", "--name-only", "HEAD~1...HEAD")
    if diff.returncode != 0:
        print("CHANGE IMPACT: BLOCKED")
        print(diff.stderr.strip())
        return 2

    files = [x.strip() for x in diff.stdout.splitlines() if x.strip()]
    risk_rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    max_risk = "LOW"
    areas = []
    for path in files:
        area, risk = AREA.get(path, ("other", "MEDIUM"))
        areas.append((path, area, risk))
        if risk_rank[risk] > risk_rank[max_risk]:
            max_risk = risk

    print("CHANGE IMPACT")
    print("=" * 86)
    for path, area, risk in areas:
        print(f"{risk:6} | {area:24} | {path}")
    print("=" * 86)
    print("RISK:", max_risk)
    if max_risk == "HIGH":
        print("REQUIRED: contracts + runtime doctor + frozen acceptance before scale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
