#!/usr/bin/env python3
"""Fail-closed, copy-isolated controlled live night pilot for Product Enricher.

Default is DRY/PRECHECK ONLY. Real Search/JEV calls require --execute-live and
explicit positive quotas. No mass execution, no auto promotion to production.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,sys,time,uuid
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/"pipeline_config.json"
OUTPUT_ROOT=ROOT/"piloto_argentina"/"controlled_night"
INPUT_NAMES=("input","cache","master","final","classification_db")
RUN_SCRIPTS=("pipeline_produtos.py","nightly_provider_router.py")

def sha256(path):
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1048576),b""):h.update(block)
    return h.hexdigest()

def sqlite_backup(source,target):
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)) as a:
        with closing(sqlite3.connect(target)) as b:a.backup(b)

def fresh_run():
    return OUTPUT_ROOT/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8])

def config_sources(cfg):
    files=cfg["files"]
    return {k:(ROOT/str(files[k])).resolve() for k in INPUT_NAMES}

def precheck(args,cfg):
    if not all((ROOT/n).is_file() for n in RUN_SCRIPTS):
        raise RuntimeError("Missing required pipeline files")
    if not args.execute_live:
        return {"mode":"PRECHECK_ONLY","live_authorized":False,
                "note":"No Search or LLM calls. Run --execute-live with bounded positive budgets to authorize."}
    if not (1<=args.search_budget<=20 and 1<=args.llm_budget<=20 and 1<=args.classify_items<=200):
        raise ValueError("Controlled live caps: search 1..20, LLM 1..20, classification 1..200")
    if not 1<=args.token_budget<=50000:raise ValueError("Token budget must be 1..50000")
    if not 15<=args.timeout_minutes<=240:raise ValueError("Timeout 15..240")
    collection=cfg.get("collection",{}).get("hard_limits",{})
    if (collection.get("max_requests_per_product")!=1 or
        collection.get("allow_automatic_retry") or
        collection.get("allow_secondary_search") or
        collection.get("allow_provider_failover") or collection.get("allow_extract")):
        raise RuntimeError("Collection cost safety contract invalid")
    idcfg=cfg.get("identity",{})
    if not idcfg.get("auto_approval_requires_strong_identity"):
        raise RuntimeError("Missing strong identity gate")
    if cfg.get("recovery",{}).get("hard_lock_review_bi_value")!="OK":
        raise RuntimeError("BI hard lock invalid")
    if not cfg.get("semantic_funnel",{}).get("local_files_only"):
        raise RuntimeError("Semantic model must be locally available")
    envfile=ROOT/".env"
    if not envfile.is_file():raise RuntimeError("Missing .env")
    # Import only the repo's own parser; never display or store credentials.
    from pipeline_produtos import load_env_file
    load_env_file(envfile)
    from nightly_provider_router import configured_slots
    slots=configured_slots()
    if not slots:raise RuntimeError("No explicit positive configured provider quota in .env")
    if not any(os.getenv(name) for name in ("JEV_API_KEY","TYPESAFE_API_KEY","CLOUDFLARE_API_TOKEN","CLOUDFLARE_AUTH_TOKEN")):
        # JEV may use a different authentication name; live phase must fail safely.
        raise RuntimeError("No known LLM credentials detected. Validate JEV configuration first.")
    return {"mode":"LIVE_AUTHORIZED","slots":[{"provider":s.provider,"account":s.account,"configured_limit":s.limit} for s in slots]}

def isolate(cfg,run):
    run.mkdir(parents=True,exist_ok=False)
    src=config_sources(cfg)
    missing=[k for k in ("input","cache","final") if not src[k].is_file()]
    if missing:raise FileNotFoundError(f"Missing required inputs {missing}")
    originals={k:{"path":str(p),"sha256":sha256(p)} for k,p in src.items() if p.is_file()}
    files=cfg.setdefault("files",{})
    for k,p in src.items():
        dest=run/("source_"+k+p.suffix)
        if p.is_file():
            if p.suffix.lower() in (".sqlite",".db"):
                sqlite_backup(p,dest)
            else:shutil.copy2(p,dest)
        files[k]=str(dest)
    for k,filename in {
        "output":"classification_output.xlsx",
        "inventory_output":"inventory_output.xlsx",
        "precollection_report":"precollection_report.xlsx",
        "collection_report":"collection_report.xlsx",
        "pending_with_cache":"pending_with_cache.xlsx",
        "pending_without_cache":"pending_without_cache.xlsx",
        "collection_report_cache_rebuild":"collection_report_cache_rebuild.xlsx",
        "baseline_backup_dir":"baseline_backups",
        "baseline_audit":"baseline_audit.csv",
        "evidence_export_jsonl":"evidence_export.jsonl",
    }.items():files[k]=str(run/filename)
    path=run/"isolated_config.json"
    path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
    (run/"original_sources.json").write_text(json.dumps(originals,indent=2,ensure_ascii=False),encoding="utf-8")
    return originals,path

def verify_sources(originals):
    return [k for k,item in originals.items()
            if not Path(item["path"]).is_file() or sha256(Path(item["path"]))!=item["sha256"]]

def report_save(path,state):
    tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False),encoding="utf-8")
    tmp.replace(path)

def run_step(name,cmd,run,minutes,state,report):
    log=run/(name+".log")
    stage={"name":name,"status":"RUNNING","command":[x for x in cmd if x!="--yes"],
           "log":str(log),"started_at":datetime.now(timezone.utc).isoformat()}
    state["stages"].append(stage);report_save(report,state)
    env=dict(os.environ)
    env.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1")
    with log.open("w",encoding="utf-8") as fh:
        try:
            with subprocess.Popen(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                                  stdout=fh,stderr=subprocess.STDOUT) as child:
                try:code=child.wait(timeout=minutes*60)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait()
                    stage.update(status="TIMEOUT",exit_code=None)
                else:stage.update(status="PASS" if code==0 else "FAIL",exit_code=code)
        except Exception as exc:stage.update(status="FAIL",error=f"{type(exc).__name__}: {exc}")
    stage["finished_at"]=datetime.now(timezone.utc).isoformat()
    report_save(report,state)
    print(f"[NIGHT] {name} => {stage['status']}",flush=True)
    return stage["status"]=="PASS"

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute-live",action="store_true",help="Explicit permission for capped real API usage")
    p.add_argument("--search-budget",type=int,default=0)
    p.add_argument("--llm-budget",type=int,default=0)
    p.add_argument("--token-budget",type=int,default=0)
    p.add_argument("--classify-items",type=int,default=80)
    p.add_argument("--timeout-minutes",type=int,default=120)
    a=p.parse_args()
    cfg=json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    gate=precheck(a,cfg)
    if not a.execute_live:
        print(json.dumps(gate,indent=2,ensure_ascii=False))
        return 0
    run=fresh_run()
    originals,config=isolate(cfg,run)
    state={"mode":"ISOLATED_CAPPED_LIVE_PILOT","production_approved":False,
           "mass_approval":"BLOCKED_PENDING_REVIEW","gate":gate,
           "budgets":{"search":a.search_budget,"llm":a.llm_budget,"tokens":a.token_budget,
                      "classification_items":a.classify_items},
           "started_at":datetime.now(timezone.utc).isoformat(),"stages":[]}
    report=run/"run_report.json"
    report_save(report,state)
    pipeline=[sys.executable,str(ROOT/"pipeline_produtos.py"),"--config",str(config),
              "--input",cfg["files"]["input"],"--cache",cfg["files"]["cache"],
              "--master",cfg["files"]["master"],"--final",cfg["files"]["final"],
              "--db",cfg["files"]["classification_db"],"--yes"]
    # Search provider auto router honors its own shared reservation quota; max-search-new is
    # a second independent finite per-run limit. Recollect-empty is explicit: no stdin prompts.
    try:
        first=run_step("01_collection",pipeline+[
            "--mode","collect","--auto-providers","--recollect-empty",
            "--max-search-new",str(a.search_budget)],run,a.timeout_minutes,state,report)
        if first:
            run_step("02_classification",pipeline+[
                "--mode","classify","--llm-provider","jev",
                "--max-items",str(a.classify_items),
                "--max-api-calls",str(a.llm_budget),
                "--max-tokens",str(a.token_budget),
                "--output",cfg["files"]["output"],
                "--inventory-output",cfg["files"]["inventory_output"]],
                run,a.timeout_minutes,state,report)
    finally:
        changes=verify_sources(originals)
        state["source_files_modified"]=changes
        state["finished_at"]=datetime.now(timezone.utc).isoformat()
        state["all_stages_pass"]=len(state["stages"])==2 and all(x["status"]=="PASS" for x in state["stages"])
        state["production_approved"]=False
        state["mass_approval"]="BLOCKED_PENDING_LIVE_QUOTA_AND_RESUME_VERIFICATION"
        report_save(report,state)
    print(json.dumps({"run_dir":str(run),"report":str(report),
                      "stages":{x["name"]:x["status"] for x in state["stages"]},
                      "original_files_changed":state["source_files_modified"],
                      "mass_approval":state["mass_approval"]},indent=2,ensure_ascii=False))
    return 0 if state["all_stages_pass"] and not changes else 2

if __name__=="__main__":raise SystemExit(main())
