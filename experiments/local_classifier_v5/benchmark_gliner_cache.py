#!/usr/bin/env python3
"""Offline GLiNER2.5 diagnostic on cached RAW, optionally restricted to unresolved SKUs.

No Search/JEV/Clef. Extraction is evidence hints, NOT classification accuracy.
"""
from __future__ import annotations
import argparse,json,sqlite3,time,sys
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from gliner_raw_fallback import raw_texts

LABELS=("brand","model","product type","power source","product family")

def normal(x):
    return "".join(ch for ch in str(x).casefold() if ch.isalnum())

def parse_entities(payload):
    """GLiNER2 extract_entities returns {'entities': {label: [text or span-dict]}}."""
    result=payload.get("entities",{}) if isinstance(payload,dict) else {}
    if not isinstance(result,dict):
        raise ValueError("GLiNER2 response missing dict entities")
    out=[]
    for label,values in result.items():
        for value in (values if isinstance(values,list) else []):
            if isinstance(value,str):
                item={"label":str(label),"text":value}
            elif isinstance(value,dict):
                item={"label":str(label),"text":str(value.get("text",""))}
                if value.get("confidence") is not None:item["confidence"]=value["confidence"]
            else:continue
            if item["text"].strip():out.append(item)
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--max-items",type=int,default=30)
    p.add_argument("--only-skus",default="",help="Comma-separated unresolved model codes, not new search queries")
    a=p.parse_args()
    if not 1<=a.max_items<=200:p.error("max-items must be 1..200")
    model_path=Path(a.model)
    if not model_path.is_dir():raise FileNotFoundError(f"Local model directory missing: {model_path}")
    required=["config.json","model.safetensors","tokenizer.json"]
    absent=[name for name in required if not (model_path/name).is_file()]
    if absent:raise FileNotFoundError(f"Incomplete GLiNER2.5 local checkpoint {model_path}; missing: {absent}")
    # GLiNER2.5 boundary checkpoints require gliner2 AutoExtractor, not gliner.GLiNER.
    try:
        from gliner2 import AutoExtractor
    except ImportError as exc:
        raise RuntimeError('Install local inference package: pip install "gliner2[local]"') from exc
    extractor=AutoExtractor.from_pretrained(str(model_path))
    start=time.perf_counter()
    only={normal(s) for s in a.only_skus.split(",") if s.strip()}
    n=0;with_entities=0;total=0;brands=0;models=0;labels={};rows=[];matched=set()
    uri=Path(a.cache).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri,uri=True)) as conn:
        for brand,sku,serialized in conn.execute(
            "SELECT brand,model,response_json FROM tavily_cache ORDER BY created_at DESC"
        ):
            key=normal(sku)
            if only and key not in only:continue
            if only and key in matched:continue
            try:payload=json.loads(serialized)
            except (TypeError,ValueError):continue
            if not isinstance(payload,dict):continue
            sources=raw_texts(payload,max_sources=2,max_chars_per_source=1200)
            if not sources:continue
            matched.add(key);n+=1
            entities=[]
            for source,evidence in sources[:2]:
                res=extractor.extract_entities(evidence,list(LABELS))
                for item in parse_entities(res):
                    item["source"]=source
                    entities.append(item)
                    labels[item["label"]]=labels.get(item["label"],0)+1
            with_entities+=bool(entities)
            total+=len(entities)
            brands+=any(e["label"]=="brand" and normal(e["text"])==normal(brand) for e in entities)
            models+=any(e["label"]=="model" and normal(e["text"])==key for e in entities)
            rows.append({"brand":str(brand),"sku":str(sku),"evidence_sources":len(sources),
                         "entities":entities})
            if n>=a.max_items:break
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={"scope":"GLINER2_5_LOCAL_CACHE_EXTRACTION",
            "selected_unresolved_skus":sorted(only),
            "matched_skus":sorted(matched),
            "missing_skus":sorted(only-matched),
            "cache_items_with_evidence":n,"items_with_entities":with_entities,
            "entity_count":total,"entity_labels":labels,
            "exact_brand_entities":brands,"exact_model_entities":models,
            "seconds":round(time.perf_counter()-start,2),"rows":rows,
            "auto_approval":False,
            "limitation":"No manually annotated entity Gold. Entity yield does not establish Category+FAMILY accuracy."}
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="rows"},indent=2,ensure_ascii=False),flush=True)

if __name__=="__main__":main()
