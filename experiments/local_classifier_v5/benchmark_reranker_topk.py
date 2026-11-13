#!/usr/bin/env python3
"""Offline taxonomy Top-K reranking against grouped held-out Gold.

Same TF-IDF candidate pool before/after reranking; only cache-curated titles
are eligible as inputs. This never automatically approves classifications.
"""
from __future__ import annotations
import argparse,json,sys,time
from pathlib import Path
import numpy as np

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import benchmark_taxonomy as bt
from benchmark_cache_title_ablation import load_cache_titles,make_texts

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold",required=True)
    p.add_argument("--cache",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--max-test",type=int,default=150)
    p.add_argument("--topk",type=int,default=5)
    p.add_argument("--batch-size",type=int,default=8)
    a=p.parse_args()
    if not (10<=a.max_test<=1500 and 2<=a.topk<=10 and 1<=a.batch_size<=64):
        p.error("Invalid limits")
    model_path=Path(a.model)
    if not model_path.is_dir():
        raise FileNotFoundError(f"Local reranker is required: {model_path}")
    start=time.perf_counter()
    normalization=bt.load_taxonomy_normalization(HERE/"taxonomy_normalization.json")
    bundle=bt.load_gold(Path(a.gold),normalization)
    df=bundle.df
    train,val,test=bt.group_split(df,.15,.15)
    for first,second in ((train,val),(train,test),(val,test)):
        if set(df.iloc[first]["__group"]) & set(df.iloc[second]["__group"]):
            raise RuntimeError("GROUP_LEAKAGE")
    titles,_=load_cache_titles(Path(a.cache))
    texts=make_texts(df,"cache_title",bundle.cols,titles)
    gold=df["__label"].to_numpy()
    classifier=bt.TfidfSVC().fit([texts[int(i)] for i in train],gold[train])
    subset=test[:a.max_test]
    probs=classifier.model.predict_proba(classifier.transform([texts[int(i)] for i in subset]))
    classes=np.asarray(classifier.model.classes_)
    ranking=bt.topk_indices(probs,a.topk)
    truth=gold[subset]
    before=classes[ranking[:,0]]
    from sentence_transformers import CrossEncoder
    reranker=CrossEncoder(str(model_path),device="cpu",max_length=192,local_files_only=True)
    pairs=[]
    for i,positions in zip(subset,ranking):
        for pos in positions:
            cat,fam=bt.split_pair(str(classes[pos]))
            pairs.append((texts[int(i)],f"Category General: {cat}; FAMILY: {fam}"))
    scores=np.asarray(reranker.predict(pairs,batch_size=a.batch_size,show_progress_bar=False)).reshape(len(subset),-1)
    rerank=np.argsort(-scores,axis=1)
    after=classes[ranking[np.arange(len(subset)),rerank[:,0]]]
    pre=float(np.mean(before==truth))
    post=float(np.mean(after==truth))
    result={
        "scope":"OFFLINE_HELDOUT_CACHE_TITLE_TOPK_RERANK",
        "test_rows":len(subset),"topk":a.topk,
        "tfidf_top1":pre,"reranker_top1":post,"delta_top1":post-pre,
        "tfidf_topk_recall":bt.candidate_recall(truth,classes,ranking,a.topk),
        "candidate_pool_unchanged":True,
        "seconds":round(time.perf_counter()-start,2),
        "auto_approval":False,
        "limitation":"Exploratory subset, no confidence calibration. Cannot recover correct labels outside TF-IDF Top-K."
    }
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False),flush=True)

if __name__=="__main__":
    main()
