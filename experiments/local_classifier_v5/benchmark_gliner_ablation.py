#!/usr/bin/env python3
"""Isolated GLiNER NER ablation on sampled grouped Gold. No production changes."""
import argparse, csv, json, hashlib, sys, time
from pathlib import Path
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import normalize

sys.path.insert(0,str(Path(__file__).resolve().parent))
from benchmark_taxonomy import load_gold, load_taxonomy_normalization, group_split, build_text, label_pair, split_pair, compact, safe

LABELS=["manufacturer brand","product model number","equipment type","power source","technical specification"]
def metrics(train, queries, y, strings):
    vec=FeatureUnion([
        ("char",TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),min_df=1,sublinear_tf=True)),
        ("word",TfidfVectorizer(analyzer="word",ngram_range=(1,2),min_df=1,sublinear_tf=True))])
    a=normalize(vec.fit_transform([strings[i] for i in train]))
    b=normalize(vec.transform([strings[i] for i in queries]))
    top1=cat=fam=rec10=0
    for start in range(0,len(queries),64):
        sim=(b[start:start+64]@a.T).toarray()
        top=np.argsort(-sim,axis=1,kind="stable")[:,:min(10,len(train))]
        for j,positions in enumerate(top):
            truth=y[queries[start+j]]
            ranked=y[train[positions]]
            tc,tf=split_pair(truth);pc,pf=split_pair(ranked[0])
            top1+=int(truth==ranked[0]);cat+=int(tc==pc);fam+=int(tf==pf)
            rec10+=int(truth in ranked)
    n=len(queries)
    return {"n":n,"pair_top1":round(top1/n,5),"category":round(cat/n,5),"family":round(fam/n,5),"recall10":round(rec10/n,5)}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--output-dir",required=True)
    p.add_argument("--model",default="urchade/gliner_multi-v2.1")
    p.add_argument("--max-rows",type=int,default=1200)
    p.add_argument("--threshold",type=float,default=.4)
    args=p.parse_args()
    if args.max_rows<100: p.error("--max-rows must be >=100")
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    gold=load_gold(Path(args.input),load_taxonomy_normalization(Path(__file__).with_name("taxonomy_normalization.json")))
    df=gold.df
    train,val,test=group_split(df,.15,.15)
    rng=np.random.default_rng(42)
    ntr=min(len(train),round(args.max_rows*.7))
    nva=min(len(val),round(args.max_rows*.15))
    nte=min(len(test),args.max_rows-ntr-nva)
    selected={"train":np.sort(rng.choice(train,ntr,replace=False)),
              "validation":np.sort(rng.choice(val,nva,replace=False)),
              "test":np.sort(rng.choice(test,nte,replace=False))}
    idx=np.concatenate(list(selected.values()))
    local={int(g):i for i,g in enumerate(idx)}
    tr=np.array([local[int(x)] for x in selected["train"]])
    va=np.array([local[int(x)] for x in selected["validation"]])
    te=np.array([local[int(x)] for x in selected["test"]])
    titles=[safe(df.iloc[i].get(gold.cols["title"])) if gold.cols.get("title") else "" for i in idx]
    originals=[build_text(df.iloc[i],gold.cols,"brand_model_title") for i in idx]
    labels=np.array([label_pair(df.iloc[i]["__category"],df.iloc[i]["__family"]) for i in idx])
    source_sig=hashlib.sha256(("\n".join(titles)+args.model+str(args.threshold)).encode()).hexdigest()[:18]
    cache=out/f"entities_{source_sig}.jsonl"
    if cache.exists():
        ner=[json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines()]
        if len(ner)!=len(idx): raise RuntimeError("Invalid entity cache length")
        print("[CACHE] loaded extracted entities",flush=True)
    else:
        from gliner import GLiNER
        print("[INFO] loading GLiNER",args.model,flush=True)
        model=GLiNER.from_pretrained(args.model)
        ner=[]
        with cache.open("w",encoding="utf-8") as f:
            for i,t in enumerate(titles):
                entities=model.predict_entities(t,LABELS,threshold=args.threshold) if t else []
                row={"source_index":int(idx[i]),"title":t,"entities":entities}
                ner.append(row);f.write(json.dumps(row,ensure_ascii=False)+"\n");f.flush()
                if (i+1)%50==0: print("[INFO] extracted",i+1,"/",len(titles),flush=True)
    enriched=[]
    n_entities=brand_hit=model_hit=0
    for i,row in enumerate(ner):
        ents=row["entities"]
        n_entities+=int(bool(ents))
        brand=compact(df.iloc[idx[i]]["__brand"])
        model_key=compact(df.iloc[idx[i]]["__model"])
        brand_hit+=int(any(compact(e["text"])==brand for e in ents if e.get("label")=="manufacturer brand"))
        model_hit+=int(any(compact(e["text"])==model_key for e in ents if e.get("label")=="product model number"))
        terms=" ".join(f'{e["label"]}: {e["text"]}' for e in ents)
        enriched.append(originals[i]+("\nNER: "+terms if terms else ""))
    report={"scope":"sampled ablation, NOT comparable to full 1276-row benchmark","model":args.model,
        "counts":{k:len(v) for k,v in selected.items()},"extracted_any_fraction":round(n_entities/len(idx),5),
        "exact_brand_extraction_fraction":round(brand_hit/len(idx),5),
        "exact_model_extraction_fraction":round(model_hit/len(idx),5)}
    for name,indices in (("validation",va),("test",te)):
        report[name]={"original":metrics(tr,indices,labels,originals),"ner_augmented":metrics(tr,indices,labels,enriched)}
        print("[RESULT]",name,report[name],flush=True)
    (out/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("[OK] summary saved",flush=True)

if __name__=="__main__": main()
