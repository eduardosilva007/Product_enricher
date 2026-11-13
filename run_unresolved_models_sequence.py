#!/usr/bin/env python3
"""One-command local unresolved-case reranker + GLiNER2.5 diagnostic.

Uses six actual CNH unresolved from 2026-11-10 offline run; no external APIs.
A stage fails independently; report and stage logs always saved.
"""
from __future__ import annotations
import argparse,json,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
EXP=ROOT/"experiments"/"local_classifier_v5"
DEFAULT_SKUS="47507106,47804068,84580265,87313617,87541270,87624549"

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    p.add_argument("--skus",default=DEFAULT_SKUS)
    p.add_argument("--reranker-model",default=r"C:\p\models\bge-reranker-v2-m3")
    p.add_argument("--gliner-model",default=r"C:\p\models\gliner2.5-multi-v1")
    p.add_argument("--output",default="piloto_argentina/unresolved_models")
    a=p.parse_args()
    cache=Path(a.cache).resolve()
    if not cache.is_file():p.error(f"Cache missing: {cache}")
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ)
    env.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1")
    steps=[
        ("reranker_evidence",Path(a.reranker_model),[
            sys.executable,str(EXP/"benchmark_unresolved_evidence.py"),
            "--cache",str(cache),"--model",str(Path(a.reranker_model)),
            "--skus",a.skus,"--output",str(out/"01_reranker_evidence.json")]),
        ("gliner_unresolved",Path(a.gliner_model),[
            sys.executable,str(EXP/"benchmark_gliner_cache.py"),
            "--cache",str(cache),"--model",str(Path(a.gliner_model)),
            "--only-skus",a.skus,"--max-items","30",
            "--output",str(out/"02_gliner_unresolved.json")]),
    ]
    report={"mode":"OFFLINE_UNRESOLVED_DIAGNOSTIC","timestamp":datetime.now(timezone.utc).isoformat(),
            "skus":a.skus.split(","),"auto_approval":False,"stages":[]}
    dest=out/"unresolved_sequence_report.json"
    def save():
        tmp=dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
        tmp.replace(dest)
    save()
    for name,model,cmd in steps:
        row={"name":name,"status":"PENDING","log":name+".log"}
        report["stages"].append(row)
        log=out/row["log"]
        print(f"[{name}] Starting",flush=True)
        if not (model/"config.json").is_file():
            row.update(status="SKIPPED",reason=f"Local model config.json missing: {model}")
            log.write_text(row["reason"],encoding="utf-8")
            save();continue
        row["status"]="RUNNING";save()
        try:
            with log.open("w",encoding="utf-8") as fh:
                with subprocess.Popen(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                    stdout=fh,stderr=subprocess.STDOUT) as proc:
                    code=proc.wait()
            row.update(status="PASS" if code==0 else "FAIL",exit_code=code)
        except Exception as exc:
            row.update(status="FAIL",error=f"{type(exc).__name__}: {exc}")
            log.write_text(row["error"],encoding="utf-8")
        save()
        print(f"[{name}] {row['status']}",flush=True)
    report["finished"]=True
    save()
    print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
    return 0 if all(x["status"]=="PASS" for x in report["stages"]) else 2

if __name__=="__main__":raise SystemExit(main())
