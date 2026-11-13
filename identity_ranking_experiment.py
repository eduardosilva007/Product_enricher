"""Blocking estrutural e ranking char n-grams 2-5 para benchmark offline.

Candidatos incompatíveis jamais são reabilitados por similaridade.
O módulo é opt-in; não altera o pipeline de produção enquanto o Gold não validar.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import re
import unicodedata
from collections import Counter
from typing import Sequence

def compact(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(x for x in s if not unicodedata.combining(x))
    return re.sub(r"[^a-z0-9]", "", s.casefold())

def char_ngrams(text: str, min_n: int = 2, max_n: int = 5) -> Counter:
    t = compact(text)
    return Counter(t[i:i+n] for n in range(min_n, max_n+1)
                   for i in range(max(0, len(t)-n+1)))

def tfidf_cosine(query: str, candidate: str, corpus: Sequence[str]) -> float:
    """TF-IDF sublinear char 2..5; IDF calculado no conjunto do candidato."""
    qs = char_ngrams(query)
    cs = char_ngrams(candidate)
    docs = [char_ngrams(x) for x in corpus]
    if not qs or not cs:
        return 0.0
    n = max(1, len(docs))
    terms = set(qs) | set(cs)
    dfs = Counter()
    for d in docs:
        dfs.update(set(d))
    idf = {x: math.log((1+n)/(1+dfs[x]))+1 for x in terms}
    dot = sum((1+math.log(qs[x]))*(1+math.log(cs[x]))*idf[x]*idf[x]
              for x in qs.keys() & cs.keys())
    norm_q = math.sqrt(sum(((1+math.log(v))*idf[k])**2 for k,v in qs.items()))
    norm_c = math.sqrt(sum(((1+math.log(v))*idf[k])**2 for k,v in cs.items()))
    return dot/norm_q/norm_c if norm_q and norm_c else 0.0

@dataclass(frozen=True)
class IdentityDecision:
    eligible: bool
    reason: str
    score: float

def evaluate(brand: str, model: str, candidate_brand: str, candidate_model: str,
             corpus: Sequence[str] = (),
             verified_equivalences: Sequence[tuple[str,str]] = ()) -> IdentityDecision:
    """Hard negative policy: sem equivalência SKU explícita, igualdade compacta é obrigatória.

    Não inferir SKU por similaridade de texto; equivalências aprovadas devem
    estar registradas explicitamente por avaliação humana.
    """
    b, m = compact(brand), compact(model)
    cb, cm = compact(candidate_brand), compact(candidate_model)
    if not b or not m or not cb or not cm:
        return IdentityDecision(False, "MISSING_IDENTITY", 0.0)
    if b != cb:
        return IdentityDecision(False, "BRAND_MISMATCH", 0.0)
    allowed = {(compact(a),compact(z)) for a,z in verified_equivalences}
    if m != cm and (m,cm) not in allowed and (cm,m) not in allowed:
        return IdentityDecision(False, "SKU_MISMATCH", 0.0)
    rank = tfidf_cosine(brand+" "+model, candidate_brand+" "+candidate_model,
                        corpus or [brand+" "+model, candidate_brand+" "+candidate_model])
    return IdentityDecision(True, "EXACT_OR_VERIFIED_EQUIVALENCE", rank)
