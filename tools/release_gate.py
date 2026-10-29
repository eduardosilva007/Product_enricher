#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label, args):
    print()
    print("=" * 86)
    print(label)
    print("=" * 86)
    proc = subprocess.run(args, cwd=ROOT)
    return proc.returncode == 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=["preflight", "postrun"], default="preflight")
    p.add_argument("--mode", choices=["local", "ci"], default="local")
    p.add_argument("--result", default="")
    p.add_argument("--gold", default="acceptance/v42_gladiator_10.csv")
    p.add_argument("--gliner-model", default="")
    p.add_argument("--provider", choices=["clef", "none"], default="clef")
    args = p.parse_args()

    py = sys.executable
    checks = []

    checks.append(("Architecture Guardian", [py, "tools/architecture_check.py"]))
    checks.append(("Repository Auditor", [py, "tools/repo_audit.py"]))
    checks.append(("Change Impact", [py, "tools/change_impact.py"]))
    checks.append(("Unit + Contract Tests", [py, "-m", "unittest", "discover", "-p", "test_*.py"]))

    doctor = [py, "tools/runtime_doctor.py", "--mode", args.mode, "--provider", args.provider]
    if args.gliner_model:
        doctor += ["--gliner-model", args.gliner_model]
    checks.append(("Runtime Doctor", doctor))

    results = []
    for label, cmd in checks:
        ok = run(label, cmd)
        results.append((label, ok))
        if not ok:
            break

    if all(ok for _, ok in results) and args.phase == "postrun":
        if not args.result:
            print("POSTRUN requires --result <xlsx>")
            results.append(("Classification Eval", False))
        else:
            results.append((
                "Classification Eval",
                run("Classification Eval", [
                    py, "tools/classification_eval.py",
                    "--result", args.result,
                    "--gold", args.gold,
                ]),
            ))

    print()
    print("=" * 86)
    print("RELEASE GATE")
    print("=" * 86)
    for label, ok in results:
        print(f"{label:.<42} {'PASS' if ok else 'FAIL'}")

    if not results or not all(ok for _, ok in results):
        print("STATUS: BLOCKED")
        return 2

    if args.phase == "postrun":
        print("STATUS: READY_FOR_SCALE")
    else:
        print("STATUS: READY_FOR_CONTROL_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
