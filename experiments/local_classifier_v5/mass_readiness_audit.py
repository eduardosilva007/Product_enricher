#!/usr/bin/env python3
"""Mass-run offline readiness audit: read-only, no HTTP, no auto approvals.

Audits real Gold and collection cache; fits supervised TF-IDF on TRAIN only;
measures evidence coverage, group-isolated TEST and fallback Top-K only among
products WITHOUT a curated cache title. Fallback returns candidate taxonomy
pairs for human/JEV review, NEVER an identity match or automatic classification.
"""
from __future__ import annotations
import argparse
import json
import sys
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity

# Supports direct CLI use and package imports by unittest on Windows.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import benchmark_taxonomy as bt
from benchmark_cache_title_ablation import load_cache_titles, make_texts

def rank_gold_fallback(df, train_idx, test_idx, eligible_test_idx, k=5):
    """TRAIN-only retrieval using Brand/Model; never read TEST labels as features."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import FeatureUnion
    x_train = ["BRAND: "+df.iloc[i]["__brand"]+" MODEL: "+df.iloc[i]["__model"] for i in train_idx]
    x_test = ["BRAND: "+df.iloc[i]["__brand"]+" MODEL: "+df.iloc[i]["__model"] for i in eligible_test_idx]
    if not x_test:
        return [], {"tested":0,"top1":None,"top3":None,"top5":None}
    vec = FeatureUnion([
        ("char", TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),sublinear_tf=True)),
        ("word", TfidfVectorizer(analyzer="word",ngram_range=(1,2),sublinear_tf=True))
    ])
    X=vec.fit_transform(x_train)
    Q=vec.transform(x_test)
    labels=df.iloc[train_idx]["__label"].to_numpy()
    rows=[]
    hit=Counter()
    for begin in range(0,len(eligible_test_idx),64):
        sims=cosine_similarity(Q[begin:begin+64],X)
        for j,sim in enumerate(sims):
            ix=eligible_test_idx[begin+j]
            truth=df.iloc[ix]["__label"]
            order=np.argsort(-sim,kind="stable")
            unique=[]
            for pos in order:
                pair=str(labels[pos])
                if pair not in unique:unique.append(pair)
                if len(unique)==k:break
            for level in (1,3,5):
                hit[f"top{level}"]+=int(truth in unique[:level])
            rows.append({"Brand":df.iloc[ix]["__brand"],"Model":df.iloc[ix]["__model"],
                         "Gold Pair":truth,"Suggested Top5":" | ".join(unique),
                         "Top1 Correct":truth in unique[:1],"Top3 Correct":truth in unique[:3],
                         "Top5 Correct":truth in unique[:5],
                         "Strict Identity Match":False,
                         "Decision":"REVIEW_ONLY_NOT_AUTO_APPROVED"})
    n=len(rows)
    return rows,{"tested":n,"top1":hit["top1"]/n,"top3":hit["top3"]/n,"top5":hit["top5"]/n}

def audit_gold_cache(gold,cache,output):
    cfg=bt.load_taxonomy_normalization(Path(__file__).with_name("taxonomy_normalization.json"))
    bundle=bt.load_gold(Path(gold),cfg)
    df=bundle.df
    train,val,test=bt.group_split(df,.15,.15)
    t_groups=set(df.iloc[train]["__group"])
    v_groups=set(df.iloc[val]["__group"])
    e_groups=set(df.iloc[test]["__group"])
    assert not (t_groups&v_groups or t_groups&e_groups or v_groups&e_groups)
    titles,cache_stats=load_cache_titles(Path(cache))
    test_with_title=np.array([i for i in test if df.iloc[i]["__key"] in titles],dtype=int)
    test_without_title=np.array([i for i in test if df.iloc[i]["__key"] not in titles],dtype=int)
    texts=make_texts(df,"cache_title",bundle.cols,titles)
    estimator=bt.TfidfSVC().fit([texts[i] for i in train],df.iloc[train]["__label"].to_numpy())
    x_test=estimator.transform([texts[i] for i in test])
    proba=estimator.model.predict_proba(x_test)
    classes=np.asarray(estimator.model.classes_)
    ranks=bt.topk_indices(proba,5)
    truth=df.iloc[test]["__label"].to_numpy()
    pred=classes[ranks[:,0]]
    index={idx:j for j,idx in enumerate(test)}
    def cohort(indices):
        loc=[index[i] for i in indices]
        if not loc:return {"n":0,"pair_top1":None,"top3":None,"top5":None}
        y=truth[loc];p=pred[loc];r=ranks[loc]
        return {"n":len(loc),"pair_top1":float(np.mean(p==y)),
                "top3":bt.candidate_recall(y,classes,r,3),
                "top5":bt.candidate_recall(y,classes,r,5)}
    recovery_rows,recovery=rank_gold_fallback(df,train,test,test_without_title)
    # Reporting recovery over generic catchall labels inflates practical utility.
    # Gold labels are read here ONLY for offline metrics, never as model features.
    catchalls=bt.catchall_labels(cfg)
    actionable=[r for r in recovery_rows if r["Gold Pair"] not in catchalls]
    recovery["actionable_tested"]=len(actionable)
    recovery["actionable_top1"]=(
        sum(bool(r["Top1 Correct"]) for r in actionable)/len(actionable)
        if actionable else None
    )
    recovery["actionable_top3"]=(
        sum(bool(r["Top3 Correct"]) for r in actionable)/len(actionable)
        if actionable else None
    )
    recovery["actionable_top5"]=(
        sum(bool(r["Top5 Correct"]) for r in actionable)/len(actionable)
        if actionable else None
    )
    recovery["catchall_tested"]=len(recovery_rows)-len(actionable)
    by_title={"with_cache_title":cohort(test_with_title),"without_cache_title":cohort(test_without_title)}
    missing=len(test_without_title)
    status={
        "mode":"OFFLINE_NO_NETWORK_READ_ONLY",
        "go_for_mass":"BLOCKED_PENDING_INTEGRATION_AND_LIVE_PILOT",
        "gold_rows":len(df),"gold_conflicting_keys":int(bundle.conflicts["__key"].nunique()),
        "train_rows":len(train),"validation_rows":len(val),"test_rows":len(test),
        "group_leakage":0,
        "cache":cache_stats,
        "test_cache_title_coverage":len(test_with_title)/len(test),
        "stratified_tfidf":by_title,
        "gold_train_only_recovery_on_missing_title":recovery,
        "gold_recovery_status":"PROXY_COHORT_NOT_REAL_CLASSIFICATION_FAILURES",
        "fallback_note":"Taxonomy Top-K suggestions are NOT product identity, may be wrong, and MUST pass original rules and JEV/Clef review.",
        "readiness_gates":{
            "offline_group_leakage":"PASS",
            "cache_read_only":"PASS",
            "model_auto_approval":"DISABLED",
            "gliner_live_extraction":"PENDING",
            "jev_clef_shadow_comparison":"PENDING",
            "provider_quotas_and_crash_resume":"PENDING",
            "end_to_end_mass_checkpoint_and_human_lock":"PENDING"
        }
    }
    path=Path(output);path.mkdir(parents=True,exist_ok=True)
    (path/"readiness.json").write_text(json.dumps(status,indent=2,ensure_ascii=False),encoding="utf-8")
    pd.DataFrame(recovery_rows).to_csv(path/"gold_failure_top5.csv",index=False,encoding="utf-8-sig")
    print(json.dumps(status,indent=2,ensure_ascii=False))
    return status

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold",default="Base Final Produtos Enriquecida.xlsx")
    p.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    p.add_argument("--output",default="piloto_argentina/mass_readiness")
    a=p.parse_args()
    audit_gold_cache(a.gold,a.cache,a.output)

if __name__=="__main__":
    main()
