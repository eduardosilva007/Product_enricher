#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.pipeline import FeatureUnion
    from sklearn.svm import LinearSVC
    from sklearn.linear_model import LogisticRegression
except ImportError as exc:
    raise SystemExit(
        "scikit-learn ausente. Instale requirements-v5.txt antes do benchmark."
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
PAIR_SEP = " || "
RANDOM_STATE = 42
TARGET_PRECISIONS = (0.95, 0.97, 0.98, 0.99)
DEFAULT_NORMALIZATION = Path(__file__).with_name("taxonomy_normalization.json")


def safe(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", safe(value))
        if not unicodedata.combining(ch)
    )


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(value).upper()).strip()


def compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", norm(value))


def find_col(df: pd.DataFrame, aliases: Sequence[str], required: bool = True) -> str:
    lookup = {norm(c): c for c in df.columns}
    for alias in aliases:
        hit = lookup.get(norm(alias))
        if hit is not None:
            return hit
    if required:
        raise KeyError(f"Coluna ausente. Esperado uma de: {aliases}")
    return ""


def model_family_core(model: str) -> str:
    """Conservative family key to keep obvious model variants in the same split."""
    raw = norm(model)
    tokens = re.findall(r"[A-Z0-9]+", raw)
    generic = {
        "PLUS", "SOLO", "KIT", "SET", "EU", "US", "UK", "BR", "AR",
        "220V", "110V", "127V", "230V", "240V", "18V", "20V", "36V",
    }
    tokens = [t for t in tokens if t not in generic]
    if not tokens:
        return compact(model)[:16] or "UNKNOWN"

    mixed = [t for t in tokens if re.search(r"[A-Z]", t) and re.search(r"\d", t)]
    if mixed:
        return mixed[0][:20]

    if tokens[0].isdigit() and len(tokens) > 1:
        return (tokens[0] + tokens[1])[:20]
    return tokens[0][:20]


def build_text(row: pd.Series, cols: Dict[str, str], mode: str) -> str:
    parts = [
        f"BRAND: {safe(row.get(cols['brand']))}",
        f"MODEL: {safe(row.get(cols['model']))}",
    ]
    if mode == "brand_model_title" and cols.get("title"):
        title = safe(row.get(cols["title"]))
        if title:
            parts.append(f"TITLE: {title}")
    return "\n".join(parts)


def label_pair(category: str, family: str) -> str:
    return f"{safe(category)}{PAIR_SEP}{safe(family)}"


def split_pair(value: str) -> Tuple[str, str]:
    if PAIR_SEP not in safe(value):
        return "", ""
    return tuple(safe(x) for x in value.split(PAIR_SEP, 1))  # type: ignore[return-value]

def load_taxonomy_normalization(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"pair_aliases": [], "catchall_pairs": [["OTHERS", "OTHERS"]]}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def taxonomy_alias_map(config: Dict[str, object]) -> Dict[Tuple[str, str], Tuple[str, str]]:
    out: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for item in config.get("pair_aliases", []) or []:
        if not isinstance(item, dict):
            continue
        source = item.get("from") or []
        target = item.get("to") or []
        if len(source) >= 2 and len(target) >= 2:
            out[(safe(source[0]), safe(source[1]))] = (safe(target[0]), safe(target[1]))
    return out


def canonical_pair(category: str, family: str, aliases: Dict[Tuple[str, str], Tuple[str, str]]) -> Tuple[str, str]:
    pair = (" ".join(safe(category).split()), " ".join(safe(family).split()))
    return aliases.get(pair, pair)


def catchall_labels(config: Dict[str, object]) -> set[str]:
    out: set[str] = set()
    for pair in config.get("catchall_pairs", []) or []:
        if isinstance(pair, list) and len(pair) >= 2:
            out.add(label_pair(safe(pair[0]), safe(pair[1])))
    return out or {label_pair("OTHERS", "OTHERS")}



@dataclass
class DatasetBundle:
    df: pd.DataFrame
    conflicts: pd.DataFrame
    cols: Dict[str, str]


def load_gold(path: Path, normalization: Dict[str, object]) -> DatasetBundle:
    df = pd.read_excel(path, dtype=object)
    cols = {
        "brand": find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"]),
        "model": find_col(df, ["Modelo", "Model", "SKU", "Part Number"]),
        "category": find_col(df, ["Category General", "Category", "Categoria Geral"]),
        "family": find_col(df, ["FAMILY", "Family", "Familia", "Família"]),
        "title": find_col(df, ["Título Real", "Titulo Real", "Real Title", "Product Title"], required=False),
    }

    work = df.copy()
    work["__brand"] = work[cols["brand"]].map(safe)
    work["__model"] = work[cols["model"]].map(safe)
    work["__raw_category"] = work[cols["category"]].map(safe)
    work["__raw_family"] = work[cols["family"]].map(safe)
    aliases = taxonomy_alias_map(normalization)
    canonical = [
        canonical_pair(c, f, aliases)
        for c, f in zip(work["__raw_category"], work["__raw_family"])
    ]
    work["__category"] = [x[0] for x in canonical]
    work["__family"] = [x[1] for x in canonical]
    work["__taxonomy_normalized"] = [
        "SIM" if (safe(rc), safe(rf)) != (c, f) else "NÃO"
        for rc, rf, (c, f) in zip(
            work["__raw_category"], work["__raw_family"], canonical
        )
    ]
    work = work[
        (work["__brand"] != "")
        & (work["__model"] != "")
        & (work["__category"] != "")
        & (work["__family"] != "")
    ].copy()
    work["__key"] = work["__brand"].map(compact) + "|" + work["__model"].map(compact)
    work["__label"] = [
        label_pair(c, f) for c, f in zip(work["__category"], work["__family"])
    ]

    label_counts = work.groupby("__key")["__label"].nunique()
    conflicting_keys = set(label_counts[label_counts > 1].index)
    conflicts = work[work["__key"].isin(conflicting_keys)].copy()
    if conflicting_keys:
        work = work[~work["__key"].isin(conflicting_keys)].copy()

    work = work.drop_duplicates(subset=["__key", "__label"], keep="first").reset_index(drop=True)
    work["__group"] = [
        compact(b) + "|" + model_family_core(m)
        for b, m in zip(work["__brand"], work["__model"])
    ]
    return DatasetBundle(work, conflicts, cols)


def group_split(df: pd.DataFrame, test_size: float, val_size: float):
    groups = df["__group"].to_numpy()
    idx = np.arange(len(df))
    outer = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=RANDOM_STATE)
    train_val_idx, test_idx = next(outer.split(idx, df["__label"], groups))

    train_val = df.iloc[train_val_idx]
    relative_val = val_size / (1.0 - test_size)
    inner = GroupShuffleSplit(n_splits=1, test_size=relative_val, random_state=RANDOM_STATE + 1)
    tv_idx = np.arange(len(train_val))
    train_idx_rel, val_idx_rel = next(
        inner.split(tv_idx, train_val["__label"], train_val["__group"])
    )
    train_idx = train_val_idx[train_idx_rel]
    val_idx = train_val_idx[val_idx_rel]
    return train_idx, val_idx, test_idx


def dataset_fingerprint(texts: Sequence[str], labels: Sequence[str]) -> str:
    h = hashlib.sha256()
    for text, label in zip(texts, labels):
        h.update(text.encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update(label.encode("utf-8", errors="ignore"))
        h.update(b"\n")
    return h.hexdigest()[:20]


def topk_indices(proba: np.ndarray, k: int) -> np.ndarray:
    k = max(1, min(k, proba.shape[1]))
    part = np.argpartition(-proba, kth=k - 1, axis=1)[:, :k]
    vals = np.take_along_axis(proba, part, axis=1)
    order = np.argsort(-vals, axis=1)
    return np.take_along_axis(part, order, axis=1)


def prob_outputs(model, X):
    proba = model.predict_proba(X)
    classes = np.asarray(model.classes_)
    rank5 = topk_indices(proba, min(5, len(classes)))
    preds = classes[rank5[:, 0]]
    conf = proba[np.arange(len(proba)), rank5[:, 0]]
    if proba.shape[1] > 1:
        second = proba[np.arange(len(proba)), rank5[:, 1]]
    else:
        second = np.zeros(len(proba))
    margin = conf - second
    return proba, classes, rank5, preds, conf, margin


def candidate_recall(y_true: np.ndarray, classes: np.ndarray, rank: np.ndarray, k: int) -> float:
    k = min(k, rank.shape[1])
    hits = []
    for i, truth in enumerate(y_true):
        candidates = set(classes[rank[i, :k]])
        hits.append(truth in candidates)
    return float(np.mean(hits)) if hits else 0.0


def threshold_from_validation(y_true: np.ndarray, pred: np.ndarray, conf: np.ndarray, target: float):
    order = np.argsort(-conf)
    correct = (pred[order] == y_true[order]).astype(float)
    csum = np.cumsum(correct)
    n = np.arange(1, len(order) + 1)
    precision = csum / n
    eligible = np.where(precision >= target)[0]
    if len(eligible) == 0:
        return None
    pos = int(eligible[-1])
    return float(conf[order[pos]])


def apply_threshold(y_true: np.ndarray, pred: np.ndarray, conf: np.ndarray, threshold):
    if threshold is None:
        return {"threshold": None, "selected": 0, "coverage": 0.0, "precision": None}
    mask = conf >= threshold
    selected = int(mask.sum())
    if selected == 0:
        return {"threshold": threshold, "selected": 0, "coverage": 0.0, "precision": None}
    return {
        "threshold": threshold,
        "selected": selected,
        "coverage": float(mask.mean()),
        "precision": float(np.mean(pred[mask] == y_true[mask])),
    }


def per_class_table(y_true: np.ndarray, pred: np.ndarray) -> pd.DataFrame:
    labels = sorted(set(y_true) | set(pred))
    rows = []
    for label in labels:
        mask = y_true == label
        n = int(mask.sum())
        if n == 0:
            continue
        rows.append({
            "Gold Pair": label,
            "N": n,
            "Top1 Accuracy": float(np.mean(pred[mask] == y_true[mask])),
        })
    return pd.DataFrame(rows).sort_values(["N", "Gold Pair"], ascending=[False, True])


class LinearSVCScoreModel:
    """LinearSVC with normalized ranking scores.

    We intentionally do not use CalibratedClassifierCV here because the reviewed
    taxonomy has a large long tail with classes that may have fewer than three
    training examples. AUTO thresholds are learned on the held-out validation
    split and then measured on TEST, so absolute probability calibration is not
    required for a valid precision/coverage gate.
    """

    def __init__(self):
        self.base = LinearSVC(C=2.0, class_weight="balanced")

    @property
    def classes_(self):
        return self.base.classes_

    def fit(self, X, y):
        self.base.fit(X, y)
        return self

    def predict_proba(self, X):
        scores = np.asarray(self.base.decision_function(X), dtype=np.float64)
        if scores.ndim == 1:
            scores = np.column_stack([-scores, scores])
        scores -= scores.max(axis=1, keepdims=True)
        exp_scores = np.exp(np.clip(scores, -50.0, 50.0))
        denom = exp_scores.sum(axis=1, keepdims=True)
        return exp_scores / np.maximum(denom, 1e-12)


class TfidfSVC:
    def __init__(self):
        self.vectorizer = FeatureUnion([
            ("char", TfidfVectorizer(
                analyzer="char_wb", ngram_range=(3, 5), min_df=2,
                max_features=180000, sublinear_tf=True,
            )),
            ("word", TfidfVectorizer(
                analyzer="word", ngram_range=(1, 2), min_df=2,
                max_features=100000, sublinear_tf=True,
            )),
        ])
        self.model = LinearSVCScoreModel()

    def fit(self, texts, labels):
        X = self.vectorizer.fit_transform(texts)
        self.model.fit(X, labels)
        return self

    def transform(self, texts):
        return self.vectorizer.transform(texts)


class EmbeddingLogReg:
    def __init__(self):
        self.model = LogisticRegression(
            max_iter=2500,
            class_weight="balanced",
            solver="lbfgs",
        )

    def fit(self, X, labels):
        self.model.fit(X, labels)
        return self


class EmbeddingCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def path(self, candidate: str, mode: str, model_id: str, fingerprint: str) -> Path:
        sig = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
        return self.cache_dir / f"{candidate}_{mode}_{sig}_{fingerprint}.npz"

    def load(self, path: Path):
        if not path.exists():
            return None
        data = np.load(path)
        return data["embeddings"]

    def save(self, path: Path, embeddings: np.ndarray):
        np.savez_compressed(path, embeddings=np.asarray(embeddings, dtype=np.float32))


def encode_small(texts: Sequence[str], model_name: str, batch_size: int, local_only: bool):
    from sentence_transformers import SentenceTransformer
    kwargs = {"device": "cpu"}
    if local_only:
        kwargs["local_files_only"] = True
    model = SentenceTransformer(model_name, **kwargs)
    return model.encode(
        list(texts),
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )


def encode_bge(texts: Sequence[str], model_path: str, batch_size: int):
    from FlagEmbedding import BGEM3FlagModel
    model = BGEM3FlagModel(model_path, use_fp16=False, device="cpu")
    chunks: List[np.ndarray] = []
    chunk_size = max(batch_size * 8, 128)
    for start in range(0, len(texts), chunk_size):
        part = list(texts[start:start + chunk_size])
        out = model.encode(
            part,
            batch_size=batch_size,
            max_length=256,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        chunks.append(np.asarray(out["dense_vecs"], dtype=np.float32))
    return np.vstack(chunks)


def evaluate_candidate(
    candidate: str,
    mode: str,
    texts: Sequence[str],
    labels: np.ndarray,
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    test_idx: np.ndarray,
    args,
    cache: EmbeddingCache,
):
    started = time.perf_counter()
    y_train, y_val, y_test = labels[train_idx], labels[val_idx], labels[test_idx]

    if candidate == "tfidf":
        estimator = TfidfSVC().fit([texts[i] for i in train_idx], y_train)
        X_val = estimator.transform([texts[i] for i in val_idx])
        X_test = estimator.transform([texts[i] for i in test_idx])
        model = estimator.model
    else:
        fp = dataset_fingerprint(texts, labels)
        if candidate == "small":
            model_id = args.small_model
            emb_path = cache.path(candidate, mode, model_id, fp)
            embeddings = cache.load(emb_path)
            if embeddings is None:
                embeddings = encode_small(
                    texts, model_id, args.embedding_batch_size, args.small_local_only
                )
                cache.save(emb_path, embeddings)
        elif candidate == "bge":
            model_id = args.bge_model
            emb_path = cache.path(candidate, mode, model_id, fp)
            embeddings = cache.load(emb_path)
            if embeddings is None:
                embeddings = encode_bge(texts, model_id, args.embedding_batch_size)
                cache.save(emb_path, embeddings)
        else:
            raise ValueError(candidate)

        estimator = EmbeddingLogReg().fit(embeddings[train_idx], y_train)
        model = estimator.model
        X_val = embeddings[val_idx]
        X_test = embeddings[test_idx]

    _, classes, _, val_pred, val_conf, _ = prob_outputs(model, X_val)
    test_proba, classes2, test_rank, test_pred, test_conf, test_margin = prob_outputs(model, X_test)
    assert np.array_equal(classes, classes2)

    metrics = {
        "Candidate": candidate,
        "Feature Mode": mode,
        "Train N": len(train_idx),
        "Validation N": len(val_idx),
        "Test N": len(test_idx),
        "Classes Train": len(set(y_train)),
        "Unseen Test Pair %": float(np.mean(~np.isin(y_test, np.unique(y_train)))) if len(y_test) else 0.0,
        "Pair Top1 Accuracy": float(accuracy_score(y_test, test_pred)),
        "Pair Macro F1": float(f1_score(y_test, test_pred, average="macro", zero_division=0)),
        "Pair Top3 Recall": candidate_recall(y_test, classes, test_rank, 3),
        "Pair Top5 Recall": candidate_recall(y_test, classes, test_rank, 5),
        "Median Confidence": float(np.median(test_conf)),
        "Median Margin": float(np.median(test_margin)),
        "Seconds": round(time.perf_counter() - started, 2),
    }

    catchall_set = set(getattr(args, "_catchall_labels", set()))
    actionable_test_mask = np.array([x not in catchall_set for x in y_test], dtype=bool)
    if actionable_test_mask.any():
        metrics["Actionable Test N"] = int(actionable_test_mask.sum())
        metrics["Actionable Top1 Accuracy"] = float(
            np.mean(test_pred[actionable_test_mask] == y_test[actionable_test_mask])
        )
        metrics["Actionable Top3 Recall"] = candidate_recall(
            y_test[actionable_test_mask], classes, test_rank[actionable_test_mask], 3
        )
        metrics["Actionable Top5 Recall"] = candidate_recall(
            y_test[actionable_test_mask], classes, test_rank[actionable_test_mask], 5
        )
    else:
        metrics["Actionable Test N"] = 0
        metrics["Actionable Top1 Accuracy"] = 0.0
        metrics["Actionable Top3 Recall"] = 0.0
        metrics["Actionable Top5 Recall"] = 0.0

    cat_true = np.array([split_pair(x)[0] for x in y_test])
    fam_true = np.array([split_pair(x)[1] for x in y_test])
    cat_pred = np.array([split_pair(x)[0] for x in test_pred])
    fam_pred = np.array([split_pair(x)[1] for x in test_pred])
    metrics["Category Accuracy"] = float(np.mean(cat_true == cat_pred))
    metrics["FAMILY Accuracy"] = float(np.mean(fam_true == fam_pred))

    coverage_rows = []
    validation_non_catchall = np.array([p not in catchall_set for p in val_pred], dtype=bool)
    test_non_catchall = np.array([p not in catchall_set for p in test_pred], dtype=bool)
    for target in TARGET_PRECISIONS:
        threshold = threshold_from_validation(y_val, val_pred, val_conf, target)
        out = apply_threshold(y_test, test_pred, test_conf, threshold)

        actionable_threshold = threshold_from_validation(
            y_val[validation_non_catchall],
            val_pred[validation_non_catchall],
            val_conf[validation_non_catchall],
            target,
        ) if validation_non_catchall.any() else None
        actionable_mask = test_non_catchall & (
            test_conf >= actionable_threshold if actionable_threshold is not None else False
        )
        actionable_selected = int(actionable_mask.sum())
        actionable_precision = (
            float(np.mean(test_pred[actionable_mask] == y_test[actionable_mask]))
            if actionable_selected else None
        )
        actionable_coverage = float(actionable_selected / max(1, len(y_test)))

        coverage_rows.append({
            "Candidate": candidate,
            "Feature Mode": mode,
            "Target Precision": target,
            "Threshold from Validation": out["threshold"],
            "Test Selected": out["selected"],
            "Test Coverage": out["coverage"],
            "Test Precision": out["precision"],
            "Actionable Threshold": actionable_threshold,
            "Actionable Selected": actionable_selected,
            "Actionable Coverage": actionable_coverage,
            "Actionable Precision": actionable_precision,
        })
        metrics[f"Coverage@{int(target*100)}"] = out["coverage"]
        metrics[f"Precision@{int(target*100)}"] = out["precision"]
        metrics[f"Actionable Coverage@{int(target*100)}"] = actionable_coverage
        metrics[f"Actionable Precision@{int(target*100)}"] = actionable_precision

    pred_rows = []
    for local_i, global_i in enumerate(test_idx):
        ranks = test_rank[local_i]
        ranked_labels = [safe(classes[j]) for j in ranks[:5]]
        row = {
            "Source Row": int(global_i),
            "Gold Pair": y_test[local_i],
            "Predicted Pair": test_pred[local_i],
            "Correct": "SIM" if test_pred[local_i] == y_test[local_i] else "NÃO",
            "Confidence": float(test_conf[local_i]),
            "Margin": float(test_margin[local_i]),
        }
        for k, label in enumerate(ranked_labels, 1):
            row[f"Top{k} Candidate"] = label
            row[f"Top{k} Probability"] = float(test_proba[local_i, ranks[k-1]])
        pred_rows.append(row)

    return (
        metrics,
        pd.DataFrame(coverage_rows),
        pd.DataFrame(pred_rows),
        per_class_table(y_test, test_pred).assign(Candidate=candidate, **{"Feature Mode": mode}),
    )


def main() -> int:
    p = argparse.ArgumentParser(description="V5 offline benchmark for Category General + FAMILY")
    p.add_argument("--input", required=True, help="Workbook containing manually classified historical rows")
    p.add_argument("--output", default="V5_Taxonomy_Benchmark.xlsx")
    p.add_argument("--cache-dir", default=".cache/v5_taxonomy")
    p.add_argument("--candidates", default="tfidf,small,bge")
    p.add_argument("--feature-modes", default="brand_model,brand_model_title")
    p.add_argument("--small-model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    p.add_argument("--small-local-only", action="store_true")
    p.add_argument("--bge-model", default=r"C:\p\models\bge-m3")
    p.add_argument("--embedding-batch-size", type=int, default=32)
    p.add_argument("--test-size", type=float, default=0.15)
    p.add_argument("--val-size", type=float, default=0.15)
    p.add_argument("--audit-only", action="store_true")
    p.add_argument(
        "--taxonomy-normalization",
        default=str(DEFAULT_NORMALIZATION),
        help="JSON com aliases conservadores de Category General + FAMILY",
    )
    args = p.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = ROOT / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    normalization_path = Path(args.taxonomy_normalization)
    if not normalization_path.is_absolute():
        normalization_path = ROOT / normalization_path
    normalization = load_taxonomy_normalization(normalization_path)

    bundle = load_gold(input_path, normalization)
    df = bundle.df
    catchalls = catchall_labels(normalization)
    if len(df) < 100:
        raise RuntimeError(f"Poucos registros classificados utilizáveis: {len(df)}")

    train_idx, val_idx, test_idx = group_split(df, args.test_size, args.val_size)
    split_names = np.full(len(df), "TRAIN", dtype=object)
    split_names[val_idx] = "VALIDATION"
    split_names[test_idx] = "TEST"
    df = df.copy()
    df["__split"] = split_names

    raw_input_rows = len(pd.read_excel(input_path, dtype=object))
    raw_pair_count = bundle.df[["__raw_category", "__raw_family"]].drop_duplicates().shape[0]
    normalized_rows = int((bundle.df["__taxonomy_normalized"] == "SIM").sum())
    catchall_rows = int(bundle.df["__label"].isin(catchalls).sum())
    taxonomy_counts_full = bundle.df["__label"].value_counts()
    audit = pd.DataFrame([{
        "Input Rows": raw_input_rows,
        "Usable Gold Rows": len(df),
        "Conflicting Brand+Model Keys Excluded": bundle.conflicts["__key"].nunique() if len(bundle.conflicts) else 0,
        "Conflicting Rows Excluded": len(bundle.conflicts),
        "Taxonomy Rows Canonicalized": normalized_rows,
        "Raw Taxonomy Pairs": raw_pair_count,
        "Canonical Taxonomy Pairs": df["__label"].nunique(),
        "Catchall Rows": catchall_rows,
        "Catchall %": catchall_rows / max(1, len(df)),
        "Singleton Classes": int((taxonomy_counts_full == 1).sum()),
        "Classes <=5 Examples": int((taxonomy_counts_full <= 5).sum()),
        "Unique Groups": df["__group"].nunique(),
        "Train Rows": int((df["__split"] == "TRAIN").sum()),
        "Validation Rows": int((df["__split"] == "VALIDATION").sum()),
        "Test Rows": int((df["__split"] == "TEST").sum()),
        "Group Leakage Train/Test": len(set(df.loc[df["__split"] == "TRAIN", "__group"]) & set(df.loc[df["__split"] == "TEST", "__group"])),
        "Group Leakage Train/Validation": len(set(df.loc[df["__split"] == "TRAIN", "__group"]) & set(df.loc[df["__split"] == "VALIDATION", "__group"])),
    }])

    split_export_cols = [
        "__brand", "__model",
        "__raw_category", "__raw_family",
        "__category", "__family", "__taxonomy_normalized",
        "__label", "__group", "__split",
    ]

    if args.audit_only:
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            audit.to_excel(writer, sheet_name="Audit", index=False)
            df[split_export_cols].to_excel(writer, sheet_name="Split", index=False)
            bundle.conflicts.to_excel(writer, sheet_name="Label_Conflicts", index=False)
        print(audit.to_string(index=False))
        print(f"RESULTADO: {output_path}")
        return 0

    candidates = [x.strip().lower() for x in args.candidates.split(",") if x.strip()]
    modes = [x.strip().lower() for x in args.feature_modes.split(",") if x.strip()]
    allowed_candidates = {"tfidf", "small", "bge"}
    allowed_modes = {"brand_model", "brand_model_title"}
    bad = set(candidates) - allowed_candidates
    if bad:
        raise ValueError(f"Candidatos inválidos: {sorted(bad)}")
    bad_modes = set(modes) - allowed_modes
    if bad_modes:
        raise ValueError(f"Feature modes inválidos: {sorted(bad_modes)}")

    cache = EmbeddingCache(cache_dir)
    labels = df["__label"].to_numpy()
    args._catchall_labels = catchalls
    metrics_rows = []
    coverage_parts = []
    prediction_parts = []
    per_class_parts = []
    skipped_rows = []

    for mode in modes:
        texts = [build_text(row, bundle.cols, mode) for _, row in df.iterrows()]
        for candidate in candidates:
            print(f"V5 BENCHMARK | candidate={candidate} | features={mode}")
            try:
                metrics, coverage, predictions, per_class = evaluate_candidate(
                    candidate, mode, texts, labels,
                    train_idx, val_idx, test_idx,
                    args, cache,
                )
                test_meta = df.iloc[test_idx][["__brand", "__model", "__category", "__family", "__group"]].reset_index(drop=True)
                predictions = pd.concat([test_meta, predictions.reset_index(drop=True)], axis=1)
                metrics_rows.append(metrics)
                coverage_parts.append(coverage)
                prediction_parts.append(predictions.assign(Candidate=candidate, **{"Feature Mode": mode}))
                per_class_parts.append(per_class)
                print(
                    f"  pair_acc={metrics['Pair Top1 Accuracy']:.3f} "
                    f"top3={metrics['Pair Top3 Recall']:.3f} "
                    f"top5={metrics['Pair Top5 Recall']:.3f} "
                    f"coverage@98={metrics['Coverage@98']:.3f}"
                )
            except Exception as exc:
                skipped_rows.append({
                    "Candidate": candidate,
                    "Feature Mode": mode,
                    "Error": f"{type(exc).__name__}: {exc}",
                })
                print(f"  SKIPPED/ERROR: {type(exc).__name__}: {exc}")

    metrics_df = pd.DataFrame(metrics_rows)
    coverage_df = pd.concat(coverage_parts, ignore_index=True) if coverage_parts else pd.DataFrame()
    predictions_df = pd.concat(prediction_parts, ignore_index=True) if prediction_parts else pd.DataFrame()
    per_class_df = pd.concat(per_class_parts, ignore_index=True) if per_class_parts else pd.DataFrame()
    skipped_df = pd.DataFrame(skipped_rows)

    taxonomy_counts = (
        df.groupby(["__category", "__family", "__label"])
        .size().reset_index(name="N").sort_values("N", ascending=False)
    )
    taxonomy_counts["Frequency Bucket"] = pd.cut(
        taxonomy_counts["N"],
        bins=[0, 1, 2, 5, 10, 20, 50, float("inf")],
        labels=["1", "2", "3-5", "6-10", "11-20", "21-50", "51+"],
        include_lowest=True,
    ).astype(str)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        audit.to_excel(writer, sheet_name="Audit", index=False)
        metrics_df.to_excel(writer, sheet_name="Model_Summary", index=False)
        coverage_df.to_excel(writer, sheet_name="Auto_Coverage", index=False)
        predictions_df.to_excel(writer, sheet_name="Test_Predictions", index=False)
        per_class_df.to_excel(writer, sheet_name="Per_Class", index=False)
        taxonomy_counts.to_excel(writer, sheet_name="Taxonomy_Distribution", index=False)
        df[split_export_cols].to_excel(writer, sheet_name="Split", index=False)
        bundle.conflicts.to_excel(writer, sheet_name="Label_Conflicts", index=False)
        skipped_df.to_excel(writer, sheet_name="Skipped", index=False)

    json_path = output_path.with_suffix(".json")
    payload = {
        "audit": audit.to_dict(orient="records")[0],
        "models": metrics_df.to_dict(orient="records"),
        "skipped": skipped_df.to_dict(orient="records"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("=" * 88)
    if len(metrics_df):
        show = [
            "Candidate", "Feature Mode", "Pair Top1 Accuracy", "Category Accuracy",
            "FAMILY Accuracy", "Pair Top3 Recall", "Pair Top5 Recall",
            "Actionable Top1 Accuracy", "Actionable Top3 Recall", "Actionable Top5 Recall",
            "Actionable Precision@98", "Actionable Coverage@98", "Seconds",
        ]
        print(metrics_df[show].to_string(index=False))
    if len(skipped_df):
        print("\nSKIPPED:")
        print(skipped_df.to_string(index=False))
    print(f"RESULTADO: {output_path}")
    print(f"JSON: {json_path}")
    return 0 if len(metrics_df) else 2


if __name__ == "__main__":
    raise SystemExit(main())
