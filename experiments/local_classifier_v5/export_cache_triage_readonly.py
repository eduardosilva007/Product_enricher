#!/usr/bin/env python3
"""Exporta títulos e auditoria de fontes do cache para revisão; somente leitura.

NÃO declara identidade confirmada por similaridade textual; NÃO classifica nem busca.
Gera uma linha por resultado do cache para os pendentes na Base Final.
"""
import argparse
import collections
import csv
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_database_readonly import load_excel,connect_ro,key,val

REJECTED={"REJECT_WRONG_MODEL","REJECT_COMPATIBILITY_ONLY","REJECT_RELATED_ONLY"}

def export(final,cache,output,limit):
    pendentes={}
    for row in load_excel(final):
        if val(row["Revisão"]).upper() not in {"PENDENTE","PENDENTE_COLETA","REVISAR"}:
            continue
        if val(row["Revisão B.I"]).upper()=="OK" or val(row["Status"]).upper()=="MANUAL":
            continue
        pendentes.setdefault(key(row["brand"],row["model"]),row)
    db=connect_ro(cache)
    if db is None:raise FileNotFoundError(cache)
    exported=[]
    seen=collections.Counter()
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tavily_cache" not in tables:
            raise RuntimeError("Tabela tavily_cache não encontrada.")
        for rec in db.execute("SELECT brand,model,response_json FROM tavily_cache"):
            k=key(val(rec["brand"]),val(rec["model"]))
            target=pendentes.get(k)
            if target is None:continue
            try:payload=json.loads(rec["response_json"])
            except (ValueError,TypeError):continue
            if not isinstance(payload,dict):continue
            results=payload.get("results")
            if not isinstance(results,list):continue
            coll=payload.get("_collection") or {}
            if not isinstance(coll,dict):coll={}
            audits={}
            for a in coll.get("source_audit",[]) or []:
                if isinstance(a,dict):
                    u=val(a.get("url"))
                    if u:audits[u]=a
            for item in results:
                if not isinstance(item,dict):continue
                if seen[k]>=limit:break
                url=val(item.get("url"))
                title=val(item.get("title"))
                if not title and not url:continue
                audit=audits.get(url,{})
                decision=val(audit.get("decision"))
                seen[k]+=1
                exported.append({
                    "Marca":target["brand"],"Modelo":target["model"],
                    "Revisão Atual":target["Revisão"],
                    "Título Real Atual":target["Título Real"],
                    "Título Candidato":title,"URL Candidata":url,
                    "Resumo Fonte":val(item.get("content"))[:350],
                    "Decisão Curadoria":decision or "NAO_AUDITADA",
                    "Rejeitada Pela Curadoria":"SIM" if decision in REJECTED else "NAO_OU_NAO_AUDITADA",
                    "Collection State":val(coll.get("state")),
                    "Ação":"REVER_IDENTIDADE_ANTES_DE_CLASSIFICAR",
                })
    finally:db.close()
    output.parent.mkdir(parents=True,exist_ok=True)
    fields=["Marca","Modelo","Revisão Atual","Título Real Atual","Título Candidato",
            "URL Candidata","Resumo Fonte","Decisão Curadoria",
            "Rejeitada Pela Curadoria","Collection State","Ação"]
    with output.open("w",newline="",encoding="utf-8-sig") as fp:
        w=csv.DictWriter(fp,fieldnames=fields)
        w.writeheader()
        w.writerows(exported)
    summary={"read_only":True,"api_requests":0,"pending_unique_keys":len(pendentes),
             "keys_with_exported_candidates":len(seen),
             "candidate_rows":len(exported),
             "rejected_by_explicit_curator":sum(r["Rejeitada Pela Curadoria"]=="SIM" for r in exported),
             "note":"Título/URL é candidato para triagem; NÃO equivale a identidade confirmada.",
             "file":str(output)}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--final",type=Path,default=ROOT/"Base Final Produtos Enriquecida.xlsx")
    p.add_argument("--cache",type=Path,default=ROOT/"cache_tavily.sqlite")
    p.add_argument("--output",type=Path,default=ROOT/"Auditoria_Status_v1"/"triagem_evidencias_cache.csv")
    p.add_argument("--max-results-per-product",type=int,default=5)
    args=p.parse_args()
    if not 1<=args.max_results_per_product<=20:
        p.error("--max-results-per-product deve estar entre 1 e 20")
    raise SystemExit(export(args.final,args.cache,args.output,args.max_results_per_product))
