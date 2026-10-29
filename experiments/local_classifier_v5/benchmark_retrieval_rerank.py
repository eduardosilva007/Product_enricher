#!/usr/bin/env python3
"""Offline V5.1: grouped Gold retrieval and optional BGE reranking. No production writes."""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.metrics.pairwise import linear_kernel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from benchmark_taxonomy import (
    load_gold, load_taxonomy_normalization, group_split, build_text, label_pair, split_pair,
)

def score_pair(truth, pred):
    return int(truth == pred)

def summarize(rows, name):
    n = len(rows)
    if not n:
        return {}
    return {
        "subset": name,
        "n": n,
        "top1_pair": round(sum(r["tfidf_pair_correct"] for r in rows) / n, 5),
        "top1_category": round(sum(r["tfidf_category_correct"] for r in rows) / n, 5),
        "top1_family": round(sum(r["tfidf_family_correct"] for r in rows) / n, 5),
        **{f"recall_pair_at_{k}": round(sum(r[f"recall_pair_at_{k}"] for r in rows) / n, 5) for k in (3, 5, 10, 20, 50)},
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output-dir", default="V51_overnight")
    p.add_argument("--feature-mode", choices=["brand_model", "brand_model_title"], default="brand_model_title")
    p.add_argument("--topk", type=int, default=20)
    p.add_argument("--reranker-model", default="")
    p.add_argument("--rerank-limit", type=int, default=0, help="0 = all validation and test queries")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--normalization", default=str(HERE / "taxonomy_normalization.json"))
    args = p.parse_args()
    if args.topk < 1 or args.topk > 50:
        p.error("--topk must be between 1 and 50")
    start = time.perf_counter()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    bundle = load_gold(Path(args.input), load_taxonomy_normalization(Path(args.normalization)))
    df = bundle.df
    cols = bundle.cols
    labels = np.array([label_pair(c, f) for c, f in zip(df["__category"], df["__family"])])
    texts = [build_text(row, cols, args.feature_mode) for _, row in df.iterrows()]
    train, val, test = group_split(df, test_size=.15, val_size=.15)
    vectorizer = FeatureUnion([
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                  min_df=2, max_features=180000, sublinear_tf=True)),
        ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                                  min_df=2, max_features=100000, sublinear_tf=True)),
    ])
    corpus = vectorizer.fit_transform([texts[i] for i in train])
    # FeatureUnion columns can produce norms > 1: explicit cosine normalization.
    from sklearn.preprocessing import normalize
    corpus = normalize(corpus)
    all_rows = []
    candidates = {}
    for subset_name, indices in (("validation", val), ("test", test)):
        print(f"[INFO] retrieving {subset_name}: {len(indices)} queries", flush=True)
        query = normalize(vectorizer.transform([texts[i] for i in indices]))
        for block_start in range(0, len(indices), 128):
            block = query[block_start:block_start + 128]
            sim = linear_kernel(block, corpus)
            k = min(50, len(train))
            top = np.argsort(-sim, axis=1, kind="stable")[:, :k]
            for offset, indexes in enumerate(top):
                source = int(indices[block_start + offset])
                truth = str(labels[source])
                positions = [int(train[x]) for x in indexes]
                candidate_labels = [str(labels[x]) for x in positions]
                top_label = candidate_labels[0]
                tc, tf = split_pair(truth)
                pc, pf = split_pair(top_label)
                row = {
                    "subset": subset_name, "row_index": source,
                    "gold_pair": truth, "tfidf_top1_pair": top_label,
                    "tfidf_pair_correct": score_pair(truth, top_label),
                    "tfidf_category_correct": score_pair(tc, pc),
                    "tfidf_family_correct": score_pair(tf, pf),
                }
                for check_k in (3, 5, 10, 20, 50):
                    row[f"recall_pair_at_{check_k}"] = int(truth in candidate_labels[:check_k])
                all_rows.append(row)
                candidates[source] = positions[:args.topk]
    report = {
        "input": str(args.input), "feature_mode": args.feature_mode,
        "train": len(train), "validation": len(val), "test": len(test),
        "group_overlap_train_validation": len(set(df.iloc[train]["__group"]) & set(df.iloc[val]["__group"])),
        "group_overlap_train_test": len(set(df.iloc[train]["__group"]) & set(df.iloc[test]["__group"])),
        "tfidf": [summarize([r for r in all_rows if r["subset"] == name], name)
                  for name in ("validation", "test")],
    }
    if args.reranker_model:
        from FlagEmbedding import FlagReranker
        print("[INFO] loading reranker", flush=True)
        rr = FlagReranker(args.reranker_model, use_fp16=False, device="cpu")
        for subset_name in ("validation", "test"):
            rows = [r for r in all_rows if r["subset"] == subset_name]
            if args.rerank_limit:
                rows = rows[:args.rerank_limit]
            for num, row in enumerate(rows, 1):
                idx = row["row_index"]
                opts = candidates[idx]
                pairs = [[texts[idx], texts[x]] for x in opts]
                scores = np.asarray(rr.compute_score(pairs, batch_size=args.batch_size,
                                                     max_length=256), dtype=float).reshape(-1)
                best = opts[int(np.argmax(scores))]
                pred = str(labels[best])
                row["rerank_top1_pair"] = pred
                row["rerank_pair_correct"] = score_pair(row["gold_pair"], pred)
                gc, gf = split_pair(row["gold_pair"])
                pc, pf = split_pair(pred)
                row["rerank_category_correct"] = score_pair(gc, pc)
                row["rerank_family_correct"] = score_pair(gf, pf)
                if num % 50 == 0:
                    print(f"[INFO] {subset_name} reranked {num}/{len(rows)}", flush=True)
                    write_results(out, all_rows, report, start)
            report.setdefault("reranker", []).append({
                "subset": subset_name, "n": len(rows),
                **{key: round(sum(r[f"rerank_{key}_correct"] for r in rows) / len(rows), 5)
                   if rows else None for key in ("pair", "category", "family")},
                "tfidf_top1_same_subset": round(sum(r["tfidf_pair_correct"] for r in rows) / len(rows), 5) if rows else None,
                "rerank_topk": args.topk,
            })
            write_results(out, all_rows, report, start)
    write_results(out, all_rows, report, start)
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)

def write_results(out, rows, report, start):
    report["elapsed_seconds"] = round(time.perf_counter() - start, 2)
    (out / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (out / "predictions.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
