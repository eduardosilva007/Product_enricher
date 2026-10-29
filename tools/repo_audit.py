#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_produtos as pp

RUNTIME = [
    "pipeline_produtos.py",
    "classification_engine_v42.py",
    "semantic_bge_v42.py",
    "v42_adapters.py",
    "classify_v42.py",
    "validate_v42_acceptance.py",
]


def git(*args: str):
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def main() -> int:
    failures = []
    warnings = []

    for rel in RUNTIME:
        path = ROOT / rel
        if not path.exists():
            failures.append(f"MISSING:{rel}")
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            failures.append(f"PY_COMPILE:{rel}:{exc}")

    try:
        json.loads((ROOT / "pipeline_config.json").read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"CONFIG_JSON:{exc}")

    try:
        rules = pp.Rules(ROOT / "regras_negocio.json")
        for err in rules.validate():
            failures.append(f"RULES:{err}")
    except Exception as exc:
        failures.append(f"RULES_LOAD:{exc}")

    runner = (ROOT / "classify_v42.py").read_text(encoding="utf-8")
    env_pos = runner.find("_load_runtime_env(base)")
    pf_pos = runner.find("provider.preflight()")
    if env_pos < 0:
        failures.append("RUNNER_DOES_NOT_LOAD_ENV")
    elif pf_pos >= 0 and env_pos > pf_pos:
        failures.append("RUNNER_LOADS_ENV_AFTER_PROVIDER_PREFLIGHT")

    ignored = git("check-ignore", ".env")
    if ignored.returncode != 0:
        failures.append("ENV_NOT_GITIGNORED")

    tracked = git("ls-files", ".env")
    if tracked.returncode == 0 and tracked.stdout.strip():
        failures.append("ENV_IS_TRACKED")

    status = git("status", "--short")
    if status.returncode == 0 and status.stdout.strip():
        warnings.append("WORKTREE_HAS_LOCAL_CHANGES")
        print("WORKTREE:")
        print(status.stdout.rstrip())

    if failures:
        print("REPOSITORY AUDIT: BLOCKED")
        for item in failures:
            print(" -", item)
        for item in warnings:
            print(" !", item)
        return 2

    print("REPOSITORY AUDIT: PASS")
    for item in warnings:
        print(" !", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
