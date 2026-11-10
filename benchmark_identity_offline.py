"""Read-only evidence ranking benchmark on actual cached search results.

No API, DB writes, baseline mutation, or classification approval.
Quality metrics need independent Gold title/URL; where unavailable report only
rank movement and coverage, NOT accuracy claims.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
from identity_ranking_experiment import tfidf_cosine, compact
from gold_failure_recovery import suggest_on_failure
from gliner_raw_fallback import extract_on_failure, clef_candidate_context

def key(brand, model):
    return compact(str(brand or "")) + "|" + compact(str(model or ""))

def clean(v):
    if v is None:
        return ""
    try:
        if pd.isna(v): return ""
    except (TypeError,ValueError): pass
    return str(v).strip()

def read_gold(path):
    df = pd.read_excel(path, dtype=object)
    required = {"Marca","Modelo"}
    if not required.issubset(df.columns):
        raise ValueError("Gold missing columns Marca/Modelo")
    seen, duplicates = {}, set()
    for _, row in df.iterrows():
        b, m = clean(row.get("Marca")), clean(row.get("Modelo"))
        if not b or not m: continue
        k = key(b,m)
        entry = {str(c):clean(row[c]) for c in df.columns}
        if k in seen:
            duplicates.add(k)
        else:
            seen[k]=entry
    for k in duplicates:
        seen.pop(k,None)  # Ambiguous Gold duplicates: no ground truth.
    return seen, len(duplicates)

def read_cache(path):
    # Readonly URI and closing are important for Windows SQLite file locks.
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tavily_cache" not in names:
            raise ValueError("Cache has no tavily_cache table")
        for row in conn.execute("SELECT brand,model,response_json FROM tavily_cache ORDER BY created_at DESC"):
            try: payload=json.loads(row["response_json"])
            except (ValueError,TypeError): continue
            if isinstance(payload,dict):
                yield str(row["brand"]),str(row["model"]),payload

def normalized_sources(payload):
    # Only evidence that the existing identity gate approved.
    collection=payload.get("_collection") or {}
    curated=collection.get("curated_results") or []
    if not isinstance(curated,list): return []
    return [dict(x) for x in curated if isinstance(x,dict)
            and clean(x.get("title")) and clean(x.get("url"))]

def ref_value(gold, *keys):
    for k in keys:
        v=clean(gold.get(k))
        if v: return v
    return ""

def matches_ground_truth(row, title, url):
    # URL exact match, or normalized Gold title equality. Never approximate.
    expected_url=ref_value(row,"Fonte URL","Fonte Referência URL","Fonte Referencia URL")
    expected_title=ref_value(row,"Título Real","Titulo Real","Título Referência","Titulo Referencia")
    if expected_url and url.strip().rstrip("/").casefold()==expected_url.rstrip("/").casefold():
        return True
    if expected_title and compact(title)==compact(expected_title):
        return True
    return False

def rank_sources(brand,model,items):
    # Mirror production order: hard identity tier, then char TF-IDF, provider score.
    rank={"WEAK":0,"CONTEXTUAL":1,"PROBABLE":2,"STRONG":3}
    corpus=[f"{brand} {model}"] + [f"{clean(x.get('title'))} {clean(x.get('url'))}" for x in items]
    def provider_score(row):
        try: return float(row.get("score") or 0)
        except (TypeError,ValueError): return 0.0
    def identity(row):return rank.get(clean(row.get("_collection_identity")).upper(),0)
    scored=[]
    for x in items:
        score=tfidf_cosine(corpus[0],f"{clean(x.get('title'))} {clean(x.get('url'))}",corpus)
        scored.append((x,score))
    baseline=sorted(scored,key=lambda t:(-identity(t[0]),-provider_score(t[0]),clean(t[0].get("url"))))
    tfidf=sorted(scored,key=lambda t:(-identity(t[0]),-t[1],-provider_score(t[0]),clean(t[0].get("url"))))
    return baseline,tfidf

def run(args):
    if not args.cache.exists(): raise FileNotFoundError(args.cache)
    if not args.gold.exists(): raise FileNotFoundError(args.gold)
    gold, duplicates=read_gold(args.gold)
    args.output.mkdir(parents=True,exist_ok=True)
    result=[]
    stats=Counter()
    gliner_model=None
    if args.gliner_model:
        try:
            from gliner import GLiNER
        except ImportError as exc:
            raise RuntimeError("GLiNER is not available in the active Python environment") from exc
        gliner_model=GLiNER.from_pretrained(args.gliner_model,local_files_only=True)
    for brand,model,payload in read_cache(args.cache):
        stats["cache_products"]+=1
        k=key(brand,model)
        items=normalized_sources(payload)
        if not items:
            stats["without_curated_evidence"]+=1
        baseline,tfidf=rank_sources(brand,model,items)
        g=gold.get(k)
        if g: stats["matched_gold"]+=1
        ref_url=ref_value(g or {},"Fonte URL","Fonte Referência URL")
        ref_title=ref_value(g or {},"Título Real","Titulo Real","Título Referência")
        has_reference=bool(ref_url or ref_title)
        if g and has_reference: stats["gold_with_reference"]+=1
        old=baseline[0][0] if baseline else {}
        new=tfidf[0][0] if tfidf else {}
        old_hit=matches_ground_truth(g,clean(old.get("title")),clean(old.get("url"))) if g and has_reference and old else False
        new_hit=matches_ground_truth(g,clean(new.get("title")),clean(new.get("url"))) if g and has_reference and new else False
        if g and has_reference:
            stats["baseline_top1_reference_matches"]+=int(old_hit)
            stats["tfidf_top1_reference_matches"]+=int(new_hit)
            stats["reference_with_curated"]+=int(bool(items))
        moved=bool(old and new and clean(old.get("url"))!=clean(new.get("url")))
        stats["top1_changed"]+=int(moved)
        c=(payload.get("_collection") or {})
        state=clean(c.get("state"))
        unresolved=state in {"NO_TARGET_EVIDENCE","LOW_TARGET_EVIDENCE","PARTIAL_TARGET_EVIDENCE"} or not items
        gold_rows=[g] if g else []
        suggestions=suggest_on_failure(brand,model,unresolved,gold_rows)
        stats["gold_suggestions_on_failure"]+=int(bool(suggestions))
        entities=[]
        if gliner_model and unresolved:
            entities=extract_on_failure(payload,True,gliner_model,max_sources=args.max_sources)
            stats["gliner_cases_attempted"]+=1
            stats["gliner_entities"]+=len(entities)
        result.append({
            "Marca":brand,"Modelo":model,"Provider":clean((payload.get("_search_meta") or {}).get("provider")),
            "Collection State":state,"Curated":len(items),"Gold Matched":bool(g),
            "Gold Reference Available":bool(g and has_reference),
            "Baseline Top1":clean(old.get("title")),"Baseline URL":clean(old.get("url")),
            "TFIDF Top1":clean(new.get("title")),"TFIDF URL":clean(new.get("url")),
            "Top1 Changed":moved,"Baseline Reference Exact Hit":old_hit,
            "TFIDF Reference Exact Hit":new_hit,"Gold Suggestion":suggestions[0].reason if suggestions else "",
            "Gold Category Suggestion":suggestions[0].category if suggestions else "",
            "Gold Family Suggestion":suggestions[0].family if suggestions else "",
            "GLiNER Entities":json.dumps(clef_candidate_context(entities),ensure_ascii=False) if gliner_model and unresolved else "",
        })
    df=pd.DataFrame(result)
    df.to_csv(args.output/"benchmark_evidencias.csv",index=False,encoding="utf-8-sig")
    report={
        "scope":"OFFLINE_READ_ONLY","cache":str(args.cache),"gold":str(args.gold),
        "gold_ambiguous_keys_excluded":duplicates,"counts":dict(stats),
        "warning":"Gold title/URL exact match measures reference overlap only, not overall identity accuracy.",
        "tfidf_note":"TF-IDF ranks only already-curated evidence; it cannot recover rejected evidence.",
        "gliner_enabled":bool(gliner_model),
    }
    (args.output/"benchmark_resumo.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(report,indent=2,ensure_ascii=False))
    print("FILES:",args.output/"benchmark_evidencias.csv",args.output/"benchmark_resumo.json")

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache",type=Path,required=True)
    parser.add_argument("--gold",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=Path("piloto_argentina/benchmark_offline"))
    parser.add_argument("--gliner-model",default="",help="Optional installed local GLiNER path (runs only unresolved)")
    parser.add_argument("--max-sources",type=int,default=5)
    run(parser.parse_args())
if __name__=="__main__":
    main()
