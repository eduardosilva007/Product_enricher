#!/usr/bin/env python3
"""V5.3: supervised flat vs hierarchical taxonomy, grouped holdout and validation gates."""
import argparse, json, time, csv, sys
from pathlib import Path
import numpy as np
from sklearn.svm import LinearSVC
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import normalize

sys.path.insert(0,str(Path(__file__).resolve().parent))
from benchmark_taxonomy import (load_gold,load_taxonomy_normalization,group_split,build_text,
                                label_pair,split_pair,threshold_from_validation,apply_threshold)

def confidence_scores(model,x):
    a=np.asarray(model.decision_function(x),dtype=float)
    if a.ndim==1: a=np.column_stack([-a,a])
    a=a-a.max(axis=1,keepdims=True)
    exp=np.exp(np.clip(a,-50,50))
    return exp / np.maximum(exp.sum(axis=1,keepdims=True),1e-12)

def predict_with_conf(model,x):
    p=confidence_scores(model,x)
    ix=np.argmax(p,axis=1)
    return np.asarray(model.classes_)[ix],p[np.arange(len(ix)),ix]

def summary(y,pred):
    cats=lambda arr:np.array([split_pair(z)[0] for z in arr])
    fams=lambda arr:np.array([split_pair(z)[1] for z in arr])
    return {"n":len(y),"pair_accuracy":round(float(np.mean(y==pred)),5),
            "category_accuracy":round(float(np.mean(cats(y)==cats(pred))),5),
            "family_accuracy":round(float(np.mean(fams(y)==fams(pred))),5)}

def gate(yv,pv,cv,yt,pt,ct):
    result={}
    for target in (.95,.97,.98):
        threshold=threshold_from_validation(yv,pv,cv,target)
        m=apply_threshold(yt,pt,ct,threshold)
        result[str(target)]={"threshold":None if threshold is None else round(threshold,6),
               "selected":m["selected"],"coverage":round(m["coverage"],5),
               "precision":None if m["precision"] is None else round(m["precision"],5)}
    return result

def hierarchy(xtr,xval,xtest,cat_train,pair_train):
    # Category first, then FAMILY restricted to that category; prediction never creates unknown pairs.
    cm=LinearSVC(C=2,class_weight="balanced").fit(xtr,cat_train)
    cats_val,cat_conf_val=predict_with_conf(cm,xval)
    cats_test,cat_conf_test=predict_with_conf(cm,xtest)
    family_models={}
    default_family={}
    family_classes={}
    family_train=np.array([split_pair(x)[1] for x in pair_train])
    for c in np.unique(cat_train):
        mask=cat_train==c
        labels=family_train[mask]
        uniques,counts=np.unique(labels,return_counts=True)
        default_family[c]=uniques[np.argmax(counts)]
        family_classes[c]=set(uniques)
        if len(uniques)>1 and np.sum(mask)>=2:
            family_models[c]=LinearSVC(C=2,class_weight="balanced").fit(xtr[mask],labels)
    outputs=[]
    for x,cats,cat_confs in ((xval,cats_val,cat_conf_val),(xtest,cats_test,cat_conf_test)):
        pred=np.empty(len(cats),dtype=object)
        conf=np.empty(len(cats),dtype=float)
        for c in np.unique(cats):
            mask=np.where(cats==c)[0]
            if c in family_models:
                fam,fc=predict_with_conf(family_models[c],x[mask])
            else:
                fam=np.array([default_family[c]]*len(mask))
                fc=np.ones(len(mask),dtype=float)
            pred[mask]=[label_pair(c,f) for f in fam]
            conf[mask]=cat_confs[mask]*fc
        outputs.append((pred,conf))
    return outputs

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True)
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--feature-mode",choices=["brand_model","brand_model_title"],default="brand_model_title")
    args=ap.parse_args()
    start=time.perf_counter()
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    gold=load_gold(Path(args.input),load_taxonomy_normalization(Path(__file__).with_name("taxonomy_normalization.json")))
    df=gold.df
    train,val,test=group_split(df,.15,.15)
    texts=[build_text(row,gold.cols,args.feature_mode) for _,row in df.iterrows()]
    y=np.array([label_pair(c,f) for c,f in zip(df["__category"],df["__family"])])
    cy=np.array([split_pair(z)[0] for z in y])
    vec=FeatureUnion([
      ("char",TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),min_df=2,max_features=180000,sublinear_tf=True)),
      ("word",TfidfVectorizer(analyzer="word",ngram_range=(1,2),min_df=2,max_features=100000,sublinear_tf=True))])
    print("[INFO] fitting TF-IDF",args.feature_mode,flush=True)
    xtr=vec.fit_transform([texts[i] for i in train])
    xval=vec.transform([texts[i] for i in val])
    xtest=vec.transform([texts[i] for i in test])
    flat=LinearSVC(C=2,class_weight="balanced").fit(xtr,y[train])
    fval,fcv=predict_with_conf(flat,xval)
    ftest,fct=predict_with_conf(flat,xtest)
    print("[INFO] fitting hierarchical category -> family",flush=True)
    (hval,hcv),(htest,hct)=hierarchy(xtr,xval,xtest,cy[train],y[train])
    results={"feature_mode":args.feature_mode,"train":len(train),"validation":len(val),"test":len(test),
             "group_overlap_train_test":len(set(df.iloc[train]["__group"])&set(df.iloc[test]["__group"])),
             "flat":{"validation":summary(y[val],fval),"test":summary(y[test],ftest),
                     "gate_test":gate(y[val],fval,fcv,y[test],ftest,fct)},
             "hierarchical":{"validation":summary(y[val],hval),"test":summary(y[test],htest),
                     "gate_test":gate(y[val],hval,hcv,y[test],htest,hct)}}
    results["elapsed_seconds"]=round(time.perf_counter()-start,2)
    (out/"summary.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    with (out/"predictions.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f,delimiter=";")
        w.writerow(["row_index","gold_pair","flat_pair","flat_confidence","hierarchical_pair","hierarchical_confidence"])
        for j,i in enumerate(test):w.writerow([int(i),y[i],ftest[j],round(float(fct[j]),6),htest[j],round(float(hct[j]),6)])
    print(json.dumps(results,ensure_ascii=False,indent=2),flush=True)
if __name__=="__main__": main()
