#!/usr/bin/env python3
"""Dry-run account validation or exactly one Bright Data HTTP request, no DB/cache."""
import argparse
import os
from pathlib import Path
import sys

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--env",default=".env")
    p.add_argument("--slot",type=int,choices=(1,2,3,4),default=1)
    p.add_argument("--live",action="store_true",help="Authorize one potentially billed POST")
    p.add_argument("--reactivate-schema",action="store_true",help="After a successful live preflight, clear only this account\u0027s BRIGHTDATA_SCHEMA lock, with an audit record")
    p.add_argument("--db",default="pipeline_v3.sqlite",help="Existing operational SQLite database")
    a=p.parse_args()
    if a.reactivate_schema and not a.live:
        p.error("--reactivate-schema requires --live")
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from pipeline_produtos import load_env_file,configured_slots,_normalize_brightdata_results
    load_env_file(Path(a.env))
    slots=[x for x in configured_slots() if x.provider=="brightdata"]
    slot=next((x for x in slots if x.account==f"brightdata:{a.slot}"),None)
    if slot is None: raise SystemExit(f"BRIGHTDATA_SLOT_{a.slot}_NOT_CONFIGURED")
    zone=os.environ[slot.zone_env].strip()
    key=os.environ[slot.key_env].strip()
    print(f"SLOT={slot.account} KEY_ENV={slot.key_env} ZONE_ENV={slot.zone_env} ZONE={zone} LIMIT={slot.limit}")
    if not a.live:
        print("DRY_RUN_OK HTTP_REQUESTS=0 DB_WRITES=0")
        return 0
    import requests
    response=requests.post(
        "https://api.brightdata.com/request",
        headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
        json={"zone":zone,"url":"https://www.google.com/search?q=pizza","format":"json","data_format":"parsed_light"},
        timeout=60,allow_redirects=False)
    print(f"HTTP_STATUS={response.status_code} HTTP_REQUESTS=1 DB_WRITES=0")
    if not 200<=response.status_code<300:
        print("BRIGHTDATA_REQUEST_FAILED; inspect provider dashboard")
        return 2
    if response.headers.get("x-brd-error") or response.headers.get("x-brd-error-code"):
        print("BRIGHTDATA_PROVIDER_ERROR_HEADER")
        return 3
    try: results=_normalize_brightdata_results(response.json())
    except (ValueError,TypeError,RuntimeError):
        print("BRIGHTDATA_RESPONSE_SCHEMA_INVALID")
        return 4
    print(f"BRIGHTDATA_LIVE_OK ORGANIC_RESULTS={len(results)}")
    if a.reactivate_schema:
        if not results:
            print("REACTIVATION_BLOCKED: live preflight returned no organic results")
            return 5
        import sqlite3
        db=Path(a.db)
        if not db.is_file():
            print("REACTIVATION_BLOCKED: operational database does not exist")
            return 6
        with sqlite3.connect(db,timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            table=conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_disabled_accounts'"
            ).fetchone()
            if not table:
                print("REACTIVATION_BLOCKED: router disabled-account table missing")
                return 7
            reason=conn.execute(
                "SELECT reason FROM collection_disabled_accounts WHERE account=?",
                (slot.account,),
            ).fetchone()
            if reason is None:
                print("REACTIVATION_NOT_NEEDED: account is not disabled")
            elif reason[0]!="BRIGHTDATA_SCHEMA":
                print(f"REACTIVATION_BLOCKED: current lock={reason[0]}")
                return 8
            else:
                conn.execute("""CREATE TABLE IF NOT EXISTS collection_router_reactivations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                    previous_reason TEXT NOT NULL, verification TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
                conn.execute(
                    "INSERT INTO collection_router_reactivations(account,previous_reason,verification) VALUES(?,?,?)",
                    (slot.account,"BRIGHTDATA_SCHEMA","LIVE_PREFLIGHT_HTTP_200_WITH_ORGANIC"),
                )
                conn.execute(
                    "DELETE FROM collection_disabled_accounts WHERE account=? AND reason=?",
                    (slot.account,"BRIGHTDATA_SCHEMA"),
                )
                print(f"SCHEMA_LOCK_REACTIVATED account={slot.account}; old SKU reservations preserved")
    return 0

if __name__=="__main__": raise SystemExit(main())
