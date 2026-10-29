#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local BGE-M3 semantic provider for V4.2."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

import classification_engine_v42 as v42
import pipeline_produtos as pp


DEFAULT_WEIGHTS = (0.40, 0.20, 0.40)


class BGEHybridSemanticProvider:
    name = "BGE_M3_HYBRID"

    def __init__(self, rules: Any, cfg: Dict[str, Any], top_k: int = 12):
        self.rules = rules
        self.cfg = cfg
        self.top_k = max(4, int(top_k))
        fcfg = cfg.get("semantic_funnel", {}) or {}
        self.embedding_model_name = pp.safe(fcfg.get("embedding_model")) or "BAAI/bge-m3"
        self.reranker_model_name = pp.safe(fcfg.get("reranker_model")) or "BAAI/bge-reranker-v2-m3"
        self.embedding_device = pp.safe(fcfg.get("embedding_device")) or "cpu"
        self.reranker_device_cfg = pp.safe(fcfg.get("reranker_device")) or "auto"
        self.rerank_batch_size = max(1, int(fcfg.get("rerank_batch_size", 8) or 8))
        self.rerank_max_length = max(512, int(fcfg.get("rerank_max_length", 512) or 512))
        self.type_labels = sorted(rules.types)
        self.type_docs = {t: self._type_document(t) for t in self.type_labels}
        self.model = None
        self.doc_repr = None
        self.reranker = None
        self.reranker_tokenizer = None
        self.reranker_device = "cpu"

    def _type_document(self, typ: str) -> str:
        meta = self.rules.types.get(typ, {}) or {}
        aliases = [pp.safe(x) for x in (meta.get("aliases", []) or []) if pp.safe(x)][:24]
        chunks = [f"Canonical physical product type: {typ}."]
        if aliases:
            chunks.append("Known names and aliases: " + "; ".join(aliases) + ".")
        return " ".join(chunks)

    def _normalize_entity_spans(self, spans: Sequence[str]) -> List[str]:
        """Exact lexical normalization outside GLiNER; never changes extractor output."""
        hints: List[str] = []
        for span in spans:
            folded = pp.strip_accents(pp.safe(span)).casefold()
            for typ, meta in self.rules.types.items():
                positives = [typ] + list((meta or {}).get("aliases", []) or [])
                negatives = list((meta or {}).get("negative_aliases", []) or [])
                if negatives and any(pp.contains_alias_prepared(folded, x) for x in negatives):
                    continue
                if any(pp.contains_alias_prepared(folded, x) for x in positives):
                    hints.append(typ)
        return sorted(set(hints))

    def _query(self, ctx: v42.ClassificationContext) -> Tuple[str, List[str]]:
        chunks = [pp.safe(ctx.semantic_text)]
        normalized: List[str] = []
        entities = ctx.extracted_entities
        if entities is not None:
            if entities.product_type:
                chunks.append(
                    "EXTRACTED PRODUCT TYPE SPANS: " + " | ".join(entities.product_type[:12])
                )
                normalized = self._normalize_entity_spans(entities.product_type)
            if entities.power_source:
                chunks.append(
                    "EXTRACTED POWER SOURCE SPANS: " + " | ".join(entities.power_source[:8])
                )
            if entities.voltage:
                chunks.append("EXTRACTED VOLTAGE SPANS: " + " | ".join(entities.voltage[:8]))
        return "\n\n".join(x for x in chunks if x), normalized

    def load(self) -> None:
        if self.model is not None:
            return
        try:
            import torch
            from FlagEmbedding import BGEM3FlagModel
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "BGE_V42_DEPENDENCY_MISSING: FlagEmbedding + transformers"
            ) from exc

        use_fp16 = bool(self.embedding_device.startswith("cuda") and torch.cuda.is_available())
        kwargs: Dict[str, Any] = {"use_fp16": use_fp16}
        if self.embedding_device in {"cpu", "cuda"}:
            kwargs["devices"] = [self.embedding_device]
        self.model = BGEM3FlagModel(self.embedding_model_name, **kwargs)

        docs = [self.type_docs[x] for x in self.type_labels]
        self.doc_repr = self.model.encode(
            docs,
            batch_size=16,
            max_length=256,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=True,
        )

        self.reranker_tokenizer = AutoTokenizer.from_pretrained(
            self.reranker_model_name, local_files_only=True
        )
        self.reranker = AutoModelForSequenceClassification.from_pretrained(
            self.reranker_model_name, local_files_only=True
        )
        if self.reranker_device_cfg in {"auto", "cuda"} and torch.cuda.is_available():
            self.reranker_device = "cuda"
        self.reranker.to(self.reranker_device)
        self.reranker.eval()

    def _hybrid(self, query: str) -> Tuple[Dict[str, Dict[str, float]], float]:
        self.load()
        started = time.monotonic()
        q = self.model.encode(
            [query],
            batch_size=1,
            max_length=512,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=True,
        )
        q_dense = q["dense_vecs"][0]
        q_sparse = q["lexical_weights"][0]
        q_colbert = q["colbert_vecs"][0]
        out: Dict[str, Dict[str, float]] = {}
        wd, ws, wc = DEFAULT_WEIGHTS
        for i, typ in enumerate(self.type_labels):
            dense = float(np.dot(q_dense, self.doc_repr["dense_vecs"][i]))
            sparse = float(
                self.model.compute_lexical_matching_score(
                    q_sparse, self.doc_repr["lexical_weights"][i]
                )
            )
            colbert = float(
                self.model.colbert_score(
                    q_colbert, self.doc_repr["colbert_vecs"][i]
                )
            )
            out[typ] = {
                "dense": dense,
                "sparse": sparse,
                "colbert": colbert,
                "hybrid": wd * dense + ws * sparse + wc * colbert,
            }
        return out, time.monotonic() - started

    def _rerank(
        self, query: str, labels: Sequence[str]
    ) -> Tuple[List[str], Dict[str, float], Dict[str, float], float]:
        import torch

        started = time.monotonic()
        labels = list(labels)
        scores: List[float] = []
        for start in range(0, len(labels), self.rerank_batch_size):
            batch = labels[start:start + self.rerank_batch_size]
            enc = self.reranker_tokenizer(
                [query] * len(batch),
                [self.type_docs[x] for x in batch],
                padding=True,
                truncation=True,
                max_length=self.rerank_max_length,
                return_tensors="pt",
            )
            enc = {k: v.to(self.reranker_device) for k, v in enc.items()}
            with torch.inference_mode():
                logits = self.reranker(**enc, return_dict=True).logits
                vals = (
                    logits[:, -1].float().view(-1)
                    if logits.ndim == 2 and logits.shape[1] > 1
                    else logits.float().view(-1)
                )
            scores.extend(vals.detach().cpu().numpy().astype(float).tolist())

        arr = np.array(scores, dtype=float)
        probs = 1.0 / (1.0 + np.exp(-np.clip(arr, -60.0, 60.0)))
        logits_map = dict(zip(labels, [float(x) for x in scores]))
        prob_map = dict(zip(labels, [float(x) for x in probs.tolist()]))
        ranked = sorted(labels, key=lambda x: (-logits_map[x], x))
        return ranked, logits_map, prob_map, time.monotonic() - started

    def resolve(self, ctx: v42.ClassificationContext) -> v42.SemanticDecision:
        query, normalized_hints = self._query(ctx)
        if not query.strip():
            raise RuntimeError("BGE_V42_EMPTY_QUERY")

        hybrid, bge_seconds = self._hybrid(query)
        hybrid_rank = sorted(
            self.type_labels,
            key=lambda x: (-hybrid[x]["hybrid"], x),
        )

        pool: List[str] = []
        # Exact normalizer hints are guaranteed a seat in the reranker pool,
        # but they do not close the decision themselves.
        for typ in normalized_hints + hybrid_rank:
            if typ in self.rules.types and typ not in pool:
                pool.append(typ)
            if len(pool) >= self.top_k:
                break

        reranked, rerank_logits, rerank_probs, rerank_seconds = self._rerank(query, pool)
        winner = reranked[0]
        runner = reranked[1] if len(reranked) > 1 else ""
        p1 = rerank_probs.get(winner, 0.0)
        p2 = rerank_probs.get(runner, 0.0)
        h1 = hybrid[winner]["hybrid"]
        h2 = hybrid[runner]["hybrid"] if runner else 0.0

        return v42.SemanticDecision(
            product_type=winner,
            power=pp.safe(getattr(ctx.s1, "power", "")) or "UNKNOWN",
            confidence=p1,
            margin=max(0.0, p1 - p2),
            # Deliberately false until thresholds are calibrated on frozen Gold.
            calibrated_close=False,
            candidates=reranked,
            metadata={
                "BGE Raw Winner": winner,
                "BGE Runner Up": runner,
                "BGE Hybrid Margin": max(0.0, h1 - h2),
                "BGE Dense": hybrid[winner]["dense"],
                "BGE Sparse": hybrid[winner]["sparse"],
                "BGE ColBERT": hybrid[winner]["colbert"],
                "BGE Rerank Logit": rerank_logits.get(winner, 0.0),
                "BGE Seconds": round(bge_seconds, 4),
                "Rerank Seconds": round(rerank_seconds, 4),
                "Entity Normalized Type Hints": " | ".join(normalized_hints),
            },
        )
