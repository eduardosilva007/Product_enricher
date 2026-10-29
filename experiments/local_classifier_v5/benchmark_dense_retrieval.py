#!/usr/bin/env python3
"""V5.1 retrieval evaluation: dense BGE-M3 or multilingual MiniLM, group held-out."""
import argparse, json, time, sys, csv
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_taxonomy import load_gold, load_taxonomy_normalization, group_split, build_text, label_pair, split_pair

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True)
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--candidate",choices=["small","bge"],required=True)
    ap.add_argument("--feature-mode",choices=["brand_model","brand_model_title"],required=True)
    ap.add_argument("--small-model",default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    ap.add_argument("--bge-model",default="C:\\p\\models\\bge-m3")
    ap.add_argument("--batch-size",type=int,default=8)
    args=ap.parse_args()
    t=time.perf_counter()
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    bundle=load_gold(Path(args.input),load_taxonomy_normalization(Path(__file__).with_name("taxonomy_normalization.json")))
    df=bundle.df
    y=np.array([label_pair(c,f) for c,f in zip(df["__category"],df["__family"])])
    texts=[build_text(row,bundle.cols,args.feature_mode) for _,row in df.iterrows()]
    tr,va,te=group_split(df,.15,.15)
    print("[INFO] encoding",args.candidate,args.feature_mode,flush=True)
    if args.candidate=="small":
        from sentence_transformers import SentenceTransformer
        model=SentenceTransformer(args.small_model,device="cpu")
        emb=model.encode(texts,batch_size=args.batch_size,normalize_embeddings=True,show_progress_bar=False,convert_to_numpy=True)
    else:
        from FlagEmbedding import BGEM3FlagModel
        model=BGEM3FlagModel(args.bge_model,use_fp16=False,device="cpu")
        chunks=[]
        for pos in range(0,len(texts),128):
            data=model.encode(texts[pos:pos+128],batch_size=args.batch_size,max_length=256,return_dense=True,return_sparse=False,return_colbert_vecs=False)
            chunks.append(np.asarray(data["dense_vecs"],dtype=np.float32))
            print("[INFO] BGE encoded",min(pos+128,len(texts)),"/",len(texts),flush=True)
        emb=np.concatenate(chunks,axis=0)
    from sklearn.preprocessing import normalize
    emb=normalize(emb.astype(np.float32))
    result={"candidate":args.candidate,"feature_mode":args.feature_mode,"train":len(tr),"validation":len(va),"test":len(te)}
    rows=[]
    for name,idxs in (("validation",va),("test",te)):
        hits={k:0 for k in (1,3,5,10,20,50)}
        cat=fam=0
        for start in range(0,len(idxs),128):
            sub=idxs[start:start+128]
            scores=emb[sub] @ emb[tr].T
            top=np.argsort(-scores,axis=1,kind="stable")[:,:50]
            for j,index in enumerate(sub):
                truth=y[index]
                labs=y[tr[top[j]]]
                for k in hits: hits[k]+=int(truth in labs[:k])
                tc,tf=split_pair(truth);pc,pf=split_pair(labs[0])
                cat+=int(tc==pc);fam+=int(tf==pf)
                rows.append({"subset":name,"gold_pair":truth,"pred_pair":labs[0],"correct":int(truth==labs[0]),"source_index":int(index)})
        result[name]={"n":len(idxs),"category_accuracy":round(cat/len(idxs),5),"family_accuracy":round(fam/len(idxs),5),**{f"pair_recall_at_{k}":round(v/len(idxs),5) for k,v in hits.items()}}
        print("[INFO]",name,result[name],flush=True)
    result["elapsed_seconds"]=round(time.perf_counter()-t,2)
    (out/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    with (out/"predictions.csv").open("w",newline="",encoding="utf-8-sig") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print("[OK] saved",out,flush=True)
if __name__=="__main__":
    main()
