#!/usr/bin/env python3
"""Unattended offline TF-IDF -> BGE-M3 -> reranker -> GLiNER benchmark suite.

One command, independent logs, checkpointed JSON statuses. Never uses JEV,
Clef, SERP providers, Gold as inference features or auto-approval.
Missing models are SKIPPED explicitly; a stage failure does not stop the next.
"""
from __future__ import annotations
import argparse,json,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
V5=ROOT/"experiments"/"local_classifier_v5"

def stages_for(args,out):
    gold=Path(args.gold).resolve()
    cache=Path(args.cache).resolve()
    bge=Path(args.bge_model).resolve()
    reranker=Path(args.reranker_model).resolve()
    gliner=Path(args.gliner_model).resolve() if args.gliner_model else None
    return [
        ("tfidf_cache_ablation",None,[
            sys.executable,str(V5/"benchmark_cache_title_ablation.py"),
            "--gold",str(gold),"--cache",str(cache),
            "--output",str(out/"01_tfidf_cache_ablation.xlsx")]),
        ("bge_m3_supervised",bge,[
            sys.executable,str(V5/"benchmark_taxonomy.py"),
            "--input",str(gold),"--output",str(out/"02_bge_m3.xlsx"),
            "--cache-dir",str(out/"bge_embeddings"),
            "--candidates","bge","--feature-modes","brand_model",
            "--bge-model",str(bge),"--embedding-batch-size",str(args.batch_size)]),
        ("reranker_tfidf_topk",reranker,[
            sys.executable,str(V5/"benchmark_reranker_topk.py"),
            "--gold",str(gold),"--cache",str(cache),"--model",str(reranker),
            "--max-test",str(args.reranker_max_test),
            "--output",str(out/"03_reranker.json")]),
        ("gliner_evidence_diagnostic",gliner,[
            sys.executable,str(V5/"benchmark_gliner_cache.py"),
            "--cache",str(cache),"--model",str(gliner) if gliner else "",
            "--max-items",str(args.gliner_max_items),
            "--output",str(out/"04_gliner.json")])
    ]

def run(args):
    source_files={"gold":Path(args.gold).resolve(),"cache":Path(args.cache).resolve()}
    for name,path in source_files.items():
        if not path.is_file():raise FileNotFoundError(f"{name} missing: {path}")
    out=Path(args.output).resolve()
    if out in source_files.values() or out==ROOT:
        raise ValueError("Output may not overwrite Gold/cache/repository")
    out.mkdir(parents=True,exist_ok=True)
    report_path=out/"benchmark_sequence_report.json"
    report={
        "mode":"OFFLINE_SEQUENTIAL_NO_APIS",
        "started_at":datetime.now(timezone.utc).isoformat(),
        "inputs":{k:str(v) for k,v in source_files.items()},
        "statuses":[],
        "auto_approval":False,
        "caveat":"TF-IDF/BGE grouped Gold classification, reranker Top-K subset, GLiNER entity diagnostic. GLiNER yield is not taxonomy accuracy."
    }
    def save():
        tmp=report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.replace(report_path)
    save()
    env=dict(os.environ)
    env.update({
        "HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1",
        "HF_DATASETS_OFFLINE":"1","HF_HUB_DISABLE_TELEMETRY":"1",
        "WANDB_DISABLED":"true"
    })
    stages=stages_for(args,out)
    for number,(name,model,cmd) in enumerate(stages,1):
        record={"name":name,"status":"NOT_STARTED","log":f"{number:02d}_{name}.log"}
        report["statuses"].append(record)
        log=out/record["log"]
        print(f"\n[{number}/{len(stages)}] {name}",flush=True)
        reason=None
        if name=="gliner_evidence_diagnostic" and model is None:
            reason="GLINER_MODEL_PATH_NOT_CONFIGURED"
        elif model is not None and not model.is_dir():
            reason=f"LOCAL_MODEL_MISSING: {model}"
        if reason:
            record.update(status="SKIPPED",reason=reason)
            log.write_text(reason,encoding="utf-8")
            save()
            print(f"  SKIPPED: {reason}",flush=True)
            continue
        record["status"]="RUNNING"
        save()
        try:
            with log.open("w",encoding="utf-8") as stream:
                with subprocess.Popen(
                    cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    encoding="utf-8",errors="replace",bufsize=1
                ) as process:
                    for line in process.stdout:
                        stream.write(line)
                        stream.flush()
                        print(line,end="",flush=True)
                    result=process.wait()
            record.update(status="PASS" if result==0 else "FAIL",exit_code=result)
        except Exception as exc:
            record.update(status="FAIL",error=f"{type(exc).__name__}: {exc}")
            with log.open("a",encoding="utf-8") as stream:
                stream.write("\n"+record["error"])
        save()
        print(f"  => {record['status']}",flush=True)
    report["finished_at"]=datetime.now(timezone.utc).isoformat()
    report["all_stages_pass"]=all(x["status"]=="PASS" for x in report["statuses"])
    report["production_approved"]=False
    save()
    print("\nFINAL:",json.dumps({
        "statuses":{x["name"]:x["status"] for x in report["statuses"]},
        "report":str(report_path),"production_approved":False
    },ensure_ascii=False,indent=2),flush=True)
    return 0 if report["all_stages_pass"] else 2

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold",default="Base Final Produtos Enriquecida.xlsx")
    parser.add_argument("--cache",default="piloto_argentina/cache.sqlite")
    parser.add_argument("--output",default="piloto_argentina/model_benchmarks_sequence")
    parser.add_argument("--bge-model",default=r"C:\p\models\bge-m3")
    parser.add_argument("--reranker-model",default=r"C:\p\models\bge-reranker-v2-m3")
    parser.add_argument("--gliner-model",default="")
    parser.add_argument("--batch-size",type=int,default=16)
    parser.add_argument("--reranker-max-test",type=int,default=150)
    parser.add_argument("--gliner-max-items",type=int,default=30)
    args=parser.parse_args()
    if not 1<=args.batch_size<=64:parser.error("batch-size 1..64")
    if not 10<=args.reranker_max_test<=1500:parser.error("reranker-max-test 10..1500")
    if not 1<=args.gliner_max_items<=200:parser.error("gliner-max-items 1..200")
    return run(args)

if __name__=="__main__":
    raise SystemExit(main())
