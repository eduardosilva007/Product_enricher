#!/usr/bin/env python3
"""V5.4 fast ablations for taxonomic classifiers, held-out group split."""
import argparse, json, csv, sys, time
from pathlib import Path
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.svm import LinearSVC
sys.path.insert(0,str(Path(__file__).resolve().parent))
from benchmark_taxonomy import (load_gold,load_taxonomy_normalization,group_split,build_text,
    label_pair,split_pair,threshold_from_validation,apply_threshold,safe)

def features(row,cols,variant):
    original=build_text(row,cols,"brand_model_title")
    brand=safe(row.get(cols["brand"]))
    model=safe(row.get(cols["model"]))
    title=safe(row.get(cols["title"])) if cols.get("title") else ""
    if variant=="full":return original
    if variant=="identity_weighted":return f"BRAND: {brand}\nMODEL: {model}\nBRAND: {brand}\nMODEL: {model}\nTITLE: {title}"
    if variant=="title_only":return f"TITLE: {title}" if title else f"BRAND: {brand}\nMODEL: {model}"
    if variant=="identity_only":return f"BRAND: {brand}\nMODEL: {model}"
    if variant=="identity_title_no_labels":return f"{brand} {model} {title}".strip()
    raise ValueError(variant)

def metrics(y,p):
    return {
        "pair":round(float(np.mean(y==p)),5),
        "category":round(float(np.mean([split_pair(a)[0]==split_pair(b)[0] for a,b in zip(y,p)])),5),
        "family":round(float(np.mean([split_pair(a)[1]==split_pair(b)[1] for a,b in zip(y,p)])),5),
    }

def main():
    a=argparse.ArgumentParser()
    a.add_argument("--input",required=True)
    a.add_argument("--output-dir",required=True)
    args=a.parse_args()
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    start=time.monotonic()
    gold=load_gold(Path(args.input),load_taxonomy_normalization(Path(__file__).with_name("taxonomy_normalization.json")))
    df=gold.df
    tr,va,te=group_split(df,.15,.15)
    y=np.array([label_pair(c,f) for c,f in zip(df["__category"],df["__family"])])
    variants=["full","identity_weighted","title_only","identity_only","identity_title_no_labels"]
    report={"train":len(tr),"validation":len(va),"test":len(te),"group_overlap_train_test":len(set(df.iloc[tr]["__group"])&set(df.iloc[te]["__group"])),"results":[]}
    rows=[]
    for variant in variants:
        print("[START]",variant,flush=True)
        texts=[features(row,gold.cols,variant) for _,row in df.iterrows()]
        vectorizer=FeatureUnion([
            ("char",TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),min_df=2,max_features=180000,sublinear_tf=True)),
            ("word",TfidfVectorizer(analyzer="word",ngram_range=(1,2),min_df=2,max_features=100000,sublinear_tf=True))])
        xtr=vectorizer.fit_transform([texts[i] for i in tr])
        model=LinearSVC(C=2,class_weight="balanced").fit(xtr,y[tr])
        score={}
        for name,indices in (("validation",va),("test",te)):
            x=vectorizer.transform([texts[i] for i in indices])
            raw=np.asarray(model.decision_function(x),dtype=np.float64)
            if raw.ndim==1:raw=np.column_stack([-raw,raw])
            pred=model.classes_[np.argmax(raw,axis=1)]
            # Ranking score margin. Unlike softmax, robust to many class count choices.
            sorted_raw=np.partition(raw,-2,axis=1)
            confidence=sorted_raw[:,-1]-sorted_raw[:,-2]
            score[name]=(pred,confidence)
        pv,cv=score["validation"];pt,ct=score["test"]
        gates={}
        for target in (.95,.97,.98):
            threshold=threshold_from_validation(y[va],pv,cv,target)
            result=apply_threshold(y[te],pt,ct,threshold)
            gates[str(target)]={"threshold":threshold,"coverage":round(result["coverage"],5),"precision":result["precision"],"selected":result["selected"]}
        result={"variant":variant,"validation":metrics(y[va],pv),"test":metrics(y[te],pt),"gates_test":gates}
        report["results"].append(result)
        for j,i in enumerate(te):rows.append([variant,int(i),y[i],pt[j],float(ct[j])])
        (out/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("[OK]",variant,result["test"],flush=True)
    report["elapsed_seconds"]=round(time.monotonic()-start,2)
    (out/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with (out/"predictions.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f,delimiter=";");w.writerow(["variant","row_index","gold_pair","predicted_pair","margin"]);w.writerows(rows)
    print("[FINAL] saved",out,flush=True)
if __name__=="__main__":main()
