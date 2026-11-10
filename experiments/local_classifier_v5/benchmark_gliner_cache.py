#!/usr/bin/env python3
"""Offline GLiNER extraction diagnostic using existing cached evidence only.

NOT a measure of Category+FAMILY accuracy or downstream lift.
Never approves products and never calls a search API.
"""
from __future__ import annotations
import argparse,json,sqlite3,sys,time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from gliner_raw_fallback import extract_on_failure,raw_texts

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--max-items",type=int,default=30)
    a=p.parse_args()
    if not 1<=a.max_items<=200:p.error("max-items must be 1..200")
    if not Path(a.model).is_dir():
        raise FileNotFoundError(f"Local GLiNER model required: {a.model}")
    from gliner import GLiNER
    model=GLiNER.from_pretrained(a.model,local_files_only=True)
    start=time.perf_counter()
    n=0;with_entities=0;total_entities=0;brand_hits=0;model_hits=0;labels={}
    uri=Path(a.cache).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri,uri=True)) as conn:
        for brand,sku,serialized in conn.execute(
            "SELECT brand,model,response_json FROM tavily_cache ORDER BY created_at DESC"
        ):
            try:payload=json.loads(serialized)
            except (TypeError,ValueError):continue
            if not isinstance(payload,dict) or not raw_texts(payload):
                continue
            n+=1
            found=extract_on_failure(payload,True,model,max_sources=2)
            with_entities+=bool(found)
            total_entities+=len(found)
            for item in found:labels[item.label]=labels.get(item.label,0)+1
            def compact(x):return "".join(c for c in str(x).casefold() if c.isalnum())
            brand_hits+=any(x.label=="brand" and compact(x.text)==compact(brand) for x in found)
            model_hits+=any(x.label=="model" and compact(x.text)==compact(sku) for x in found)
            if n>=a.max_items:break
    if not n:raise RuntimeError("NO_ELIGIBLE_SAVED_EVIDENCE")
    report={
        "scope":"GLINER_LOCAL_EXTRACTION_DIAGNOSTIC_NOT_TAXONOMY_ACCURACY",
        "eligible_cache_items":n,"items_with_entities":with_entities,
        "entity_count":total_entities,"entity_labels":labels,
        "exact_brand_mentions":brand_hits,"exact_model_mentions":model_hits,
        "seconds":round(time.perf_counter()-start,2),
        "auto_approval":False,
        "limitation":"Entity yield is not Category+FAMILY accuracy or incremental improvement. Downstream controlled pairing is still required."
    }
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)

if __name__=="__main__":main()
