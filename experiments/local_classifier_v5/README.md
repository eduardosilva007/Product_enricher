# V5 — Taxonomy Learning Benchmark

## Objective

Measure whether the historical manually classified base can predict the authoritative business taxonomy:

- `Category General`
- `FAMILY`

The benchmark deliberately treats `Category General || FAMILY` as one joint target. This prevents a model from producing a Category/FAMILY combination that never existed in the Gold data.

This experiment is offline. It does not call JEV/Clef, does not search the web, does not modify the production pipeline, and does not use GLiNER in the first baseline.

## Why TYPE and power are not Gold targets

The historical manual review focused on Category General and FAMILY. Therefore TYPE, power and other derived classification fields are not considered authoritative labels in this benchmark and are not used as default input features.

## Candidates

1. `tfidf`: character + word TF-IDF with calibrated LinearSVC.
2. `small`: multilingual MiniLM sentence embeddings + LogisticRegression.
3. `bge`: local BGE-M3 dense embeddings + LogisticRegression.

All candidates use the same train/validation/test split.

## Feature modes

- `brand_model`: Marca + Modelo only. This is the safest deployment baseline.
- `brand_model_title`: Marca + Modelo + Título Real. This measures the incremental value of a title when that title is available before classification.

Do not interpret `brand_model_title` as production-safe if Título Real is only created after manual classification.

## Leakage control

The split is group-based, not row-random. Obvious variants of the same Brand/Model family are kept in the same split. Exact Brand+Model duplicates are deduplicated. Brand+Model rows with conflicting Category/FAMILY labels are excluded and exported to `Label_Conflicts`.

## Metrics

Primary:

- Joint Pair Top-1 Accuracy
- Category Accuracy
- FAMILY Accuracy
- Joint Pair Top-3 Recall
- Joint Pair Top-5 Recall
- Macro F1

Automation gate:

- Precision and coverage at target precision 95%, 97%, 98%, 99%
- Thresholds are chosen on VALIDATION and measured on TEST.

This distinction matters:

- `AUTO classifier`: how much can be safely closed automatically?
- `Candidate generator`: how often does the correct Category/FAMILY pair appear in Top-3/Top-5 even when Top-1 cannot be trusted?

A candidate can therefore be useful for JEV even if it is not safe enough for automatic closure.

## Recommended first run

First audit the dataset only:

```powershell
C:\p\venv\Scripts\python.exe experiments\local_classifier_v5\benchmark_taxonomy.py `
  --input "<ARQUIVO_CLASSIFICADO.xlsx>" `
  --output "V5_Taxonomy_Audit.xlsx" `
  --audit-only
```

Check `Usable Gold Rows`, label conflicts, taxonomy distribution, and that group leakage is zero.

Then run the three candidates overnight:

```powershell
C:\p\venv\Scripts\python.exe experiments\local_classifier_v5\benchmark_taxonomy.py `
  --input "<ARQUIVO_CLASSIFICADO.xlsx>" `
  --output "V5_Taxonomy_Benchmark.xlsx" `
  --candidates "tfidf,small,bge" `
  --feature-modes "brand_model,brand_model_title" `
  --bge-model "C:\p\models\bge-m3"
```

Embeddings are cached under `.cache/v5_taxonomy`, so reruns do not recompute an unchanged candidate/feature dataset.

If the small multilingual model is not already local and the corporate network blocks model download, rerun with `--candidates "tfidf,bge"`; the benchmark remains valid for those candidates. The small embedding candidate can be added later with a local model path via `--small-model`.

## Go/no-go criteria for the first experiment

The initial goal is not 100% automatic classification. The useful threshold is approximately:

- AUTO precision >= 98%
- AUTO coverage >= 30–35%
- Top-3 candidate recall materially higher than Top-1 accuracy
- zero train/test group leakage

If a candidate reaches high Top-3/Top-5 recall but low safe AUTO coverage, it is still valuable as a closed candidate generator for JEV.

## GLiNER

GLiNER is intentionally excluded from this first benchmark. After the baseline is measured, it can be tested as an ablation only on uncertain cases. That tells us whether GLiNER actually increases candidate recall/automation coverage enough to justify its CPU cost.
