#!/usr/bin/env python3
"""Score identity relevance of saved evidence for genuinely unresolved SKU list.

CrossEncoder scores evidence passages, NOT Category/FAMILY strings. Scores are
relative only; no auto-approval and no new Search or API calls.
"""
from __future__ import annotations
import argparse,json,sqlite3,sys,time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from gliner_raw_fallback import raw_texts

def compact(x):
    return "".join(ch for ch in str(x).casefold() if ch.isalnum())

def load_rows(cache,skus,max_sources):
    selected={compact(x) for x in skus}
    found=set();rows=[]
    uri=Path(cache).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri,uri=True)) as conn:
        for brand,sku,raw in conn.execute(
            "SELECT brand,model,response_json FROM tavily_cache ORDER BY created_at DESC"
        ):
            key=compact(sku)
            if key not in selected or key in found:continue
            try:payload=json.loads(raw)
            except (TypeError,ValueError):continue
            if not isinstance(payload,dict):continue
            source_rows=raw_texts(payload,max_sources=max_sources,max_chars_per_source=1000)
            found.add(key)
            rows.append({"brand":str(brand),"sku":str(sku),"source_rows":source_rows})
    return rows,selected-found

def evaluate(rows,model,batch_size):
    from sentence_transformers import CrossEncoder
    encoder=CrossEncoder(str(model),device="cpu",max_length=192,local_files_only=True)
    pairs=[];offsets=[]
    for row in rows:
        start=len(pairs)
        for _,text in row["source_rows"]:
            pairs.append((f"Manufacturer: {row['brand']}. Exact product model: {row['sku']}. Identify this same product.",text))
        offsets.append((start,len(pairs)))
    if not pairs:return rows
    scores=encoder.predict(pairs,batch_size=batch_size,show_progress_bar=False)
    for row,(start,end) in zip(rows,offsets):
        entries=[]
        for (source,text),score in zip(row.pop("source_rows"),scores[start:end]):
            entries.append({
                "source":source,"reranker_score":float(score),
                "sku_exact_in_excerpt":compact(row["sku"]) in compact(text),
                "brand_exact_in_excerpt":compact(row["brand"]) in compact(text),
                "excerpt":text[:350]
            })
        row["ranked_evidence"]=sorted(entries,key=lambda x:-x["reranker_score"])
        row["score_interpretation"]="Relative evidence relevance only; not an identity gate or probability"
    return rows

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--skus",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--max-sources",type=int,default=5)
    p.add_argument("--batch-size",type=int,default=8)
    a=p.parse_args()
    if not 1<=a.max_sources<=10:p.error("max-sources 1..10")
    candidates=[x.strip() for x in a.skus.split(",") if x.strip()]
    if not candidates:p.error("At least one unresolved SKU required")
    model=Path(a.model)
    if not model.is_dir() or not (model/"config.json").is_file():
        raise FileNotFoundError(f"Incomplete local reranker model: {model}")
    started=time.perf_counter()
    rows,missing=load_rows(a.cache,candidates,a.max_sources)
    result=evaluate(rows,model,a.batch_size)
    report={
        "scope":"UNRESOLVED_SKU_SAVED_EVIDENCE_RERANK",
        "requested_skus":candidates,"matched":len(rows),
        "missing_from_cache":sorted(missing),
        "rows":result,"seconds":round(time.perf_counter()-started,2),
        "auto_approval":False,
        "limitation":"No verified passage-level relevance annotations; cannot claim accuracy gain from scores alone."
    }
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="rows"},indent=2,ensure_ascii=False),flush=True)

if __name__=="__main__":main()
