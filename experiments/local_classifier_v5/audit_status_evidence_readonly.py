#!/usr/bin/env python3
"""Diagnóstico de pendências por evidência, sem alterar Excel/SQLite nem acessar APIs.

Usa a auditoria legada para ler as bases e reconciliar tentativas de coleta.
Não altera 'Revisão', 'Revisão B.I', Gold ou decisões do pipeline.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_database_readonly import load_excel, connect_ro, key, val

def evaluate(row, cache):
    """Status ortogonais; nunca inferir confirmação de marca/modelo apenas da planilha."""
    title=val(row.get("Título Real"))
    typ=val(row.get("O que é (Tipo)")).upper()
    category=val(row.get("Category General"))
    family=val(row.get("FAMILY"))
    power=val(row.get("Alimentação")).upper()
    raw=cache.get("raw_state","RAW_MISSING")
    # Coleta depende EXCLUSIVAMENTE do cache/tentativas efetivamente encontradas.
    collection={"RAW_MISSING":"NAO_COMPROVADA","RAW_EMPTY":"COLETA_VAZIA",
                "RAW_CORRUPT":"CACHE_CORROMPIDO","RAW_OK":"COLETADO"}.get(raw,"INDETERMINADA")
    if raw=="RAW_MISSING" and title:
        collection="EVIDENCIA_EXCEL_SEM_CACHE"
    if title:
        identity="IDENTIDADE_NAO_VERIFICADA"
    else:
        identity="SEM_TITULO"
    # Evidência de marca/modelo deve ser derivada de source identity auditado
    # (não apenas porque Marca e Modelo aparecem no Excel).
    evidence_identity=val(row.get("Título Referência Identity")).upper()
    if evidence_identity=="STRONG":
        identity="MARCA_MODELO_CONFIRMADOS"
    elif evidence_identity=="PROBABLE":
        identity="IDENTIDADE_PROVAVEL"
    elif evidence_identity in {"CONFLICT","WEAK"}:
        identity="IDENTIDADE_NAO_CONFIAVEL"
    has_type=bool(typ and typ not in {"REVISAR","UNKNOWN","N/A","NA"})
    has_pair=bool(category and family)
    has_partial=bool(has_type or category or family or (power and power!="UNKNOWN"))
    if has_type and has_pair:
        classification="CLASSIFICACAO_COMPLETA_NAO_VALIDADA"
    elif has_partial:
        classification="CLASSIFICACAO_PARCIAL"
    elif title:
        classification="PENDENTE_CLASSIFICACAO"
    else:
        classification="SEM_CLASSIFICACAO"
    if val(row.get("Revisão B.I")).upper()=="OK" or val(row.get("Status")).upper()=="MANUAL":
        action="PROTEGIDO_GOLD_NAO_ALTERAR"
    elif classification=="CLASSIFICACAO_COMPLETA_NAO_VALIDADA":
        action="VALIDAR_CLASSIFICACAO"
    elif classification=="CLASSIFICACAO_PARCIAL":
        action="RECUPERAR_CLASSIFICACAO_COM_EVIDENCIA" if title else "VALIDAR_DADOS_PARCIAIS"
    elif title:
        action="AVALIAR_IDENTIDADE_E_CLASSIFICAR"
    elif raw=="RAW_OK":
        action="RECUPERAR_EVIDENCIA_DO_CACHE"
    elif raw=="RAW_EMPTY":
        action="RECOLETA_DIRIGIDA_APOS_ANALISE"
    elif raw=="RAW_CORRUPT":
        action="REPARAR_CACHE"
    else:
        action="VERIFICAR_COLETA_ANTES_DE_BUSCAR"
    return {"Status Coleta":collection,"Status Identidade":identity,
            "Status Classificação":classification,"Próxima Ação Recomendada":action}

def load_cache(path):
    cache={}
    db=connect_ro(path)
    if db is None: return cache,False
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tavily_cache" not in tables:return cache,False
        for rec in db.execute("SELECT brand, model, response_json FROM tavily_cache"):
            k=key(val(rec["brand"]),val(rec["model"]))
            entry=cache.setdefault(k,{"records":0,"valid":0,"results":0,"corrupt":0})
            entry["records"]+=1
            try:
                body=json.loads(rec["response_json"])
                results=body.get("results") if isinstance(body,dict) else None
                if isinstance(results,list):
                    entry["valid"]+=1
                    entry["results"]+=len(results)
                else:entry["corrupt"]+=1
            except (ValueError,TypeError):entry["corrupt"]+=1
        return {k:{"raw_state":("RAW_OK" if v["results"] else
                                "RAW_EMPTY" if v["valid"] else "RAW_CORRUPT"),
                   **v} for k,v in cache.items()},True
    finally:db.close()

def run(args):
    final=Path(args.final)
    cache_path=Path(args.cache)
    output=Path(args.output)
    if not final.is_file():raise FileNotFoundError(final)
    if output.resolve() in {final.resolve().parent,ROOT.resolve()}:
        raise ValueError("Use uma pasta exclusiva de relatórios; não escreva na raiz do projeto.")
    # Não cria automaticamente planilhas produtivas.
    cached,cache_table_present=load_cache(cache_path)
    rows=load_excel(final)
    output.mkdir(parents=True,exist_ok=True)
    details=[]
    for row in rows:
        c=cached.get(key(row["brand"],row["model"]),{"raw_state":"RAW_MISSING"})
        states=evaluate(row,c)
        details.append({"Linha Excel":row["source_row"],"Marca":row["brand"],"Modelo":row["model"],
            "Revisão Atual":row["Revisão"],"Status Atual":row["Status"],
            "Título Real":row["Título Real"],"O que é (Tipo)":row["O que é (Tipo)"],
            "Alimentação":row["Alimentação"],"Category General":row["Category General"],
            "FAMILY":row["FAMILY"],"Cache Registros":c.get("records",0),
            "Cache Resultados":c.get("results",0),**states})
    target=output/"diagnostico_status.csv"
    with target.open("w",newline="",encoding="utf-8-sig") as fp:
        writer=csv.DictWriter(fp,fieldnames=list(details[0])) if details else None
        if writer:
            writer.writeheader()
            writer.writerows(details)
    by_review=Counter(x["Revisão Atual"] for x in details)
    pending=[x for x in details if x["Revisão Atual"].upper() in {"PENDENTE_COLETA","PENDENTE"}]
    summary={
        "read_only":True,"api_requests":0,"final_rows":len(details),
        "cache_exists":cache_path.is_file(),"cache_table_present":cache_table_present,
        "pending_count":len(pending),"pending_by_original_review":dict(Counter(x["Revisão Atual"] for x in pending)),
        "pending_by_collection":dict(Counter(x["Status Coleta"] for x in pending)),
        "pending_by_classification":dict(Counter(x["Status Classificação"] for x in pending)),
        "pending_by_action":dict(Counter(x["Próxima Ação Recomendada"] for x in pending)),
        "identity_confirmed_in_excel":False,
        "warning":"Identidade confirmada requer campo auditável com score/fonte; arquivo Base Final atual não possui esse campo. Sem esse dado, não atribuir marca+modelo confirmados.",
        "note":"Ausência no cache consultado não prova que nunca houve busca com outro provedor ou outro banco."
    }
    (output/"resumo_status.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("Relatórios:",output)
    return 0

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--final",default=str(ROOT/"Base Final Produtos Enriquecida.xlsx"))
    parser.add_argument("--cache",default=str(ROOT/"cache_tavily.sqlite"))
    parser.add_argument("--output",default=str(ROOT/"Auditoria_Status_v1"))
    raise SystemExit(run(parser.parse_args()))
