#!/usr/bin/env python3
"""Auditoria READ-ONLY do cache SQLite, banco de processamento e cobertura da entrada.

Nunca grava nos bancos ou nas bases Excel. CSV/JSON de diagnóstico em pasta separada.
Execute sem outras instâncias da coleta/classificação em andamento.
"""
import argparse
import collections
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

def val(v):
    return "" if v is None else str(v).strip()

def load_excel(path):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    ws = book.active
    iterator = ws.iter_rows(values_only=True)
    columns = [val(x) for x in next(iterator)]
    def col(*names):
        for name in names:
            for n, label in enumerate(columns):
                if label.casefold() == name.casefold():
                    return n
        return None
    brand_i = col("Marca", "Fabricante", "Brand", "Manufacturer")
    model_i = col("Modelo", "Model", "SKU", "Part Number")
    if brand_i is None or model_i is None:
        raise ValueError("Colunas de marca/modelo não identificadas: " + str(path))
    ix = {key:col(key) for key in ("O que é (Tipo)","Alimentação","Category General","FAMILY",
              "Título Real","Revisão","Revisão B.I","Próxima Ação","Status")}
    rows=[]
    for number, row in enumerate(iterator,2):
        def read(i):
            return val(row[i]) if i is not None and i<len(row) else ""
        item={"source_row":number,"brand":read(brand_i),"model":read(model_i)}
        item.update({k:read(i) for k,i in ix.items()})
        rows.append(item)
    book.close()
    return rows

def connect_ro(path):
    if not path.exists(): return None
    db=sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)
    db.row_factory=sqlite3.Row
    return db

def key(b,m):
    # Use exact pairs in this standalone audit; pipeline key mapping is applied
    # by importing its normalization function when available.
    try:
        from pipeline_produtos import business_key
        return business_key(b,m)
    except ImportError:
        return (val(b)+"|"+val(m)).upper()

def audit(args):
    import sys
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    inp=Path(args.input) if args.input else root/"Base de Produtos.xlsx"
    final=Path(args.final) if args.final else root/"Base Final Produtos Enriquecida.xlsx"
    cache=Path(args.cache) if args.cache else root/"cache_tavily.sqlite"
    db_path=Path(args.db) if args.db else root/"pipeline_v3.sqlite"
    out=Path(args.output) if args.output else root/"Auditoria_Database"/datetime.now().strftime("%Y%m%d_%H%M%S")
    if out.resolve() in {root.resolve(),inp.parent.resolve()}:
        raise ValueError("Escolha pasta separada para os relatórios")
    out.mkdir(parents=True,exist_ok=True)
    source=load_excel(inp)
    current=load_excel(final)
    by_pair=collections.Counter((r["brand"],r["model"]) for r in current)
    by_key=collections.defaultdict(list)
    for row in current:by_key[key(row["brand"],row["model"])].append(row)
    input_by_key=collections.defaultdict(list)
    for row in source: input_by_key[key(row["brand"],row["model"])].append(row)
    cache_db=connect_ro(cache)
    cache_data=collections.defaultdict(lambda:{"records":0,"results":0,"invalid_json":0,"latest":"","providers":set(),"collection_states":set()})
    cache_tables=[]
    if cache_db:
        cache_tables=[r[0] for r in cache_db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if "tavily_cache" in cache_tables:
            for r in cache_db.execute("SELECT brand,model,response_json,created_at FROM tavily_cache"):
                state=cache_data[key(r["brand"],r["model"])]
                state["records"]+=1
                state["latest"]=max(state["latest"],val(r["created_at"]))
                try:
                    payload=json.loads(r["response_json"])
                    results=payload.get("results") if isinstance(payload,dict) else None
                    if not isinstance(results,list):state["invalid_json"]+=1
                    else:state["results"]+=len(results)
                    meta=payload.get("_search_meta",{}) if isinstance(payload,dict) else {}
                    if isinstance(meta,dict):
                        provider=meta.get("provider")
                        if provider:state["providers"].add(val(provider))
                    c=payload.get("_collection",{}) if isinstance(payload,dict) else {}
                    if isinstance(c,dict) and c.get("state"):state["collection_states"].add(val(c["state"]))
                except (ValueError,TypeError):state["invalid_json"]+=1
        cache_db.close()
    other_db=connect_ro(db_path)
    db_info={"exists":other_db is not None}
    if other_db:
        db_info["quick_check"]=other_db.execute("PRAGMA quick_check").fetchone()[0]
        db_info["tables"]={r["name"]:other_db.execute("SELECT COUNT(*) FROM '"+r["name"].replace("'","''")+"'").fetchone()[0]
                      for r in other_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        other_db.close()
    detail=[]
    for row in source:
        b,m=row["brand"],row["model"]
        canon=key(b,m)
        cached=cache_data.get(canon)
        if cached is None: state="RAW_MISSING"
        elif cached["invalid_json"]>=cached["records"]:state="RAW_CORRUPT"
        elif cached["results"]==0:state="RAW_EMPTY"
        else:state="RAW_OK"
        matches=by_key.get(canon,[])
        exact_count=by_pair.get((b,m),0)
        examples=[x for x in matches if x.get("Título Real") and x.get("O que é (Tipo)") not in {"","REVISAR","UNKNOWN"}]
        detail.append({
            "linha_entrada":row["source_row"],"Marca":b,"Modelo":m,
            "input_occurrences_same_key":len(input_by_key[canon]),
            "output_exact_occurrences":exact_count,
            "output_normalized_matches":len(matches),
            "raw_state":state,
            "raw_results":cached["results"] if cached else 0,
            "raw_variants":cached["records"] if cached else 0,
            "provider":",".join(sorted(cached["providers"])) if cached else "",
            "collection_states":",".join(sorted(cached["collection_states"])) if cached else "",
            "has_classified_candidate":bool(examples),
            "title_example":examples[0]["Título Real"] if examples else "",
            "type_example":examples[0]["O que é (Tipo)"] if examples else "",
            "category_example":examples[0]["Category General"] if examples else "",
            "family_example":examples[0]["FAMILY"] if examples else "",
        })
    # Deficits must compare counts per exact pair, not one row at a time.
    source_counts=collections.Counter((r["brand"],r["model"]) for r in source)
    missing=sum(max(0,n-by_pair[p]) for p,n in source_counts.items())
    suspicious=[]
    human_legacy=[]
    for r in current:
        auto=r["Revisão"].upper()=="AUTO_APROVADO"
        missing_essentials=(not r["O que é (Tipo)"] or r["O que é (Tipo)"].upper() in {"REVISAR","UNKNOWN"}
                            or not r["Título Real"] or not r["Category General"] or not r["FAMILY"])
        partial=bool(r["Category General"] or r["FAMILY"]) and (not r["O que é (Tipo)"] or r["O que é (Tipo)"].upper()=="REVISAR")
        if (auto and missing_essentials) or partial:
            item={"Marca":r["brand"],"Modelo":r["model"],"Revisao":r["Revisão"],
               "Revisao_BI":r["Revisão B.I"],"Tipo":r["O que é (Tipo)"],"Category":r["Category General"],
               "Family":r["FAMILY"],"Titulo":r["Título Real"],
               "reason":"AUTO_INCOMPLETO" if auto and missing_essentials else "TAXONOMIA_PARCIAL"}
            if r["Revisão B.I"].upper()=="OK" or r["Status"].upper()=="MANUAL":
                human_legacy.append(item)
            else:
                suspicious.append(item)
    def write_csv(filename, rows):
        if not rows:return
        with (out/filename).open("w",encoding="utf-8-sig",newline="") as fp:
            w=csv.DictWriter(fp,fieldnames=list(rows[0]))
            w.writeheader();w.writerows(rows)
    write_csv("entrada_vs_cache.csv",detail)
    write_csv("classificacoes_suspeitas.csv",suspicious)
    write_csv("legado_humano_protegido.csv",human_legacy)
    states=collections.Counter(r["raw_state"] for r in detail)
    summary={
       "input_rows":len(source),"input_unique_pairs":len(source_counts),
       "input_unique_normalized":len(input_by_key),
       "final_rows":len(current),"missing_exact_input_occurrences":missing,
       "cache_path":str(cache),"cache_table_exists":"tavily_cache" in cache_tables,
       "cache_unique_normalized":len(cache_data),"input_raw_states":dict(states),
       "suspect_auto_rows":len(suspicious),"protected_human_legacy_rows":len(human_legacy),
       "final_unique_exact_pairs":len(by_pair),
       "input_exact_pairs_matched":sum(1 for p in source_counts if by_pair[p]>0),
       "process_db":db_info,
       "read_only":True,"changes_to_database":0,
       "note":"RAW_EMPTY no cache não é RAW_MISSING; '--recollect-empty' é necessário para recolher. Estado PENDENTE_COLETA na planilha não prova ausência do cache."
    }
    (out/"resumo.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    print("Relatórios:",out)
    return 0

if __name__=="__main__":
    p=argparse.ArgumentParser()
    for name in ("input","final","cache","db","output"):
        p.add_argument("--"+name,default="")
    raise SystemExit(audit(p.parse_args()))
