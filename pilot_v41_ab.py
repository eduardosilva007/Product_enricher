#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pilot V4.1 A/B: BGE-M3 híbrido sem GLiNER vs com GLiNER.

Este arquivo é deliberadamente separado do pipeline produtivo. Ele:
- usa o MESMO cohort, RAW/cache, regras e evidência para os dois braços;
- fortalece identidade com MARCA+MODELO em campos primários e JSON-LD;
- usa BGE-M3 local como classificador fechado sobre TODOS os PRODUCT_TYPES;
- usa dense + sparse + ColBERT e o reranker local já configurado;
- executa GLiNER apenas no braço B, uma vez por produto;
- NÃO grava Consolidado/Base Final e NÃO chama Search/LLM;
- exporta tempos, candidatos, identidade, acerto contra Gold validado e delta A/B.

Uso:
  python pilot_v41_ab.py --input "Base de Produtos.xlsx" --max-items 50
  python pilot_v41_ab.py --input "Base de Produtos.xlsx" --max-items 50 --gliner-model "C:\\caminho\\gliner2-multi"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests

import pipeline_produtos as pp


PILOT_VERSION = "2026-10-28-v41-bge-hybrid-jsonld-gliner-ab"
DEFAULT_WEIGHTS = (0.40, 0.20, 0.40)
DEFAULT_TOP_K = 8


def _safe(v: Any) -> str:
    return pp.safe(v)


def _norm_tokens(v: Any) -> List[str]:
    return re.findall(r"[a-z0-9]+", pp.strip_accents(_safe(v)).casefold())


def _phrase_tokens_occurs(needle: str, haystack: str) -> bool:
    n = _norm_tokens(needle)
    h = _norm_tokens(haystack)
    if not n or not h or len(n) > len(h):
        return False
    width = len(n)
    return any(h[i:i + width] == n for i in range(0, len(h) - width + 1))


def _brand_primary_occurs(brand: str, title: str, url: str) -> bool:
    if _phrase_tokens_occurs(brand, title):
        return True
    host = urlparse(_safe(url)).netloc
    return _phrase_tokens_occurs(brand, host)


def _model_primary_occurs(model: str, title: str, url: str, cfg: Dict[str, Any]) -> bool:
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    title_match, _ = pp._model_match_in_field(model, title, suffixes)
    url_match, _ = pp._model_match_in_field(model, url, suffixes)
    return bool(title_match or url_match)


def _focused_product_title(title: str, brand: str, model: str, cfg: Dict[str, Any]) -> str:
    raw = _safe(title)
    if not raw or not model:
        return raw
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    segments = [x.strip() for x in re.split(r"\s*[|–—]\s*|\s+-\s+", raw) if x.strip()]
    hit_idx: Optional[int] = None
    for i, seg in enumerate(segments):
        hit, _ = pp._model_match_in_field(model, seg, suffixes)
        if hit:
            hit_idx = i
            break
    if hit_idx is None:
        return raw
    seg = segments[hit_idx]
    reduced = pp.norm_text(seg)
    for token in (brand, model):
        nt = pp.norm_text(token)
        if nt:
            reduced = reduced.replace(nt, " ")
    reduced = re.sub(r"\s+", " ", reduced).strip()
    if len(reduced.split()) >= 2:
        return seg
    if hit_idx + 1 < len(segments) and len(segments[hit_idx + 1]) <= 100:
        return seg + " | " + segments[hit_idx + 1]
    return seg


def _walk_json(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_json(item)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk_json(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json(item)


def _brand_value(value: Any) -> str:
    if isinstance(value, dict):
        return _safe(value.get("name") or value.get("brand"))
    if isinstance(value, list):
        return " | ".join(x for x in (_brand_value(v) for v in value) if x)
    return _safe(value)


def parse_jsonld_products(html: str) -> List[Dict[str, str]]:
    """Extrai apenas Product/IndividualProduct JSON-LD, sem dependência externa."""
    out: List[Dict[str, str]] = []
    pattern = re.compile(
        r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script\s*>",
        flags=re.I | re.S,
    )
    for m in pattern.finditer(html or ""):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            cleaned = re.sub(r"<!--|-->", "", raw).strip()
            try:
                payload = json.loads(cleaned)
            except Exception:
                continue
        for node in _walk_json(payload):
            typ = node.get("@type")
            types = [typ] if isinstance(typ, str) else (typ if isinstance(typ, list) else [])
            types_fold = {str(x).casefold() for x in types}
            if not types_fold.intersection({"product", "individualproduct"}):
                continue
            row = {
                "name": _safe(node.get("name")),
                "brand": _brand_value(node.get("brand")),
                "sku": _safe(node.get("sku")),
                "mpn": _safe(node.get("mpn")),
                "model": _safe(node.get("model")),
                "productID": _safe(node.get("productID")),
                "gtin": _safe(node.get("gtin")),
                "gtin8": _safe(node.get("gtin8")),
                "gtin12": _safe(node.get("gtin12")),
                "gtin13": _safe(node.get("gtin13")),
                "gtin14": _safe(node.get("gtin14")),
                "category": _safe(node.get("category")),
                "description": _safe(node.get("description")),
            }
            if any(row.values()):
                out.append(row)
    # dedupe determinístico
    seen = set()
    clean = []
    for row in out:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            clean.append(row)
    return clean


class JsonLdCache:
    def __init__(self, path: Path, timeout: float = 10.0, max_bytes: int = 1_500_000):
        self.path = path
        self.timeout = timeout
        self.max_bytes = max_bytes
        try:
            self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}

    def get(self, url: str) -> List[Dict[str, str]]:
        url = _safe(url)
        if not url:
            return []
        if url in self.data:
            cached = self.data[url]
            return cached if isinstance(cached, list) else []
        products: List[Dict[str, str]] = []
        try:
            r = requests.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 ProductEnricherPilot/1.0"},
                stream=True,
            )
            if 200 <= r.status_code < 300:
                chunks = []
                size = 0
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    remaining = self.max_bytes - size
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    size += len(chunks[-1])
                    if size >= self.max_bytes:
                        break
                raw = b"".join(chunks)
                encoding = r.encoding or "utf-8"
                html = raw.decode(encoding, errors="replace")
                products = parse_jsonld_products(html)
        except Exception:
            products = []
        self.data[url] = products
        return products

    def flush(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _structured_identity_confirmed(
    brand: str, model: str, products: Sequence[Dict[str, str]], cfg: Dict[str, Any]
) -> bool:
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    variants = {value for value, _ in pp._model_identity_variants(model, suffixes)}
    for row in products:
        b = _safe(row.get("brand"))
        if not b or not (_phrase_tokens_occurs(brand, b) or _phrase_tokens_occurs(b, brand)):
            continue
        ids = [
            _safe(row.get("sku")), _safe(row.get("mpn")), _safe(row.get("model")),
            _safe(row.get("productID")),
        ]
        for value in ids:
            if value and pp.compact(value) in variants:
                return True
    return False


def _structured_text(products: Sequence[Dict[str, str]]) -> str:
    chunks = []
    for row in products[:3]:
        fields = []
        for key in ["name", "brand", "sku", "mpn", "model", "category", "description", "gtin", "gtin13"]:
            value = _safe(row.get(key))
            if value:
                fields.append(f"{key}={value[:400]}")
        if fields:
            chunks.append("JSON-LD PRODUCT: " + " | ".join(fields))
    return "\n".join(chunks)


@dataclass
class EvidenceBundle:
    brand: str
    model: str
    identity: str
    identity_reasons: List[str]
    evidence: str
    source_count: int
    structured_count: int
    stage1_state: str
    stage1_type: str
    stage1_power: str


def build_evidence_bundle(
    brand: str,
    model: str,
    s1: Any,
    cfg: Dict[str, Any],
    jsonld_cache: JsonLdCache,
    max_sources: int = 4,
    source_chars: int = 550,
    fetch_jsonld: bool = True,
) -> EvidenceBundle:
    search_model = pp.derive_search_model(brand, model, cfg)
    ranked = sorted(
        list(s1.sources or []),
        key=lambda s: (
            pp.DeterministicEngine.IDENTITY_RANK.get(_safe(s.relevance), 0),
            int(s.identity_score or 0),
            float(s.source_weight or 0.0),
            float(s.tavily_score or 0.0),
        ),
        reverse=True,
    )[:max_sources]

    identity_reasons: List[str] = []
    chunks = [f"TARGET BRAND: {brand}", f"TARGET MODEL: {model}"]
    structured_count = 0
    primary_confirmed = 0
    structured_confirmed = 0

    for src in ranked:
        title = _focused_product_title(src.title, brand, search_model, cfg)
        strict = _brand_primary_occurs(brand, src.title, src.url) and _model_primary_occurs(
            search_model, src.title, src.url, cfg
        )
        if strict:
            primary_confirmed += 1
            identity_reasons.append(f"S{src.source_id}:BRAND+MODEL_PRIMARY")

        products: List[Dict[str, str]] = []
        if fetch_jsonld and src.url:
            products = jsonld_cache.get(src.url)
            structured_count += len(products)
            if _structured_identity_confirmed(brand, search_model, products, cfg):
                structured_confirmed += 1
                identity_reasons.append(f"S{src.source_id}:JSONLD_BRAND+MODEL")

        chunks.append(
            f"SOURCE {src.source_id} [identity={src.relevance}; current_score={src.identity_score}]\n"
            f"TITLE: {title}\nCONTENT: {_safe(src.content)[:source_chars]}"
        )
        st = _structured_text(products)
        if st:
            chunks.append(st)

    identity = "TARGET_CONFIRMED" if (primary_confirmed or structured_confirmed) else "NOT_CONFIRMED"
    return EvidenceBundle(
        brand=brand,
        model=model,
        identity=identity,
        identity_reasons=list(dict.fromkeys(identity_reasons)),
        evidence="\n\n".join(chunks),
        source_count=len(ranked),
        structured_count=structured_count,
        stage1_state=_safe(s1.state),
        stage1_type=_safe(s1.product_type),
        stage1_power=_safe(s1.power) or "UNKNOWN",
    )


class HybridTypeClassifier:
    def __init__(self, rules: Any, cfg: Dict[str, Any], top_k: int = DEFAULT_TOP_K):
        self.rules = rules
        self.cfg = cfg
        self.top_k = max(3, int(top_k))
        fcfg = cfg.get("semantic_funnel", {}) or {}
        self.embedding_model_name = _safe(fcfg.get("embedding_model")) or "BAAI/bge-m3"
        self.reranker_model_name = _safe(fcfg.get("reranker_model")) or "BAAI/bge-reranker-v2-m3"
        self.embedding_device = _safe(fcfg.get("embedding_device")) or "cpu"
        self.reranker_device_cfg = _safe(fcfg.get("reranker_device")) or "auto"
        self.rerank_batch_size = max(1, int(fcfg.get("rerank_batch_size", 8) or 8))
        self.rerank_max_length = max(128, int(fcfg.get("rerank_max_length", 192) or 192))
        self.type_labels = sorted(rules.types)
        self.type_docs = {t: self._type_document(t) for t in self.type_labels}
        self.model = None
        self.doc_repr = None
        self.reranker = None
        self.reranker_tokenizer = None
        self.reranker_device = "cpu"

    def _type_document(self, typ: str) -> str:
        meta = self.rules.types.get(typ, {}) or {}
        aliases = [_safe(x) for x in (meta.get("aliases", []) or []) if _safe(x)][:24]
        negs = [_safe(x) for x in (meta.get("negative_aliases", []) or []) if _safe(x)][:12]
        pairs = sorted(pp._pair_label(x) for x in pp._taxonomy_pairs_for_type(self.rules, typ))
        chunks = [f"Canonical physical product type: {typ}."]
        if aliases:
            chunks.append("Known names and aliases: " + "; ".join(aliases) + ".")
        if negs:
            chunks.append("Do not confuse with: " + "; ".join(negs) + ".")
        if pairs:
            chunks.append("Business taxonomy mappings: " + "; ".join(pairs) + ".")
        return " ".join(chunks)

    def load(self) -> None:
        if self.model is not None:
            return
        try:
            import torch
            from FlagEmbedding import BGEM3FlagModel
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Piloto requer FlagEmbedding + transformers. Instale FlagEmbedding antes de executar."
            ) from exc

        use_fp16 = bool(self.embedding_device.startswith("cuda") and torch.cuda.is_available())
        devices = [self.embedding_device] if self.embedding_device in {"cpu", "cuda"} else None
        kwargs: Dict[str, Any] = {"use_fp16": use_fp16}
        if devices:
            kwargs["devices"] = devices
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
        if self.reranker_device_cfg == "cuda" and torch.cuda.is_available():
            self.reranker_device = "cuda"
        elif self.reranker_device_cfg == "auto" and torch.cuda.is_available():
            self.reranker_device = "cuda"
        else:
            self.reranker_device = "cpu"
        self.reranker.to(self.reranker_device)
        self.reranker.eval()

    def _hybrid_scores(self, query: str) -> Tuple[Dict[str, Dict[str, float]], float]:
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
        w_dense, w_sparse, w_colbert = DEFAULT_WEIGHTS
        for i, typ in enumerate(self.type_labels):
            dense = float(np.dot(q_dense, self.doc_repr["dense_vecs"][i]))
            sparse = float(self.model.compute_lexical_matching_score(
                q_sparse, self.doc_repr["lexical_weights"][i]
            ))
            colbert = float(self.model.colbert_score(
                q_colbert, self.doc_repr["colbert_vecs"][i]
            ))
            hybrid = w_dense * dense + w_sparse * sparse + w_colbert * colbert
            out[typ] = {
                "dense": dense, "sparse": sparse, "colbert": colbert, "hybrid": hybrid
            }
        return out, time.monotonic() - started

    def _rerank(self, query: str, labels: Sequence[str]) -> Tuple[List[str], Dict[str, float], Dict[str, float], float]:
        import torch
        started = time.monotonic()
        scores: List[float] = []
        labels = list(labels)
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
                if logits.ndim == 2 and logits.shape[1] > 1:
                    vals = logits[:, -1].float().view(-1)
                else:
                    vals = logits.float().view(-1)
            scores.extend(vals.detach().cpu().numpy().astype(float).tolist())

        arr = np.array(scores, dtype=float)
        exp = np.exp(arr - arr.max()) if len(arr) else np.array([])
        probs_arr = exp / exp.sum() if len(exp) and exp.sum() else np.zeros_like(exp)
        logits_map = {label: float(score) for label, score in zip(labels, scores)}
        prob_map = {label: float(prob) for label, prob in zip(labels, probs_arr.tolist())}
        ranked = sorted(labels, key=lambda x: (-logits_map[x], x))
        return ranked, logits_map, prob_map, time.monotonic() - started

    def classify(self, query: str) -> Dict[str, Any]:
        hybrid, bge_seconds = self._hybrid_scores(query)
        hybrid_rank = sorted(
            self.type_labels, key=lambda x: (-hybrid[x]["hybrid"], x)
        )
        pool = hybrid_rank[:self.top_k]
        reranked, rerank_logits, rerank_probs, rerank_seconds = self._rerank(query, pool)
        winner = reranked[0]
        runner = reranked[1] if len(reranked) > 1 else ""
        p1 = rerank_probs.get(winner, 0.0)
        p2 = rerank_probs.get(runner, 0.0)
        h1 = hybrid[winner]["hybrid"]
        h2 = hybrid[runner]["hybrid"] if runner else 0.0
        return {
            "winner": winner,
            "runner_up": runner,
            "rerank_probability": p1,
            "rerank_margin": max(0.0, p1 - p2),
            "hybrid_score": h1,
            "hybrid_margin": max(0.0, h1 - h2),
            "top_candidates": reranked,
            "hybrid_top": hybrid_rank[:self.top_k],
            "winner_dense": hybrid[winner]["dense"],
            "winner_sparse": hybrid[winner]["sparse"],
            "winner_colbert": hybrid[winner]["colbert"],
            "rerank_logit": rerank_logits.get(winner, 0.0),
            "bge_seconds": bge_seconds,
            "rerank_seconds": rerank_seconds,
        }


class OptionalGliner:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None

    def load(self) -> None:
        if self.model is not None:
            return
        if not self.model_name:
            raise RuntimeError(
                "Braço com GLiNER solicitado, mas nenhum --gliner-model/GLINER_MODEL_PATH foi informado."
            )
        try:
            import torch
            try:
                from gliner2 import AutoExtractor
                loader = AutoExtractor
            except ImportError:
                from gliner2 import GLiNER2
                loader = GLiNER2
        except ImportError as exc:
            raise RuntimeError(
                'GLiNER local não disponível. Instale "gliner2[local]" ou use o ambiente antigo do GLiNER.'
            ) from exc
        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self.model = loader.from_pretrained(self.model_name, map_location=device)
        except TypeError:
            self.model = loader.from_pretrained(self.model_name)

    @staticmethod
    def _flatten(obj: Any) -> List[str]:
        vals: List[str] = []
        if isinstance(obj, str):
            if obj.strip():
                vals.append(obj.strip())
        elif isinstance(obj, dict):
            for v in obj.values():
                vals.extend(OptionalGliner._flatten(v))
        elif isinstance(obj, list):
            for v in obj:
                vals.extend(OptionalGliner._flatten(v))
        return list(dict.fromkeys(vals))

    def extract(self, text: str) -> Tuple[str, float]:
        self.load()
        started = time.monotonic()
        labels = ["product type", "power source", "voltage"]
        result: Any = {}
        if hasattr(self.model, "extract_entities"):
            try:
                result = self.model.extract_entities(text[:4000], labels)
            except TypeError:
                schema = {
                    "product_type": "physical product type",
                    "power_source": "primary power source",
                    "voltage": "input voltage",
                }
                result = self.model.extract_entities(text[:4000], schema)
        elif hasattr(self.model, "extract"):
            try:
                schema = self.model.create_schema().entities({
                    "product_type": "physical product type",
                    "power_source": "primary power source",
                    "voltage": "input voltage",
                })
                result = self.model.extract(text[:4000], schema)
            except Exception:
                result = {}
        vals = self._flatten(result)
        elapsed = time.monotonic() - started
        summary = "GLINER EXTRACTED FEATURES: " + " | ".join(vals[:20]) if vals else ""
        return summary, elapsed


def _auto_candidate(
    bundle: EvidenceBundle, pred: Dict[str, Any], pair: Tuple[str, str], rules: Any
) -> Tuple[bool, str]:
    if bundle.identity != "TARGET_CONFIRMED":
        return False, "IDENTITY_NOT_CONFIRMED"
    if pair not in rules.valid_pairs or pair == ("OTHERS", "OTHERS"):
        return False, "TAXONOMY_NOT_CLOSED"
    if pred["rerank_probability"] < 0.55:
        return False, "RERANK_PROB_LOW"
    if pred["rerank_margin"] < 0.15:
        return False, "RERANK_MARGIN_LOW"
    if pred["hybrid_margin"] < 0.02:
        return False, "HYBRID_MARGIN_LOW"
    return True, "PILOT_LOCAL_GATE_OK"


def _validated_gold_map(path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if not path or not path.exists():
        return {}
    df = pd.read_excel(path, dtype=object)
    try:
        b = pp.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
        m = pp.find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    except Exception:
        return {}
    typ = pp.find_col(df, ["O que é (Tipo)", "Tipo", "Product Type"], required=False)
    cg = pp.find_col(df, ["Category General"], required=False)
    fam = pp.find_col(df, ["FAMILY", "Family"], required=False)
    status = pp.find_col(df, ["Status"], required=False)
    rbi = pp.find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)
    out: Dict[str, Dict[str, str]] = {}
    for _, row in df.iterrows():
        valid = bool(rbi and _safe(row.get(rbi)).upper() == "OK") or bool(
            status and _safe(row.get(status)).upper() == "MANUAL"
        )
        if not valid or not typ:
            continue
        brand, model = _safe(row.get(b)), _safe(row.get(m))
        gold_type = _safe(row.get(typ)).upper()
        if brand and model and gold_type and gold_type != "REVISAR":
            out[pp.business_key(brand, model)] = {
                "type": gold_type,
                "category": _safe(row.get(cg)).upper() if cg else "",
                "family": _safe(row.get(fam)).upper() if fam else "",
            }
    return out


def _cohort(input_path: Optional[Path], store: Any, limit: int) -> List[Tuple[str, str, str]]:
    if input_path and input_path.exists():
        df = pd.read_excel(input_path, dtype=object)
        b = pp.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
        m = pp.find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
        out: List[Tuple[str, str, str]] = []
        seen = set()
        for _, row in df.iterrows():
            brand, model = _safe(row.get(b)), _safe(row.get(m))
            if not brand or not model:
                continue
            key = pp.business_key(brand, model)
            if key not in seen:
                seen.add(key)
                out.append((brand, model, key))
    else:
        out = store.catalog()
    return out[:limit] if limit > 0 else out


def _run_arm(
    name: str,
    bundles: Sequence[Tuple[EvidenceBundle, Any, str]],
    classifier: HybridTypeClassifier,
    gliner: Optional[OptionalGliner],
    rules: Any,
    gold: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, (bundle, s1, key) in enumerate(bundles, 1):
        query = bundle.evidence
        gliner_seconds = 0.0
        gliner_features = ""
        if gliner is not None:
            gliner_features, gliner_seconds = gliner.extract(query)
            if gliner_features:
                query = query + "\n\n" + gliner_features

        started = time.monotonic()
        pred = classifier.classify(query)
        inference_total = time.monotonic() - started + gliner_seconds
        power = bundle.stage1_power or "UNKNOWN"
        cg, fam, tax_reason = rules.map_taxonomy(
            pred["winner"], power, query, bundle.brand, bundle.model
        )
        auto, gate = _auto_candidate(bundle, pred, (cg, fam), rules)
        g = gold.get(key, {})
        gold_type = _safe(g.get("type"))
        rows.append({
            "Arm": name,
            "Marca": bundle.brand,
            "Modelo": bundle.model,
            "Identity": bundle.identity,
            "Identity Reasons": " | ".join(bundle.identity_reasons),
            "JSON-LD Products": bundle.structured_count,
            "Sources": bundle.source_count,
            "Stage1 State": bundle.stage1_state,
            "Stage1 Type": bundle.stage1_type,
            "Stage1 Power": power,
            "Pred Type": pred["winner"],
            "Pred Runner Up": pred["runner_up"],
            "Rerank Probability": pred["rerank_probability"],
            "Rerank Margin": pred["rerank_margin"],
            "Hybrid Score": pred["hybrid_score"],
            "Hybrid Margin": pred["hybrid_margin"],
            "Dense": pred["winner_dense"],
            "Sparse": pred["winner_sparse"],
            "ColBERT": pred["winner_colbert"],
            "Top Candidates": " | ".join(pred["top_candidates"]),
            "Category General": cg,
            "FAMILY": fam,
            "Taxonomy Reason": tax_reason,
            "Local Auto Candidate": "SIM" if auto else "NÃO",
            "Local Gate Reason": gate,
            "Gold Type": gold_type,
            "Type Correct": ("SIM" if pred["winner"] == gold_type else "NÃO") if gold_type else "",
            "BGE Seconds": pred["bge_seconds"],
            "Rerank Seconds": pred["rerank_seconds"],
            "GLiNER Seconds": gliner_seconds,
            "Inference Total Seconds": inference_total,
            "GLiNER Features": gliner_features,
        })
        if idx % 10 == 0 or idx == len(bundles):
            print(
                f"[{name}] {idx}/{len(bundles)} | "
                f"avg={np.mean([x['Inference Total Seconds'] for x in rows]):.2f}s | "
                f"auto={sum(x['Local Auto Candidate']=='SIM' for x in rows)}"
            )
    return rows


def _summary(df: pd.DataFrame, arm: str) -> Dict[str, Any]:
    d = df[df["Arm"] == arm]
    gold = d[d["Gold Type"].astype(str).str.len() > 0]
    return {
        "Arm": arm,
        "Produtos": len(d),
        "Identity Confirmed": int((d["Identity"] == "TARGET_CONFIRMED").sum()) if len(d) else 0,
        "Local Auto Candidates": int((d["Local Auto Candidate"] == "SIM").sum()) if len(d) else 0,
        "Auto Coverage %": round(100 * (d["Local Auto Candidate"] == "SIM").mean(), 2) if len(d) else 0,
        "Gold Validado": len(gold),
        "Type Accuracy %": round(100 * (gold["Type Correct"] == "SIM").mean(), 2) if len(gold) else "",
        "Avg BGE s": round(float(d["BGE Seconds"].mean()), 3) if len(d) else 0,
        "Avg Rerank s": round(float(d["Rerank Seconds"].mean()), 3) if len(d) else 0,
        "Avg GLiNER s": round(float(d["GLiNER Seconds"].mean()), 3) if len(d) else 0,
        "Avg Total s": round(float(d["Inference Total Seconds"].mean()), 3) if len(d) else 0,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Piloto V4.1 A/B BGE híbrido + JSON-LD + GLiNER opcional")
    p.add_argument("--config", default="pipeline_config.json")
    p.add_argument("--rules", default="regras_negocio.json")
    p.add_argument("--cache", default="")
    p.add_argument("--input", default="")
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--output", default="Piloto_V41_BGE_GLiNER_AB.xlsx")
    p.add_argument("--jsonld-cache", default="pilot_v41_jsonld_cache.json")
    p.add_argument("--no-jsonld-fetch", action="store_true")
    p.add_argument("--gliner-model", default=os.getenv("GLINER_MODEL_PATH", ""))
    p.add_argument("--skip-gliner-arm", action="store_true")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = p.parse_args()

    base = Path(__file__).resolve().parent
    cfg = json.loads((base / args.config).read_text(encoding="utf-8"))
    rules = pp.Rules(base / args.rules)
    cache_path = Path(args.cache) if args.cache else base / _safe((cfg.get("files", {}) or {}).get("cache") or "cache_tavily.sqlite")
    input_path = Path(args.input) if args.input else None
    if input_path and not input_path.is_absolute():
        input_path = base / input_path
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = base / out_path
    jsonld_path = Path(args.jsonld_cache)
    if not jsonld_path.is_absolute():
        jsonld_path = base / jsonld_path

    store = pp.TavilyReadOnlyStore(cache_path)
    engine = pp.DeterministicEngine(rules, cfg)
    classifier = HybridTypeClassifier(rules, cfg, top_k=args.top_k)
    jsonld_cache = JsonLdCache(jsonld_path)
    gold = _validated_gold_map(input_path)

    try:
        cohort = _cohort(input_path, store, args.max_items)
        print(f"PILOT {PILOT_VERSION} | cohort={len(cohort)} | gold_validado={len(gold)}")
        bundles: List[Tuple[EvidenceBundle, Any, str]] = []
        skipped: List[Dict[str, Any]] = []
        for idx, (brand, model, key) in enumerate(cohort, 1):
            raw = store.get(brand, model)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                skipped.append({"Marca": brand, "Modelo": model, "Reason": raw.state})
                continue
            s1 = engine.classify(brand, model, raw.payload)
            bundle = build_evidence_bundle(
                brand, model, s1, cfg, jsonld_cache,
                fetch_jsonld=not args.no_jsonld_fetch,
            )
            if not bundle.evidence.strip():
                skipped.append({"Marca": brand, "Modelo": model, "Reason": "NO_EVIDENCE"})
                continue
            bundles.append((bundle, s1, key))
            if idx % 10 == 0:
                jsonld_cache.flush()
        jsonld_cache.flush()

        rows_a = _run_arm("SEM_GLINER", bundles, classifier, None, rules, gold)
        rows_b: List[Dict[str, Any]] = []
        gliner_error = ""
        if not args.skip_gliner_arm:
            try:
                gliner = OptionalGliner(args.gliner_model)
                rows_b = _run_arm("COM_GLINER", bundles, classifier, gliner, rules, gold)
            except RuntimeError as exc:
                gliner_error = str(exc)
                print("GLINER ARM SKIPPED:", gliner_error)

        dfa = pd.DataFrame(rows_a)
        dfb = pd.DataFrame(rows_b)
        both = pd.concat([dfa, dfb], ignore_index=True) if rows_b else dfa.copy()

        comp_rows: List[Dict[str, Any]] = []
        if rows_b:
            aidx = {(r["Marca"], r["Modelo"]): r for r in rows_a}
            bidx = {(r["Marca"], r["Modelo"]): r for r in rows_b}
            for key in aidx:
                a, b = aidx[key], bidx[key]
                comp_rows.append({
                    "Marca": key[0], "Modelo": key[1],
                    "Identity": a["Identity"],
                    "Gold Type": a["Gold Type"],
                    "Sem GLiNER": a["Pred Type"],
                    "Com GLiNER": b["Pred Type"],
                    "Mudou Type": "SIM" if a["Pred Type"] != b["Pred Type"] else "NÃO",
                    "Sem Correct": a["Type Correct"],
                    "Com Correct": b["Type Correct"],
                    "GLiNER Ganhou": "SIM" if a["Type Correct"] == "NÃO" and b["Type Correct"] == "SIM" else "NÃO",
                    "GLiNER Perdeu": "SIM" if a["Type Correct"] == "SIM" and b["Type Correct"] == "NÃO" else "NÃO",
                    "Sem Auto": a["Local Auto Candidate"],
                    "Com Auto": b["Local Auto Candidate"],
                    "Delta Tempo s": b["Inference Total Seconds"] - a["Inference Total Seconds"],
                    "GLiNER Features": b["GLiNER Features"],
                })
        comp = pd.DataFrame(comp_rows)

        summary_rows = [_summary(both, "SEM_GLINER")]
        if rows_b:
            summary_rows.append(_summary(both, "COM_GLINER"))
            gold_comp = comp[comp["Gold Type"].astype(str).str.len() > 0] if not comp.empty else comp
            summary_rows.append({
                "Arm": "DELTA_GLINER",
                "Produtos": len(comp),
                "Identity Confirmed": "",
                "Local Auto Candidates": "",
                "Auto Coverage %": "",
                "Gold Validado": len(gold_comp),
                "Type Accuracy %": "",
                "Avg BGE s": "",
                "Avg Rerank s": "",
                "Avg GLiNER s": "",
                "Avg Total s": round(float(comp["Delta Tempo s"].mean()), 3) if len(comp) else 0,
                "GLiNER ganhou casos": int((gold_comp["GLiNER Ganhou"] == "SIM").sum()) if len(gold_comp) else 0,
                "GLiNER perdeu casos": int((gold_comp["GLiNER Perdeu"] == "SIM").sum()) if len(gold_comp) else 0,
            })
        if gliner_error:
            summary_rows.append({"Arm": "GLINER_SKIPPED", "Reason": gliner_error})
        summary = pd.DataFrame(summary_rows)

        with pd.ExcelWriter(out_path, engine="openpyxl") as w:
            summary.to_excel(w, sheet_name="Resumo", index=False)
            dfa.to_excel(w, sheet_name="Sem GLiNER", index=False)
            if rows_b:
                dfb.to_excel(w, sheet_name="Com GLiNER", index=False)
                comp.to_excel(w, sheet_name="Comparativo", index=False)
            if skipped:
                pd.DataFrame(skipped).to_excel(w, sheet_name="Skipped", index=False)

        print(f"RESULTADO: {out_path}")
        for row in summary_rows:
            print(row)
    finally:
        store.close()
        jsonld_cache.flush()


if __name__ == "__main__":
    main()
