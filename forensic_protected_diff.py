#!/usr/bin/env python3
"""Read-only forensic diff for an isolated controlled_night run.

Produces full before/after human-lock differences, not a truncated top-20
list. Never changes Excel/SQLite and never contacts an API.
"""
from __future__ import annotations
import argparse,csv,json,sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))

def examine(run:Path):
    from pipeline_produtos import business_key,find_col,safe,_is_manual_status,_is_bi_ok_value
    import pandas as pd
    manifest=json.loads((run/"original_sources.json").read_text(encoding="utf-8"))
    isolated=json.loads((run/"isolated_config.json").read_text(encoding="utf-8"))
    before=Path(manifest["final"]["path"])
    after=Path(isolated["files"]["final"])
    if not before.is_file() or not after.is_file():raise FileNotFoundError("Missing baseline or isolated final")
    left=pd.read_excel(before,dtype=object,keep_default_na=False)
    right=pd.read_excel(after,dtype=object,keep_default_na=False)
    def columns(df):
        return (find_col(df,["Marca","Fabricante","Brand","Manufacturer"]),
                find_col(df,["Modelo","Model","SKU","Part Number"]))
    def collect(df):
        b,m=columns(df);by={}
        for i,row in df.iterrows():
            brand,model=safe(row.get(b)),safe(row.get(m))
            if brand and model:
                by.setdefault(business_key(brand,model),[]).append((i+2,row))
        return by
    old,new=collect(left),collect(right)
    fields=("O que é (Tipo)","Alimentação","Category General","FAMILY",
            "Título Real","Status","Revisão","Revisão B.I")
    aliases={"O que é (Tipo)":["O que é (Tipo)","Tipo","Product Type"],
             "Alimentação":["Alimentação"],
             "Category General":["Category General"],
             "FAMILY":["FAMILY","Family"],
             "Título Real":["Título Real"],
             "Status":["Status"],
             "Revisão":["Revisão","Revisao"],
             "Revisão B.I":["Revisão B.I","Revisao B.I"]}
    map_left={name:find_col(left,aliases[name],required=False) for name in fields}
    map_right={name:find_col(right,aliases[name],required=False) for name in fields}
    details=[]
    counts=Counter()
    for key,oldrows in old.items():
        for old_num,oldrow in oldrows:
            manual=_is_manual_status(oldrow.get(map_left["Status"])) if map_left["Status"] else False
            bi=_is_bi_ok_value(oldrow.get(map_left["Revisão B.I"])) if map_left["Revisão B.I"] else False
            if not (manual or bi):continue
            newrows=new.get(key,[])
            b,m=columns(left)
            orig_pair=(safe(oldrow.get(b)),safe(oldrow.get(m)))
            pairs=[(n,r) for n,r in newrows if
                   (safe(r.get(columns(right)[0])),safe(r.get(columns(right)[1])))==orig_pair]
            if len(pairs)!=1:
                counts["missing_or_duplicated_exact_pair"]+=1
                details.append({"key":key,"old_row":old_num,"new_row":"",
                                "protection":"BI_OK" if bi else "MANUAL",
                                "field":"EXACT_PAIR","before":str(orig_pair),
                                "after":str(len(pairs)),"severity":"CRITICAL"})
                continue
            new_num,newrow=pairs[0]
            for name in fields:
                lv=safe(oldrow.get(map_left[name])) if map_left[name] else ""
                rv=safe(newrow.get(map_right[name])) if map_right[name] else ""
                if lv==rv:continue
                severity="CRITICAL" if name in {"O que é (Tipo)","Alimentação",
                  "Category General","FAMILY","Título Real","Status","Revisão B.I"} else "REVIEW"
                counts[f"changed_{name}"]+=1
                counts["bi_differences" if bi else "manual_differences"]+=1
                details.append({"key":key,"old_row":old_num,"new_row":new_num,
                                "protection":"BI_OK" if bi else "MANUAL",
                                "field":name,"before":lv,"after":rv,"severity":severity})
    return details,dict(sorted(counts.items())),len(left),len(right)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--run-dir",required=True)
    args=ap.parse_args()
    run=Path(args.run_dir).resolve()
    details,counts,old_n,new_n=examine(run)
    report={"status":"FAIL" if details else "PASS","original_rows":old_n,
            "isolated_rows":new_n,"changed_protected_cells":len(details),
            "by_type":counts,"sample":details[:12],
            "search_calls":0,"source_modified":False}
    with (run/"protected_diff_full.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["key","old_row","new_row","protection",
                                             "field","before","after","severity"])
        writer.writeheader()
        writer.writerows(details)
    (run/"protected_diff_summary.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
