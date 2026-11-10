"""Offline Gold-assisted recovery for ABSTAIN/UNRESOLVED only.

No automatic classification, no network calls, no mutation of the production
pipeline. Gold examples must come from manually reviewed data. Never accept
a similar but structurally different SKU as an exact product identity.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Sequence
from identity_ranking_experiment import compact, tfidf_cosine

@dataclass(frozen=True)
class GoldSuggestion:
    brand: str
    model: str
    category: str
    family: str
    score: float
    reason: str

def suggest_on_failure(
    brand: str,
    model: str,
    unresolved: bool,
    gold_rows: Sequence[Mapping[str, str]],
    *,
    top_k: int = 5,
) -> list[GoldSuggestion]:
    """Gold retrieval, not decision. Only run after the baseline has abstained.

    Exact Brand+SKU can be returned as GOLD_EXACT_IDENTITY. Non-identical
    SKU suggestions are explicitly blocked, even if char n-gram similarity
    is high. Gold category/family must both be non-empty.
    """
    if not unresolved:
        return []
    target_b, target_m = compact(brand), compact(model)
    if not target_b or not target_m:
        return []
    valid = []
    for row in gold_rows:
        rb = str(row.get("Marca", row.get("brand", "")) or "")
        rm = str(row.get("Modelo", row.get("model", "")) or "")
        cat = str(row.get("Category General", row.get("category", "")) or "").strip()
        fam = str(row.get("FAMILY", row.get("family", "")) or "").strip()
        if compact(rb) != target_b or compact(rm) != target_m or not cat or not fam:
            continue
        valid.append((rb, rm, cat, fam))
    corpus = [f"{brand} {model}"] + [f"{b} {m}" for b, m, _, _ in valid]
    ranked = [
        GoldSuggestion(b,m,cat,fam,tfidf_cosine(corpus[0],f"{b} {m}",corpus),
                       "GOLD_EXACT_IDENTITY_REQUIRES_REVALIDATION")
        for b,m,cat,fam in valid
    ]
    # Conflicting human Gold labels must not be treated as an automatic vote.
    distinct = {(x.category,x.family) for x in ranked}
    if len(distinct) > 1:
        return [GoldSuggestion(x.brand,x.model,x.category,x.family,x.score,
                               "CONFLICTING_GOLD_LABELS_REVIEW_ONLY") for x in ranked[:top_k]]
    return sorted(ranked,key=lambda x:-x.score)[:max(0,top_k)]
