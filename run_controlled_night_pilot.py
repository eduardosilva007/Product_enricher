#!/usr/bin/env python3
"""Bounded, resumable, isolated RAW_EMPTY campaign, defaulting to NO API calls.

First run audits all RAW_EMPTY in real input/cache and freezes up to N eligible
SKUs into isolated input; resume reuses this cohort and the same SQLite ledgers.
Cannot promote outputs or silently start processing the remaining 18K SKUs.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,sys,uuid
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from audit_raw_empty_campaign import audit

OUT=ROOT/"piloto_argentina"/"controlled_night"

def digest(p):
    h=hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def snapshot(src,dst):
    with closing(sqlite3.connect(src.resolve().as_uri()+"?mode=ro",uri=True)) as a:
        with closing(sqlite3.connect(dst)) as b:a.backup(b)

def atomic_json(path,data):
    tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
    tmp.replace(path)

def source_paths(cfg):
    return {k:(ROOT/str(cfg["files"][k])).resolve()
            for k in ("input","cache","master","final","classification_db")}

def gate(cfg,args):
    if not args.execute_live:return
    if not 1<=args.search_budget<=20:raise ValueError("Search budget must be 1..20 in controlled pilot")
    if not 1<=args.llm_budget<=100:raise ValueError("JEV cap must be 1..100")
    if not 1<=args.token_budget<=200000:raise ValueError("Token cap must be 1..200000")
    if not 1<=args.timeout_minutes<=240:raise ValueError("Timeout must be 1..240 minutes")
    if cfg.get("recovery",{}).get("hard_lock_review_bi_value")!="OK":
        raise ValueError("Missing BI hard lock")
    if not cfg.get("identity",{}).get("auto_approval_requires_strong_identity"):
        raise ValueError("Missing strong identity gate")
    c=cfg.get("collection",{}).get("hard_limits",{})
    if c.get("max_requests_per_product")!=1 or any(c.get(k) for k in (
        "allow_automatic_retry","allow_secondary_search","allow_provider_failover","allow_extract")):
        raise ValueError("Collection safety contract violated")
    if not cfg.get("semantic_funnel",{}).get("offline_mode"):
        raise ValueError("Local semantic offline mode required")
    from pipeline_produtos import load_env_file
    load_env_file(ROOT/".env")
    from nightly_provider_router import configured_slots
    if not configured_slots():raise ValueError("No explicit positive provider limit configured in .env")
    jev=cfg.get("jev",{})
    key=jev.get("api_key_env") or "TYPESAFE_API_KEY"
    if not os.getenv(key):raise ValueError(f"JEV credential not configured: {key}")

def create_run(cfg,args):
    from pipeline_produtos import load_baseline_inventory
    sources=source_paths(cfg)
    for key in ("input","cache","master","final"):
        if not sources[key].is_file():raise FileNotFoundError(f"Missing input: {key}: {sources[key]}")
    result=audit(sources["input"],sources["cache"],sources["master"],sources["final"])
    eligible=result["candidate_rows"][:args.search_budget]
    if not eligible:raise RuntimeError("No eligible RAW_EMPTY candidates. Review raw_empty_audit.json")
    run=OUT/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8])
    run.mkdir(parents=True,exist_ok=False)
    originals={k:{"path":str(p),"sha256":digest(p)} for k,p in sources.items() if p.is_file()}
    isolated=json.loads(json.dumps(cfg))
    files=isolated["files"]
    for k,p in sources.items():
        target=run/("source_"+k+p.suffix)
        if p.is_file():
            if p.suffix.lower() in (".sqlite",".db"):snapshot(p,target)
            else:shutil.copy2(p,target)
        files[k]=str(target)
    # A subset input is the strongest protection against accidental RAW_MISSING
    # collection. No 18K files are ever used as collection input in this campaign.
    import pandas as pd
    cohort=run/"raw_empty_cohort.xlsx"
    pd.DataFrame([{"Marca":r["brand"],"Modelo":r["model"]} for r in eligible]).to_excel(cohort,index=False)
    files["input"]=str(cohort)
    for k,v in {
        "output":"classification_output.xlsx",
        "inventory_output":"inventory_output.xlsx",
        "precollection_report":"precollection.xlsx",
        "collection_report":"collection.xlsx",
        "pending_with_cache":"pending_with_cache.xlsx",
        "pending_without_cache":"pending_without_cache.xlsx",
        "collection_report_cache_rebuild":"curation.xlsx",
        "baseline_backup_dir":"backups",
        "baseline_audit":"audit.csv",
        "evidence_export_jsonl":"evidence.jsonl",
    }.items():files[k]=str(run/v)
    conf=run/"isolated_config.json"
    atomic_json(conf,isolated)
    atomic_json(run/"original_sources.json",originals)
    atomic_json(run/"raw_empty_audit.json",{k:v for k,v in result.items() if k!="candidate_rows"})
    state={"mode":"ISOLATED_RAW_EMPTY_CONTROLLED",
           "created_at":datetime.now(timezone.utc).isoformat(),
           "run_dir":str(run),"config":str(conf),
           "originals":str(run/"original_sources.json"),
           "budget":{"search":args.search_budget,"jev_calls":args.llm_budget,"tokens":args.token_budget},
           "cohort_count":len(eligible),"cohort_skus":[r["model"] for r in eligible],
           "stages":{},"production_approved":False,"mass_approval":"BLOCKED_REVIEW_REQUIRED"}
    atomic_json(run/"run_report.json",state)
    return run,state,isolated

def protected_originals(state):
    originals=json.loads(Path(state["originals"]).read_text(encoding="utf-8"))
    return [k for k,v in originals.items()
            if not Path(v["path"]).is_file() or digest(Path(v["path"]))!=v["sha256"]]

def run_stage(run,state,label,cmd,timeout):
    item=state["stages"].setdefault(label,{"attempts":0})
    item["attempts"]+=1
    item["status"]="RUNNING"
    item["started_at"]=datetime.now(timezone.utc).isoformat()
    atomic_json(run/"run_report.json",state)
    name=f"{label}_{item['attempts']:02d}.log"
    item["log"]=name
    env=dict(os.environ)
    env.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",
               HF_DATASETS_OFFLINE="1",HF_HUB_DISABLE_TELEMETRY="1")
    try:
        with (run/name).open("w",encoding="utf-8") as log:
            with subprocess.Popen(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                                  stdout=log,stderr=subprocess.STDOUT) as proc:
                try:ret=proc.wait(timeout=timeout*60)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait()
                    item["status"]="TIMEOUT";ret=None
                item["status"]="PASS" if ret==0 else ("TIMEOUT" if ret is None else "FAIL")
                item["exit_code"]=ret
    except Exception as exc:
        item["status"]="FAIL";item["error"]=f"{type(exc).__name__}: {exc}"
    item["finished_at"]=datetime.now(timezone.utc).isoformat()
    atomic_json(run/"run_report.json",state)
    print(f"[NIGHT] {label}: {item['status']} (attempt {item['attempts']})",flush=True)
    return item["status"]=="PASS"

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute-live",action="store_true",
                   help="Explicit authorization to consume bounded real Search+JEV calls")
    p.add_argument("--search-budget",type=int,default=0)
    p.add_argument("--llm-budget",type=int,default=0)
    p.add_argument("--token-budget",type=int,default=0)
    p.add_argument("--timeout-minutes",type=int,default=120)
    p.add_argument("--resume-dir",default="",help="Resume SAME isolated cohort and SQLite")
    args=p.parse_args()
    cfg=json.loads((ROOT/"pipeline_config.json").read_text(encoding="utf-8-sig"))
    if not args.execute_live:
        paths=source_paths(cfg)
        result=audit(paths["input"],paths["cache"],paths["master"],paths["final"])
        dest=OUT/"raw_empty_audit_precheck.json"
        dest.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(dest,{k:v for k,v in result.items() if k!="candidate_rows"})
        print(json.dumps({"mode":"AUDIT_ONLY_NO_APIS","raw_empty_summary":result["summary"],
              "candidates":len(result["candidate_rows"]),"prior_errors":len(result["error_rows"]),
              "report":str(dest),"live_calls":0},indent=2,ensure_ascii=False))
        return 0
    gate(cfg,args)
    if args.resume_dir:
        run=Path(args.resume_dir).resolve()
        if run.parent!=OUT.resolve():raise ValueError("Resume must target a direct controlled_night run directory")
        state=json.loads((run/"run_report.json").read_text(encoding="utf-8"))
        if state["budget"]!={"search":args.search_budget,"jev_calls":args.llm_budget,"tokens":args.token_budget}:
            raise ValueError("Resume budgets must match original campaign exactly")
        isolated=json.loads((run/"isolated_config.json").read_text(encoding="utf-8"))
    else:run,state,isolated=create_run(cfg,args)
    altered=protected_originals(state)
    if altered:raise RuntimeError(f"Original source files changed since campaign started: {altered}")
    base=[sys.executable,str(ROOT/"pipeline_produtos.py"),"--config",str(run/"isolated_config.json"),
          "--input",isolated["files"]["input"],"--cache",isolated["files"]["cache"],
          "--master",isolated["files"]["master"],"--final",isolated["files"]["final"],
          "--db",isolated["files"]["classification_db"],"--yes"]
    stages=state["stages"]
    try:
        if stages.get("collection",{}).get("status")!="PASS":
            success=run_stage(run,state,"collection",base+[
                "--mode","collect","--auto-providers","--recollect-empty",
                "--max-search-new",str(state["budget"]["search"])],
                args.timeout_minutes)
            if not success:
                print("Collection stopped. Same cohort may resume; no new input keys allowed.")
                return 2
        if stages.get("classification",{}).get("status")!="PASS":
            success=run_stage(run,state,"classification",base+[
                "--mode","classify","--llm-provider","jev",
                "--max-items",str(state["cohort_count"]),
                "--max-api-calls",str(state["budget"]["jev_calls"]),
                "--max-tokens",str(state["budget"]["tokens"]),
                "--output",isolated["files"]["output"],
                "--inventory-output",isolated["files"]["inventory_output"]],
                args.timeout_minutes)
            if not success:return 2
        return 0
    finally:
        state["source_files_modified"]=protected_originals(state)
        state["all_stages_pass"]=all(stages.get(k,{}).get("status")=="PASS" for k in (
            "collection","classification"))
        state["production_approved"]=False
        state["mass_approval"]="BLOCKED_PENDING_QUALITY_AUDIT_AND_REAL_RESUME"
        atomic_json(run/"run_report.json",state)
        print(f"Report: {run/'run_report.json'}",flush=True)

if __name__=="__main__":raise SystemExit(main())
