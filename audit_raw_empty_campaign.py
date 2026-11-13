#!/usr/bin/env python3
"""Audit raw_empty from real configured input+SQLite without writing to source files.

RAW_EMPTY is NOT proof a Search was attempted, or proof of a true no-result.
Existing operational failures are listed separately, never silently retried.
"""
from __future__ import annotations
import argparse,collections,json,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))

def audit(input_path,cache_path,master_path,final_path):
    from pipeline_produtos import read_input_rows,TavilyReadOnlyStore,load_baseline_inventory,safe
    from contextlib import closing
    protected=load_baseline_inventory(master_path,final_path)["hard_lock"]
    processed=load_baseline_inventory(master_path,final_path)["processed"]
    rows=read_input_rows(input_path)
    seen=set();summary=collections.Counter();eligible=[];errors=[];examples=[]
    with closing(TavilyReadOnlyStore(cache_path)) as store:
        for row in rows:
            if not row.valid:
                summary["invalid_input"]+=1;continue
            if row.key in seen:
                summary["duplicate_input"]+=1;continue
            seen.add(row.key)
            raw=store.get(row.brand_original,row.model_original)
            summary["state_"+raw.state]+=1
            if raw.state!="RAW_EMPTY":continue
            payload=raw.payload or {}
            coll=payload.get("_collection",{}) if isinstance(payload,dict) else {}
            coll=coll if isinstance(coll,dict) else {}
            meta=payload.get("_search_meta",{}) if isinstance(payload,dict) else {}
            meta=meta if isinstance(meta,dict) else {}
            prior=safe(coll.get("state")).upper()
            error=safe(coll.get("error")) or safe(meta.get("error"))
            provider=safe(coll.get("provider")) or safe(meta.get("provider")) or "UNKNOWN"
            summary["raw_empty_provider_"+provider]+=1
            if row.key in protected:
                summary["raw_empty_hard_locked"]+=1;continue
            if row.key in processed:
                summary["raw_empty_processed"]+=1;continue
            rec={"brand":row.brand_original,"model":row.model_original,"business_key":row.key,
                 "prior_state":prior,"provider":provider,"previous_error":error[:250]}
            if prior in {"REQUEST_ATTEMPTED_NO_AUTO_RETRY","REQUEST_ERROR"} or error:
                summary["raw_empty_previous_error"]+=1
                errors.append(rec)
            else:
                summary["raw_empty_candidate"]+=1
                eligible.append(rec)
            if len(examples)<10:examples.append(rec)
    return {"unique_input_keys":len(seen),"summary":dict(sorted(summary.items())),
            "recollectable_without_prior_error":len(eligible),
            "previous_search_errors_requiring_explicit_review":len(errors),
            "candidate_rows":eligible,"error_rows":errors,"examples":examples,
            "interpretation":"RAW_EMPTY does not prove a completed Search, and requerying can incur charges.",
            "search_calls":0,"auto_approval":False}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",default="pipeline_config.json")
    p.add_argument("--output",default="piloto_argentina/raw_empty_audit.json")
    a=p.parse_args()
    cfg=json.loads((ROOT/a.config).read_text(encoding="utf-8-sig"))
    f=cfg["files"]
    data=audit(*(ROOT/f[k] for k in ("input","cache","master","final")))
    out=(ROOT/a.output).resolve();out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in data.items() if k not in ("candidate_rows","error_rows","examples")},ensure_ascii=False,indent=2))

if __name__=="__main__":main()
