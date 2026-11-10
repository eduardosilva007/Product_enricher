#!/usr/bin/env python3
"""Sequential, fail-fast OFFLINE readiness gate for Argentina mass pilot.

No network operations, model downloads, API-key access or production mutations.
The only outputs are written below --output, never to the Gold or cache.
The gates are intentionally NOT a mass-production approval.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = [
    ("V5 taxonomy regression", ["-m","unittest","test_v5_taxonomy_benchmark","-v"]),
    ("V5 cache-title regression", ["-m","unittest","discover","-s","tests","-p","test_v5_cache_title_ablation.py","-v"]),
    ("Mass readiness regression", ["-m","unittest","discover","-s","tests","-p","test_mass_readiness_audit.py","-v"]),
    ("Observed-cohort regression", ["-m","unittest","discover","-s","tests","-p","test_observed_failure_cohort.py","-v"]),
    ("Provider quotas/resume (synthetic)",["-m","unittest","discover","-s","tests","-p","test_nightly_provider_router.py","-v"]),
    ("Optional identity fallbacks",["-m","unittest","discover","-s","tests","-p","test_optional_identity_fallbacks.py","-v"]),
    ("Offline identity benchmark regression",["-m","unittest","discover","-s","tests","-p","test_benchmark_identity_offline.py","-v"]),
]

def run_sequence(gold:Path, cache:Path, out:Path) -> dict:
    gold=gold.resolve()
    cache=cache.resolve()
    out=out.resolve()
    if not gold.is_file() or not cache.is_file():
        raise FileNotFoundError(f"Required input missing: gold={gold.is_file()}, cache={cache.is_file()}")
    if out == ROOT or out == gold or out == cache:
        raise ValueError("Output must be separate from source input paths")
    out.mkdir(parents=True,exist_ok=True)
    stages = TESTS + [
        ("Gold/cache offline cohort audit",[
            str(ROOT/"experiments/local_classifier_v5/mass_readiness_audit.py"),
            "--gold",str(gold),"--cache",str(cache),
            "--output",str(out/"mass_readiness")
        ]),
    ]
    report={
        "mode":"OFFLINE_FAIL_FAST_NO_APIS",
        "started_at":datetime.now(timezone.utc).isoformat(),
        "inputs":{"gold":str(gold),"cache":str(cache)},
        "go_for_mass":"BLOCKED_PENDING_LIVE_AND_SHADOW_GATES",
        "stages":[],
        "outstanding":["REAL_GLINER_EVALUATION","JEV_CLEF_SHADOW",
                       "REAL_PROVIDER_QUOTA_CRASH_RECOVERY","MANUAL_BI_LOCK_PRESERVATION",
                       "100_SKU_CONTROLLED_END_TO_END"],
    }
    report_path=out/"sequential_readiness_report.json"
    def save():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    save()
    for number,(label,argv) in enumerate(stages,1):
        print(f"\n[{number}/{len(stages)}] {label}",flush=True)
        started=time.monotonic()
        # Captured logs are saved regardless of pass/fail; all commands explicit.
        try:
            completed=subprocess.run([sys.executable,*argv],cwd=ROOT,stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                     text=True,encoding="utf-8",errors="replace",check=False)
            output=completed.stdout
            code=completed.returncode
        except Exception as exc:
            code=1
            output=f"{type(exc).__name__}: {exc}"
        log_name=f"stage_{number:02d}.log"
        (out/log_name).write_text(output,encoding="utf-8")
        status="PASS" if code==0 else "FAIL"
        item={"stage":label,"status":status,"exit_code":code,
              "seconds":round(time.monotonic()-started,2),"log":log_name}
        report["stages"].append(item)
        report["last_status"]=status
        save()
        print(f"  {status} | log={out/log_name} | {item['seconds']}s",flush=True)
        if code:
            report["stop_reason"]=f"STOP_ON_FAILURE: {label}"
            save()
            print("Last log excerpt:\\n"+output[-4000:],flush=True)
            return report
    report["stop_reason"]="OFFLINE_COMPLETE_LIVE_GATES_REMAIN"
    save()
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold",default="Base Final Produtos Enriquecida.xlsx")
    parser.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    parser.add_argument("--output",default="piloto_argentina/sequential_readiness")
    args=parser.parse_args()
    report=run_sequence(Path(args.gold),Path(args.cache),Path(args.output))
    print("\\n"+json.dumps({
        "result":report["stop_reason"],"stages_passed":sum(x["status"]=="PASS" for x in report["stages"]),
        "stages_executed":len(report["stages"]),
        "report":str(Path(args.output).resolve()),
        "go_for_mass":report["go_for_mass"]},ensure_ascii=False,indent=2),flush=True)
    return 0 if report["stop_reason"]=="OFFLINE_COMPLETE_LIVE_GATES_REMAIN" else 1

if __name__=="__main__":
    raise SystemExit(main())
