#!/usr/bin/env python3
"""Bounded, resumable, isolated RAW_EMPTY campaign, defaulting to NO API calls.

First run audits all RAW_EMPTY in real input/cache and freezes up to N eligible
SKUs into isolated input; resume reuses this cohort and the same SQLite ledgers.
Cannot promote outputs or silently start processing the remaining 18K SKUs.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,sys,uuid
from contextlib import closing,contextmanager
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
    slots=[slot for slot in configured_slots() if slot.provider!="serper"]
    if not slots:raise ValueError("No Tavily/Bright Data account with explicit positive budget; Serper quota-error retry forbidden")
    jev=cfg.get("jev",{})
    key=jev.get("api_key_env") or "TYPESAFE_API_KEY"
    if not os.getenv(key):raise ValueError(f"JEV credential not configured: {key}")

def import_prior_reservations(destination: Path, history_root: Path = OUT) -> dict:
    """Seed the NEW isolated SQLite with earlier real reservation attempts.

    Every sampled key from a previously-started pilot must exist in its
    SQLite ledger, including errors/ambiguous requests. Missing or corrupt
    history blocks further collection instead of silently resetting quota.
    Never writes to old ledgers or the production SQLite.
    """
    reports = sorted(history_root.glob("*/run_report.json"))
    expected = set()
    actual = {}
    for report_path in reports:
        state = json.loads(report_path.read_text(encoding="utf-8"))
        if not state.get("stages", {}).get("collection", {}).get("attempts", 0):
            continue
        cohort = state.get("cohort_keys", [])
        if len(cohort) != len(set(cohort)) or not cohort:
            raise RuntimeError(f"Invalid earlier campaign cohort: {report_path}")
        if expected.intersection(cohort):
            raise RuntimeError(f"Overlapping earlier campaign cohorts: {report_path}")
        expected.update(cohort)
        config_path = report_path.parent / "isolated_config.json"
        previous_config = json.loads(config_path.read_text(encoding="utf-8"))
        ledger_path = Path(previous_config["files"]["classification_db"])
        if not ledger_path.is_file():
            raise RuntimeError(f"Missing previous reservation ledger: {ledger_path}")
        with closing(sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True)) as source:
            rows = source.execute(
                "SELECT business_key, account, provider FROM collection_attempt_reservations"
            ).fetchall()
        for key, account, provider in rows:
            if key in actual and actual[key] != (account, provider):
                raise RuntimeError(f"Conflicting historical reservation for SKU: {key}")
            actual[key] = (account, provider)
        if not set(cohort).issubset(actual):
            raise RuntimeError(f"Unaccounted previous collection attempts: {report_path}")
    # Existing production snapshot rows are also attempts; avoid silent
    # account rewrites and count all reservations across previous pilots.
    with closing(sqlite3.connect(destination)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS collection_attempt_reservations (
            business_key TEXT PRIMARY KEY, account TEXT NOT NULL,
            provider TEXT NOT NULL, reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for key, (account, provider) in actual.items():
            prior = conn.execute(
                "SELECT account, provider FROM collection_attempt_reservations WHERE business_key=?",
                (key,),
            ).fetchone()
            if prior and prior != (account, provider):
                raise RuntimeError(f"Conflicting production/historical reservation: {key}")
            if not prior:
                conn.execute(
                    "INSERT INTO collection_attempt_reservations(business_key, account, provider) VALUES (?, ?, ?)",
                    (key, account, provider),
                )
        conn.commit()
    return {"historical_cohort_keys": len(expected),
            "historical_reservation_keys": len(actual)}


def create_run(cfg,args):
    from pipeline_produtos import load_baseline_inventory
    sources=source_paths(cfg)
    for key in ("input","cache","master","final"):
        if not sources[key].is_file():raise FileNotFoundError(f"Missing input: {key}: {sources[key]}")
    result=audit(sources["input"],sources["cache"],sources["master"],sources["final"])
    # New rounds must not repeatedly spend on the same frozen SKU from a prior
    # pilot. Resume that run instead; do not silently re-authorize Search.
    prior_attempts=set()
    if OUT.is_dir():
        for prior_report in OUT.glob("*/run_report.json"):
            try:
                history=json.loads(prior_report.read_text(encoding="utf-8"))
                if history.get("stages",{}).get("collection",{}).get("attempts",0)>0:
                    prior_attempts.update(history.get("cohort_keys",[]))
            except (OSError,ValueError,TypeError):
                raise RuntimeError(f"Unreadable previous campaign report: {prior_report}")
    eligible=[x for x in result["candidate_rows"] if x["business_key"] not in prior_attempts]
    if args.allow_prior_serper_quota_error:
        eligible.extend(x for x in result["error_rows"]
                        if x["business_key"] not in prior_attempts
                        and x["provider"].lower()=="serper"
                        and x["prior_state"]=="REQUEST_ATTEMPTED_NO_AUTO_RETRY"
                        and "not enough credits" in x["previous_error"].lower())
    eligible=eligible[:args.search_budget]
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
    # Persistent accounting across isolated pilot DB copies. This must happen
    # BEFORE the new cohort is handed to the HTTP worker.
    prior_reservation_stats = import_prior_reservations(Path(files["classification_db"]))
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
           "cohort_keys":[r["business_key"] for r in eligible],
           "excluded_prior_pilot_cohorts":len(prior_attempts),
           "prior_reservation_stats":prior_reservation_stats,
           "prior_serper_quota_error_rescue_authorized":bool(args.allow_prior_serper_quota_error),
           "search_provider_exclusion":["serper"],
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
    # Block Serper in the worker AND suppress repopulation by load_env_file.
    # All Serper keys (primary and numbered variants) are overridden to empty.
    if label=="collection":
        for key in ("SERPER_API_KEY","SERPER_API_KEY1","SERPER_API_KEY_1",
                    "SERPER_API_KEY_2","SERPER_API_KEY_3","SERPER_API_KEY_4"):
            env[key]=""
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

def outcome_audit(run,state,isolated):
    """Independent post-run checks. No promotion/approval happens here."""
    import pandas as pd
    from pipeline_produtos import business_key,find_col,safe,TavilyReadOnlyStore,_is_manual_status,_is_bi_ok_value
    from contextlib import closing
    cohort=pd.read_excel(isolated["files"]["input"],dtype=object,keep_default_na=False)
    final=pd.read_excel(isolated["files"]["final"],dtype=object,keep_default_na=False)
    original=json.loads(Path(state["originals"]).read_text(encoding="utf-8"))
    # Compare exact Brand+Model human locks, not the last record sharing
    # a normalized business_key. Structural aliases can share that key.
    from forensic_protected_diff import examine
    protected_details, protected_counts, original_rows_count, final_rows_count = examine(run)
    cohort_b=find_col(cohort,["Marca","Fabricante","Brand","Manufacturer"])
    cohort_m=find_col(cohort,["Modelo","Model","SKU","Part Number"])
    final_b=find_col(final,["Marca","Fabricante","Brand","Manufacturer"])
    final_m=find_col(final,["Modelo","Model","SKU","Part Number"])
    cohort_keys={business_key(safe(row[cohort_b]),safe(row[cohort_m])) for _,row in cohort.iterrows()}
    final_keys={business_key(safe(row[final_b]),safe(row[final_m])) for _,row in final.iterrows()}
    missing=sorted(cohort_keys-final_keys)
    missing_existing=[]
    if final_rows_count < original_rows_count:
        missing_existing=["ROW_COUNT_DECREASED"]
    protected_changes=protected_details
    raw_states={}
    with closing(TavilyReadOnlyStore(Path(isolated["files"]["cache"]))) as store:
        for _, row in cohort.iterrows():
            ev=store.get(safe(row[cohort_b]),safe(row[cohort_m]))
            raw_states[ev.state]=raw_states.get(ev.state,0)+1
    labels=["BASE FINAL INPUT ROW COVERAGE OK","BASE FINAL PROCV COVERAGE OK",
            "BASE FINAL CLASSIFICATION INTEGRITY"]
    log=run/"classification_01.log"
    if state["stages"].get("classification",{}).get("log"):
        log=run/state["stages"]["classification"]["log"]
    logtext=log.read_text(encoding="utf-8",errors="replace") if log.is_file() else ""
    log_checks={label:label in logtext for label in labels}
    useful_raw=sum(raw_states.get(x,0) for x in ("RAW_OK","RAW_OK_MERGED"))
    passed=(not missing and not missing_existing and not protected_changes
            and not protected_originals(state) and useful_raw>0 and all(log_checks.values()))
    result={"status":"PASS" if passed else "FAIL","cohort":len(cohort),
            "missing_cohort_in_final":missing[:20],"missing_original_in_final":missing_existing[:20],
            "protected_manual_or_bi_changes":protected_changes[:20],
            "protected_changes_total":len(protected_changes),
            "protected_changes_by_type":protected_counts,
            "cache_states_after":raw_states,"useful_raw_count":useful_raw,
            "log_contracts":log_checks,
            "original_source_modified":protected_originals(state),
            "quality_decision":"REVIEW_REQUIRED","taxonomy_accuracy_measured":False}
    state["quality_audit"]=result
    atomic_json(run/"quality_audit.json",result)
    atomic_json(run/"run_report.json",state)
    return passed

@contextmanager
def exclusive_campaign_lock():
    """OS-managed interprocess lock, automatically released after crash."""
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/".campaign.lock").open("a+b") as handle:
        handle.seek(0);handle.write(b"\0");handle.flush();handle.seek(0)
        if os.name=="nt":
            import msvcrt
            try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:raise RuntimeError("Another live pilot is running; concurrent night runs blocked") from exc
            try:yield
            finally:
                handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:raise RuntimeError("Another live pilot is running") from exc
            try:yield
            finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--allow-prior-serper-quota-error",action="store_true",
                   help="Explicitly rescue only prior Serper HTTP 400 Not enough credits, using OTHER provider, capped and isolated")
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
        if state.get("prior_serper_quota_error_rescue_authorized")!=bool(args.allow_prior_serper_quota_error):
            raise ValueError("Resume must preserve the same prior-error authorization flag")
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
                # Recover only unexpected worker termination/timeout. Do not
                # restart on pipeline policy failure (2) or provider pause (3).
                last=state["stages"]["collection"]
                if last.get("exit_code") not in (2,3) and last["status"] in ("FAIL","TIMEOUT"):
                    print("[NIGHT] Worker interrupted. One automatic restart with SAME SQLite reservation ledger.",flush=True)
                    success=run_stage(run,state,"collection",base+[
                        "--mode","collect","--auto-providers","--recollect-empty",
                        "--max-search-new",str(state["budget"]["search"])],
                        args.timeout_minutes)
                if not success:
                    print("Collection stopped. Same cohort may resume; no new input keys allowed.")
                    return 2
        if stages.get("classification",{}).get("status")!="PASS":
            if stages.get("classification",{}).get("attempts",0)>0:
                raise RuntimeError(
                    "JEV_INCOMPLETE_REQUIRES_AUDIT: automatic classification resume disabled; "
                    "per-run HTTP/token limits do not guarantee cumulative limits across restarts."
                )
            success=run_stage(run,state,"classification",base+[
                "--mode","classify","--llm-provider","jev",
                "--max-items",str(state["cohort_count"]),
                "--max-api-calls",str(state["budget"]["jev_calls"]),
                "--max-tokens",str(state["budget"]["tokens"]),
                "--output",isolated["files"]["output"],
                "--inventory-output",isolated["files"]["inventory_output"]],
                args.timeout_minutes)
            if not success:return 2
        passed=outcome_audit(run,state,isolated)
        return 0 if passed else 2
    finally:
        state["source_files_modified"]=protected_originals(state)
        state["all_stages_pass"]=all(stages.get(k,{}).get("status")=="PASS" for k in (
            "collection","classification"))
        state["production_approved"]=False
        state["mass_approval"]="BLOCKED_PENDING_QUALITY_AUDIT_AND_REAL_RESUME"
        atomic_json(run/"run_report.json",state)
        print(f"Report: {run/'run_report.json'}",flush=True)

if __name__=="__main__":
    with exclusive_campaign_lock():
        raise SystemExit(main())
