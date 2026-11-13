#!/usr/bin/env python3
"""Audit *actual observed* failure states in existing classified workbook.

No inference from missing title, no network and no file mutation. This report
does not prove a historical pre-manual failure: source provenance matters.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from collections import Counter
import pandas as pd
HERE=Path(__file__).resolve().parent
if str(HERE) not in sys.path:sys.path.insert(0,str(HERE))
import benchmark_taxonomy as bt

FAILURE_STATES={"UNRESOLVED","AMBIGUOUS","PARTIAL","PENDENTE","PENDENTE_CLASSIFICACAO","NEEDS_RECOLLECTION","BLOCKED_NO_EVIDENCE","NO_TARGET_EVIDENCE","LOW_TARGET_EVIDENCE","PARTIAL_TARGET_EVIDENCE"}
STATE_COLUMNS=("Resolution State","Classification Outcome","Status Classificação","Status Processamento","Collection State")
GOLD_COLUMNS=("Category General","FAMILY")

def observed_failure_rows(df:pd.DataFrame):
    available=[x for x in STATE_COLUMNS if x in df.columns]
    results=[]
    for ix,row in df.iterrows():
        reasons=[f"{col}={str(row[col]).strip()}" for col in available
                 if str(row[col]).strip().upper() in FAILURE_STATES]
        if reasons:
            results.append({"Row":int(ix)+2,"Marca":bt.safe(row.get("Marca")),
                            "Modelo":bt.safe(row.get("Modelo")),
                            "Observed Failure Fields":"; ".join(reasons)})
    return available,results

def run(source:Path,output:Path):
    df=pd.read_excel(source,dtype=object,keep_default_na=False)
    available,rows=observed_failure_rows(df)
    output.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(rows,columns=["Row","Marca","Modelo","Observed Failure Fields"]).to_csv(
        output/"observed_failure_candidates.csv",index=False,encoding="utf-8-sig")
    summary={
        "source":str(source),"total_rows":int(len(df)),
        "observed_state_columns":available,
        "observed_failure_rows":len(rows),
        "provenance_status":"UNVERIFIED_PRE_MANUAL_STATE",
        "warning":"Status in final workbook may reflect post-manual state; NOT proof of failed deterministic pipeline at the time of classification.",
        "next_gate":"Verify original unmodified run logs or baseline snapshots before measuring genuine recovery.",
        "mutated_original":False
    }
    (output/"observed_failure_summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    return summary

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",default="Base Final Produtos Enriquecida.xlsx")
    p.add_argument("--output",default="piloto_argentina/mass_readiness/observed_failures")
    args=p.parse_args()
    run(Path(args.input),Path(args.output))
if __name__=="__main__":main()
