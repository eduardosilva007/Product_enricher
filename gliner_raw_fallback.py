"""Optional GLiNER extractor for unresolved cases from saved RAW evidence.

No calls to search APIs. This module NEVER approves a classification or
relaxes SKU identity. Loading the model is explicit and lazy.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
import re

LABELS = ("brand", "model", "product type", "power source", "product family")

@dataclass(frozen=True)
class ExtractedEntity:
    label: str
    text: str
    score: float
    source: str

def raw_texts(payload: Mapping[str, Any], max_sources: int = 10,
              max_chars_per_source: int = 3000) -> list[tuple[str,str]]:
    """Return strictly local previously persisted RAW snippets and evidence."""
    results = payload.get("results", []) or []
    evidence = payload.get("_evidence_cache", []) or []
    out: list[tuple[str,str]] = []
    for ix, row in enumerate(list(results)[:max_sources]):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "") or "")
        snippet = str(row.get("content", "") or "")
        raw = str(row.get("raw_content", "") or "")
        text = re.sub(r"\s+", " ", f"{title} {snippet} {raw}").strip()
        if text:
            out.append((f"raw_result:{ix}", text[:max_chars_per_source]))
    if isinstance(evidence, list):
        for ix, row in enumerate(evidence[:max_sources]):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title", "") or "")
            text = str(row.get("content", row.get("excerpt", "")) or "")
            combined = re.sub(r"\s+", " ", title + " " + text).strip()
            if combined:
                out.append((f"evidence_cache:{ix}", combined[:max_chars_per_source]))
    return out

def extract_on_failure(
    payload: Mapping[str, Any],
    unresolved: bool,
    model: Any,
    *,
    min_confidence: float = 0.55,
    max_sources: int = 10,
) -> list[ExtractedEntity]:
    """Caller provides loaded GLiNER model to avoid auto-loading overhead.

    Its predictions are *hints* for JEV/Clef, never verified manufacturer
    identity or an auto-approved category/family.
    """
    if not unresolved:
        return []
    out: list[ExtractedEntity] = []
    for source, text in raw_texts(payload, max_sources=max_sources):
        for item in model.predict_entities(text, list(LABELS), threshold=min_confidence):
            label = str(item.get("label", "") or "").casefold().strip()
            value = str(item.get("text", "") or "").strip()
            try:
                score = float(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if label in LABELS and value and score >= min_confidence:
                out.append(ExtractedEntity(label,value,score,source))
    return sorted(out, key=lambda x:-x.score)

def clef_candidate_context(entities: list[ExtractedEntity], max_entities: int = 20) -> dict:
    """Auditable hints to augment an EXISTING closed-set candidate prompt."""
    return {"source": "GLINER_RAW_OPTIONAL",
            "verified": False,
            "may_override_identity_gate": False,
            "entities": [
                {"label": e.label, "text":e.text, "confidence":round(e.score,4),
                 "source":e.source}
                for e in entities[:max_entities]
            ]}
