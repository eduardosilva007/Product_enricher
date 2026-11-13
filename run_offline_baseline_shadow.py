#!/usr/bin/env python3
"""Offline end-to-end baseline persistence probe on isolated Excel/SQLite copies.

No API classification or new search: --llm-provider local --no-clef.
Executes the real classification persistence path and verifies source hashes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):h.update(block)
    return h.hexdigest()

def sqlite_snapshot(source:Path,target:Path):
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True,timeout=30)) as src:
        with closing(sqlite3.connect(target,timeout=30)) as dst:
            src.backup(dst)

def offline_baseline_probe(args):
    if not (0<=args.max_items<=10000):
        raise ValueError("Explicit safety cap: 0 (all eligible) through 10000 products")
    if args.pipeline not in {"pipeline_argentina_piloto.py", "pipeline_argentina_excel_fix.py"}:
        raise ValueError("Only isolated Argentina pilot scripts are allowed; never call production pipeline_produtos.py")
    if not (ROOT/args.pipeline).is_file():
        raise FileNotFoundError(f"Pilot script not found: {ROOT/args.pipeline}")
    source_config=(ROOT/"pipeline_config.json").resolve()
    config=json.loads(source_config.read_text(encoding="utf-8-sig"))
    paths={
      "input":(ROOT/args.input).resolve(),
      "cache":(ROOT/args.cache).resolve(),
      "master":(ROOT/args.master).resolve(),
      "final":(ROOT/args.final).resolve(),
      "classification_db":(ROOT/args.db).resolve(),
    }
    mandatory=("input","cache","final")
    for key in mandatory:
        if not paths[key].is_file():raise FileNotFoundError(f"{key}: {paths[key]}")
    outroot=(ROOT/"piloto_argentina"/"offline_baseline_shadow").resolve()
    outroot.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run=outroot/now
    run.mkdir(exist_ok=False)
    original_hashes={k:digest(v) for k,v in paths.items() if v.is_file()}
    local={}
    for key,path in paths.items():
        dest=run/("source_"+key+path.suffix)
        if path.is_file():
            if path.suffix.lower() in (".sqlite",".db"):
                sqlite_snapshot(path,dest)
            else:shutil.copy2(path,dest)
        local[key]=dest
    files=config.setdefault("files",{})
    for key,loc in local.items():files[key]=str(loc)
    for key,filename in {
        "output":"classification_output.xlsx",
        "inventory_output":"inventory_output.xlsx",
        "baseline_backup_dir":"baseline_backups",
        "baseline_audit":"baseline_audit.csv",
        "collection_report":"collection_report.xlsx",
        "precollection_report":"precollection_report.xlsx",
        "pending_with_cache":"pending_with_cache.xlsx",
        "pending_without_cache":"pending_without_cache.xlsx",
        "collection_report_cache_rebuild":"collection_report_cache_rebuild.xlsx",
        "evidence_export_jsonl":"evidence_export.jsonl",
    }.items():files[key]=str(run/filename)
    # Defense in depth: the process CLI disables LLM; caps also prevent accidental calls.
    for provider in ("jev", "clef"):
        config.setdefault(provider,{})["max_api_calls_per_run"]=0
    config_file=run/"shadow_config.json"
    config_file.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    cmd=[
        sys.executable,str(ROOT/args.pipeline),
        "--mode","classify","--config",str(config_file),
        "--llm-provider","local","--no-clef",
        "--input",str(local["input"]),"--cache",str(local["cache"]),
        "--master",str(local["master"]),"--final",str(local["final"]),
        "--db",str(local["classification_db"]),
        "--output",str(run/"classification_output.xlsx"),
        "--inventory-output",str(run/"inventory_output.xlsx"),
        "--max-items",str(args.max_items),
        "--max-api-calls","0",
        "--max-tokens","0",
        "--yes"
    ]
    status={"mode":"OFFLINE_LOCAL_BASELINE_PERSISTENCE_NO_APIS","isolation_dir":str(run),
        "max_items":args.max_items,"llm_provider":"local","search_calls_allowed":0,
        "production_files":list(original_hashes),
        "status":"NOT_STARTED"}
    report=run/"probe_report.json"
    def save():report.write_text(json.dumps(status,indent=2,ensure_ascii=False),encoding="utf-8")
    save()
    try:
        status["status"]="RUNNING";save()
        with (run/"pipeline_stdout.log").open("w",encoding="utf-8") as log:
            proc=subprocess.run(cmd,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                timeout=args.timeout_minutes*60,check=False)
            status["pipeline_exit_code"]=proc.returncode
            outputs = [run/"classification_output.xlsx",local["master"],local["final"]]
            status["isolated_outputs_exist"]=all(p.is_file() for p in outputs)
            status["status"]="FINISHED" if proc.returncode==0 and status["isolated_outputs_exist"] else "PIPELINE_OR_EXPORT_FAILED"
    except subprocess.TimeoutExpired:
        status["status"]="TIMEOUT"
    finally:
        changed=[key for key,value in original_hashes.items() if digest(paths[key])!=value]
        status["source_files_modified"]=changed
        if changed:status["status"]="SOURCE_INTEGRITY_FAILURE"
        # These stages do not establish classification quality until output reviewed.
        status["mass_approval"]="BLOCKED_PENDING_MANUAL_AND_GOLD_REVIEW"
        save()
    print(json.dumps(status,indent=2,ensure_ascii=False))
    return 0 if status["status"]=="FINISHED" and not status["source_files_modified"] else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline",default="pipeline_argentina_excel_fix.py")
    parser.add_argument("--input",default="Base de Produtos.xlsx")
    parser.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    parser.add_argument("--master",default="Consolidado Base de Produtos Enriquecida.xlsx")
    parser.add_argument("--final",default="Base Final Produtos Enriquecida.xlsx")
    parser.add_argument("--db",default="piloto_argentina/pipeline.sqlite")
    parser.add_argument("--max-items",type=int,default=5)
    parser.add_argument("--timeout-minutes",type=int,default=60)
    args=parser.parse_args()
    if not 1<=args.timeout_minutes<=60:parser.error("timeout must be 1..60 minutes")
    return offline_baseline_probe(args)

if __name__=="__main__":raise SystemExit(main())
