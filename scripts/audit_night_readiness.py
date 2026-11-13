#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed, API-free night-readiness assessment for 1K/18K input cohorts.

Reads the authoritative RAW and durable attempt ledger separately. Does NOT
authorize any search, reset reservations, promote results or edit official data.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p

COLUMNS = [
    "Marca","Modelo","Business Key","RAW State","Prior Collection State",
    "Prior Error Recorded","Historical Reservation","Classification Protected",
    "Human Locked","Final/Consolidado Classification","Recomendação","Motivo",
]

def classify_readiness(raw_state, prior_state, prior_error, reserved, protected, hard_locked):
    """No attempt is ever automatically retried based on this report."""
    if hard_locked:
        return "PRESERVAR_HUMANO"
    if protected:
        return "PRESERVAR_CLASSIFICADO"
    if raw_state in {"RAW_OK","RAW_OK_MERGED"}:
        return "REAPROVEITAR_CACHE"
    if raw_state == "RAW_EMPTY":
        if reserved:
            return "RAW_EMPTY_JA_RESERVADO"
        if prior_state in {"REQUEST_ATTEMPTED_NO_AUTO_RETRY","REQUEST_ERROR"} or prior_error:
            return "RAW_EMPTY_COM_MARCADOR_TENTATIVA"
        return "RAW_EMPTY_SEM_COMPROVACAO_TENTATIVA"
    if raw_state == "RAW_MISSING":
        return "RAW_MISSING_JA_RESERVADO" if reserved else "NOVA_COLETA_POTENCIAL"
    return "BLOQUEIO_CACHE_INTEGRO"

def reservations_from_db(db_path: Path):
    if not db_path.is_file():
        return set(), "MISSING_LEDGER"
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        check=connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower()!="ok":
            raise RuntimeError("CLASSIFICATION_DB_INTEGRITY_FAILED")
        table=connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_attempt_reservations'"
        ).fetchone()
        if not table:
            return set(), "NO_RESERVATION_TABLE"
        return {str(x[0]) for x in connection.execute(
            "SELECT business_key FROM collection_attempt_reservations")}, "OK"

def read_baseline_rows(path):
    df,_=p._read_business_sheet(path)
    b=p.find_col(df,["Marca","Fabricante","Brand","Manufacturer"])
    m=p.find_col(df,["Modelo","Model","SKU","Part Number"])
    out={}
    for _,r in df.iterrows():
        brand,model=p.safe(r.get(b)),p.safe(r.get(m))
        if not brand or not model:
            continue
        key=p.business_key(brand,model)
        typ=p.safe(r.get("O que é (Tipo)")).upper()
        cg=p.safe(r.get("Category General"))
        fam=p.safe(r.get("FAMILY"))
        out[key]=bool(typ not in {"","REVISAR","UNKNOWN","N/A"} and cg and fam)
    return out

def hash_file(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def assess(paths, input_override=None, output_dir=None):
    input_path=Path(input_override).resolve() if input_override else paths["input"]
    critical=[input_path,paths["cache"],paths["master"],paths["final"],paths["classification_db"]]
    missing=[str(x) for x in critical if not x.is_file()]
    if missing:raise FileNotFoundError("REQUIRED_FILE_MISSING: "+" | ".join(missing))
    protected_data=p.load_baseline_inventory(paths["master"],paths["final"])
    protected=protected_data["processed"]
    hard_locked=protected_data["hard_lock"]
    baseline=read_baseline_rows(paths["final"])
    reserved,ledger_status=reservations_from_db(paths["classification_db"])
    if ledger_status!="OK":
        # With unknown reservations no search can be authorized.
        raise RuntimeError("RESERVATION_LEDGER_UNVERIFIED: "+ledger_status)
    rows=p.read_input_rows(input_path)
    seen=set(); counters=Counter();records=[]
    with closing(p.TavilyReadOnlyStore(paths["cache"])) as store:
        for item in rows:
            if not item.valid:
                counters["invalid_input"]+=1
                continue
            if item.key in seen:
                counters["duplicate_input"]+=1
                continue
            seen.add(item.key)
            raw=store.get(item.brand_original,item.model_original)
            payload=raw.payload or {}
            coll=payload.get("_collection") if isinstance(payload.get("_collection"),dict) else {}
            meta=payload.get("_search_meta") if isinstance(payload.get("_search_meta"),dict) else {}
            prior=p.safe(coll.get("state")).upper()
            error=bool(p.safe(coll.get("error")) or p.safe(meta.get("error")))
            reservation=item.key in reserved
            human=item.key in hard_locked
            accepted=item.key in protected
            route=classify_readiness(raw.state,prior,error,reservation,accepted,human)
            counters[route]+=1
            counters["raw_"+raw.state]+=1
            if accepted and raw.state=="RAW_EMPTY":
                counters["classified_with_raw_empty"]+=1
            records.append({
                "Marca":item.brand_original,"Modelo":item.model_original,
                "Business Key":item.key,"RAW State":raw.state,
                "Prior Collection State":prior,
                "Prior Error Recorded":"SIM" if error else "NÃO",
                "Historical Reservation":"SIM" if reservation else "NÃO",
                "Classification Protected":"SIM" if accepted else "NÃO",
                "Human Locked":"SIM" if human else "NÃO",
                "Final/Consolidado Classification":"SIM" if baseline.get(item.key,False) else "NÃO",
                "Recomendação":route,
                "Motivo":raw.reason[:240],
            })
    # Not claiming that a marker proves an actual provider HTTP call.
    night_new=counters["NOVA_COLETA_POTENCIAL"]
    if counters["RAW_EMPTY_SEM_COMPROVACAO_TENTATIVA"] or counters["RAW_EMPTY_COM_MARCADOR_TENTATIVA"]:
        release="BLOCKED_REVIEW_RAW_EMPTY_PROVENANCE"
    elif counters["RAW_MISSING_JA_RESERVADO"]:
        release="BLOCKED_RESERVED_MISSING"
    else:
        release="INPUT_READY_FOR_CONTROLLED_PILOT_ONLY"
    summary={
        "scope_input":str(input_path),
        "input_valid_unique":len(seen),
        "counts":dict(sorted(counters.items())),
        "total_ledger_reservations":len(reserved),
        "potentially_new_raw_missing_unreserved":night_new,
        "release_status":release,
        "search_calls":0,"llm_calls":0,"official_files_modified":False,
        "source_hash_sha256":{
            "input":hash_file(input_path),
            "cache":hash_file(paths["cache"]),
            "master":hash_file(paths["master"]),
            "final":hash_file(paths["final"]),
            "classification_db":hash_file(paths["classification_db"]),
        },
        "notes":[
            "RAW_EMPTY is not proof of a successful search or of no prior HTTP request.",
            "Prior marker does not prove HTTP; reservation proves an allocated attempt, not provider success.",
            "No automatic retry of any RAW_EMPTY occurs from this audit.",
            "A 18K input must be explicitly selected for 18K assessment; the current 1K input cannot certify 18K.",
            "Pilot promotion and human approval remain separate.",
        ]
    }
    output_dir=Path(output_dir or ROOT/"piloto_argentina"/"night_readiness").resolve()
    output_dir.mkdir(parents=True,exist_ok=True)
    csvfile=output_dir/"night_readiness_rows.csv"
    jsonfile=output_dir/"night_readiness_summary.json"
    with csvfile.open("w",encoding="utf-8-sig",newline="") as fh:
        writer=csv.DictWriter(fh,fieldnames=COLUMNS,delimiter=";")
        writer.writeheader();writer.writerows(records)
    summary["report_csv"]=str(csvfile)
    jsonfile.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return summary

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",default=None,help="Input XLSX cohort; omit for configured default")
    parser.add_argument("--output-dir",default=None)
    args=parser.parse_args()
    cfg=json.loads((ROOT/"pipeline_config.json").read_text(encoding="utf-8-sig"))
    result=assess(p.source_paths(cfg),input_override=args.input,output_dir=args.output_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    sys.exit(main())
