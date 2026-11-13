#!/usr/bin/env python3
"""Safely separate legacy Search HTTP failures from RAW evidence (dry-run default).

Migration order:
 1. create backup snapshots of both DBs
 2. record failed attempt in durable ledger (commit)
 3. delete ONLY recognized error-only tombstones from tavily_cache (commit)
Never changes valid RAW or retries HTTP. Run --apply only after explicit review.
"""
from __future__ import annotations
import argparse,json,sqlite3,sys
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))

def is_error_only(body):
    try: payload=json.loads(body)
    except (TypeError,ValueError):return False
    if not isinstance(payload,dict):return False
    if payload.get("results"):return False
    coll=payload.get("_collection",{})
    if not isinstance(coll,dict):coll={}
    meta=payload.get("_search_meta",{})
    if not isinstance(meta,dict):meta={}
    status=str(coll.get("state","")).upper().strip()
    reason=str(coll.get("error") or meta.get("error") or "")
    return bool(reason) and status in {"REQUEST_ATTEMPTED_NO_AUTO_RETRY","REQUEST_ERROR",
                                      "SERPER_LIMIT","TAVILY_LIMIT","BRIGHTDATA_LIMIT"}

def snapshot(db,target):
    with closing(sqlite3.connect(db.resolve().as_uri()+"?mode=ro",uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:src.backup(dst)

def migrate(cache,db,apply,backups):
    with closing(sqlite3.connect(cache.resolve().as_uri()+"?mode=ro",uri=True)) as src:
        rows=src.execute("SELECT cache_key,brand,model,response_json FROM tavily_cache").fetchall()
    flagged=[r for r in rows if is_error_only(r[3])]
    from pipeline_produtos import business_key
    keys=[business_key(brand,model) for _,brand,model,_ in flagged]
    if len(keys)!=len(set(keys)):
        raise RuntimeError("Duplicate canonical business keys in legacy error records; manual review required")
    result={"scanned_cache":len(rows),"error_only":len(flagged),"untouched_raw":len(rows)-len(flagged),
            "new_attempt_ledger_rows":0,"removed_legacy_error_cache_rows":0,
            "apply":apply,"search_calls":0,"backup_dir":None}
    if not apply:return result
    backups.mkdir(parents=True,exist_ok=False)
    snapshot(cache,backups/"cache_before.sqlite")
    if db.exists():snapshot(db,backups/"attempts_before.sqlite")
    result["backup_dir"]=str(backups)
    with closing(sqlite3.connect(db,timeout=30)) as attempts:
        attempts.execute("PRAGMA busy_timeout=30000")
        attempts.execute("""CREATE TABLE IF NOT EXISTS collection_attempt_reservations (
            business_key TEXT PRIMARY KEY,account TEXT NOT NULL,provider TEXT NOT NULL,
            reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for (_,_,_,body),key in zip(flagged,keys):
            payload=json.loads(body)
            coll=payload.get("_collection",{}) or {}
            meta=payload.get("_search_meta",{}) or {}
            provider=str(coll.get("provider") or meta.get("provider") or "legacy").lower()
            result["new_attempt_ledger_rows"]+=attempts.execute(
                "INSERT OR IGNORE INTO collection_attempt_reservations (business_key,account,provider) VALUES(?,?,?)",
                (key,"legacy_error:"+provider,provider)).rowcount
        attempts.commit()
        reserved={k for (k,) in attempts.execute(
            "SELECT business_key FROM collection_attempt_reservations WHERE business_key IN ("+
            ",".join("?" for _ in keys)+")",keys)} if keys else set()
        if len(reserved)!=len(keys):
            raise RuntimeError("Ledger verification failed; RAW cache must remain unchanged")
    with closing(sqlite3.connect(cache,timeout=30)) as src:
        src.execute("PRAGMA busy_timeout=30000")
        for cache_key,brand,model,body in flagged:
            result["removed_legacy_error_cache_rows"]+=src.execute(
                "DELETE FROM tavily_cache WHERE cache_key=? AND response_json=?",
                (cache_key,body)).rowcount
        src.commit()
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",default="pipeline_config.json")
    p.add_argument("--apply",action="store_true")
    p.add_argument("--output",default="piloto_argentina/legacy_error_migration_report.json")
    a=p.parse_args()
    cfg=json.loads((ROOT/a.config).read_text(encoding="utf-8-sig"))
    files=cfg["files"]
    cache=(ROOT/files["cache"]).resolve()
    db=(ROOT/files["classification_db"]).resolve()
    if not cache.is_file():p.error(f"Cache missing: {cache}")
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup=(ROOT/"piloto_argentina"/"legacy_error_backups"/stamp)
    result=migrate(cache,db,a.apply,backup)
    out=(ROOT/a.output).resolve()
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=="__main__":main()
