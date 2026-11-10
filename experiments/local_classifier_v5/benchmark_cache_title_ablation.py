#!/usr/bin/env python3
"""Offline V5 TF-IDF ablations using production-available cache titles.

Modes: brand_model, gold_title_oracle, cache_title; candidates: flat, hierarchy.
No Search, JEV, Clef or DB writes. Training and scoring use a grouped Gold split.
The gold-title mode is an optimistic oracle, not a production deployment option.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
import time
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import benchmark_taxonomy as bt

def load_cache_titles(cache: Path) -> tuple[dict[str, str], dict]:
    if not cache.is_file():
        raise FileNotFoundError(cache)
    titles: dict[str, str] = {}
    stats = {"rows_read": 0, "usable_curated_titles": 0, "keys_with_titles": 0}
    uri = cache.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        tables = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tavily_cache" not in tables:
            raise RuntimeError("Cache without tavily_cache table")
        for brand, model, raw in db.execute(
            "SELECT brand,model,response_json FROM tavily_cache ORDER BY created_at DESC"
        ):
            stats["rows_read"] += 1
            key = bt.compact(brand) + "|" + bt.compact(model)
            if key in titles:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError,ValueError):
                continue
            if not isinstance(payload,dict):
                continue
            coll = payload.get("_collection") or {}
            if not isinstance(coll,dict):
                continue
            curated = coll.get("curated_results") or []
            if not isinstance(curated,list):
                continue
            # ONLY sources already approved by existing curated evidence pipeline.
            for item in curated:
                if not isinstance(item,dict): continue
                title = bt.safe(item.get("title"))
                if title:
                    titles[key] = title
                    stats["usable_curated_titles"] += 1
                    break
    stats["keys_with_titles"] = len(titles)
    return titles, stats

def make_texts(df: pd.DataFrame, mode: str, cols: dict, cache_titles: dict[str,str]) -> list[str]:
    texts = []
    for _, row in df.iterrows():
        brand, model = row["__brand"], row["__model"]
        base = f"BRAND: {brand}\nMODEL: {model}"
        title = ""
        if mode == "gold_title_oracle" and cols.get("title"):
            title = bt.safe(row[cols["title"]])
        elif mode == "cache_title":
            title = cache_titles.get(row["__key"], "")
        texts.append(base + (f"\nTITLE: {title}" if title else ""))
    return texts

def model_probabilities(texts: list[str], labels: np.ndarray, train_idx, val_idx, test_idx, hierarchical: bool):
    trained = bt.TfidfSVC().fit([texts[i] for i in train_idx], labels[train_idx])
    v = trained.transform([texts[i] for i in val_idx])
    t = trained.transform([texts[i] for i in test_idx])
    classes = np.asarray(trained.model.classes_)
    val_proba = trained.model.predict_proba(v)
    test_proba = trained.model.predict_proba(t)
    if not hierarchical:
        return classes, val_proba, test_proba
    cats = np.asarray([bt.split_pair(c)[0] for c in classes])
    y_cat = np.asarray([bt.split_pair(c)[0] for c in labels[train_idx]])
    if len(set(y_cat)) < 2:
        return classes,val_proba,test_proba
    from sklearn.svm import LinearSVC
    cat_classifier = bt.LinearSVCScoreModel().fit(trained.transform(
        [texts[i] for i in train_idx]), y_cat)
    def combine(pair_proba, features):
        cat_proba = cat_classifier.predict_proba(features)
        cat_indices = {label:i for i,label in enumerate(cat_classifier.classes_)}
        category_weights = np.asarray(
            [cat_proba[:,cat_indices[c]] for c in cats]).T
        weighted = pair_proba * category_weights
        normalizer = weighted.sum(axis=1,keepdims=True)
        return weighted / np.maximum(normalizer,1e-12)
    return classes,combine(val_proba,v),combine(test_proba,t)

def evaluate_run(df, texts, train_idx, val_idx, test_idx, method, mode, catchalls):
    start=time.perf_counter()
    labels=df["__label"].to_numpy()
    classes,val_p,test_p=model_probabilities(texts,labels,train_idx,val_idx,test_idx,
                                            hierarchical=(method=="hierarchical"))
    y_val,y_test=labels[val_idx],labels[test_idx]
    val_rank=bt.topk_indices(val_p,1)
    test_rank=bt.topk_indices(test_p,min(5,len(classes)))
    val_pred=classes[val_rank[:,0]]
    test_pred=classes[test_rank[:,0]]
    val_conf=val_p[np.arange(len(val_idx)),val_rank[:,0]]
    test_conf=test_p[np.arange(len(test_idx)),test_rank[:,0]]
    hit=(test_pred==y_test)
    predicted_actionable=np.asarray([p not in catchalls for p in test_pred])
    actual_actionable=np.asarray([p not in catchalls for p in y_test])
    target=.98
    val_actionable=np.asarray([p not in catchalls for p in val_pred])
    threshold=bt.threshold_from_validation(
        y_val[val_actionable],val_pred[val_actionable],val_conf[val_actionable],target
    ) if val_actionable.any() else None
    auto_mask = (predicted_actionable & (test_conf>=threshold)) if threshold is not None else np.zeros(len(test_idx),bool)
    metrics={
      "Mode":mode,"Method":method,
      "Test Rows":len(test_idx),
      "Pair Top1":float(hit.mean()),
      "Category Accuracy":float(np.mean([bt.split_pair(y)[0]==bt.split_pair(p)[0] for y,p in zip(y_test,test_pred)])),
      "FAMILY Accuracy":float(np.mean([bt.split_pair(y)[1]==bt.split_pair(p)[1] for y,p in zip(y_test,test_pred)])),
      "Top3 Recall":bt.candidate_recall(y_test,classes,test_rank,3),
      "Top5 Recall":bt.candidate_recall(y_test,classes,test_rank,5),
      "Actionable Test N":int(actual_actionable.sum()),
      "Actionable Top1":float(hit[actual_actionable].mean()) if actual_actionable.any() else None,
      "Auto Threshold@98":float(threshold) if threshold is not None else None,
      "Auto Selected":int(auto_mask.sum()),
      "Auto Coverage@98":float(auto_mask.mean()),
      "Auto Precision@98":float(hit[auto_mask].mean()) if auto_mask.any() else None,
      "Seconds":round(time.perf_counter()-start,2)
    }
    per_rows=[]
    for j,idx in enumerate(test_idx):
        r=df.iloc[idx]
        per_rows.append({
            "Mode":mode,"Method":method,"Brand":r["__brand"],"Model":r["__model"],
            "Gold Pair":y_test[j],"Predicted Pair":test_pred[j],
            "Correct":bool(hit[j]),"Confidence":float(test_conf[j]),
            "Top5": " | ".join(classes[test_rank[j]]),
            "Cache Title Used":bool(mode=="cache_title" and "\nTITLE:" in texts[idx])
        })
    return metrics,per_rows

def run(args):
    normalization=bt.load_taxonomy_normalization(Path(args.taxonomy_normalization))
    bundle=bt.load_gold(Path(args.gold),normalization)
    df=bundle.df
    if len(df)<100:raise RuntimeError("At least 100 unique reviewed Gold rows are required")
    train,val,test=bt.group_split(df,.15,.15)
    for a,b in ((train,val),(train,test),(val,test)):
        assert not (set(df.iloc[a]["__group"]) & set(df.iloc[b]["__group"])), "GROUP LEAKAGE"
    titles,cache_stats=load_cache_titles(Path(args.cache))
    modes=["brand_model","cache_title"]
    if args.oracle:
        modes.append("gold_title_oracle")
    out_metrics=[]
    rows=[]
    for mode in modes:
        texts=make_texts(df,mode,bundle.cols,titles)
        for method in ["flat","hierarchical"]:
            print(f"V5 CACHE TITLE ABLATION | mode={mode} method={method}",flush=True)
            metrics,predictions=evaluate_run(df,texts,train,val,test,method,mode,
                                              bt.catchall_labels(normalization))
            out_metrics.append(metrics)
            rows.extend(predictions)
            print(f" pair_top1={metrics['Pair Top1']:.4f} top5={metrics['Top5 Recall']:.4f} auto_precision={metrics['Auto Precision@98']} auto_coverage={metrics['Auto Coverage@98']:.4f}",flush=True)
    cache_keys=set(df["__key"]) & set(titles)
    test_keys=set(df.iloc[test]["__key"])
    summary={
      "Gold Usable":len(df),"Train":len(train),"Validation":len(val),"Test":len(test),
      "Unique Gold Groups":int(df["__group"].nunique()),
      "Gold Cache Title Coverage":len(cache_keys),
      "Test Cache Title Coverage":len(test_keys & set(titles)),
      "Test Cache Title Coverage Rate":len(test_keys & set(titles))/len(test),
      "Cache Stats":cache_stats,
      "warning":"Gold title oracle is not production-safe. Sparse cache-title rows use Brand+Model only. Auto precision is evaluated on TEST, threshold selected on VALIDATION; no auto-deployment.",
      "models":out_metrics,
    }
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    with pd.ExcelWriter(target,engine="openpyxl") as writer:
        pd.DataFrame(out_metrics).to_excel(writer,sheet_name="Summary",index=False)
        pd.DataFrame(rows).to_excel(writer,sheet_name="Predictions",index=False)
        audit_flat = {k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in summary.items() if k!="models"}
        pd.DataFrame([audit_flat]).to_excel(writer,sheet_name="Audit",index=False)
    target.with_suffix(".json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("RESULTADO:",target)
    return summary

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold",default="Base Final Produtos Enriquecida.xlsx")
    parser.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    parser.add_argument("--output",default="piloto_argentina/V5_Cache_Title_Ablation.xlsx")
    parser.add_argument("--taxonomy-normalization",default=str(HERE/"taxonomy_normalization.json"))
    parser.add_argument("--oracle",action="store_true",help="Include historical Gold title for reference ONLY")
    run(parser.parse_args())

if __name__=="__main__":
    main()
