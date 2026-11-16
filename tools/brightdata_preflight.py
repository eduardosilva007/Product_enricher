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
    a=p.parse_args()
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
    return 0

if __name__=="__main__": raise SystemExit(main())
