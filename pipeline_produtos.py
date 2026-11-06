#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIPELINE DE ENRIQUECIMENTO DE PRODUTOS v4.0.7.3-structural-sku-core / Collection R9.6p7.2.3

Arquitetura:
    Stage 0A - Search Provider único (Tavily/Serper/Bright Data) - MAX 1 request
    Stage 0B - Source Sieve: Query Integrity + Target Identity + Source Purpose
    Stage 0C - Evidence Enrichment: Tavily raw_content na mesma Search ou fetch local
    Stage 0D - Target Isolation + Collection Quality Gate
    Stage 1 - Regras determinísticas do JSON
    Stage 1.5 - Funil híbrido: regras/aliases + BGE-M3 + BGE reranker
    Stage 2 - Cloudflare Clef: decisão estruturada de TYPE/POWER/TAXONOMY
    Stage 3 - Gate local de consistência, confiança e identidade STRONG
    Stage 4 - AUTO_APROVADO ou CLASSIFIED_CLEF; somente falta real de evidência vira NEEDS_RECOLLECTION

Princípios:
- Search RAW do provider selecionado descobre fontes; somente fontes aprovadas no Source Sieve podem ser aprofundadas.
- regras_negocio.json é a fonte única semântica: regras + quality gates.
- Stage 1 usa o comportamento comprovado do R5: aliases JSON formam candidatos; resolução automática exige evidência explícita/repetida.
- O funil semântico recupera apenas PRODUCT_TYPEs e pares TAXONOMY_PAIRS já existentes no JSON.
- Clef escolhe TYPE, POWER e um par Category General|FAMILY dentro do contrato fechado.
- Todo AMBIGUOUS/UNRESOLVED com evidência target utilizável passa obrigatoriamente pelo Clef.
- BGE/reranker preparam candidatos; nunca encerram sozinhos um caso não resolvido por regra forte.
- Resultado Clef válido abaixo do gate AUTO é preservado como CLASSIFIED_CLEF para double-check, não apagado em REVISAR.
- Falta real de evidência vira NEEDS_RECOLLECTION; Classification Identity=WEAK não descarta resposta Clef válida.
- Clef nunca inventa Category/FAMILY: somente arbitra entre pares válidos do JSON.
- Status=Manual/Revisão=MANUAL são FULL LOCK.
- Revisão B.I=OK é HARD LOCK absoluto e vence inclusive --reprocess.
- Cache/checkpoint são separados por provider/model/prompt/rules/raw_hash.
- Base Final preserva 100% das chaves; Revisão B.I é exclusivamente humana.
- Novos/reprocessados mantêm Revisão B.I vazia até o double check.
- Erro operacional de API nunca vira REVIEW nem falta de cache.
- CLASSIFY não consulta nenhum provider de busca.
- Enriquecimento local de URL aprovada não consome Search API e nunca autoriza nova Search.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlencode, urlparse

PIPELINE_VERSION = "4.0.7.3.1-input-scoped-recuration"
PROMPT_VERSION = "2026-10-13-v19-clef-joint-repair-identity-guard"
DEFAULT_CLEF_MODEL = "clef"
BUILD_LABEL = "v4.0.7.3.1 Structural SKU Core + Input-Scoped Recuration"
COLLECTION_ENGINE_VERSION = "2026-10-22-r9.6p7.2.3-structural-sku-core"
COLLECTION_LABEL = "R9.6p7.2.3"
IDENTITY_PROFILE_VERSION = "2026-10-22-structural-sku-core-v3"
EVIDENCE_CACHE_SCHEMA_VERSION = "1"

DEFAULT_STRUCTURAL_EDGE_DECORATOR_REGEXES = (
    r"^M(?:12|18)$",
    r"^(?:EU|US|UK|AU)$",
    r"^\d+(?:[.,]\d+)?V(?:AC|DC)?$",
    r"^\d+(?:[.,]\d+)?AH$",
    r"^\d+(?:[.,]\d+)?HZ$",
    r"^\d+(?:[.,]\d+)?W$",
)
STRUCTURAL_CORE_MIN_COMPACT_CHARS = 6
STRUCTURAL_CORE_MAX_EDGE_DECORATORS = 2
MAX_COLLECTION_REQUESTS_PER_PRODUCT = 1
ALLOWED_SEARCH_PROVIDERS = {"tavily", "serper", "brightdata"}

LOG = logging.getLogger("pipeline_v3")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe(v: Any) -> str:
    """Normaliza valores escalares vazios, incluindo pandas.NA, sem criar '<NA>'."""
    if v is None:
        return ""
    try:
        import pandas as pd
        missing = pd.isna(v)
        if missing is pd.NA or (isinstance(missing, bool) and missing):
            return ""
    except (TypeError, ValueError, ImportError):
        pass
    return str(v).strip()


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"JSON deve ser objeto: {path}")
    return obj


def stable_json_hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", safe(text)) if not unicodedata.combining(c))


def norm_text(text: str) -> str:
    """Normalização semântica Unicode.

    Preserva letras/números não latinos (ex.: cirílico) usados em aliases do
    próprio JSON. A chave de negócio continua usando compact() e permanece
    conservadora/ASCII para Marca+Modelo.
    """
    t = strip_accents(text).casefold()
    t = t.replace("_", " ")
    t = re.sub(r"[^\w]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(text).casefold())


def business_key(brand: str, model: str) -> str:
    """Chave AGRESSIVA usada somente para deduplicação/cache.

    Remove acentos, espaços, hífens, pontos, barras, underscores, parênteses e
    demais pontuações. NÃO usa fuzzy matching e NÃO substitui Marca/Modelo
    originais na base ou nos relatórios.
    """
    return f"{compact(brand)}|{compact(model)}"


def _brand_config_list(mapping: Dict[str, Any], brand: str) -> List[Any]:
    """Busca configuração de marca por chave visual ou compacta.

    Isso evita drift por &, espaços, pontuação ou caracteres invisíveis no nome
    da marca sem introduzir fuzzy matching.
    """
    if not isinstance(mapping, dict) or not mapping:
        return []
    visual = canonical_display(brand)
    direct = mapping.get(visual)
    if isinstance(direct, list):
        return direct
    target = compact(brand)
    if not target:
        return []
    for key, value in mapping.items():
        if compact(key) == target and isinstance(value, list):
            return value
    return []


def derive_search_model(brand: str, model: str, cfg: Dict[str, Any]) -> str:
    """Deriva uma âncora técnica para busca/identidade sem alterar o Modelo original.

    Regras conservadoras:
    - remove apenas sufixos operacionais explicitamente configurados por marca;
    - para marcas autorizadas, extrai SKU alfanumérico embutido em descrições longas;
    - não faz fuzzy e nunca altera a chave de negócio ou a coluna Modelo.
    """
    original = safe(model)
    if not original:
        return original
    icfg = cfg.get("identity", {}) or {}
    brand_key = canonical_display(brand)
    suffix_map = icfg.get("operational_suffixes_by_brand", {}) or {}
    suffixes = {canonical_display(x) for x in _brand_config_list(suffix_map, brand) if safe(x)}

    work = re.sub(r"\s+", " ", original).strip()
    tokens = work.split()
    if len(tokens) > 1 and canonical_display(tokens[-1]) in suffixes:
        work = " ".join(tokens[:-1]).strip()

    # Regras estruturais brand-specific para formatos publicados de SKU.
    # Fail-safe: a regra só roda para a marca explicitamente configurada e exige
    # fullmatch do código compactado. Não há fuzzy/edit-distance nem aproximação.
    rewrite_map = icfg.get("search_model_rewrite_rules_by_brand", {}) or {}
    brand_compact_key = compact(brand).upper()
    compact_work = compact(work).upper()
    for rule in (rewrite_map.get(brand_compact_key, []) or []):
        if not isinstance(rule, dict):
            continue
        expr = safe(rule.get("pattern"))
        replacement = safe(rule.get("replacement"))
        if not expr or not replacement:
            continue
        try:
            match = re.fullmatch(expr, compact_work, flags=re.I)
        except re.error:
            continue
        if match:
            work = match.expand(replacement).strip()
            break

    # Especificacoes operacionais anexadas ao final do campo Modelo (ex.:
    # ``DJG02 355 220V``) nao fazem parte do SKU publicado. A remocao e
    # deliberadamente conservadora: somente token FINAL, regex configurada e
    # nucleo anterior contendo letras + numeros. Assim ``GSA 18V 32`` permanece
    # intacto, enquanto ``DJG02 355 220V`` vira ``DJG02 355`` apenas para busca
    # e identidade. Marca/Modelo originais e business_key nunca sao alterados.
    trailing_patterns = [
        safe(x) for x in (icfg.get("trailing_spec_suffix_regexes", []) or []) if safe(x)
    ]
    max_trailing = max(0, int(icfg.get("max_trailing_spec_suffixes", 2) or 2))
    for _ in range(max_trailing):
        toks = work.split()
        if len(toks) <= 1 or not trailing_patterns:
            break
        candidates_to_try: List[Tuple[str, str]] = [
            (toks[-1], " ".join(toks[:-1]).strip())
        ]
        # Tambem cobre base operacional escrita como ``220 V`` / ``50 Hz``.
        if len(toks) >= 3:
            candidates_to_try.append((toks[-2] + toks[-1], " ".join(toks[:-2]).strip()))

        stripped = False
        for candidate, core in candidates_to_try:
            core_c = compact(core)
            core_is_model_like = (
                len(core_c) >= 5
                and bool(re.search(r"[a-z]", core_c))
                and bool(re.search(r"\d", core_c))
            )
            if not core_is_model_like:
                continue
            matched = False
            for expr in trailing_patterns:
                try:
                    if re.fullmatch(expr, candidate, flags=re.I):
                        matched = True
                        break
                except re.error:
                    continue
            if matched:
                work = core
                stripped = True
                break
        if not stripped:
            break

    # Revisões anexadas ao SKU, somente por marca explicitamente configurada.
    revision_map = icfg.get("revision_suffixes_by_brand", {}) or {}
    revision_suffixes = sorted(
        [safe(x) for x in _brand_config_list(revision_map, brand) if safe(x)],
        key=lambda x: len(compact(x)),
        reverse=True,
    )
    for suffix in revision_suffixes:
        wc = compact(work)
        sc = compact(suffix)
        if sc and wc.endswith(sc) and len(wc) - len(sc) >= 5:
            # remove apenas o sufixo textual no final, preservando o restante do SKU
            work = re.sub(re.escape(suffix) + r"$", "", work, flags=re.I).rstrip(" -_./")
            break

    embedded_brands = {compact(x) for x in (icfg.get("embedded_sku_brands", []) or []) if compact(x)}
    min_len = int(icfg.get("embedded_sku_min_length", 5) or 5)
    if compact(brand) in embedded_brands:
        folded = canonical_display(work)
        looks_descriptive = brand_key in folded or len(work.split()) >= 5 or "/" in work
        if looks_descriptive:
            candidates = []
            for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]*", work):
                c = compact(tok)
                if len(c) < min_len:
                    continue
                if not (re.search(r"[a-z]", c) and re.search(r"\d", c)):
                    continue
                # evita escolher voltagem/potência como se fosse SKU
                if re.fullmatch(r"\d+(?:\.\d+)?(?:v|w|kw|ah)", c, re.I):
                    continue
                if c == compact(brand):
                    continue
                candidates.append(tok)
            if candidates:
                # favorece identificador compacto, sem pontuação desnecessária
                candidates.sort(key=lambda x: (len(compact(x)), len(x)), reverse=True)
                work = candidates[0]

    # Search Model: pontuação estrutural vira espaço, sem concatenar tokens.
    # O Modelo original e a business_key permanecem intactos. A chave de identidade
    # continua usando compact(), que remove separadores por completo.
    phrase_patterns = [
        safe(x) for x in (icfg.get("trailing_operational_phrase_regexes", []) or []) if safe(x)
    ]
    for expr in phrase_patterns:
        try:
            m = re.search(expr, work, flags=re.I)
        except re.error:
            continue
        if m and m.end() == len(work):
            core = work[:m.start()].rstrip(" -_./")
            core_c = compact(core)
            if len(core_c) >= 5 and re.search(r"[a-z]", core_c) and re.search(r"\d", core_c):
                work = core
                break

    return re.sub(r"\s+", " ", work).strip() or original


def derive_query_model(brand: str, model: str, cfg: Dict[str, Any]) -> str:
    """Versão limpa somente para a consulta ao Search provider."""
    work = derive_search_model(brand, model, cfg)
    icfg = cfg.get("identity", {}) or {}
    if not bool(icfg.get("normalize_search_model_separators", True)):
        return work
    work = strip_accents(work).replace("_", " ")
    work = re.sub(r"[^\w]+", " ", work, flags=re.UNICODE)
    return re.sub(r"\s+", " ", work).strip() or derive_search_model(brand, model, cfg)


def canonical_display(text: str) -> str:
    """Representação canônica para auditoria, sem substituir o original."""
    t = strip_accents(text).upper().strip()
    return re.sub(r"\s+", " ", t)


@lru_cache(maxsize=4096)
def alias_pattern(alias: str) -> re.Pattern:
    """Alias lexical com tolerancia apenas a separadores estruturais.

    Hifen, barra, ponto, underscore e espaco sao equivalentes ENTRE os mesmos
    tokens (ex.: ``multi-tool`` == ``multi tool``; ``drill/driver`` ==
    ``drill driver``). Nao ha fuzzy, stemming, reordenacao ou expansao semantica.
    """
    folded = strip_accents(alias).casefold().strip()
    parts = [re.escape(x) for x in re.split(r"[\s\-_./+]+", folded) if x]
    if not parts:
        return re.compile(r"(?!x)x")
    body = r"[\s\-_./+]+".join(parts)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)


def contains_alias(text_normish: str, alias: str) -> bool:
    return bool(alias_pattern(alias).search(strip_accents(text_normish).casefold()))


def contains_alias_prepared(text_folded: str, alias: str) -> bool:
    """Alias search when text was already strip_accents(...).casefold()."""
    return bool(alias_pattern(alias).search(text_folded))


def find_col(df: Any, names: Sequence[str], required: bool = True) -> Optional[str]:
    by_norm = {norm_text(str(c)): c for c in df.columns}
    for name in names:
        if norm_text(name) in by_norm:
            return by_norm[norm_text(name)]
    if required:
        raise ValueError(f"Coluna não encontrada. Esperado um de: {names}")
    return None


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_env_file(path: Path) -> Dict[str, Any]:
    """Carrega .env local sem dependência externa e sem sobrescrever o ambiente.

    Retorna metadados seguros para diagnóstico. O valor das chaves nunca é logado.
    """
    info: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "loaded_keys": [],
        "skipped_existing": [],
    }
    if not path.exists():
        return info

    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        # Aceita também "export KEY=VALUE".
        if line.lower().startswith("export "):
            line = line[7:].strip()

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Remove aspas externas simples/duplas.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if not key:
            continue

        if key in os.environ:
            info["skipped_existing"].append(key)
            continue

        os.environ[key] = value
        info["loaded_keys"].append(key)

    return info


# -----------------------------------------------------------------------------
# Proteções operacionais / APIs
# -----------------------------------------------------------------------------
class OperationalPause(RuntimeError):
    """Pausa segura da execução por quota, autenticação ou indisponibilidade."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RateLimiter:
    """Agenda slots globais de requests/minute, inclusive entre threads."""
    def __init__(self, requests_per_minute: float):
        import threading
        rpm = max(0.0, float(requests_per_minute or 0))
        self.interval = (60.0 / rpm) if rpm > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> float:
        if self.interval <= 0:
            return 0.0
        # Reserva um slot sob lock; não segura o lock enquanto dorme.
        # Todas as threads compartilham o mesmo calendário de requests.
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
            remaining = max(0.0, slot - now)
        if remaining:
            time.sleep(remaining)
        return remaining


def classify_api_error(exc: Exception) -> str:
    """Converte erros HTTP/SDK em estados operacionais discretos."""
    code = getattr(exc, "code", None)
    msg = safe(getattr(exc, "message", "")) or safe(exc)
    folded = msg.casefold()

    if code == 401 or any(x in folded for x in ["unauthorized", "api key not valid", "invalid api key"]):
        return "AUTH"
    if code == 403 or any(x in folded for x in ["permission_denied", "forbidden"]):
        return "PERMISSION"
    if code == 429:
        # A API usa 429 tanto para rate-limit quanto para quota diária.
        if any(x in folded for x in ["quota_exceeded", "daily quota", "requests per day", "rpd"]):
            return "QUOTA"
        return "RATE_LIMIT"
    if code in {408, 500, 502, 503, 504} or any(
        x in folded for x in ["service_unavailable", "deadline_exceeded", "timeout", "timed out"]
    ):
        return "TRANSIENT"
    return "OTHER"


def should_checkpoint_record(rec: Dict[str, Any]) -> bool:
    """Falha operacional de API nunca vira checkpoint final de classificação."""
    stage = safe(rec.get("Stage")).upper()
    return stage != "API_PAUSED" and not stage.endswith("_ERROR")


# -----------------------------------------------------------------------------
# Regras
# -----------------------------------------------------------------------------
class Rules:
    def __init__(self, path: Path):
        self.path = path
        self.raw = read_json(path)
        self.version = safe(self.raw.get("VERSION")) or "unknown"
        # Checkpoint hash contém apenas semântica realmente executável.
        # VERSION, DESIGN_PRINCIPLES, SCHEMA_VERSION e QUALITY_GATES são
        # auditáveis, mas não devem forçar reprocessamento quando a lógica não mudou.
        semantic_keys = (
            "PRODUCT_TYPES", "POWER_RULES", "TAXONOMY_PAIRS", "TAXONOMY_RULES",
            "TYPE_POLICY", "RESOLUTION_POLICY", "CLASSIFICATION_POLICY",
            "SOURCE_RULES", "COMPONENT_CONTEXT_RULES",
            "COMPOSITE_TYPE_RULES", "TAXONOMY_EVIDENCE_RULES", "COMBO_KIT_POLICY",
        )
        semantic_payload = {k: self.raw.get(k) for k in semantic_keys}
        self.hash = stable_json_hash(semantic_payload)
        self.quality_gates = self.raw.get("QUALITY_GATES", {}) or {}
        self.quality_hash = stable_json_hash(self.quality_gates)
        self.types: Dict[str, Dict[str, Any]] = self.raw.get("PRODUCT_TYPES", {}) or {}
        self.power_rules: Dict[str, Dict[str, Any]] = self.raw.get("POWER_RULES", {}) or {}
        self.taxonomy_rules: List[Dict[str, Any]] = self.raw.get("TAXONOMY_RULES", []) or []
        self.taxonomy_evidence_rules: List[Dict[str, Any]] = self.raw.get("TAXONOMY_EVIDENCE_RULES", []) or []
        self.valid_pairs = {
            (safe(x.get("category_general")), safe(x.get("family")))
            for x in (self.raw.get("TAXONOMY_PAIRS", []) or [])
        }
        self.type_policy = self.raw.get("TYPE_POLICY", {}) or {}
        self.resolution_policy = self.raw.get("RESOLUTION_POLICY", {}) or {}
        self.classification_policy = self.raw.get("CLASSIFICATION_POLICY", {}) or {}
        self.composite_rules = self.raw.get("COMPOSITE_TYPE_RULES", []) or []
        self.component_context = self.raw.get("COMPONENT_CONTEXT_RULES", {}) or {}
        self.source_rules = self.raw.get("SOURCE_RULES", {}) or {}
        self.component_types = set(self.type_policy.get("component_primary_types", []) or [])
        self.passive_types = set(self.type_policy.get("passive_power_types", []) or [])
        self.output_voltage_not_input_power_types = set(
            self.type_policy.get("output_voltage_not_input_power_types", []) or []
        )
        self.specificity_groups: List[List[str]] = self.type_policy.get("specificity_groups", []) or []
        self.specificity_rank: Dict[str, int] = {}
        for group in self.specificity_groups:
            n = len(group)
            for i, typ in enumerate(group):
                typ = safe(typ)
                if typ:
                    self.specificity_rank[typ] = max(self.specificity_rank.get(typ, 0), n - i)
        self.allowed_powers = list(self.power_rules.keys()) + ["UNKNOWN"]
        self.alias_collision_policy = {
            norm_text(k): safe(v)
            for k, v in (self.resolution_policy.get("alias_collision_policy", {}) or {}).items()
        }
        self.alias_to_types: Dict[str, List[str]] = defaultdict(list)
        self._compiled_aliases: Dict[str, List[Tuple[str, re.Pattern]]] = {}
        self._compiled_negative: Dict[str, List[Tuple[str, re.Pattern]]] = {}
        self._compile()

    def _compile(self) -> None:
        for t, data in self.types.items():
            self._compiled_aliases[t] = [(a, alias_pattern(a)) for a in (data.get("aliases", []) or []) if safe(a)]
            self._compiled_negative[t] = [(a, alias_pattern(a)) for a in (data.get("negative_aliases", []) or []) if safe(a)]
            for a in (data.get("aliases", []) or []):
                na = norm_text(a)
                if na:
                    self.alias_to_types[na].append(t)

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.types:
            errors.append("PRODUCT_TYPES vazio")
        if not self.power_rules:
            errors.append("POWER_RULES vazio")
        if not self.valid_pairs:
            errors.append("TAXONOMY_PAIRS vazio")

        for t, data in self.types.items():
            pos = {norm_text(x) for x in data.get("aliases", []) or []}
            neg = {norm_text(x) for x in data.get("negative_aliases", []) or []}
            both = sorted(x for x in pos & neg if x)
            if both:
                errors.append(f"{t}: alias positivo e negativo ao mesmo tempo: {both[:5]}")
            fp = data.get("fallback_pair")
            if fp:
                if not isinstance(fp, list) or len(fp) != 2 or (safe(fp[0]), safe(fp[1])) not in self.valid_pairs:
                    errors.append(f"{t}: fallback_pair inválido: {fp}")

        for r in self.taxonomy_rules:
            t = safe(r.get("primary_type"))
            if t not in self.types:
                errors.append(f"TAXONOMY_RULE desconhece type={t}")
            pair = (safe(r.get("category_general")), safe(r.get("family")))
            if pair not in self.valid_pairs:
                errors.append(f"TAXONOMY_RULE par inválido: {pair}")
            if pair == ("OTHERS", "OTHERS"):
                errors.append(f"TAXONOMY_DEAD_END: {t} termina em OTHERS|OTHERS")
            p = safe(r.get("power")) or "*"
            if p != "*" and p not in self.allowed_powers:
                errors.append(f"TAXONOMY_RULE power inválido: {p}")

        for i, r in enumerate(self.taxonomy_evidence_rules):
            t = safe(r.get("primary_type"))
            if t not in self.types:
                errors.append(f"TAXONOMY_EVIDENCE_RULE desconhece type={t}")
            pair = (safe(r.get("category_general")), safe(r.get("family")))
            if pair not in self.valid_pairs:
                errors.append(f"TAXONOMY_EVIDENCE_RULE par inválido: {pair}")
            aliases = r.get("aliases", []) or []
            if not isinstance(aliases, list):
                errors.append(f"TAXONOMY_EVIDENCE_RULES[{i}].aliases deve ser lista")
            neg = r.get("negative_aliases", []) or []
            if not isinstance(neg, list):
                errors.append(f"TAXONOMY_EVIDENCE_RULES[{i}].negative_aliases deve ser lista")
            model_regex = safe(r.get("model_regex"))
            if model_regex:
                try:
                    re.compile(model_regex, flags=re.I)
                except re.error as exc:
                    errors.append(f"TAXONOMY_EVIDENCE_RULES[{i}].model_regex inválido: {exc}")

        # Todo PRODUCT_TYPE precisa chegar a uma taxonomia executável.
        # R9.5 HF2: tipos amplos podem depender de subtipo comprovado na evidência
        # (ex.: WATERPUMP -> PERIPHERAL/SUBMERSIBLE/CIRCULATION...).
        mapped_types = {safe(r.get("primary_type")) for r in self.taxonomy_rules}
        mapped_types.update(safe(r.get("primary_type")) for r in self.taxonomy_evidence_rules)
        for t, data in self.types.items():
            fp = data.get("fallback_pair")
            has_valid_fallback = bool(
                isinstance(fp, list) and len(fp) == 2
                and (safe(fp[0]), safe(fp[1])) in self.valid_pairs
                and (safe(fp[0]), safe(fp[1])) != ("OTHERS", "OTHERS")
            )
            if t not in mapped_types and not has_valid_fallback:
                errors.append(f"TYPE_WITHOUT_EXECUTABLE_TAXONOMY: {t}")

        if ("OTHERS", "OTHERS") in self.valid_pairs:
            errors.append("FORBIDDEN_TAXONOMY_DEAD_END: OTHERS|OTHERS não pode existir em TAXONOMY_PAIRS")

        for t in sorted(self.output_voltage_not_input_power_types):
            if t not in self.types:
                errors.append(
                    f"TYPE_POLICY.output_voltage_not_input_power_types desconhece type={t}"
                )

        for fld in ("negative_page_terms", "negative_title_terms"):
            vals = self.source_rules.get(fld, []) or []
            if not isinstance(vals, list):
                errors.append(f"SOURCE_RULES.{fld} deve ser lista")

        # Gate anti-regressão: não aceitar tipo de combo kit com alias genérico.
        # "combo kit" sozinho aparece em muitos kits de uma única ferramenta e
        # aumentou candidatos conflitantes no piloto v2.5.
        power_tool_kit = self.types.get("POWER TOOL KIT", {}) or {}
        kit_aliases = {
            norm_text(x) for x in (power_tool_kit.get("aliases", []) or [])
        }
        if "combo kit" in kit_aliases:
            errors.append(
                "POWER TOOL KIT contém alias genérico 'combo kit'; "
                "exija frase explícita de power/tool combo ou regra composta."
            )

        # Gate anti-regressão: a v2.5 removeu globalmente as tensões de CORDED e
        # isso afetou famílias não relacionadas. A exceção agora é type-aware.
        corded = self.power_rules.get("CORDED", {}) or {}
        common_mains = {norm_text(x) for x in (corded.get("voltage_keywords", []) or [])}
        if not {"110v", "120v", "220v", "230v"}.issubset(common_mains):
            errors.append(
                "POWER_RULES.CORDED perdeu tensões de rede básicas; "
                "use exceção type-aware para geradores em vez de remover globalmente."
            )

        # R9: o JSON não pode declarar políticas que o executor ignora silenciosamente.
        required_policy_sections = ["RESOLUTION_POLICY", "CLASSIFICATION_POLICY"]
        for section in required_policy_sections:
            if not self.raw.get(section):
                errors.append(f"{section} ausente/vazio; R9.4 exige política executável.")
        llm_gate = self.classification_policy.get("llm_resolution_gate", {}) or {}
        for key in (
            "allow_only_stage1_type_candidates",
            "allow_only_stage1_power_candidates",
            "require_cited_source_support",
            "require_title_support_or_min_sources",
            "block_multi_primary_title_conflict",
            "no_candidate_means_no_llm",
        ):
            if key not in llm_gate:
                errors.append(f"CLASSIFICATION_POLICY.llm_resolution_gate.{key} ausente")
        for key in (
            "allow_only_stage1_type_candidates",
            "allow_only_stage1_power_candidates",
            "require_cited_source_support",
            "block_multi_primary_title_conflict",
            "no_candidate_means_no_llm",
        ):
            if llm_gate.get(key) is not True:
                errors.append(f"CLEF_SAFETY_GATE_DISABLED: {key} deve ser true")
        if safe(llm_gate.get("require_identity_at_least")) not in {"PROBABLE", "STRONG"}:
            errors.append("CLEF_SAFETY_GATE_WEAK: require_identity_at_least deve ser PROBABLE ou STRONG")
        funnel_policy = llm_gate.get("candidate_funnel", {}) or {}
        if not funnel_policy:
            errors.append("CLASSIFICATION_POLICY.llm_resolution_gate.candidate_funnel ausente")
        else:
            max_c = int(funnel_policy.get("max_candidates", 0) or 0)
            if max_c < 1 or max_c > 4:
                errors.append("CANDIDATE_FUNNEL.max_candidates deve ficar entre 1 e 4")
            rel = float(funnel_policy.get("relative_score_floor", 0) or 0)
            if not (0.0 < rel <= 1.0):
                errors.append("CANDIDATE_FUNNEL.relative_score_floor inválido")
            if safe(funnel_policy.get("require_identity_at_least")) not in {"PROBABLE", "STRONG"}:
                errors.append("CANDIDATE_FUNNEL.require_identity_at_least deve ser PROBABLE ou STRONG")
        qg = self.quality_gates
        if not isinstance(qg.get("SYNTHETIC_CASES"), list) or not qg.get("SYNTHETIC_CASES"):
            errors.append("QUALITY_GATES.SYNTHETIC_CASES ausente/vazio")
        if not isinstance(qg.get("REAL_VALIDATED_CASES"), list) or not qg.get("REAL_VALIDATED_CASES"):
            errors.append("QUALITY_GATES.REAL_VALIDATED_CASES ausente/vazio")
        if not isinstance(qg.get("IDENTITY_CASES"), list) or not qg.get("IDENTITY_CASES"):
            errors.append("QUALITY_GATES.IDENTITY_CASES ausente/vazio")

        # Colisões de alias cross-type precisam ser governadas explicitamente.
        for alias, owners in sorted(self.alias_to_types.items()):
            uniq = sorted(set(owners))
            if len(uniq) <= 1:
                continue
            preferred = self.alias_collision_policy.get(alias)
            if not preferred:
                errors.append(f"ALIAS_COLLISION_UNDECLARED: '{alias}' pertence a {uniq}")
            elif preferred not in uniq:
                errors.append(f"ALIAS_COLLISION_BAD_POLICY: '{alias}' prefere {preferred}, mas owners={uniq}")

        # Requisito explícito do projeto: MERCHANDISING foi removido por falso positivo.
        if "MERCHANDISING" in self.types:
            errors.append("FORBIDDEN_TYPE: MERCHANDISING não pode existir em PRODUCT_TYPES")
        if any(cg == "MERCHANDISING" or fam == "MERCHANDISING" for cg, fam in self.valid_pairs):
            errors.append("FORBIDDEN_TAXONOMY: MERCHANDISING|MERCHANDISING não pode existir")

        # Closed schema: evita voltar a acumular seções/campos mortos no JSON.
        allowed_root = {
            "VERSION", "SCHEMA_VERSION", "DESIGN_PRINCIPLES",
            "TAXONOMY_PAIRS", "PRODUCT_TYPES", "POWER_RULES",
            "SOURCE_RULES", "TAXONOMY_RULES", "COMPONENT_CONTEXT_RULES",
            "TYPE_POLICY", "RESOLUTION_POLICY", "CLASSIFICATION_POLICY",
            "COMPOSITE_TYPE_RULES", "TAXONOMY_EVIDENCE_RULES", "COMBO_KIT_POLICY", "QUALITY_GATES",
        }
        for key in self.raw:
            if key not in allowed_root:
                errors.append(f"UNKNOWN_ROOT_SECTION: {key} não possui executor no R9.4")

        allowed_resolution = {"alias_collision_policy"}
        for key in self.resolution_policy:
            if key not in allowed_resolution:
                errors.append(f"UNKNOWN_RESOLUTION_POLICY_FIELD: {key}")

        allowed_classification = {"llm_resolution_gate"}
        for key in self.classification_policy:
            if key not in allowed_classification:
                errors.append(f"UNKNOWN_CLASSIFICATION_POLICY_FIELD: {key}")

        allowed_type_policy = {
            "component_primary_types", "passive_power_types", "specificity_groups",
            "output_voltage_not_input_power_types", "output_voltage_rule",
            "replacement_compatibility_rule", "source_conflict_rule",
            "agricultural_mower_rule",
        }
        for key in self.type_policy:
            if key not in allowed_type_policy:
                errors.append(f"UNKNOWN_TYPE_POLICY_FIELD: {key}")

        allowed_type_fields = {"aliases", "negative_aliases", "fallback_pair"}
        for typ, data in self.types.items():
            for key in data:
                if key not in allowed_type_fields:
                    errors.append(f"UNKNOWN_PRODUCT_TYPE_FIELD: {typ}.{key}")

        # Closed schema também nas demais seções executáveis: nenhum campo
        # decorativo pode parecer regra ativa sem ter executor no Python.
        allowed_taxonomy_fields = {"primary_type", "power", "category_general", "family", "priority"}
        for i, row in enumerate(self.taxonomy_rules):
            for key in row:
                if key not in allowed_taxonomy_fields:
                    errors.append(f"UNKNOWN_TAXONOMY_RULE_FIELD: TAXONOMY_RULES[{i}].{key}")

        allowed_taxonomy_evidence_fields = {
            "primary_type", "category_general", "family", "aliases",
            "negative_aliases", "priority", "brand", "model_regex",
        }
        for i, row in enumerate(self.taxonomy_evidence_rules):
            for key in row:
                if key not in allowed_taxonomy_evidence_fields:
                    errors.append(
                        f"UNKNOWN_TAXONOMY_EVIDENCE_RULE_FIELD: TAXONOMY_EVIDENCE_RULES[{i}].{key}"
                    )

        for power, data in self.power_rules.items():
            for key in data:
                if key == "negative_keywords" or key.endswith("_keywords"):
                    continue
                errors.append(f"UNKNOWN_POWER_RULE_FIELD: {power}.{key}")

        allowed_source_fields = {"negative_page_terms", "negative_title_terms", "notes"}
        for key in self.source_rules:
            if key not in allowed_source_fields:
                errors.append(f"UNKNOWN_SOURCE_RULE_FIELD: {key}")

        allowed_component_fields = {"negation_phrases"}
        for key in self.component_context:
            if key not in allowed_component_fields:
                errors.append(f"UNKNOWN_COMPONENT_CONTEXT_FIELD: {key}")

        allowed_composite_fields = {"target_type", "any", "all_groups", "none"}
        for i, row in enumerate(self.composite_rules):
            for key in row:
                if key not in allowed_composite_fields:
                    errors.append(f"UNKNOWN_COMPOSITE_RULE_FIELD: COMPOSITE_TYPE_RULES[{i}].{key}")

        return errors

    def map_taxonomy(
        self,
        product_type: str,
        power: str,
        evidence_text: str = "",
        brand: str = "",
        model: str = "",
    ) -> Tuple[str, str, str]:
        """Mapeia Category/FAMILY de forma determinística.

        HF2 introduz TAXONOMY_EVIDENCE_RULES para tipos fisicamente amplos cuja
        família não pode ser decidida apenas por TYPE+POWER (principalmente
        WATERPUMP e LIFT PUMP). Essas regras só usam evidência já aprovada pelo
        Stage 0 / Collection Quality Gate; o Clef continua sem superar identidade/taxonomia válida.
        """
        evidence_fold = strip_accents(evidence_text).casefold()
        evidence_candidates = []
        for r in self.taxonomy_evidence_rules:
            if safe(r.get("primary_type")) != product_type:
                continue
            rule_brand = safe(r.get("brand"))
            if rule_brand and canonical_display(rule_brand) != canonical_display(brand):
                continue
            model_regex = safe(r.get("model_regex"))
            if model_regex and not re.search(model_regex, safe(model), flags=re.I):
                continue
            negatives = [x for x in (r.get("negative_aliases", []) or []) if safe(x)]
            if negatives and any(contains_alias_prepared(evidence_fold, x) for x in negatives):
                continue
            aliases = [x for x in (r.get("aliases", []) or []) if safe(x)]
            hits = [x for x in aliases if contains_alias_prepared(evidence_fold, x)]
            if aliases and not hits:
                continue
            # Não existe regra evidence-only sem alias ou Brand+Model específico.
            if not aliases and not (rule_brand and model_regex):
                continue
            pair = (safe(r.get("category_general")), safe(r.get("family")))
            if pair not in self.valid_pairs:
                continue
            priority = int(r.get("priority", 0) or 0)
            specificity = max([len(norm_text(x).split()) for x in hits] or [0])
            constrained = int(bool(rule_brand)) + int(bool(model_regex))
            evidence_candidates.append((priority, constrained, specificity, pair, hits))

        if evidence_candidates:
            evidence_candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            best = evidence_candidates[0]
            # Empate real entre famílias diferentes => não arbitrar silenciosamente.
            tied = [x for x in evidence_candidates if x[:3] == best[:3] and x[3] != best[3]]
            if not tied:
                hit_label = ",".join(best[4][:3]) or "BRAND_MODEL_RULE"
                return best[3][0], best[3][1], f"TAXONOMY_EVIDENCE:{hit_label}"

        candidates = []
        for r in self.taxonomy_rules:
            if safe(r.get("primary_type")) != product_type:
                continue
            rp = safe(r.get("power")) or "*"
            if rp not in {power, "*"}:
                continue
            candidates.append(r)
        if candidates:
            candidates.sort(key=lambda x: (int(x.get("priority", 0)), safe(x.get("power")) == power), reverse=True)
            best = candidates[0]
            pair = (safe(best.get("category_general")), safe(best.get("family")))
            if pair in self.valid_pairs:
                return pair[0], pair[1], f"TAXONOMY_RULE:{safe(best.get('power')) or '*'}"

        fp = (self.types.get(product_type, {}) or {}).get("fallback_pair")
        if isinstance(fp, list) and len(fp) == 2:
            pair = (safe(fp[0]), safe(fp[1]))
            if pair in self.valid_pairs:
                return pair[0], pair[1], "TYPE_FALLBACK_PAIR"
        return "OTHERS", "OTHERS", "TAXONOMY_UNMAPPED"



# -----------------------------------------------------------------------------
# Evidence store Tavily - somente leitura
# -----------------------------------------------------------------------------
@dataclass
class RawEvidence:
    state: str
    brand: str
    model: str
    cache_key_stored: str = ""
    payload: Optional[Dict[str, Any]] = None
    reason: str = ""

    @property
    def raw_hash(self) -> str:
        """Hash semantico do RAW + evidencia curada da coleta R9.5.

        Metricas volateis (tempo/request_id/creditos) nao entram no checkpoint.
        Se o cache for legado, mantem o comportamento anterior e usa results.
        """
        if not self.payload:
            return ""
        collection = self.payload.get("_collection")
        if not isinstance(collection, dict):
            return stable_json_hash(self.payload.get("results", []) or [])
        semantic = {
            "results": self.payload.get("results", []) or [],
            "collection_version": safe(collection.get("version")),
            "collection_state": safe(collection.get("state")),
            "curated_results": collection.get("curated_results", []) or [],
        }
        return stable_json_hash(semantic)


class TavilyReadOnlyStore:
    def __init__(self, path: Path):
        if not path.exists():
            raise FileNotFoundError(f"Cache Tavily não existe: {path}")
        self.path = path
        uri = f"file:{quote(str(path.resolve()).replace(os.sep, '/'), safe='/:')}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        qc = self.conn.execute("PRAGMA quick_check").fetchone()
        if not qc or safe(qc[0]).lower() != "ok":
            raise RuntimeError(f"SQLite quick_check falhou: {qc[0] if qc else 'sem resposta'}")
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(tavily_cache)")}
        required = {"cache_key", "brand", "model", "response_json", "created_at"}
        if not required.issubset(cols):
            raise RuntimeError(f"Tabela tavily_cache inválida. Faltam: {sorted(required-cols)}")
        self._rows: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        self.merged_variants: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for row in self.conn.execute("SELECT cache_key,brand,model,response_json,created_at FROM tavily_cache"):
            k = business_key(row["brand"], row["model"])
            self._rows[k].append(row)
        for k, rows in self._rows.items():
            pairs = sorted({(safe(r["brand"]), safe(r["model"])) for r in rows})
            if len(pairs) > 1:
                self.merged_variants[k] = pairs

    def close(self) -> None:
        self.conn.close()

    def catalog(self) -> List[Tuple[str, str, str]]:
        """Catálogo em ordem determinística pela business_key.

        Isso garante que um piloto de N produtos use o mesmo cohort ao trocar
        apenas provider/modelo, permitindo comparação determinística entre execuções.
        """
        out = []
        for k in sorted(self._rows):
            rows = self._rows[k]
            row = rows[-1]
            out.append((safe(row["brand"]), safe(row["model"]), k))
        return out

    def get(self, brand: str, model: str) -> RawEvidence:
        k = business_key(brand, model)
        rows = self._rows.get(k)
        if not rows:
            return RawEvidence("RAW_MISSING", brand, model, reason="Produto não existe na tabela tavily_cache")

        # RAW Search continua sendo consolidado entre variantes estruturais, mas o
        # bloco de Collection Quality deve vir somente do payload mais recente que
        # o possua. Isso evita misturar decisoes de curadoria de versoes diferentes.
        merged_results: List[Dict[str, Any]] = []
        seen = set()
        valid_payloads = 0
        cache_keys = []
        newest_collection: Optional[Tuple[str, Dict[str, Any]]] = None
        newest_search_meta: Optional[Tuple[str, Dict[str, Any]]] = None

        for row in rows:
            cache_keys.append(safe(row["cache_key"]))
            try:
                payload = json.loads(row["response_json"])
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            results = payload.get("results", []) or []
            if not isinstance(results, list):
                continue
            valid_payloads += 1
            created_at = safe(row["created_at"])
            collection = payload.get("_collection")
            if isinstance(collection, dict):
                if newest_collection is None or created_at >= newest_collection[0]:
                    newest_collection = (created_at, collection)
            search_meta = payload.get("_search_meta")
            if isinstance(search_meta, dict):
                if newest_search_meta is None or created_at >= newest_search_meta[0]:
                    newest_search_meta = (created_at, search_meta)
            for res in results:
                if not isinstance(res, dict):
                    continue
                sig = (safe(res.get("url")), safe(res.get("title")), safe(res.get("content"))[:200])
                if sig in seen:
                    continue
                seen.add(sig)
                merged_results.append(res)

        if valid_payloads == 0:
            return RawEvidence("RAW_CORRUPT", brand, model, " | ".join(cache_keys), reason="Nenhuma variante contém JSON Tavily válido")
        payload: Dict[str, Any] = {"results": merged_results}
        if newest_collection is not None:
            payload["_collection"] = newest_collection[1]
        if newest_search_meta is not None:
            payload["_search_meta"] = newest_search_meta[1]
        if not merged_results:
            return RawEvidence("RAW_EMPTY", brand, model, " | ".join(cache_keys), payload, "Tavily persistiu resposta sem resultados")
        state = "RAW_OK_MERGED" if len(rows) > 1 else "RAW_OK"
        reason = f"{len(rows)} variantes de Marca/Modelo consolidadas" if len(rows) > 1 else ""
        return RawEvidence(state, brand, model, " | ".join(cache_keys), payload, reason)


# -----------------------------------------------------------------------------
# Tavily - gravação SOMENTE no modo COLETA
# -----------------------------------------------------------------------------
class TavilyWritableStore:
    """Writer isolado do RAW Tavily com commit por produto."""
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tavily_cache (
                cache_key TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tavily_cache_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL
            )"""
        )
        self.conn.commit()
        qc = self.conn.execute("PRAGMA quick_check").fetchone()
        if not qc or safe(qc[0]).lower() != "ok":
            raise RuntimeError(f"SQLite quick_check falhou antes da coleta: {qc[0] if qc else 'sem resposta'}")

    def put(self, brand: str, model: str, payload: Dict[str, Any], archive_reason: str = "COLLECT") -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise ValueError("Payload Tavily inválido; RAW não será gravado")
        key = "v3|" + business_key(brand, model)
        previous = self.conn.execute(
            "SELECT cache_key,brand,model,response_json,created_at FROM tavily_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
        if previous is not None:
            self.conn.execute(
                """INSERT INTO tavily_cache_history(
                       cache_key,brand,model,response_json,created_at,archived_at,archive_reason
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    safe(previous["cache_key"]), safe(previous["brand"]), safe(previous["model"]),
                    safe(previous["response_json"]), safe(previous["created_at"]), now_iso(),
                    safe(archive_reason) or "RECOLLECT",
                ),
            )
        self.conn.execute(
            """INSERT INTO tavily_cache(cache_key,brand,model,response_json,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 brand=excluded.brand,
                 model=excluded.model,
                 response_json=excluded.response_json,
                 created_at=excluded.created_at""",
            (key, brand, model, json.dumps(payload, ensure_ascii=False, default=str), now_iso()),
        )
        self.conn.commit()  # RAW + histórico persistidos imediatamente por produto

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


@dataclass
class InputProductRow:
    row_number: int
    brand_original: str
    model_original: str
    brand_canonical: str
    model_canonical: str
    key: str
    valid: bool
    duplicate_rank: int = 0
    duplicate_count: int = 0
    representative: bool = False
    raw_state: str = ""
    action: str = ""
    reason: str = ""


@dataclass
class PreCollectionPlan:
    input_path: Path
    report_path: Path
    rows: List[InputProductRow]
    representatives: List[InputProductRow]
    candidates: List[InputProductRow]
    summary: Dict[str, Any]


def read_input_rows(path: Path) -> List[InputProductRow]:
    """Lê TODAS as linhas e preserva Marca/Modelo exatamente como vieram."""
    if not path.exists():
        raise FileNotFoundError(f"Base de entrada não encontrada: {path}")

    import pandas as pd
    if path.suffix.casefold() in {".xlsx", ".xlsm", ".xls"}:
        df = pd.read_excel(path, dtype=object, keep_default_na=False)
    elif path.suffix.casefold() == ".csv":
        df = pd.read_csv(path, dtype=object, sep=None, engine="python")
    else:
        raise ValueError("Entrada deve ser Excel ou CSV")

    b = find_col(df, ["Fabricante", "Marca", "Manufacturer", "Brand"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])

    out: List[InputProductRow] = []
    for idx, row in df.iterrows():
        brand = safe(row[b])
        model = safe(row[m])
        valid = bool(brand and model and model.strip().upper() not in {"N/A", "NA", "NULL", "NONE", "NAN"})
        key = business_key(brand, model) if valid else ""
        out.append(
            InputProductRow(
                row_number=int(idx) + 2,
                brand_original=brand,
                model_original=model,
                brand_canonical=canonical_display(brand),
                model_canonical=canonical_display(model),
                key=key,
                valid=valid,
                reason="" if valid else "MARCA_OU_MODELO_AUSENTE",
            )
        )
    return out


def _raw_missing_without_cache(brand: str, model: str) -> RawEvidence:
    return RawEvidence("RAW_MISSING", brand, model, reason="Cache ainda não existe")


def build_precollection_plan(
    input_path: Path,
    cache_path: Path,
    report_path: Path,
    recollect_empty: bool = False,
    recollect_low_evidence: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
    max_tavily_new: int = 0,
) -> PreCollectionPlan:
    """Normaliza/deduplica/cruza cache sem rede.

    Recoleta somente quando solicitada explicitamente; cada execução mantém
    o contrato preexistente de uma request por produto e arquivo de entrada.
    """
    rows = read_input_rows(input_path)

    groups: Dict[str, List[InputProductRow]] = defaultdict(list)
    for row in rows:
        if row.valid:
            groups[row.key].append(row)

    representatives: List[InputProductRow] = []
    for _, grp in groups.items():
        total = len(grp)
        representative = grp[0]
        representatives.append(representative)
        for rank, row in enumerate(grp, 1):
            row.duplicate_rank = rank
            row.duplicate_count = total
            row.representative = rank == 1
            if rank > 1:
                row.action = "DUPLICADO_ENTRADA"
                row.reason = f"Mesma business_key da linha {representative.row_number}"

    store: Optional[TavilyReadOnlyStore] = TavilyReadOnlyStore(cache_path) if cache_path.exists() else None
    try:
        for rep in representatives:
            raw = store.get(rep.brand_original, rep.model_original) if store else _raw_missing_without_cache(rep.brand_original, rep.model_original)
            rep.raw_state = raw.state

            if raw.state in {"RAW_OK", "RAW_OK_MERGED"}:
                if recollect_low_evidence and raw.payload is not None and cfg is not None:
                    search_model = derive_search_model(rep.brand_original, rep.model_original, cfg)
                    target_level = _payload_target_evidence_level(
                        rep.brand_original, search_model, raw.payload, cfg
                    )
                    if target_level not in {"STRONG", "PROBABLE"}:
                        rep.action = "RECOLETAR_LOW_EVIDENCE"
                        rep.reason = f"RAW existente sem evidência target PROBABLE/STRONG (best={target_level})"
                    else:
                        rep.action = "JA_NO_CACHE"
                        rep.reason = (raw.reason + " | " if raw.reason else "") + f"target={target_level}"
                else:
                    rep.action = "JA_NO_CACHE"
                    rep.reason = raw.reason or "RAW Tavily já disponível"
            elif raw.state == "RAW_EMPTY":
                if recollect_empty:
                    rep.action = "RECOLETAR"
                    rep.reason = "RAW vazio e --recollect-empty habilitado"
                else:
                    rep.action = "RAW_EMPTY_PULADO"
                    rep.reason = "RAW vazio preservado; use --recollect-empty para nova consulta"
            elif raw.state == "RAW_CORRUPT":
                rep.action = "REVISAR_PRECOLETA"
                rep.reason = "RAW corrompido; nao gastar nova request sem auditoria"
            elif raw.state == "RAW_MISSING":
                rep.action = "NOVA_COLETA"
                rep.reason = "business_key não encontrada no cache"
            else:
                rep.action = "REVISAR_PRECOLETA"
                rep.reason = f"Estado RAW inesperado: {raw.state}"

        rep_by_key = {r.key: r for r in representatives}
        for row in rows:
            if not row.valid:
                row.action = "INVALIDO"
                row.raw_state = ""
                continue

            rep = rep_by_key[row.key]
            row.raw_state = rep.raw_state
            if not row.representative:
                row.reason = f"Duplicado estrutural da linha {rep.row_number}; ação do grupo={rep.action}"
    finally:
        if store:
            store.close()

    candidates_all = [r for r in representatives if r.action in {"NOVA_COLETA", "RECOLETAR", "RECOLETAR_LOW_EVIDENCE"}]
    candidates = candidates_all[:max_tavily_new] if max_tavily_new else list(candidates_all)

    valid_rows = sum(1 for r in rows if r.valid)
    invalid_rows = len(rows) - valid_rows

    raw_counts = defaultdict(int)
    action_counts = defaultdict(int)
    for r in representatives:
        raw_counts[r.raw_state] += 1
        action_counts[r.action] += 1

    summary = {
        "Linhas recebidas": len(rows),
        "Linhas válidas": valid_rows,
        "Linhas inválidas": invalid_rows,
        "Produtos únicos por business_key": len(representatives),
        "Duplicidades internas removidas": max(0, valid_rows - len(representatives)),
        "RAW_OK": raw_counts["RAW_OK"],
        "RAW_OK_MERGED": raw_counts["RAW_OK_MERGED"],
        "RAW_EMPTY": raw_counts["RAW_EMPTY"],
        "RAW_CORRUPT": raw_counts["RAW_CORRUPT"],
        "RAW_MISSING": raw_counts["RAW_MISSING"],
        "Já no cache": action_counts["JA_NO_CACHE"],
        "Recoleta LOW_TARGET_EVIDENCE": action_counts["RECOLETAR_LOW_EVIDENCE"],
        "RAW vazio pulado": action_counts["RAW_EMPTY_PULADO"],
        "Fila real Tavily antes do limite": len(candidates_all),
        "Fila Tavily desta execução": len(candidates),
        "Business key": "AGRESSIVA_ESTRUTURAL_SEM_FUZZY",
        "Tavily chamada até este ponto": 0,
    }

    plan = PreCollectionPlan(
        input_path=input_path,
        report_path=report_path,
        rows=rows,
        representatives=representatives,
        candidates=candidates,
        summary=summary,
    )
    export_precollection_report(plan)
    return plan


def export_precollection_report(plan: PreCollectionPlan) -> None:
    """Gera auditoria pré-coleta ANTES de qualquer chamada Tavily."""
    import pandas as pd

    normalizacao = []
    for r in plan.rows:
        normalizacao.append({
            "Linha Entrada": r.row_number,
            "Marca Original": r.brand_original,
            "Modelo Original": r.model_original,
            "Marca Canônica": r.brand_canonical,
            "Modelo Canônico": r.model_canonical,
            "Business Key": r.key,
            "Válido": "SIM" if r.valid else "NÃO",
            "Qtd no Grupo": r.duplicate_count,
            "Ordem no Grupo": r.duplicate_rank,
            "Representante": "SIM" if r.representative else "NÃO",
            "Estado Cache": r.raw_state,
            "Ação Pré-Coleta": r.action,
            "Motivo": r.reason,
        })

    fila = [{
        "Linha Entrada": r.row_number,
        "Marca Original": r.brand_original,
        "Modelo Original": r.model_original,
        "Marca Canônica": r.brand_canonical,
        "Modelo Canônico": r.model_canonical,
        "Business Key": r.key,
        "Estado Cache": r.raw_state,
        "Ação": r.action,
        "Motivo": r.reason,
    } for r in plan.candidates]

    duplicados = [x for x in normalizacao if int(x["Qtd no Grupo"] or 0) > 1]
    resumo = [{"Indicador": k, "Valor": v} for k, v in plan.summary.items()]

    plan.report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.report_path.with_name(
        plan.report_path.stem + f".tmp_{os.getpid()}" + plan.report_path.suffix
    )

    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        pd.DataFrame(resumo).to_excel(writer, sheet_name="Resumo", index=False)
        pd.DataFrame(normalizacao).to_excel(writer, sheet_name="Normalizacao", index=False)
        pd.DataFrame(fila).to_excel(writer, sheet_name="Fila_Coleta", index=False)
        pd.DataFrame(duplicados).to_excel(writer, sheet_name="Duplicidades", index=False)

    os.replace(tmp, plan.report_path)


def _collection_policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("_collection_policy", {}) or {}


def validate_collection_policy(policy: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(policy, dict) or not policy:
        return ["COLLECTION_POLICY ausente/vazia"]
    identity = policy.get("identity", {}) or {}
    isolation = policy.get("isolation", {}) or {}
    if not identity.get("direct_accept_levels"):
        errors.append("COLLECTION_POLICY.identity.direct_accept_levels ausente")
    if int(isolation.get("target_window_chars", 0) or 0) < 100:
        errors.append("COLLECTION_POLICY.isolation.target_window_chars muito baixo")
    if int(isolation.get("minimum_block_chars", 0) or 0) < 5:
        errors.append("COLLECTION_POLICY.isolation.minimum_block_chars muito baixo")
    decisions = set(policy.get("source_decisions", []) or [])
    required = {
        "ACCEPT_TARGET_CLEAN", "ACCEPT_TARGET_ISOLATED", "REJECT_WRONG_MODEL",
        "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY",
        "REJECT_GENERIC_NO_TARGET_BLOCK", "REJECT_NON_ISOLATABLE",
        "REJECT_TITLE_OTHER_SKU_BODY_ONLY",
        "REJECT_WEAK_IDENTITY", "REJECT_EXTRACT_FAILED",
    }
    missing = sorted(required - decisions)
    if missing:
        errors.append(f"COLLECTION_POLICY.source_decisions faltando={missing}")
    persistence = policy.get("persistence", {}) or {}
    cleaning = policy.get("cleaning", {}) or {}
    body_rescue = policy.get("body_identity_rescue", {}) or {}
    if bool(persistence.get("raw_content_full_persisted", True)):
        errors.append("COLLECTION_POLICY.persistence.raw_content_full_persisted deve ser false")
    if not bool(persistence.get("persist_target_evidence_window", False)):
        errors.append("COLLECTION_POLICY.persistence.persist_target_evidence_window deve ser true")
    excerpt_chars = int(persistence.get("evidence_excerpt_chars", 0) or 0)
    if excerpt_chars < 300 or excerpt_chars > 4000:
        errors.append("COLLECTION_POLICY.persistence.evidence_excerpt_chars deve ficar entre 300 e 4000")
    max_cached_sources = int(persistence.get("max_cached_sources", 0) or 0)
    if max_cached_sources < 1 or max_cached_sources > 20:
        errors.append("COLLECTION_POLICY.persistence.max_cached_sources deve ficar entre 1 e 20")
    if not isinstance(cleaning.get("boilerplate_line_regexes", []), list):
        errors.append("COLLECTION_POLICY.cleaning.boilerplate_line_regexes deve ser lista")
    if bool(body_rescue.get("enabled", False)) and safe(body_rescue.get("max_identity_level")) != "PROBABLE":
        errors.append("COLLECTION_POLICY.body_identity_rescue.max_identity_level deve ser PROBABLE")
    return errors


def _payload_collection_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    collection = payload.get("_collection")
    if isinstance(collection, dict):
        rows = collection.get("curated_results", [])
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return [x for x in (payload.get("results", []) or []) if isinstance(x, dict)]


def _payload_target_evidence_level(brand: str, model: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """Melhor nivel de identidade sobre evidencia que realmente alimentara Stage 1."""
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    rank = {"WEAK": 0, "CONTEXTUAL": 1, "PROBABLE": 2, "STRONG": 3}
    best = "WEAK"
    for res in _payload_collection_results(payload):
        title = safe(res.get("title"))
        content = safe(res.get("content"))
        url = safe(res.get("url"))
        level, *_ = _source_identity_baseline(
            brand, model, title, content, url, suffixes,
            int((cfg.get("identity", {}) or {}).get("short_model_requires_brand_length", 5) or 5),
        )
        if rank.get(level, 0) > rank.get(best, 0):
            best = level
    return best


def _merge_tavily_payloads(payloads: Sequence[Dict[str, Any]], search_meta: Dict[str, Any]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    seen = set()
    for payload in payloads:
        for res in (payload.get("results", []) or []):
            if not isinstance(res, dict):
                continue
            sig = (safe(res.get("url")), safe(res.get("title")), safe(res.get("content"))[:240])
            if sig in seen:
                continue
            seen.add(sig)
            results.append(res)
    return {"results": results, "_search_meta": search_meta}


def _url_key(url: str) -> str:
    value = safe(url).strip()
    value = re.sub(r"#.*$", "", value)
    return value.rstrip("/").casefold()


def _tavily_usage_credits(payload: Dict[str, Any]) -> float:
    try:
        return float(((payload.get("usage") or {}).get("credits", 0)) or 0)
    except Exception:
        return 0.0


def _source_identity_for_result(brand: str, model: str, res: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    level, score, match, weight, field_name = _source_identity_baseline(
        brand, model,
        safe(res.get("title")), safe(res.get("content")), safe(res.get("url")), suffixes,
        int((cfg.get("identity", {}) or {}).get("short_model_requires_brand_length", 5) or 5),
    )
    return {
        "level": level, "score": int(score), "model_match": match,
        "model_match_field": field_name, "source_weight": float(weight),
    }


def _term_present(text: str, terms: Sequence[str]) -> bool:
    folded = norm_text(text)
    return any(norm_text(x) and norm_text(x) in folded for x in terms or [])


def _target_model_patterns(model: str, cfg: Dict[str, Any]) -> List[re.Pattern]:
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    pats: List[re.Pattern] = []
    seen = set()
    for compact_variant, _ in _model_identity_variants(model, suffixes):
        if compact_variant and compact_variant not in seen:
            seen.add(compact_variant)
            pats.append(_model_exact_pattern(compact_variant))
    return pats


def _target_spans(text: str, model: str, cfg: Dict[str, Any]) -> List[Tuple[int, int]]:
    hay = strip_accents(unquote(safe(text))).casefold()
    spans: List[Tuple[int, int]] = []
    for pat in _target_model_patterns(model, cfg):
        for m in pat.finditer(hay):
            spans.append((m.start(), m.end()))
    spans.sort()
    dedup: List[Tuple[int, int]] = []
    for span in spans:
        if span not in dedup:
            dedup.append(span)
    return dedup


def _same_target_sku_token(
    brand: str,
    model: str,
    token: str,
    cfg: Dict[str, Any],
) -> bool:
    """Equivalência estrutural estrita para impedir que o próprio SKU vire contaminante."""
    compact_token = compact(token)
    if not compact_token:
        return False
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    target_compacts = {x[0] for x in _model_identity_variants(model, suffixes)}
    if compact_token in target_compacts:
        return True

    # Briggs publica Model-Type-Trim (ex.: 40U777-0010-G1), enquanto algumas
    # bases internas trazem somente Model+Type (40U7770010). Um sufixo de TRIM
    # letra+dígito é equivalente apenas para esta marca e somente sobre o mesmo
    # prefixo exato de 10 caracteres. Isso não aceita SKU "parecido".
    if compact(brand) == "briggsstratton":
        for target in target_compacts:
            extra = compact_token[len(target):] if compact_token.startswith(target) else ""
            if (
                len(target) == 10
                and len(compact_token) == 12
                and re.fullmatch(r"[a-z]\d", extra, flags=re.I)
            ):
                return True
    return False


def _looks_like_other_sku_token(
    token: str,
    start: int,
    text: str,
    brand: str,
    model: str,
    cfg: Dict[str, Any],
    policy: Dict[str, Any],
) -> bool:
    raw = safe(token).upper().strip(".,:;()[]{}")
    compact_token = compact(raw)
    if not compact_token:
        return False
    if _same_target_sku_token(brand, model, raw, cfg):
        return False
    for expr in policy.get("model_token_ignore_regexes", []) or []:
        try:
            if re.fullmatch(expr, raw, flags=re.I):
                return False
        except re.error:
            continue
    has_alpha = bool(re.search(r"[A-Z]", raw, re.I))
    has_digit = bool(re.search(r"\d", raw))
    if has_alpha and has_digit:
        return len(compact_token) >= 5
    if compact_token.isdigit() and 6 <= len(compact_token) <= 10:
        prefix = text[max(0, start - 3):start]
        return start == 0 or bool(re.search(r"(?:^|[\n|;])\s*$", prefix))
    return False


def _other_sku_spans(
    text: str,
    brand: str,
    model: str,
    cfg: Dict[str, Any],
    policy: Dict[str, Any],
) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    # Conservador: so tokens com estrutura de codigo; palavras comuns nao viram fronteira.
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9._/-]{4,})(?![A-Za-z0-9])", safe(text)):
        token = m.group(1)
        if _looks_like_other_sku_token(token, m.start(), text, brand, model, cfg, policy):
            out.append((m.start(), m.end(), token))
    return out


def _cut_related_tail(text: str, policy: Dict[str, Any], target_end: int = 0) -> str:
    folded = strip_accents(text).casefold()
    cut = len(text)
    for term in policy.get("related_section_terms", []) or []:
        t = strip_accents(term).casefold()
        pos = folded.find(t, max(0, target_end))
        if pos >= 0:
            cut = min(cut, pos)
    return text[:cut].strip()


def _target_context_has_only_relation(text: str, model: str, cfg: Dict[str, Any], terms: Sequence[str], window: int = 150) -> bool:
    folded = strip_accents(safe(text)).casefold()
    spans = _target_spans(text, model, cfg)
    if not spans:
        return False
    normalized_terms = [strip_accents(x).casefold() for x in terms or [] if safe(x)]
    relation_hits = 0
    for a, b in spans:
        context = folded[max(0, a-window):min(len(folded), b+window)]
        if any(t in context for t in normalized_terms):
            relation_hits += 1
    return relation_hits == len(spans)


def _title_has_conflicting_family_sku(title: str, model: str) -> bool:
    """Detecta variante explícita da mesma família no TITLE sem fuzzy matching.

    Exemplos bloqueados:
    - target GENIE 3000; title GENIE 600 / GENIE600
    - target 2446 21XC; title 2446-20

    A regra só dispara quando o primeiro token estrutural do target é repetido
    no título e o token de variante imediatamente seguinte é diferente.
    """
    normalized_title = strip_accents(safe(title)).casefold()
    model_tokens = re.findall(r"[A-Za-z]+\d*|\d+[A-Za-z]*", strip_accents(safe(model)))
    model_tokens = [t for t in model_tokens if compact(t)]
    if len(model_tokens) < 2:
        return False

    family = re.escape(model_tokens[0].casefold())
    expected_variant = compact(model_tokens[1])
    if not expected_variant:
        return False

    if re.fullmatch(r"[A-Za-z]+", model_tokens[0]):
        pattern = rf"(?<![a-z0-9]){family}[\s._/-]*([0-9][a-z0-9]*)"
    else:
        pattern = rf"(?<![a-z0-9]){family}[\s._/-]+([a-z0-9]+)"

    for m in re.finditer(pattern, normalized_title, flags=re.I):
        found = compact(m.group(1))
        if found and found != expected_variant and any(ch.isdigit() for ch in found):
            return True
    return False


def _isolate_target_block(
    brand: str,
    model: str,
    title: str,
    url: str,
    extracted_text: str,
    search_content: str,
    cfg: Dict[str, Any],
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Isola o bloco do SKU antes de TYPE/POWER.

    Multi-produto/catalogo pode ser aceito, mas somente o trecho entre fronteiras
    de outros SKUs chega ao Stage 1. Compatibilidade/related-only e paginas sem
    bloco target recuperavel sao rejeitadas.
    """
    iso = policy.get("isolation", {}) or {}
    generic_terms = policy.get("generic_page_terms", []) or []
    related_terms = policy.get("related_section_terms", []) or []
    compatibility_terms = policy.get("compatibility_terms", []) or []
    minimum_chars = int(iso.get("minimum_block_chars", 12) or 12)
    window = int(iso.get("target_window_chars", 850) or 850)
    max_other = int(iso.get("max_other_skus_after_isolation", 0) or 0)

    raw = safe(extracted_text) or safe(search_content)
    title_target = bool(_target_spans(title, model, cfg))
    url_target = bool(_target_spans(url, model, cfg))
    title_url_target = title_target or url_target
    body_target = bool(_target_spans(raw, model, cfg))
    search_target = bool(_target_spans(search_content, model, cfg))
    if not (title_url_target or body_target or search_target):
        return {"decision": "REJECT_WRONG_MODEL", "reason": "SKU alvo nao aparece em TITLE/URL/conteudo extraido", "content": "", "flags": ["NO_TARGET_MODEL"], "other_skus": []}

    title_other_spans = _other_sku_spans(title, brand, model, cfg, policy)
    title_family_conflict = _title_has_conflicting_family_sku(title, model)
    if not title_target and body_target and (title_other_spans or title_family_conflict):
        other_title_tokens = [x[2] for x in title_other_spans]
        if title_family_conflict and not other_title_tokens:
            other_title_tokens = ["SAME_FAMILY_DIFFERENT_VARIANT"]
        return {
            "decision": "REJECT_TITLE_OTHER_SKU_BODY_ONLY",
            "reason": "TITLE aponta para outro SKU/variante; target aparece somente no BODY",
            "content": "",
            "flags": ["TITLE_OTHER_SKU", "BODY_ONLY_TARGET"],
            "other_skus": other_title_tokens,
        }

    other_spans = _other_sku_spans(raw, brand, model, cfg, policy)
    generic = _term_present(f"{title}\n{url}\n{raw[:1200]}", generic_terms)
    multi = bool(other_spans)
    flags: List[str] = []
    if generic:
        flags.append("GENERIC_PAGE")
    if multi:
        flags.append("MULTI_PRODUCT_SOURCE")

    # Se o target so aparece como compatibilidade/related e nao domina TITLE/URL,
    # essa fonte nao pode definir o produto principal.
    if not title_url_target and _target_context_has_only_relation(raw, model, cfg, compatibility_terms):
        return {"decision": "REJECT_COMPATIBILITY_ONLY", "reason": "SKU aparece somente em contexto de compatibilidade/substituicao", "content": "", "flags": flags + ["COMPATIBILITY_ONLY"], "other_skus": [x[2] for x in other_spans]}
    if not title_url_target and _target_context_has_only_relation(raw, model, cfg, related_terms):
        return {"decision": "REJECT_RELATED_ONLY", "reason": "SKU aparece somente em secao de produtos relacionados", "content": "", "flags": flags + ["RELATED_ONLY"], "other_skus": [x[2] for x in other_spans]}

    # Fonte dedicada: modelo em TITLE/URL e sem sinais estruturais de outros SKUs.
    # Mesmo assim corta cauda de related products antes de liberar o texto.
    if title_url_target and not multi and not generic:
        clean = _cut_related_tail(raw, policy)
        if len(clean) >= minimum_chars:
            return {"decision": "ACCEPT_TARGET_CLEAN", "reason": "Fonte dedicada ao SKU; sem contaminacao estrutural detectada", "content": clean, "flags": flags, "other_skus": []}

    # Multi-produto/generico: o target precisa aparecer dentro do texto isolavel.
    if not body_target:
        return {"decision": "REJECT_GENERIC_NO_TARGET_BLOCK", "reason": "Fonte generica/multi-produto sem bloco textual do SKU alvo", "content": "", "flags": flags, "other_skus": [x[2] for x in other_spans]}

    chunks = [x.strip() for x in re.split(policy.get("isolation", {}).get("chunk_separator_regex", r"\s*\[\.\.\.\]\s*"), raw) if x.strip()]
    if not chunks:
        chunks = [raw]

    isolated_regions: List[str] = []
    for chunk in chunks:
        target_spans = _target_spans(chunk, model, cfg)
        if not target_spans:
            # Em fonte dedicada, Extract pode omitir o SKU no chunk; aceita apenas
            # chunk sem outro SKU e sem heading de related/compatibilidade.
            if title_url_target and bool(iso.get("allow_dedicated_page_chunks_without_repeated_sku", True)):
                if not _other_sku_spans(chunk, brand, model, cfg, policy) and not _term_present(chunk, related_terms):
                    isolated_regions.append(chunk)
            continue
        other = _other_sku_spans(chunk, brand, model, cfg, policy)
        for a, b in target_spans:
            left = max(0, a - window)
            right = min(len(chunk), b + window)
            for oa, ob, _ in other:
                if ob <= a:
                    left = max(left, ob)
                elif oa >= b:
                    right = min(right, oa)
                    break
            region = chunk[left:right].strip(" \n\t|;,-")
            local_target = _target_spans(region, model, cfg)
            target_end = local_target[0][1] if local_target else 0
            region = _cut_related_tail(region, policy, target_end=target_end)
            if len(region) >= minimum_chars:
                isolated_regions.append(region)

    # Dedupe mantendo ordem.
    unique_regions: List[str] = []
    seen_regions = set()
    for region in isolated_regions:
        key = re.sub(r"\s+", " ", region).strip().casefold()
        if key and key not in seen_regions:
            seen_regions.add(key)
            unique_regions.append(region)
    isolated = "\n[...]\n".join(unique_regions).strip()
    if not isolated:
        return {"decision": "REJECT_NON_ISOLATABLE", "reason": "SKU existe, mas nao foi possivel separar bloco target confiavel", "content": "", "flags": flags + ["TARGET_ISOLATION_FAILED"], "other_skus": [x[2] for x in other_spans]}

    residual_other = _other_sku_spans(isolated, brand, model, cfg, policy)
    if len(residual_other) > max_other:
        return {"decision": "REJECT_NON_ISOLATABLE", "reason": f"Bloco isolado ainda contem {len(residual_other)} SKU(s) concorrente(s)", "content": "", "flags": flags + ["RESIDUAL_OTHER_SKU"], "other_skus": [x[2] for x in residual_other]}

    return {
        "decision": "ACCEPT_TARGET_ISOLATED" if (generic or multi or not title_url_target) else "ACCEPT_TARGET_CLEAN",
        "reason": "Bloco do SKU alvo isolado antes da deteccao de TYPE/POWER",
        "content": isolated,
        "flags": flags + (["TARGET_ISOLATED"] if (generic or multi or not title_url_target) else []),
        "other_skus": [x[2] for x in other_spans],
    }


def _collection_state(brand: str, model: str, curated: List[Dict[str, Any]], cfg: Dict[str, Any], policy: Dict[str, Any]) -> str:
    states = policy.get("states", {}) or {}
    if not curated:
        return safe(states.get("none")) or "NO_TARGET_EVIDENCE"
    identity_policy = policy.get("identity", {}) or {}
    strong = 0
    probable = 0
    exact_isolated = 0
    urls = set()
    for res in curated:
        key = _url_key(res.get("url", ""))
        if key and key in urls:
            continue
        if key:
            urls.add(key)
        meta = _source_identity_for_result(brand, model, res, cfg)
        if meta["level"] == "STRONG":
            strong += 1
        elif meta["level"] == "PROBABLE":
            probable += 1
        if safe(res.get("_collection_decision")) == "ACCEPT_TARGET_ISOLATED" and bool(_target_spans(safe(res.get("content")), model, cfg)):
            exact_isolated += 1
    if strong >= int(identity_policy.get("ready_if_strong_sources", 1) or 1):
        return safe(states.get("ready")) or "READY_FOR_CLASSIFICATION"
    if exact_isolated >= int(identity_policy.get("ready_if_exact_isolated_sources", 1) or 1):
        return safe(states.get("ready")) or "READY_FOR_CLASSIFICATION"
    if probable >= int(identity_policy.get("ready_if_probable_sources", 2) or 2):
        return safe(states.get("ready")) or "READY_FOR_CLASSIFICATION"
    if strong or probable or exact_isolated:
        return safe(states.get("partial")) or "PARTIAL_TARGET_EVIDENCE"
    return safe(states.get("low")) or "LOW_TARGET_EVIDENCE"


def _make_curated_result(search_res: Dict[str, Any], isolation: Dict[str, Any], extract_depth: str, source_mode: str) -> Dict[str, Any]:
    return {
        "title": safe(search_res.get("title")),
        "url": safe(search_res.get("url")),
        "content": safe(isolation.get("content")),
        "score": search_res.get("score"),
        "raw_content": None,
        "_collection_decision": safe(isolation.get("decision")),
        "_collection_reason": safe(isolation.get("reason")),
        "_collection_quality_flags": list(isolation.get("flags", []) or []),
        "_collection_other_skus": list(isolation.get("other_skus", []) or []),
        "_collection_extract_depth": extract_depth,
        "_collection_source_mode": source_mode,
    }


class CollectionCostPolicyError(RuntimeError):
    """Violação de custo/arquitetura da coleta. Nunca deve ser convertida em retry."""


@dataclass
class CollectionRequestBudget:
    provider: str
    max_requests: int = MAX_COLLECTION_REQUESTS_PER_PRODUCT
    used: int = 0

    def reserve(self) -> None:
        if self.used >= self.max_requests:
            raise CollectionCostPolicyError(
                f"HARD LIMIT: provider={self.provider} tentou exceder "
                f"{self.max_requests} request por produto"
            )
        self.used += 1


def _collection_runtime_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("collection", {}) or {}


def _resolve_search_provider(args: Optional[argparse.Namespace], cfg: Dict[str, Any]) -> str:
    provider = safe(getattr(args, "search_provider", "")) if args is not None else ""
    if not provider:
        provider = safe((_collection_runtime_cfg(cfg)).get("provider")) or "tavily"
    provider = provider.casefold().strip()
    if provider not in ALLOWED_SEARCH_PROVIDERS:
        raise ValueError(
            f"Search provider inválido: {provider}. Permitidos={sorted(ALLOWED_SEARCH_PROVIDERS)}"
        )
    return provider


def _provider_cfg(cfg: Dict[str, Any], provider: str) -> Dict[str, Any]:
    providers = (_collection_runtime_cfg(cfg).get("providers", {}) or {})
    out = dict(providers.get(provider, {}) or {})
    # Compatibilidade segura com config R9.5 antiga: somente parâmetros semânticos
    # inofensivos são herdados. FALLBACK/RESCUE/EXTRACT nunca são lidos aqui.
    if provider == "tavily" and not out:
        legacy = cfg.get("tavily", {}) or {}
        legacy_search = legacy.get("search", {}) or {}
        out = {
            "base_url": "https://api.tavily.com/search",
            "api_key_env": "TAVILY_API_KEY",
            "requests_per_minute": legacy.get("requests_per_minute", 30),
            "timeout_seconds": legacy.get("timeout_seconds", 60),
            "max_results": legacy_search.get("max_results", 10),
            "chunks_per_source": legacy_search.get("chunks_per_source", 2),
            "search_depth": "basic",
        }
    return out


def validate_collection_cost_policy(cfg: Dict[str, Any]) -> List[str]:
    """Fail-closed: a coleta só inicia se a política 1 produto = 1 request estiver íntegra."""
    errors: List[str] = []
    ccfg = _collection_runtime_cfg(cfg)
    hard = ccfg.get("hard_limits", {}) or {}
    max_req = int(hard.get("max_requests_per_product", MAX_COLLECTION_REQUESTS_PER_PRODUCT) or 0)
    if max_req != MAX_COLLECTION_REQUESTS_PER_PRODUCT:
        errors.append(
            f"COLLECTION_COST_POLICY: max_requests_per_product deve ser exatamente "
            f"{MAX_COLLECTION_REQUESTS_PER_PRODUCT}; recebido={max_req}"
        )
    for key in ("allow_automatic_retry", "allow_secondary_search", "allow_provider_failover", "allow_extract"):
        if bool(hard.get(key, False)):
            errors.append(f"COLLECTION_COST_POLICY: {key} deve ser false")

    tav = _provider_cfg(cfg, "tavily")
    if tav:
        depth = safe(tav.get("search_depth", "basic")).casefold() or "basic"
        if depth != "basic":
            errors.append(f"TAVILY_COST_POLICY: search_depth deve ser basic; recebido={depth}")
        max_results = int(tav.get("max_results", 10) or 10)
        if max_results < 1 or max_results > 20:
            errors.append("TAVILY_CONFIG: max_results deve ficar entre 1 e 20")
        raw_mode = safe(tav.get("include_raw_content", "text")).casefold() or "text"
        if raw_mode not in {"text", "true"}:
            errors.append("TAVILY_CONFIG: include_raw_content deve ser text/true para one-step evidence")

    bright = _provider_cfg(cfg, "brightdata")
    if bright:
        response_format = safe(bright.get("format", "json")).casefold() or "json"
        if response_format != "json":
            errors.append(f"BRIGHTDATA_CONFIG: format deve ser json; recebido={response_format}")
        fmt = safe(bright.get("data_format", "parsed_light")).casefold() or "parsed_light"
        if fmt != "parsed_light":
            errors.append(f"BRIGHTDATA_CONFIG: somente parsed_light permitido; recebido={fmt}")
    return errors


def _provider_credentials(provider: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    pcfg = _provider_cfg(cfg, provider)
    if provider == "tavily":
        key_env = safe(pcfg.get("api_key_env")) or "TAVILY_API_KEY"
        api_key = os.getenv(key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"{key_env} não configurada no .env")
        return {"api_key": api_key, "key_env": key_env}
    if provider == "serper":
        key_env = safe(pcfg.get("api_key_env")) or "SERPER_API_KEY"
        api_key = os.getenv(key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"{key_env} não configurada no .env")
        return {"api_key": api_key, "key_env": key_env}
    if provider == "brightdata":
        key_env = safe(pcfg.get("api_key_env")) or "BRIGHTDATA_API_KEY"
        zone_env = safe(pcfg.get("zone_env")) or "BRIGHTDATA_ZONE"
        api_key = os.getenv(key_env, "").strip()
        zone = os.getenv(zone_env, "").strip() or safe(pcfg.get("zone"))
        if not api_key:
            raise RuntimeError(f"{key_env} não configurada no .env")
        if not zone:
            raise RuntimeError(f"{zone_env} não configurada no .env/config")
        return {"api_key": api_key, "zone": zone, "key_env": key_env, "zone_env": zone_env}
    raise ValueError(provider)


def _provider_post_once(
    provider: str,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    timeout: float,
    limiter: RateLimiter,
    budget: CollectionRequestBudget,
) -> Dict[str, Any]:
    """EXATAMENTE UM HTTP POST. Sem retry, redirect, fallback ou failover."""
    import requests

    budget.reserve()
    limiter.wait()
    try:
        response = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # Não repetir automaticamente: em timeout/conexão incerta o provider pode
        # ter recebido a request. O próximo passo deve ser decisão humana explícita.
        raise RuntimeError(
            f"{provider.upper()} REQUEST_OUTCOME_UNKNOWN: {type(exc).__name__}: {safe(exc)}"
        ) from exc

    status = int(response.status_code)
    if 200 <= status < 300:
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"{provider.upper()} retornou JSON inválido") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{provider.upper()} retornou payload não-objeto")
        return payload

    msg = safe(response.text)[:1200]
    if status in {401, 403}:
        raise OperationalPause(f"{provider.upper()}_AUTH", f"{provider.upper()} HTTP {status}: autenticação/permissão")
    if status in {429, 432, 433}:
        raise OperationalPause(f"{provider.upper()}_LIMIT", f"{provider.upper()} HTTP {status}: limite/rate/quota")
    if 300 <= status < 400:
        raise RuntimeError(f"{provider.upper()} redirect bloqueado pela política de 1 request: HTTP {status}")
    raise RuntimeError(f"{provider.upper()} HTTP {status}: {msg}")


def _rank_score(position: Any) -> float:
    try:
        pos = max(1, int(position or 1))
        return round(1.0 / pos, 6)
    except Exception:
        return 0.0


def _normalize_tavily_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normaliza a Search da Tavily preservando raw_content apenas em memória.

    O raw_content vem da MESMA Search request. Ele só será usado depois do
    Source Sieve; nunca prova identidade por si só.
    """
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(payload.get("results", []) or [], 1):
        if not isinstance(row, dict):
            continue
        out.append({
            "title": safe(row.get("title")),
            "url": safe(row.get("url")),
            "content": safe(row.get("content")),
            "score": row.get("score", _rank_score(idx)),
            "raw_content": safe(row.get("raw_content")),
            "_provider_position": idx,
            "_provider_meta": {"tavily_id": safe(row.get("id"))},
        })
    return out


def _normalize_serper_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Adapter inspirado no padrão Haystack/Qwen: organic -> documento normalizado."""
    rows = payload.get("organic", []) or []
    if not isinstance(rows, list):
        raise RuntimeError("SERPER_SCHEMA_INVALID: organic deve ser lista")
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        pos = row.get("position", idx)
        out.append({
            "title": safe(row.get("title")),
            "url": safe(row.get("link") or row.get("url")),
            "content": safe(row.get("snippet") or row.get("description")),
            "score": _rank_score(pos),
            "raw_content": "",
            "_provider_position": pos,
            "_provider_meta": {},
        })
    return out


def _normalize_brightdata_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Bright Data: SOMENTE Google Light JSON / parsed_light.

    Não existe fallback para Full JSON, Bing, Markdown, HTML ou outro schema.
    A documentação do Light JSON garante organic[] em respostas válidas.
    """
    rows = payload.get("organic")
    if not isinstance(rows, list):
        raise RuntimeError("BRIGHTDATA_LIGHT_JSON_SCHEMA_INVALID: organic[] ausente")
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        pos = row.get("global_rank", idx)
        extensions = row.get("extensions", []) if isinstance(row.get("extensions"), list) else []
        out.append({
            "title": safe(row.get("title")),
            "url": safe(row.get("link")),
            "content": safe(row.get("description")),
            "score": _rank_score(pos),
            "raw_content": "",
            "_provider_position": pos,
            "_provider_meta": {"extensions": extensions},
        })
    return out


def _brightdata_query_meta(payload: Dict[str, Any], submitted_query: str) -> Dict[str, Any]:
    general = payload.get("general", {}) if isinstance(payload.get("general"), dict) else {}
    spelling = payload.get("spelling", {}) if isinstance(payload.get("spelling"), dict) else {}
    return {
        "submitted_query": submitted_query,
        "effective_query": safe(general.get("query")) or submitted_query,
        "detected_query": safe(general.get("detected_query")),
        "spelling_present": bool(spelling),
        "spelling": spelling,
    }


def _query_identity_gate(
    provider: str,
    brand: str,
    search_model: str,
    query_meta: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[bool, str, List[str]]:
    """Valida se o buscador alterou a intenção Marca+Modelo antes do Source Sieve."""
    flags: List[str] = []
    if provider != "brightdata":
        return True, "QUERY_INTEGRITY_NOT_APPLICABLE", flags

    detected = safe(query_meta.get("detected_query"))
    effective = safe(query_meta.get("effective_query")) or safe(query_meta.get("submitted_query"))
    if not detected or norm_text(detected) == norm_text(effective):
        return True, "QUERY_INTEGRITY_OK", flags

    flags.append("SEARCH_ENGINE_QUERY_CHANGED")
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    match, _ = _model_match_in_field(search_model, detected, suffixes)
    brand_c = compact(brand)
    brand_ok = bool(brand_c and brand_c in compact(detected))
    target_retained = bool(match and brand_ok)

    if bool(query_meta.get("spelling_present")) and target_retained:
        flags.append("QUERY_SPELLING_CORRECTED_TARGET_RETAINED")
        return True, "QUERY_SPELLING_CORRECTED_SAFE", flags

    flags.append("QUERY_IDENTITY_DRIFT")
    return False, "QUERY_IDENTITY_DRIFT", flags



def _clean_semantic_text(text: str, policy: Dict[str, Any]) -> str:
    """Remove markup/boilerplate de forma conservadora e auditável."""
    value = safe(text)
    if not value:
        return ""
    cleaning = policy.get("cleaning", {}) or {}
    value = html.unescape(value)
    value = re.sub(r"(?is)<!--.*?-->", " ", value)
    value = re.sub(
        r"(?is)<(script|style|noscript|svg|iframe|nav|footer|header|form)\b[^>]*>.*?</\1\s*>",
        "\n",
        value,
    )
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = value.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)

    terms = [norm_text(x) for x in (cleaning.get("boilerplate_terms", []) or []) if norm_text(x)]
    patterns: List[re.Pattern] = []
    for raw_pattern in (cleaning.get("boilerplate_line_regexes", []) or []):
        try:
            patterns.append(re.compile(safe(raw_pattern), re.I))
        except re.error:
            continue

    max_boilerplate_chars = max(20, int(cleaning.get("max_boilerplate_line_chars", 180) or 180))
    drop_duplicates = bool(cleaning.get("drop_duplicate_lines", True))
    kept: List[str] = []
    seen = set()
    for raw_line in value.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip(" \t|")
        if not line:
            continue
        normalized = norm_text(line)
        if any(p.search(line) for p in patterns):
            continue
        if len(line) <= max_boilerplate_chars and any(term in normalized for term in terms):
            continue
        if drop_duplicates and normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        kept.append(line)

    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _target_evidence_excerpt(
    text: str,
    model: str,
    cfg: Dict[str, Any],
    policy: Dict[str, Any],
    max_chars: Optional[int] = None,
) -> str:
    """Retém uma janela limpa em torno do SKU; sem SKU, no máximo um snippet limpo."""
    cleaned = _clean_semantic_text(text, policy)
    if not cleaned:
        return ""
    persistence = policy.get("persistence", {}) or {}
    cleaning = policy.get("cleaning", {}) or {}
    limit = int(max_chars or persistence.get("evidence_excerpt_chars") or cleaning.get("evidence_excerpt_chars") or 1200)
    limit = max(300, min(4000, limit))
    spans = _target_spans(cleaned, model, cfg)
    if not spans:
        return cleaned[:limit].strip()

    max_occurrences = max(1, int(cleaning.get("max_target_occurrences", 2) or 2))
    per_window = max(300, limit // min(max_occurrences, len(spans)))
    regions: List[str] = []
    seen = set()
    for a, b in spans[:max_occurrences]:
        half = max(150, per_window // 2)
        region = cleaned[max(0, a-half):min(len(cleaned), b+half)].strip(" \n\t|;,-")
        region = _cut_related_tail(region, policy)
        key = norm_text(region)
        if region and key not in seen:
            seen.add(key)
            regions.append(region)
    return "\n[...]\n".join(regions)[:limit].strip()


def _body_identity_rescue(
    brand: str,
    model: str,
    raw_content: str,
    cfg: Dict[str, Any],
    policy: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Permite BODY rescue apenas com Marca+Modelo próximos; teto = PROBABLE."""
    rescue = policy.get("body_identity_rescue", {}) or {}
    if not bool(rescue.get("enabled", False)):
        return None
    cleaned = _clean_semantic_text(raw_content, policy)
    if not cleaned:
        return None
    window_chars = max(500, int(rescue.get("identity_window_chars", 2400) or 2400))
    excerpt = _target_evidence_excerpt(cleaned, model, cfg, policy, max_chars=window_chars)
    spans = _target_spans(excerpt, model, cfg)
    if not spans:
        return None
    if bool(rescue.get("block_compatibility_only", True)) and _target_context_has_only_relation(
        excerpt, model, cfg, policy.get("compatibility_terms", []) or [], window=220
    ):
        return None
    if bool(rescue.get("block_related_only", True)) and _target_context_has_only_relation(
        excerpt, model, cfg, policy.get("related_section_terms", []) or [], window=220
    ):
        return None

    require_brand = bool(rescue.get("require_brand_near_model", True))
    max_distance = max(120, int(rescue.get("brand_model_max_distance_chars", 500) or 500))
    brand_variants = [x for x in _brand_identity_variants(brand) if x]
    brand_near = False
    for a, b in spans:
        local_c = compact(excerpt[max(0, a-max_distance):min(len(excerpt), b+max_distance)])
        if any(v in local_c for v in brand_variants):
            brand_near = True
            break
    if require_brand and not brand_near:
        return None
    return {
        "level": "PROBABLE",
        "score": 76,
        "model_match": "EXACT",
        "model_match_field": "BODY",
        "source_weight": 0.68,
        "identity_origin": "BODY_RESCUE",
        "rescue_excerpt": excerpt,
    }


def _evidence_domain(url: str) -> str:
    try:
        host = (urlparse(safe(url)).hostname or "").casefold().strip()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _build_evidence_cache_entries(
    brand: str,
    model: str,
    rows: Sequence[Dict[str, Any]],
    audits: Sequence[Dict[str, Any]],
    curated: Sequence[Dict[str, Any]],
    cfg: Dict[str, Any],
    provider: str,
) -> List[Dict[str, Any]]:
    """Materializa evidência compacta, limpa e rastreável para fallback/revisão."""
    policy = _collection_policy(cfg)
    persistence = policy.get("persistence", {}) or {}
    max_sources = max(1, min(20, int(persistence.get("max_cached_sources", 10) or 10)))
    excerpt_chars = max(300, min(4000, int(persistence.get("evidence_excerpt_chars", 1200) or 1200)))

    audit_by_url: Dict[str, Dict[str, Any]] = {}
    for audit in audits:
        if isinstance(audit, dict):
            key = _url_key(audit.get("url", ""))
            if key and key not in audit_by_url:
                audit_by_url[key] = audit
    curated_by_url: Dict[str, Dict[str, Any]] = {}
    for item in curated:
        if isinstance(item, dict):
            key = _url_key(item.get("url", ""))
            if key and key not in curated_by_url:
                curated_by_url[key] = item

    rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1}
    tier_rank = {"PRIMARY": 0, "AUXILIARY_REVIEW_ONLY": 1, "REJECTED": 2}
    entries: List[Dict[str, Any]] = []
    seen_payloads = set()

    for idx, row in enumerate([x for x in rows if isinstance(x, dict)], 1):
        url = safe(row.get("url"))
        key = _url_key(url)
        audit = audit_by_url.get(key, {})
        curated_row = curated_by_url.get(key)
        title = _clean_semantic_text(safe(row.get("title")), policy)
        snippet = _clean_semantic_text(safe(row.get("content")), policy)
        identity = safe(
            (curated_row or {}).get("_collection_identity")
            or audit.get("identity_after")
            or audit.get("identity_before")
        ) or "WEAK"
        decision = safe(audit.get("decision") or audit.get("sieve_decision"))
        flags = [safe(x) for x in (audit.get("quality_flags", []) or []) if safe(x)]

        if curated_row is not None:
            excerpt = _clean_semantic_text(safe(curated_row.get("content")), policy)[:excerpt_chars]
            tier = "PRIMARY" if excerpt else "REJECTED"
            origin = safe(curated_row.get("_collection_source_mode")) or "CURATED"
        else:
            raw_content = safe(row.get("raw_content"))
            excerpt = _target_evidence_excerpt(raw_content or snippet, model, cfg, policy, max_chars=excerpt_chars)
            tier = "AUXILIARY_REVIEW_ONLY" if identity in {"STRONG", "PROBABLE"} and excerpt else "REJECTED"
            origin = "TAVILY_RAW_WINDOW" if raw_content else "SEARCH_SNIPPET"

        digest = ""
        excerpt_key = norm_text(excerpt)
        if excerpt_key:
            digest = hashlib.sha256(excerpt_key.encode("utf-8")).hexdigest()[:20]
            if digest in seen_payloads and tier != "PRIMARY":
                continue
            seen_payloads.add(digest)

        try:
            provider_score = float(row.get("score", 0) or 0)
        except Exception:
            provider_score = 0.0
        try:
            provider_position = int(row.get("_provider_position") or idx)
        except Exception:
            provider_position = idx

        entries.append({
            "source_id": provider_position,
            "title": title,
            "url": url,
            "domain": _evidence_domain(url),
            "provider": provider,
            "provider_score": provider_score,
            "identity": identity,
            "decision": decision,
            "quality_flags": flags,
            "tier": tier,
            "eligible_for_classification": tier == "PRIMARY",
            "eligible_for_llm_rescue": tier in {"PRIMARY", "AUXILIARY_REVIEW_ONLY"},
            "content_origin": origin,
            "snippet": snippet[:700],
            "evidence_excerpt": excerpt,
            "evidence_chars": len(excerpt),
            "evidence_hash": digest,
            "raw_content_available": bool(safe(row.get("raw_content"))),
        })
    entries.sort(key=lambda x: (
        tier_rank.get(safe(x.get("tier")), 9),
        -rank.get(safe(x.get("identity")), 0),
        -float(x.get("provider_score", 0) or 0),
        int(x.get("source_id", 9999) or 9999),
    ))
    return entries[:max_sources]


def _evidence_cache_quality(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [x for x in entries if isinstance(x, dict)]
    return {
        "schema_version": EVIDENCE_CACHE_SCHEMA_VERSION,
        "sources": len(rows),
        "primary": sum(1 for x in rows if safe(x.get("tier")) == "PRIMARY"),
        "auxiliary": sum(1 for x in rows if safe(x.get("tier")) == "AUXILIARY_REVIEW_ONLY"),
        "rejected": sum(1 for x in rows if safe(x.get("tier")) == "REJECTED"),
        "strong": sum(1 for x in rows if safe(x.get("identity")) == "STRONG"),
        "probable": sum(1 for x in rows if safe(x.get("identity")) == "PROBABLE"),
        "evidence_chars_total": sum(int(x.get("evidence_chars", 0) or 0) for x in rows),
    }


def _prepare_results_for_persistence(rows: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Persiste Search metadata + snippet limpo; raw_content integral é descartado."""
    policy = _collection_policy(cfg)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        original = safe(r.get("content"))
        r["title"] = _clean_semantic_text(safe(r.get("title")), policy)
        r["content"] = _clean_semantic_text(original, policy)
        r["_snippet_original_chars"] = len(original)
        r["_snippet_clean_chars"] = len(safe(r.get("content")))
        r["_raw_content_available"] = bool(safe(r.get("raw_content")))
        r["raw_content"] = None
        out.append(r)
    return out


def _llm_evidence_context(brand: str, model: str, entries: Sequence[Dict[str, Any]], max_sources: int = 6) -> str:
    """Contexto confirmado/auxiliar para LLM; REJECTED nunca entra aqui."""
    usable = [
        x for x in entries
        if isinstance(x, dict)
        and safe(x.get("tier")) in {"PRIMARY", "AUXILIARY_REVIEW_ONLY"}
        and safe(x.get("evidence_excerpt"))
    ][:max(1, int(max_sources or 6))]
    lines = [f"PRODUCT: {brand} | {model}"]
    for idx, row in enumerate(usable, 1):
        lines.extend([
            f"SOURCE {idx} [{safe(row.get('tier'))}] [{safe(row.get('identity'))}]",
            f"TITLE: {safe(row.get('title'))}",
            f"URL: {safe(row.get('url'))}",
            f"EVIDENCE: {safe(row.get('evidence_excerpt'))}",
        ])
    return "\n".join(lines).strip()


def _llm_unverified_review_context(
    brand: str,
    model: str,
    entries: Sequence[Dict[str, Any]],
    max_sources: int = 4,
) -> str:
    """Contexto separado para investigação humana/LLM; nunca autoriza classificação.

    Só inclui rejeições por identidade/isolamento. Wrong-model, compatibility-only
    e related-only ficam fora para reduzir risco de falso positivo.
    """
    allowed_decisions = {
        "REJECT_WEAK_IDENTITY",
        "REJECT_GENERIC_NO_TARGET_BLOCK",
        "REJECT_NON_ISOLATABLE",
    }
    usable = [
        x for x in entries
        if isinstance(x, dict)
        and safe(x.get("tier")) == "REJECTED"
        and safe(x.get("decision")) in allowed_decisions
        and safe(x.get("evidence_excerpt"))
    ][:max(0, int(max_sources or 0))]
    if not usable:
        return ""
    lines = [
        f"PRODUCT: {brand} | {model}",
        "WARNING: UNVERIFIED REVIEW CONTEXT. These sources failed automatic identity/isolation.",
        "Use them only to investigate the product; do not treat them as confirmed identity.",
    ]
    for idx, row in enumerate(usable, 1):
        lines.extend([
            f"REVIEW SOURCE {idx} [{safe(row.get('decision'))}] [{safe(row.get('identity'))}]",
            f"TITLE: {safe(row.get('title'))}",
            f"URL: {safe(row.get('url'))}",
            f"EVIDENCE: {safe(row.get('evidence_excerpt'))}",
        ])
    return "\n".join(lines).strip()


def _source_sieve(
    brand: str,
    search_model: str,
    rows: Sequence[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Peneira ANTES de qualquer aprofundamento.

    Só STRONG/PROBABLE sem sinais de compatibilidade/related seguem para
    raw_content/local fetch. Score/rank nunca substitui identidade.
    """
    policy = _collection_policy(cfg)
    compatibility = policy.get("compatibility_terms", []) or []
    related = policy.get("related_section_terms", []) or []
    generic = policy.get("generic_page_terms", []) or []
    accepted: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        identity_initial = _source_identity_for_result(brand, search_model, row, cfg)
        identity = dict(identity_initial)
        identity_origin = "TITLE_URL_SNIPPET"
        title = _clean_semantic_text(safe(row.get("title")), policy)
        snippet = _clean_semantic_text(safe(row.get("content")), policy)
        url = safe(row.get("url"))
        raw_content = safe(row.get("raw_content"))
        if identity["level"] not in {"STRONG", "PROBABLE"} and raw_content:
            body_rescue = _body_identity_rescue(brand, search_model, raw_content, cfg, policy)
            if body_rescue is not None:
                identity = dict(body_rescue)
                identity_origin = "BODY_RESCUE"
        search_text = f"{title}\n{snippet}".strip()
        flags: List[str] = []

        decision = "ACCEPT_SOURCE_SIEVE_BODY_RESCUE" if identity_origin == "BODY_RESCUE" else "ACCEPT_SOURCE_SIEVE"
        reason = (
            "Identidade target PROBABLE resgatada no BODY com Marca+Modelo próximos"
            if identity_origin == "BODY_RESCUE"
            else "Identidade target STRONG/PROBABLE e sem propósito negativo detectado"
        )
        if identity["level"] not in {"STRONG", "PROBABLE"}:
            decision = "REJECT_WEAK_IDENTITY"
            reason = f"Identity={identity['level']} abaixo de PROBABLE"
        elif _target_context_has_only_relation(search_text, search_model, cfg, compatibility, window=180):
            decision = "REJECT_COMPATIBILITY_ONLY"
            reason = "Marca/Modelo aparecem em contexto de compatibilidade/substituição"
            flags.append("COMPATIBILITY_ONLY")
        elif _target_context_has_only_relation(search_text, search_model, cfg, related, window=180):
            decision = "REJECT_RELATED_ONLY"
            reason = "Marca/Modelo aparecem em contexto de related/similar products"
            flags.append("RELATED_ONLY")

        if _term_present(f"{title}\n{url}\n{snippet}", generic):
            flags.append("GENERIC_PAGE_SIGNAL")

        audit = {
            "url": url,
            "title": title,
            "tavily_score": row.get("score"),
            "identity_before": identity["level"],
            "identity_initial": identity_initial["level"],
            "identity_origin": identity_origin,
            "model_match_field_before": identity["model_match_field"],
            "sieve_decision": decision,
            "sieve_reason": reason,
            "decision": decision,
            "reason": reason,
            "quality_flags": flags,
            "other_skus": [],
            "isolated_chars": 0,
            "extract_depth": "SOURCE_SIEVE",
            "source_mode": "SEARCH_SNIPPET",
            "identity_after": identity["level"] if decision.startswith("ACCEPT_") else "WEAK",
            "enrichment_attempted": False,
            "enrichment_mode": "NONE",
            "enrichment_reason": "",
        }
        audits.append(audit)
        if decision.startswith("ACCEPT_"):
            row2 = dict(row)
            row2["_collection_identity"] = identity["level"]
            row2["_collection_identity_origin"] = identity_origin
            row2["_sieve_audit_index"] = len(audits) - 1
            accepted.append(row2)

    rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1}
    accepted.sort(key=lambda r: (
        -rank.get(safe(r.get("_collection_identity")), 0),
        -float(r.get("score", 0) or 0),
        _url_key(r.get("url", "")),
    ))
    return accepted, audits


class _SimpleHTMLTextExtractor:
    """Fallback stdlib quando Trafilatura não estiver instalada."""
    def __init__(self) -> None:
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.parts: List[str] = []
                self.skip = 0
            def handle_starttag(self, tag, attrs):
                if tag.casefold() in {"script", "style", "noscript", "svg"}:
                    self.skip += 1
            def handle_endtag(self, tag):
                if tag.casefold() in {"script", "style", "noscript", "svg"} and self.skip:
                    self.skip -= 1
            def handle_data(self, data):
                if not self.skip:
                    text = safe(data)
                    if text:
                        self.parts.append(text)
        self._parser_cls = Parser

    def extract(self, html_text: str) -> str:
        parser = self._parser_cls()
        parser.feed(html_text)
        return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _extract_html_locally(html_text: str, url: str) -> Tuple[str, str]:
    """Extrai texto localmente. Trafilatura é preferida; stdlib é fallback."""
    try:
        from trafilatura import extract as trafilatura_extract  # type: ignore
        text = trafilatura_extract(
            html_text,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if safe(text):
            return safe(text), "LOCAL_TRAFILATURA"
    except ImportError:
        pass
    except Exception:
        pass
    text = _SimpleHTMLTextExtractor().extract(html_text)
    return text, "LOCAL_HTML_FALLBACK"


def _local_fetch_text(url: str, cfg: Dict[str, Any]) -> Tuple[str, str, str]:
    """Uma tentativa local, sem Search API, sem retry e sem browser pesado."""
    import requests

    ecfg = (_collection_runtime_cfg(cfg).get("local_enrichment", {}) or {})
    parsed = urlparse(safe(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "", "LOCAL_FETCH_SKIPPED", "URL não HTTP(S)"

    timeout = float(ecfg.get("timeout_seconds", 12) or 12)
    max_bytes = max(100_000, int(ecfg.get("max_bytes", 1_500_000) or 1_500_000))
    headers = {
        "User-Agent": safe(ecfg.get("user_agent")) or "Mozilla/5.0 ProductEnricher/1.0",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return "", "LOCAL_FETCH_FAILED", f"{type(exc).__name__}: {safe(exc)}"
    if not (200 <= int(response.status_code) < 300):
        return "", "LOCAL_FETCH_FAILED", f"HTTP {response.status_code}"

    ctype = safe(response.headers.get("Content-Type", "")).casefold()
    if ctype and not any(x in ctype for x in ["text/html", "application/xhtml", "text/plain"]):
        return "", "LOCAL_FETCH_UNSUPPORTED", f"Content-Type={ctype}"

    raw = response.content[:max_bytes]
    encoding = response.encoding or "utf-8"
    try:
        html_text = raw.decode(encoding, errors="replace")
    except Exception:
        html_text = raw.decode("utf-8", errors="replace")
    text, mode = _extract_html_locally(html_text, url)
    if not text:
        return "", mode, "Extração local não retornou texto"
    return text, mode, "OK"


def _cap_evidence_text(text: str, model: str, cfg: Dict[str, Any]) -> str:
    ecfg = (_collection_runtime_cfg(cfg).get("local_enrichment", {}) or {})
    limit = max(2000, int(ecfg.get("max_text_chars", 30000) or 30000))
    text = safe(text)
    if len(text) <= limit:
        return text
    spans = _target_spans(text, model, cfg)
    if spans:
        a, b = spans[0]
        half = max(1000, limit // 2)
        left = max(0, a - half)
        right = min(len(text), left + limit)
        left = max(0, right - limit)
        return text[left:right]
    return text[:limit]


def _curate_after_source_sieve(
    brand: str,
    search_model: str,
    provider: str,
    search_results: Sequence[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Source Sieve -> Evidence Enrichment -> Target Isolation.

    Tavily usa raw_content retornado NA MESMA Search. Serper/Bright Data podem
    aprofundar apenas URLs já aprovadas usando fetch local gratuito.
    """
    policy = _collection_policy(cfg)
    accepted, audits = _source_sieve(brand, search_model, search_results, cfg)
    ecfg = (_collection_runtime_cfg(cfg).get("local_enrichment", {}) or {})
    local_enabled = bool(ecfg.get("enabled", True))
    max_local = max(0, int(ecfg.get("max_urls_per_product", 2) or 2))
    min_snippet = max(0, int(ecfg.get("snippet_sufficient_chars", 220) or 220))
    local_used = 0
    curated: List[Dict[str, Any]] = []

    for res in accepted:
        audit_idx = int(res.pop("_sieve_audit_index"))
        audit = audits[audit_idx]
        snippet = _clean_semantic_text(safe(res.get("content")), policy)
        raw_content = safe(res.get("raw_content"))
        evidence_text = ""
        evidence_mode = "SEARCH_SNIPPET"
        enrichment_reason = "Snippet usado como evidência"

        if provider == "tavily" and raw_content:
            evidence_text = raw_content
            evidence_mode = "TAVILY_SEARCH_RAW_CONTENT"
            enrichment_reason = "raw_content veio na mesma Search request"
            audit["enrichment_attempted"] = True
            audit["enrichment_mode"] = evidence_mode
            audit["enrichment_reason"] = enrichment_reason
        elif local_enabled and local_used < max_local and len(snippet) < min_snippet:
            audit["enrichment_attempted"] = True
            local_used += 1
            fetched, mode, reason = _local_fetch_text(safe(res.get("url")), cfg)
            audit["enrichment_mode"] = mode
            audit["enrichment_reason"] = reason
            if fetched:
                evidence_text = fetched
                evidence_mode = mode
                enrichment_reason = reason
        if not evidence_text:
            evidence_text = snippet

        evidence_text = _clean_semantic_text(evidence_text, policy)
        evidence_text = _cap_evidence_text(evidence_text, search_model, cfg)
        isolation = _isolate_target_block(
            brand, search_model, safe(res.get("title")), safe(res.get("url")),
            evidence_text if evidence_mode != "SEARCH_SNIPPET" else "",
            snippet,
            cfg, policy,
        )
        decision = safe(isolation.get("decision"))
        audit["decision"] = decision
        audit["reason"] = safe(isolation.get("reason"))
        audit["quality_flags"] = list(dict.fromkeys(
            list(audit.get("quality_flags", []) or []) + list(isolation.get("flags", []) or [])
        ))
        audit["other_skus"] = list(isolation.get("other_skus", []) or [])
        audit["isolated_chars"] = len(safe(isolation.get("content")))
        audit["extract_depth"] = evidence_mode
        audit["source_mode"] = evidence_mode
        audit["enrichment_mode"] = audit.get("enrichment_mode") or evidence_mode
        audit["enrichment_reason"] = audit.get("enrichment_reason") or enrichment_reason

        if decision.startswith("ACCEPT_"):
            row = _make_curated_result(res, isolation, evidence_mode, evidence_mode)
            row["_collection_identity"] = safe(res.get("_collection_identity"))
            row["_collection_identity_origin"] = safe(res.get("_collection_identity_origin"))
            row["raw_content"] = None
            curated.append(row)

    return _dedupe_curated(curated), audits


def _provider_request_once(
    provider: str,
    query: str,
    cfg: Dict[str, Any],
    limiter: RateLimiter,
    budget: CollectionRequestBudget,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    pcfg = _provider_cfg(cfg, provider)
    creds = _provider_credentials(provider, cfg)
    timeout = float(pcfg.get("timeout_seconds", 60) or 60)
    max_results = max(1, int(pcfg.get("max_results", 10) or 10))

    if provider == "tavily":
        # HARD RULE: basic é fixo no executor. Config não pode elevar para advanced.
        max_results = min(20, max_results)
        chunks = max(1, min(3, int(pcfg.get("chunks_per_source", 3) or 3)))
        url = safe(pcfg.get("base_url")) or "https://api.tavily.com/search"
        headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
        body = {
            "query": query,
            "search_depth": "basic",
            "chunks_per_source": chunks,
            "max_results": max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": "text",
            "include_images": False,
            "include_image_descriptions": False,
            "include_favicon": False,
            "auto_parameters": False,
            "exact_match": False,
            "include_usage": True,
        }
        payload = _provider_post_once(provider, url, headers, body, timeout, limiter, budget)
        results = _normalize_tavily_results(payload)
        reported_credits = _tavily_usage_credits(payload)
        if reported_credits > 1.0:
            raise CollectionCostPolicyError(
                f"TAVILY COST POLICY VIOLATION: basic retornou usage.credits={reported_credits}; execução abortada"
            )
        return results, {
            "requests": budget.used,
            "provider_units": reported_credits or 1,
            "reported_credits": reported_credits,
            "max_results_requested": max_results,
            "raw_result_count": len(results),
            "raw_content_requested": True,
            "query_meta": {"submitted_query": query},
        }

    if provider == "serper":
        # Um único POST /search. Sem scrape endpoint, paginação, fallback ou segunda query.
        url = safe(pcfg.get("base_url")) or "https://google.serper.dev/search"
        headers = {"X-API-KEY": creds["api_key"], "Content-Type": "application/json"}
        body: Dict[str, Any] = {"q": query, "num": max_results}
        payload = _provider_post_once(provider, url, headers, body, timeout, limiter, budget)
        results = _normalize_serper_results(payload)
        return results, {
            "requests": budget.used,
            "provider_units": 1,
            "reported_credits": None,
            "max_results_requested": max_results,
            "raw_result_count": len(results),
            "raw_content_requested": False,
            "query_meta": {
                "submitted_query": query,
                "effective_query": safe(((payload.get("searchParameters") or {}) if isinstance(payload.get("searchParameters"), dict) else {}).get("q")) or query,
            },
        }

    if provider == "brightdata":
        # parsed_light retorna top 10 orgânicos em uma única request SERP.
        max_results = min(10, max_results)
        url = safe(pcfg.get("base_url")) or "https://api.brightdata.com/request"
        headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
        google_url = "https://www.google.com/search?" + urlencode({"q": query, "num": max_results})
        body = {
            "zone": creds["zone"],
            "url": google_url,
            # Bright Data SERP API contract for parsed Light JSON.
            # Hardcoded fail-closed: no Raw HTML / Full JSON / Bing / Markdown fallback.
            "format": "json",
            "data_format": "parsed_light",
        }
        payload = _provider_post_once(provider, url, headers, body, timeout, limiter, budget)
        results = _normalize_brightdata_results(payload)
        return results, {
            "requests": budget.used,
            "provider_units": 1,
            "reported_credits": None,
            "max_results_requested": max_results,
            "raw_result_count": len(results),
            "raw_content_requested": False,
            "query_meta": _brightdata_query_meta(payload, query),
        }

    raise ValueError(provider)


def _dedupe_curated(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_url: Dict[str, Dict[str, Any]] = {}
    rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1}
    for row in rows:
        key = _url_key(row.get("url", "")) or stable_json_hash(row)[:16]
        cur = by_url.get(key)
        if cur is None:
            by_url[key] = row
            continue
        old = (rank.get(safe(cur.get("_collection_identity")), 0), len(safe(cur.get("content"))))
        new = (rank.get(safe(row.get("_collection_identity")), 0), len(safe(row.get("content"))))
        if new > old:
            by_url[key] = row
    return list(by_url.values())


def _identity_variants_audit(model: str, cfg: Dict[str, Any]) -> str:
    suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []
    return " | ".join(
        f"{value}[{reason}]"
        for value, reason in _model_identity_variants(model, suffixes)
    )


def search_product_once(
    brand: str,
    model: str,
    cfg: Dict[str, Any],
    limiter: RateLimiter,
    provider: str,
) -> Dict[str, Any]:
    """R9.6p6: 1 Search -> Query Gate -> Source Sieve -> Evidence Enrichment -> Isolation.

    Contratos absolutos:
    - no máximo 1 HTTP request ao Search provider por produto;
    - Tavily sempre basic, com raw_content na MESMA request e usage auditado;
    - Bright Data somente Google Light JSON / parsed_light;
    - sem Search retry, segunda query, Advanced, /extract ou provider failover;
    - aprofundamento adicional só ocorre em URL já aprovada e via fetch LOCAL;
    - fetch local não pode gerar nova Search API request.
    """
    cost_errors = validate_collection_cost_policy(cfg)
    if cost_errors:
        raise CollectionCostPolicyError(" | ".join(cost_errors))

    provider = provider.casefold().strip()
    search_model = derive_search_model(brand, model, cfg)
    identity_variants_audit = _identity_variants_audit(model, cfg)
    query_model = derive_query_model(brand, model, cfg)
    ccfg = _collection_runtime_cfg(cfg)
    template = safe(ccfg.get("query_template")) or "{brand} {search_model}"
    query = template.format(brand=brand, model=model, search_model=query_model).strip()
    if not query:
        raise ValueError("Query de coleta vazia")

    budget = CollectionRequestBudget(provider=provider)
    raw_results, usage = _provider_request_once(provider, query, cfg, limiter, budget)
    if budget.used != 1:
        raise CollectionCostPolicyError(
            f"HARD LIMIT BROKEN: provider={provider}; request_count={budget.used}; esperado=1"
        )

    normalized: List[Dict[str, Any]] = []
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        annotated = dict(row)
        annotated["_search_query"] = query
        annotated["_search_stage"] = "SINGLE_REQUEST"
        annotated["_search_depth"] = "basic" if provider == "tavily" else "provider_default"
        annotated["_search_provider"] = provider
        normalized.append(annotated)

    query_meta = usage.get("query_meta", {}) if isinstance(usage.get("query_meta"), dict) else {}
    query_ok, query_decision, query_flags = _query_identity_gate(
        provider, brand, search_model, query_meta, cfg
    )

    if query_ok:
        curated, audits = _curate_after_source_sieve(
            brand, search_model, provider, normalized, cfg
        )
    else:
        curated = []
        audits = []
        for res in normalized:
            audits.append({
                "url": safe(res.get("url")),
                "title": safe(res.get("title")),
                "tavily_score": res.get("score"),
                "identity_before": "BLOCKED_QUERY_GATE",
                "model_match_field_before": "NONE",
                "sieve_decision": "REJECT_QUERY_IDENTITY_DRIFT",
                "sieve_reason": query_decision,
                "decision": "REJECT_QUERY_IDENTITY_DRIFT",
                "reason": query_decision,
                "quality_flags": list(query_flags),
                "other_skus": [],
                "isolated_chars": 0,
                "extract_depth": "QUERY_GATE",
                "source_mode": "QUERY_GATE",
                "identity_after": "WEAK",
                "enrichment_attempted": False,
                "enrichment_mode": "NONE",
                "enrichment_reason": "Query bloqueada antes do Source Sieve",
            })

    curated = _dedupe_curated(curated)
    id_rank = {"WEAK": 0, "CONTEXTUAL": 1, "PROBABLE": 2, "STRONG": 3}
    curated.sort(key=lambda r: (
        -id_rank.get(safe(r.get("_collection_identity")), 0),
        -float(r.get("score", 0) or 0),
        _url_key(r.get("url", "")),
    ))
    curated = curated[:max(1, int((cfg.get("evidence", {}) or {}).get("max_results", 5) or 5))]
    policy = _collection_policy(cfg)
    state = _collection_state(brand, search_model, curated, cfg, policy)

    for row in audits:
        row["search_stage"] = "SINGLE_REQUEST"
        row["search_query"] = query
        row["provider"] = provider
        if query_flags:
            row["quality_flags"] = list(dict.fromkeys(
                list(row.get("quality_flags", []) or []) + list(query_flags)
            ))

    enrichment_attempted = sum(1 for a in audits if bool(a.get("enrichment_attempted")))
    tavily_raw_used = sum(1 for a in audits if safe(a.get("enrichment_mode")) == "TAVILY_SEARCH_RAW_CONTENT")
    local_fetch_attempted = sum(
        1 for a in audits
        if bool(a.get("enrichment_attempted")) and safe(a.get("enrichment_mode")).startswith("LOCAL_")
    )
    local_fetch_success = sum(
        1 for a in audits
        if safe(a.get("enrichment_mode")) in {"LOCAL_TRAFILATURA", "LOCAL_HTML_FALLBACK"}
        and bool(a.get("enrichment_attempted"))
    )
    enrichment_success = tavily_raw_used + local_fetch_success
    query_audit = [{
        "provider": provider,
        "stage": "SINGLE_REQUEST",
        "query": query,
        "search_depth": "basic" if provider == "tavily" else "provider_default",
        "results": len(normalized),
        "source_sieve_accepted": sum(1 for a in audits if safe(a.get("sieve_decision")).startswith("ACCEPT_")),
        "enrichment_attempted": enrichment_attempted,
        "enrichment_success": enrichment_success,
        "accepted_total_after_stage": len(curated),
        "collection_state_after_stage": state,
        "query_integrity": query_decision,
        "effective_query": safe(query_meta.get("effective_query")),
        "detected_query": safe(query_meta.get("detected_query")),
        "spelling_present": bool(query_meta.get("spelling_present")),
        "provider_requests": budget.used,
        "provider_units": usage.get("provider_units"),
        "reported_credits": usage.get("reported_credits"),
        "max_results_requested": usage.get("max_results_requested"),
        "raw_content_requested": bool(usage.get("raw_content_requested")),
        "identity_variants": identity_variants_audit,
    }]

    # v4.0.7: cria Evidence Cache antes de descartar raw_content integral.
    evidence_cache = _build_evidence_cache_entries(
        brand, search_model, normalized, audits, curated, cfg, provider
    )
    evidence_cache_quality = _evidence_cache_quality(evidence_cache)
    if query_audit:
        query_audit[0].update({
            "evidence_cache_sources": evidence_cache_quality["sources"],
            "evidence_cache_primary": evidence_cache_quality["primary"],
            "evidence_cache_auxiliary": evidence_cache_quality["auxiliary"],
            "evidence_cache_rejected": evidence_cache_quality["rejected"],
            "evidence_cache_chars": evidence_cache_quality["evidence_chars_total"],
        })
    persisted_results = _prepare_results_for_persistence(normalized, cfg)

    tavily_credits = float(usage.get("reported_credits") or 0.0) if provider == "tavily" else 0.0
    return {
        "results": persisted_results,
        "_search_meta": {
            "provider": provider,
            "engine_version": COLLECTION_ENGINE_VERSION,
            "brand_original": brand,
            "model_original": model,
            "search_model": search_model,
            "identity_variants": identity_variants_audit,
            "query": query,
            "query_integrity": query_decision,
            "query_flags": query_flags,
            "effective_query": safe(query_meta.get("effective_query")),
            "detected_query": safe(query_meta.get("detected_query")),
            "spelling_present": bool(query_meta.get("spelling_present")),
            "request_count": budget.used,
            "max_requests_per_product": MAX_COLLECTION_REQUESTS_PER_PRODUCT,
            "exact_match": False,
            "include_domains": False,
            "country_filter": False,
            "language_filter": False,
            "auto_parameters": False,
            "provider_extract_enabled": False,
            "advanced_enabled": False,
            "fallback_enabled": False,
            "provider_failover_enabled": False,
            "local_enrichment_enabled": bool((ccfg.get("local_enrichment", {}) or {}).get("enabled", True)),
        },
        "_collection": {
            "version": safe(policy.get("VERSION")) or "R9.6",
            "engine_version": COLLECTION_ENGINE_VERSION,
            "provider": provider,
            "state": state,
            "search_model": search_model,
            "identity_variants": identity_variants_audit,
            "curated_results": curated,
            "evidence_cache_schema": EVIDENCE_CACHE_SCHEMA_VERSION,
            "evidence_cache": evidence_cache,
            "evidence_cache_quality": evidence_cache_quality,
            "source_audit": audits,
            "query_audit": query_audit,
            "raw_extract_results": [],
            "usage": usage,
            # Campo legado mantido para relatórios antigos. Não existe provider Extract.
            "credits": {"search": tavily_credits, "extract": 0, "total": tavily_credits},
            "hard_limits": {
                "max_requests_per_product": MAX_COLLECTION_REQUESTS_PER_PRODUCT,
                "automatic_retry": False,
                "secondary_search": False,
                "provider_extract": False,
                "advanced": False,
                "provider_failover": False,
            },
            "enrichment": {
                "source_sieve": True,
                "tavily_search_raw_content": provider == "tavily",
                "local_fetch_enabled": bool((ccfg.get("local_enrichment", {}) or {}).get("enabled", True)),
                "tavily_raw_content_used": tavily_raw_used,
                "local_fetch_attempted": local_fetch_attempted,
                "local_fetch_success": local_fetch_success,
                "enrichment_success_total": enrichment_success,
            },
        },
    }


def _curate_existing_payload(brand: str, model: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Recuradoria local do cache legado; zero Search e zero conteúdo inventado."""
    policy = _collection_policy(cfg)
    search_model = derive_search_model(brand, model, cfg)
    raw_results = [x for x in (payload.get("results", []) or []) if isinstance(x, dict)]
    curated: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []

    for res in raw_results:
        identity = _source_identity_for_result(brand, search_model, res, cfg)
        raw_content = safe(res.get("raw_content"))
        snippet = _clean_semantic_text(safe(res.get("content")), policy)
        evidence_text = _clean_semantic_text(raw_content or snippet, policy)
        evidence_text = _cap_evidence_text(evidence_text, search_model, cfg)
        isolation = _isolate_target_block(
            brand,
            search_model,
            _clean_semantic_text(safe(res.get("title")), policy),
            safe(res.get("url")),
            evidence_text if raw_content else "",
            snippet,
            cfg,
            policy,
        )
        decision = safe(isolation.get("decision"))
        if decision.startswith("ACCEPT_"):
            row = _make_curated_result(res, isolation, "CACHE_RAW", "CACHE_REBUILD_NO_SEARCH")
            row["_collection_identity"] = _source_identity_for_result(brand, search_model, row, cfg)["level"]
            curated.append(row)
        audit.append({
            "url": safe(res.get("url")),
            "title": _clean_semantic_text(safe(res.get("title")), policy),
            "tavily_score": res.get("score"),
            "identity_before": identity["level"],
            "identity_after": identity["level"],
            "model_match_field_before": identity["model_match_field"],
            "sieve_decision": decision,
            "decision": decision,
            "reason": safe(isolation.get("reason")),
            "quality_flags": list(isolation.get("flags", []) or []),
            "other_skus": list(isolation.get("other_skus", []) or []),
            "isolated_chars": len(safe(isolation.get("content"))),
            "extract_depth": "CACHE_RAW",
            "source_mode": "CACHE_REBUILD_NO_SEARCH",
        })

    curated = _dedupe_curated(curated)
    curated.sort(key=lambda x: (
        safe(x.get("_collection_identity")) != "STRONG",
        -float(x.get("score", 0) or 0),
    ))
    curated = curated[:max(1, int((cfg.get("evidence", {}) or {}).get("max_results", 5) or 5))]
    state = _collection_state(brand, search_model, curated, cfg, policy)
    provider = safe(
        ((payload.get("_search_meta") or {}) if isinstance(payload.get("_search_meta"), dict) else {}).get("provider")
    ) or "cache"
    evidence_cache = _build_evidence_cache_entries(
        brand, search_model, raw_results, audit, curated, cfg, provider
    )
    quality = _evidence_cache_quality(evidence_cache)

    out = dict(payload)
    out["results"] = _prepare_results_for_persistence(raw_results, cfg)
    out["_collection"] = {
        "version": safe(policy.get("VERSION")) or "R9.6p7.2",
        "engine_version": COLLECTION_ENGINE_VERSION,
        "provider": provider,
        "state": state,
        "search_model": search_model,
        "identity_variants": _identity_variants_audit(model, cfg),
        "curated_results": curated,
        "evidence_cache_schema": EVIDENCE_CACHE_SCHEMA_VERSION,
        "evidence_cache": evidence_cache,
        "evidence_cache_quality": quality,
        "source_audit": audit,
        "query_audit": [{
            "stage": "CACHE_REBUILD",
            "query": "",
            "search_depth": "CACHE_RAW",
            "results": len(raw_results),
            "accepted_total_after_stage": len(curated),
            "collection_state_after_stage": state,
            "evidence_cache_sources": quality["sources"],
            "evidence_cache_primary": quality["primary"],
            "evidence_cache_auxiliary": quality["auxiliary"],
            "evidence_cache_rejected": quality["rejected"],
            "evidence_cache_chars": quality["evidence_chars_total"],
            "search_credits_total": 0,
            "extract_credits_total": 0,
        }],
        "raw_extract_results": [],
        "credits": {"search": 0, "extract": 0, "total": 0},
    }
    return out

def _export_collection_quality_report(products: List[Dict[str, Any]], sources: List[Dict[str, Any]], queries: List[Dict[str, Any]], path: Path) -> None:
    import pandas as pd
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f".tmp_{os.getpid()}" + path.suffix)
    state_counts = defaultdict(int)
    decision_counts = defaultdict(int)
    provider_counts = defaultdict(int)
    requests_total = 0
    tavily_credits = 0.0
    max_requests_observed = 0
    query_integrity_counts = defaultdict(int)
    local_enrichment_attempts = 0
    for row in products:
        state_counts[safe(row.get("Collection State"))] += 1
        provider = safe(row.get("Provider"))
        if provider:
            provider_counts[provider] += 1
        try:
            req = int(row.get("Provider Requests", 0) or 0)
        except Exception:
            req = 0
        requests_total += req
        max_requests_observed = max(max_requests_observed, req)
        qint = safe(row.get("Query Integrity"))
        if qint:
            query_integrity_counts[qint] += 1
        try:
            local_enrichment_attempts += int(row.get("Local Enrichment Attempts", 0) or 0)
        except Exception:
            pass
        if provider.casefold() == "tavily":
            try:
                tavily_credits += float(row.get("Reported Credits", 0) or 0)
            except Exception:
                pass
    for row in sources:
        decision_counts[safe(row.get("Decision"))] += 1
    resumo = [
        {"Indicador": "Produtos processados", "Valor": len(products)},
        {"Indicador": "Provider requests observadas", "Valor": requests_total},
        {"Indicador": "Máximo requests observado por produto", "Valor": max_requests_observed},
        {"Indicador": "HARD LIMIT requests por produto", "Valor": MAX_COLLECTION_REQUESTS_PER_PRODUCT},
        {"Indicador": "Fontes auditadas", "Valor": len(sources)},
        {"Indicador": "Queries executadas", "Valor": len(queries)},
        {"Indicador": "Tavily credits reportados", "Valor": round(tavily_credits, 4)},
        {"Indicador": "Local enrichment attempts", "Valor": local_enrichment_attempts},
    ]
    resumo += [{"Indicador": f"PROVIDER::{k}", "Valor": v} for k, v in sorted(provider_counts.items()) if k]
    resumo += [{"Indicador": f"QUERY::{k}", "Valor": v} for k, v in sorted(query_integrity_counts.items()) if k]
    resumo += [{"Indicador": f"STATE::{k}", "Valor": v} for k, v in sorted(state_counts.items()) if k]
    resumo += [{"Indicador": f"SOURCE::{k}", "Valor": v} for k, v in sorted(decision_counts.items()) if k]
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        pd.DataFrame(resumo).to_excel(writer, sheet_name="Resumo", index=False)
        pd.DataFrame(products).to_excel(writer, sheet_name="Produtos", index=False)
        pd.DataFrame(sources).to_excel(writer, sheet_name="Fontes", index=False)
        pd.DataFrame(queries).to_excel(writer, sheet_name="Queries", index=False)
    os.replace(tmp, path)


def _failed_collection_payload(
    brand: str,
    model: str,
    provider: str,
    cfg: Dict[str, Any],
    state: str,
    reason: str,
) -> Dict[str, Any]:
    """Tombstone auditável: uma request tentada nunca volta automaticamente para a fila."""
    search_model = derive_search_model(brand, model, cfg)
    return {
        "results": [],
        "_search_meta": {
            "provider": provider,
            "engine_version": COLLECTION_ENGINE_VERSION,
            "brand_original": brand,
            "model_original": model,
            "search_model": search_model,
            "request_count": 1,
            "max_requests_per_product": MAX_COLLECTION_REQUESTS_PER_PRODUCT,
            "error": reason,
        },
        "_collection": {
            "version": safe((_collection_policy(cfg)).get("VERSION")) or "R9.5",
            "engine_version": COLLECTION_ENGINE_VERSION,
            "provider": provider,
            "state": state,
            "search_model": search_model,
            "curated_results": [],
            "source_audit": [],
            "query_audit": [],
            "raw_extract_results": [],
            "usage": {"requests": 1, "provider_units": None, "reported_credits": None},
            "credits": {"search": 0, "extract": 0, "total": 0},
            "error": reason,
            "hard_limits": {
                "max_requests_per_product": MAX_COLLECTION_REQUESTS_PER_PRODUCT,
                "automatic_retry": False,
                "secondary_search": False,
                "extract": False,
                "advanced": False,
                "provider_failover": False,
            },
        },
    }


def collect_products(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> bool:
    provider = _resolve_search_provider(args, cfg)
    policy_errors = validate_collection_cost_policy(cfg)
    if policy_errors:
        raise CollectionCostPolicyError(" | ".join(policy_errors))
    _provider_credentials(provider, cfg)  # preflight antes da primeira request

    input_path = Path(args.input or cfg.get("files", {}).get("input", "Base de Produtos.xlsx"))
    cache_path = Path(args.cache or cfg["files"]["cache"])
    db_path = Path(args.db or cfg["files"]["classification_db"])
    pre_report = Path(args.precollection_output or cfg.get("files", {}).get("precollection_report", "Pre_Coleta_R9_6.xlsx"))
    report_path = Path(cfg.get("files", {}).get("collection_report", "Coleta_Search_R9_6.xlsx"))

    plan = build_precollection_plan(
        input_path=input_path, cache_path=cache_path, report_path=pre_report,
        recollect_empty=bool(args.recollect_empty), recollect_low_evidence=bool(args.recollect_low_evidence),
        cfg=cfg, max_tavily_new=int(args.max_tavily_new or 0),
    )
    candidates = [(r.brand_original, r.model_original, r.raw_state) for r in plan.candidates]

    print()
    print("=" * 86)
    print(f" {COLLECTION_LABEL} PRÉ-COLETA — 1 SEARCH / SOURCE SIEVE / EVIDENCE ENRICHMENT")
    print("=" * 86)
    for label in [
        "Linhas recebidas", "Linhas válidas", "Linhas inválidas", "Produtos únicos por business_key",
        "Duplicidades internas removidas", "Já no cache", "RAW_EMPTY", "RAW_CORRUPT", "RAW_MISSING",
        "Fila real Tavily antes do limite", "Fila Tavily desta execução",
    ]:
        display = label.replace("Tavily", "Search")
        print(f"{display:.<54} {plan.summary.get(label, 0)}")
    print("-" * 86)
    print(f"Provider........................................ {provider.upper()}")
    print("HARD LIMIT...................................... 1 PRODUTO = MAX. 1 REQUEST")
    print("Automatic retry................................. DESABILITADO")
    print("Secondary/Fallback/Rescue search................ DESABILITADO")
    print("Provider Extract (/extract etc.)............... DESABILITADO")
    print("Tavily raw_content na mesma Search.............. HABILITADO / BASIC / 1 CRÉDITO AUDITADO")
    print("Local URL enrichment pós-sieve.................. HABILITADO (Trafilatura; 0 Search requests)")
    print("Advanced........................................ DESABILITADO")
    print("Provider failover............................... DESABILITADO")
    print("Search.......................................... ABERTO / SEM DOMAIN-COUNTRY-LANGUAGE FILTER")
    print("Stage 1 recebe.................................. SOMENTE curated_results")
    print(f"Requests máximas planejadas..................... {len(candidates)}")
    print(f"Relatório pré-coleta............................ {pre_report}")
    print("=" * 86)

    if not candidates:
        LOG.info("Nenhuma nova consulta Search necessária.")
        return True
    if not args.yes:
        print(f"Fila real desta execução: {len(candidates)} produto(s) = no máximo {len(candidates)} request(s).")
        if input("Digite CONFIRMAR para iniciar a coleta: ").strip().upper() != "CONFIRMAR":
            LOG.info("Coleta cancelada. Nenhuma chamada ao provider foi realizada.")
            return False

    pcfg = _provider_cfg(cfg, provider)
    limiter = RateLimiter(float(pcfg.get("requests_per_minute", 30) or 30))
    db = ClassificationDB(db_path)
    run_id = db.start_run("collect", rules.hash, f"SEARCH_{COLLECTION_LABEL}:{provider}")
    writer = TavilyWritableStore(cache_path)  # nome legado; payload agora registra provider explicitamente
    product_rows: List[Dict[str, Any]] = []
    source_rows: List[Dict[str, Any]] = []
    query_rows: List[Dict[str, Any]] = []
    completed = True
    total_requests = 0
    try:
        for idx, (brand, model, prior_state) in enumerate(candidates, 1):
            key = business_key(brand, model)
            try:
                payload = search_product_once(brand, model, cfg, limiter, provider)
                coll = payload.get("_collection", {}) or {}
                usage = coll.get("usage", {}) or {}
                req_count = int(usage.get("requests", 0) or 0)
                if req_count != 1:
                    raise CollectionCostPolicyError(
                        f"REQUEST COUNT INVALIDO: {brand} {model} retornou requests={req_count}"
                    )
                total_requests += req_count
                state = safe(coll.get("state")) or "NO_TARGET_EVIDENCE"
                curated = coll.get("curated_results", []) or []
                raw_n = len(payload.get("results", []) or [])
                # Nunca substituir RAW com fontes por uma recoleta que retornou zero.
                preserve_previous = prior_state in {"RAW_OK", "RAW_OK_MERGED"} and raw_n == 0
                if preserve_previous:
                    db.event(run_id, key, "RECOLLECTION_EMPTY_OLD_CACHE_PRESERVED",
                             f"provider={provider}; new_raw=0; previous={prior_state}")
                    LOG.warning("RECOLETA VAZIA: preservando cache anterior de %s %s", brand, model)
                else:
                    writer.put(
                        brand, model, payload,
                        archive_reason=(
                            f"RECOLLECT_{provider.upper()}_{COLLECTION_LABEL}"
                            if prior_state in {"RAW_OK", "RAW_OK_MERGED", "RAW_EMPTY"}
                            else f"COLLECT_{provider.upper()}_{COLLECTION_LABEL}"
                        ),
                    )
                reported_credits = usage.get("reported_credits")
                product_rows.append({
                    "Marca": brand, "Modelo": model, "Search Model": safe(coll.get("search_model")),
                    "Identity Variants": safe(coll.get("identity_variants")),
                    "Provider": provider, "Estado Anterior": prior_state, "Collection State": state,
                    "RAW Results": raw_n, "Curated Sources": len(curated),
                    "Provider Requests": req_count, "Provider Units": usage.get("provider_units"),
                    "Reported Credits": reported_credits,
                    "Max Results Requested": usage.get("max_results_requested"),
                    "Query Integrity": safe((payload.get("_search_meta", {}) or {}).get("query_integrity")),
                    "Local Enrichment Attempts": int(((coll.get("enrichment", {}) or {}).get("local_fetch_attempted", 0)) or 0),
                    "Data": now_iso(),
                })
                for s in coll.get("source_audit", []) or []:
                    source_rows.append({
                        "Marca": brand, "Modelo": model, "Search Model": safe(coll.get("search_model")),
                        "Identity Variants": safe(coll.get("identity_variants")),
                        "Provider": provider,
                        "URL": s.get("url"), "Title": s.get("title"), "Provider Rank Score": s.get("tavily_score"),
                        "Identity Before": s.get("identity_before"), "Model Field": s.get("model_match_field_before"),
                        "Decision": s.get("decision"), "Reason": s.get("reason"),
                        "Quality Flags": " | ".join(s.get("quality_flags", []) or []),
                        "Other SKUs": " | ".join(s.get("other_skus", []) or []),
                        "Isolated Chars": s.get("isolated_chars"), "Evidence Mode": s.get("extract_depth"),
                        "Source Mode": s.get("source_mode"), "Search Stage": s.get("search_stage"),
                        "Sieve Decision": s.get("sieve_decision"), "Sieve Reason": s.get("sieve_reason"),
                        "Enrichment Attempted": s.get("enrichment_attempted"), "Enrichment Mode": s.get("enrichment_mode"),
                        "Enrichment Reason": s.get("enrichment_reason"),
                    })
                for q in coll.get("query_audit", []) or []:
                    query_rows.append({"Marca": brand, "Modelo": model, **q})
                db.event(
                    run_id, key, f"SEARCH_{COLLECTION_LABEL}_SAVED",
                    f"provider={provider}; state={state}; raw={raw_n}; curated={len(curated)}; requests={req_count}; credits={reported_credits}",
                )
                LOG.info(
                    f"COLETA {COLLECTION_LABEL} %d/%d | %s %s | provider=%s | state=%s | raw=%d curated=%d | requests=%d | credits=%s",
                    idx, len(candidates), brand, model, provider, state, raw_n, len(curated), req_count,
                    (reported_credits if reported_credits is not None else "N/A"),
                )
            except CollectionCostPolicyError as exc:
                db.event(run_id, key, "COLLECTION_COST_POLICY_VIOLATION", safe(exc))
                LOG.critical("%s", exc)
                completed = False
                break
            except OperationalPause as exc:
                db.event(run_id, key, exc.code, safe(exc))
                product_rows.append({
                    "Marca": brand, "Modelo": model, "Provider": provider,
                    "Estado Anterior": prior_state, "Collection State": exc.code,
                    "Provider Requests": 1, "Data": now_iso(),
                })
                LOG.error("%s. Coleta pausada; tudo salvo até aqui permanece persistido.", exc)
                completed = False
                break
            except Exception as exc:
                # Fail-closed e sem retry. Persiste tombstone para o mesmo item não voltar
                # silenciosamente à fila em uma nova execução e potencialmente gastar de novo.
                failure = _failed_collection_payload(
                    brand, model, provider, cfg, "REQUEST_ATTEMPTED_NO_AUTO_RETRY", safe(exc)
                )
                if prior_state in {"RAW_OK", "RAW_OK_MERGED", "RAW_EMPTY"}:
                    # Uma falha de recoleta não pode substituir material já existente.
                    db.event(run_id, key, "RECOLLECTION_ERROR_OLD_CACHE_PRESERVED", safe(exc))
                else:
                    writer.put(brand, model, failure, archive_reason=f"{provider.upper()}_REQUEST_ERROR_{COLLECTION_LABEL}")
                total_requests += 1
                db.event(run_id, key, f"SEARCH_{COLLECTION_LABEL}_ERROR_NO_RETRY", safe(exc))
                product_rows.append({
                    "Marca": brand, "Modelo": model, "Provider": provider,
                    "Estado Anterior": prior_state, "Collection State": "REQUEST_ATTEMPTED_NO_AUTO_RETRY",
                    "Provider Requests": 1, "Reason": safe(exc), "Data": now_iso(),
                })
                LOG.error("Falha coleta R9.6 SEM RETRY | %s %s | provider=%s | %s", brand, model, provider, exc)
            if idx % 25 == 0:
                _export_collection_quality_report(product_rows, source_rows, query_rows, report_path)
        _export_collection_quality_report(product_rows, source_rows, query_rows, report_path)
        db.finish_run(
            run_id,
            "COMPLETED" if completed else "PAUSED",
            f"executados={len(product_rows)} planejados={len(candidates)} requests={total_requests} provider={provider}",
        )
        LOG.info(
            f"COLLECTION {COLLECTION_LABEL} FINAL | provider=%s | produtos=%d | requests=%d | hard_cap=%d/produto | relatório=%s",
            provider, len(product_rows), total_requests, MAX_COLLECTION_REQUESTS_PER_PRODUCT, report_path,
        )
        return completed
    except KeyboardInterrupt:
        _export_collection_quality_report(product_rows, source_rows, query_rows, report_path)
        db.finish_run(
            run_id, "INTERRUPTED",
            f"RAW/Collection Quality commitado por produto | requests={total_requests} | provider={provider}",
        )
        LOG.warning("Coleta interrompida. Cache preservado até o último produto concluído.")
        raise
    finally:
        writer.close()
        db.close()


def curate_cache_without_tavily(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Recuradoria local do cache sem Search; por padrão, escopo = arquivo de entrada atual."""
    cache_path = Path(args.cache or cfg["files"]["cache"])
    report_path = Path(cfg.get("files", {}).get("collection_report_cache_rebuild", "Curadoria_Cache_R9_5.xlsx"))
    input_path = Path(args.input or cfg.get("files", {}).get("input", "Base de Produtos.xlsx"))
    store = TavilyReadOnlyStore(cache_path)
    writer = TavilyWritableStore(cache_path)
    products: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    queries: List[Dict[str, Any]] = []
    try:
        catalog = store.catalog()
        if not bool(getattr(args, "curate_all_cache", False)):
            if not input_path.exists():
                raise RuntimeError(
                    f"CURATION_INPUT_SCOPE: arquivo de entrada não encontrado: {input_path}. "
                    "Use --curate-all-cache somente quando quiser explicitamente percorrer todo o cache."
                )
            allowed_keys = {
                row.key for row in read_input_rows(input_path)
                if row.valid and row.key
            }
            if not allowed_keys:
                raise RuntimeError("CURATION_INPUT_SCOPE: arquivo de entrada não possui Marca+Modelo válida.")
            before = len(catalog)
            catalog = [row for row in catalog if row[2] in allowed_keys]
            LOG.info(
                "CURATION INPUT SCOPE | arquivo=%s | business_keys=%d | cache_total=%d | selecionados=%d | Search=0",
                input_path, len(allowed_keys), before, len(catalog),
            )
        else:
            LOG.warning("CURATION ALL CACHE | produtos=%d | Search=0 | escopo explícito=TODO_CACHE", len(catalog))
        if args.max_items:
            catalog = catalog[:int(args.max_items)]
        for idx, (brand, model, key) in enumerate(catalog, 1):
            raw = store.get(brand, model)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                continue
            existing = raw.payload.get("_collection") if isinstance(raw.payload, dict) else None
            if (not bool(getattr(args, "force_curate", False)) and isinstance(existing, dict)
                    and safe(existing.get("version")) == safe((_collection_policy(cfg)).get("VERSION"))):
                continue
            rebuilt = _curate_existing_payload(brand, model, raw.payload, cfg)
            coll = rebuilt.get("_collection", {}) or {}
            old_curated = len((existing or {}).get("curated_results", []) or []) if isinstance(existing, dict) else 0
            new_curated = len(coll.get("curated_results", []) or [])
            if new_curated < old_curated:
                LOG.warning("CURADORIA SEM GRAVAR | %s %s | fontes antigas=%d novas=%d",
                            brand, model, old_curated, new_curated)
                continue
            writer.put(brand, model, rebuilt, archive_reason="CACHE_CURATED_R9_6P7_2_NO_SEARCH")
            products.append({
                "Marca": brand, "Modelo": model, "Search Model": coll.get("search_model"),
                "Identity Variants": coll.get("identity_variants") or _identity_variants_audit(model, cfg),
                "Collection State": coll.get("state"), "RAW Results": len(rebuilt.get("results", []) or []),
                "Curated Sources": len(coll.get("curated_results", []) or []), "Tavily Credits": 0, "Data": now_iso(),
            })
            for s in coll.get("source_audit", []) or []:
                sources.append({
                    "Marca": brand, "Modelo": model,
                    "Identity Variants": coll.get("identity_variants") or _identity_variants_audit(model, cfg),
                    "URL": s.get("url"), "Title": s.get("title"),
                    "Tavily Score": s.get("tavily_score"), "Identity Before": s.get("identity_before"),
                    "Model Field": s.get("model_match_field_before"), "Decision": s.get("decision"), "Reason": s.get("reason"),
                    "Quality Flags": " | ".join(s.get("quality_flags", []) or []), "Other SKUs": " | ".join(s.get("other_skus", []) or []),
                    "Isolated Chars": s.get("isolated_chars"), "Extract Depth": "CACHE_RAW", "Source Mode": "CACHE_REBUILD_NO_TAVILY",
                })
            for q in coll.get("query_audit", []) or []:
                queries.append({"Marca": brand, "Modelo": model, **q})
            if idx % 100 == 0:
                LOG.info("CURADORIA CACHE %d/%d | salvos=%d", idx, len(catalog), len(products))
                _export_collection_quality_report(products, sources, queries, report_path)
        _export_collection_quality_report(products, sources, queries, report_path)
        LOG.info("CURADORIA CACHE CONCLUIDA | produtos=%d | Search=0 | arquivo=%s", len(products), report_path)
    finally:
        store.close()
        writer.close()


# -----------------------------------------------------------------------------
# Evidências e Stage 1 determinístico — R9.3
# -----------------------------------------------------------------------------
@dataclass
class Source:
    source_id: int
    title: str
    content: str
    url: str
    relevance: str
    quality_flags: List[str] = field(default_factory=list)
    type_hits: Dict[str, List[str]] = field(default_factory=dict)
    power_hits: Dict[str, List[str]] = field(default_factory=dict)
    power_evidence_fields: Dict[str, List[str]] = field(default_factory=dict)
    identity_score: int = 0
    model_match: str = "NONE"
    model_match_field: str = "NONE"
    source_weight: float = 0.0
    identity_conflict: bool = False
    type_scores: Dict[str, float] = field(default_factory=dict)
    power_scores: Dict[str, float] = field(default_factory=dict)
    type_evidence_fields: Dict[str, List[str]] = field(default_factory=dict)
    collection_decision: str = ""
    collection_reason: str = ""
    tavily_score: float = 0.0
    collection_source_mode: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.content}".strip()


def taxonomy_evidence_text(
    sources: Sequence["Source"],
    product_type: str,
    source_ids: Optional[Sequence[int]] = None,
) -> str:
    """Monta contexto taxonômico apenas de fontes target-aware que sustentam o TYPE."""
    allowed_ids = {int(x) for x in (source_ids or []) if str(x).isdigit()}
    chunks: List[str] = []
    for src in sources:
        if allowed_ids and src.source_id not in allowed_ids:
            continue
        if src.relevance not in {"STRONG", "PROBABLE"}:
            continue
        supports_type = (
            product_type in (src.type_hits or {})
            or float((src.type_scores or {}).get(product_type, 0.0) or 0.0) > 0.0
        )
        if not supports_type:
            continue
        if src.text:
            chunks.append(src.text)
    return "\n---SOURCE---\n".join(chunks)


@dataclass
class Stage1Result:
    state: str
    product_type: Optional[str]
    power: Optional[str]
    type_candidates: List[str]
    power_candidates: List[str]
    sources: List[Source]
    reasons: List[str]
    taxonomy: Tuple[str, str, str] = ("", "", "")
    type_scores: Dict[str, float] = field(default_factory=dict)
    type_share: float = 0.0
    type_margin: float = 0.0
    type_support: Dict[str, int] = field(default_factory=dict)
    power_scores: Dict[str, float] = field(default_factory=dict)
    power_share: float = 0.0
    power_margin: float = 0.0
    best_source_score: int = 0
    identity_best: str = "WEAK"
    conflict_count: int = 0


def _weighted_stats(scores: Dict[str, float]) -> Tuple[str, float, float, str]:
    clean = {k: float(v) for k, v in scores.items() if k and float(v) > 0}
    if not clean:
        return "", 0.0, 0.0, ""
    ordered = sorted(clean.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(v for _, v in ordered) or 1.0
    winner, top = ordered[0]
    runner, second = ordered[1] if len(ordered) > 1 else ("", 0.0)
    share = top / total
    margin = (top - second) / top if top else 0.0
    return winner, share, margin, runner


def _model_identity_norm(value: str) -> str:
    text = strip_accents(value).casefold()
    text = re.sub(r"[^a-z0-9+./-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _structural_core_is_specific(tokens: Sequence[str]) -> bool:
    """Garante que remover um decorator não transforme o alvo em código genérico."""
    parts = [safe(x) for x in tokens if safe(x)]
    if len(parts) < 2:
        return False
    compact_core = compact(" ".join(parts))
    if len(compact_core) < STRUCTURAL_CORE_MIN_COMPACT_CHARS:
        return False
    if not re.search(r"\d", compact_core):
        return False

    mixed_token = any(
        bool(re.search(r"[a-z]", compact(tok), flags=re.I))
        and bool(re.search(r"\d", compact(tok)))
        for tok in parts
    )
    long_numeric_plus_alpha = (
        any(compact(tok).isdigit() and len(compact(tok)) >= 4 for tok in parts)
        and any(bool(re.search(r"[a-z]", compact(tok), flags=re.I)) for tok in parts)
    )
    return mixed_token or long_numeric_plus_alpha


def _is_structural_edge_decorator(token: str, regional_suffixes: Sequence[str]) -> bool:
    raw = canonical_display(token)
    if not raw:
        return False
    suffixes = {compact(x) for x in regional_suffixes if compact(x)}
    if compact(raw) in suffixes:
        return True
    for expr in DEFAULT_STRUCTURAL_EDGE_DECORATOR_REGEXES:
        try:
            if re.fullmatch(expr, raw, flags=re.I):
                return True
        except re.error:
            continue
    return False


def _model_identity_variants(model: str, regional_suffixes: Sequence[str]) -> List[Tuple[str, str]]:
    """Variações equivalentes de SKU sem fuzzy/edit-distance.

    O SKU original é sempre preservado. Variantes CORE removem apenas decorators
    estruturais nas bordas (plataforma/região/especificação) quando o restante
    continua sendo um identificador suficientemente específico.
    """
    raw = _model_identity_norm(model)
    if not raw:
        return []
    out: List[Tuple[str, str]] = []

    def add(value: str, reason: str) -> None:
        c = compact(value)
        if len(c) >= 4 and c not in {x[0] for x in out}:
            out.append((c, reason))

    add(raw, "EXACT")
    suffixes = {compact(x) for x in regional_suffixes if compact(x)}
    tokens = raw.split()

    # Structural SKU Core: remove somente decorators nas bordas, no máximo dois,
    # preservando o modelo original como EXACT. Isso resolve casos como
    # "2446 21XC M12" -> "2446 21XC" e "GENIE 1000 EU" -> "GENIE 1000"
    # sem aceitar aproximação textual.
    queue: List[Tuple[List[str], int]] = [(list(tokens), 0)]
    seen_states = {tuple(tokens)}
    while queue:
        parts, removed = queue.pop(0)
        if removed >= STRUCTURAL_CORE_MAX_EDGE_DECORATORS or len(parts) < 3:
            continue
        candidates: List[List[str]] = []
        if _is_structural_edge_decorator(parts[-1], regional_suffixes):
            candidates.append(parts[:-1])
        if _is_structural_edge_decorator(parts[0], regional_suffixes):
            candidates.append(parts[1:])
        for core_parts in candidates:
            state = tuple(core_parts)
            if state in seen_states:
                continue
            seen_states.add(state)
            if not _structural_core_is_specific(core_parts):
                continue
            add(" ".join(core_parts), "STRUCTURAL_EDGE_DECORATOR")
            queue.append((core_parts, removed + 1))

    if len(tokens) > 1 and compact(tokens[-1]) in suffixes:
        add(" ".join(tokens[:-1]), "REGIONAL_SUFFIX")

    # Identificadores duplos separados por "/" (ex.: G24HG/3400207).
    # Cada lado pode ser publicado isoladamente pelo fabricante/revenda.
    if "/" in raw:
        for part in raw.split("/"):
            pc = compact(part)
            if len(pc) >= 5:
                add(part, "ALT_ID")

    # Código interno numérico seguido do modelo comercial
    # (ex.: "628439 NHJ 051" -> "NHJ 051"). Só remove prefixo numérico longo.
    if len(tokens) >= 2 and re.fullmatch(r"\d{5,8}", tokens[0] or ""):
        rest = " ".join(tokens[1:])
        rc = compact(rest)
        if re.search(r"[a-z]", rc) and re.search(r"\d", rc) and len(rc) >= 5:
            add(rest, "CATALOG_PREFIX")

    # Revisões comerciais curtas anexadas ao fim (01..04), quando o núcleo
    # anterior já contém letras e números. Mantidas apenas como CORE.
    raw_c = compact(raw)
    m_rev = re.fullmatch(r"(.+?)(0[1-4])", raw_c)
    if m_rev:
        core = m_rev.group(1)
        if len(core) >= 6 and re.search(r"[a-z]", core) and re.search(r"\d", core):
            add(core, "ATTACHED_REVISION")

    # Letra isolada após revisão decimal: "RMA 353.0 V" -> "RMA 353.0"
    for base, reason in [(raw, "LETTER_REVISION")]:
        toks = base.split()
        if len(toks) >= 2 and re.fullmatch(r"[a-z]", toks[-1]) and re.search(r"\d+\.\d+$", toks[-2]):
            add(" ".join(toks[:-1]), reason)

    # Revisão decimal final: "RMA 353.0" -> "RMA 353".
    for value in [raw] + [" ".join(tokens[:-1]) if len(tokens) > 1 and compact(tokens[-1]) in suffixes else ""]:
        if value:
            stripped = re.sub(r"(?<=\d)\.\d+$", "", value)
            if stripped != value:
                add(stripped, "DECIMAL_REVISION")
            toks = value.split()
            if len(toks) >= 2 and re.fullmatch(r"[a-z]", toks[-1]) and re.search(r"\d+\.\d+$", toks[-2]):
                without_letter = " ".join(toks[:-1])
                add(without_letter, "LETTER_REVISION")
                add(re.sub(r"(?<=\d)\.\d+$", "", without_letter), "DECIMAL_REVISION")

    return out


@lru_cache(maxsize=4096)
def _model_exact_pattern(compact_variant: str) -> re.Pattern:
    """Regex estrutural exata do SKU.

    Permite apenas separadores entre caracteres. Não faz fuzzy e não soma tokens
    espalhados pela página. Isso impede falsos matches como CTVLI20018 contra uma
    página CGTLI20018 que também menciona CTVLI2010 em produtos relacionados.
    """
    c = compact(compact_variant)
    if not c:
        return re.compile(r"(?!x)x")
    pieces = [re.escape(ch) for ch in c]
    return re.compile(
        r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(pieces) + r"(?![a-z0-9])",
        re.I,
    )


def _model_match_in_field(
    model: str,
    text: str,
    suffixes: Sequence[str],
) -> Tuple[str, str]:
    """Retorna (match_kind, matched_variant) para UM campo da fonte.

    Match é estritamente estrutural: EXACT/CORE. Não existe mais bag-of-tokens
    global para identidade, pois ele podia montar um SKU usando pedaços de
    produtos diferentes na mesma página.
    """
    hay = strip_accents(unquote(safe(text))).casefold()
    if not hay:
        return "", ""
    for value, reason in _model_identity_variants(model, suffixes):
        if _model_exact_pattern(value).search(hay):
            return ("EXACT" if reason == "EXACT" else "CORE"), value
    return "", ""


def _brand_identity_variants(brand: str) -> List[str]:
    """Variações determinísticas de marca para identidade.

    Remove apenas sufixos jurídicos empresariais no FINAL do nome (SA, LTD,
    LLC, INC etc.). Não usa fuzzy e não reduz marcas compostas reais como
    AIR KING ou NEW HOLLAND a um token genérico.
    """
    raw = strip_accents(safe(brand)).upper().replace("&", " ")
    tokens = re.findall(r"[A-Z0-9]+", raw)
    if not tokens:
        return []
    variants: List[str] = []
    def add(parts: Sequence[str]) -> None:
        value = compact(" ".join(parts))
        if value and value not in variants:
            variants.append(value)
    add(tokens)
    legal = {
        "SA", "SAS", "LTDA", "LTD", "LLC", "INC", "CORP", "CORPORATION",
        "CO", "COMPANY", "GMBH", "SRL", "PLC", "BV", "NV", "SPA", "SL",
        "PTE", "PVT", "LIMITED"
    }
    core = list(tokens)
    while len(core) > 1 and core[-1] in legal:
        core.pop()
    add(core)
    return variants


def _source_identity_baseline(
    brand: str,
    model: str,
    title: str,
    content: str,
    url: str,
    suffixes: Sequence[str],
    short_model_requires_brand_length: int = 5,
) -> Tuple[str, int, str, float, str]:
    """Identidade target-aware e conservadora.

    Regras:
    - modelo em TITLE/URL é evidência primária;
    - modelo apenas em BODY só sobe para PROBABLE se a marca também estiver
      presente em TITLE/URL/DOMÍNIO;
    - marca sem modelo é apenas CONTEXTUAL e nunca pode, sozinha, sustentar TYPE;
    - modelos numéricos curtos sem marca não recebem PROBABLE;
    - não há fuzzy nem cobertura de tokens espalhados.
    """
    title_match, _ = _model_match_in_field(model, title, suffixes)
    url_match, _ = _model_match_in_field(model, url, suffixes)
    body_match, _ = _model_match_in_field(model, content, suffixes)

    if title_match:
        model_match, model_field = title_match, "TITLE"
    elif url_match:
        model_match, model_field = url_match, "URL"
    elif body_match:
        model_match, model_field = body_match, "BODY"
    else:
        model_match, model_field = "NONE", "NONE"

    brand_variants = _brand_identity_variants(brand)
    title_c = compact(title)
    url_c = compact(url)
    body_c = compact(content)
    def _brand_variant_ok(value: str) -> bool:
        short_alnum = bool(re.search(r"[a-z]", value) and re.search(r"\d", value))
        return len(value) >= (2 if short_alnum else 3)
    usable_brand_variants = [v for v in brand_variants if _brand_variant_ok(v)]
    brand_primary = any(v in title_c or v in url_c for v in usable_brand_variants)
    brand_body = any(v in body_c for v in usable_brand_variants)
    brand_any = brand_primary or brand_body

    primary_model = model_field in {"TITLE", "URL"}
    body_model = model_field == "BODY"
    core_model = compact(_model_identity_norm(model))
    numeric_short = bool(core_model.isdigit() and len(core_model) <= 5)
    short_ambiguous = bool(core_model and len(core_model) <= max(1, int(short_model_requires_brand_length or 5)))

    if primary_model and brand_primary:
        level, score, weight = "STRONG", 100, 1.0
    elif primary_model and brand_body and not numeric_short and not short_ambiguous:
        level, score, weight = "PROBABLE", 88, 0.82
    elif primary_model and not numeric_short and not short_ambiguous:
        level, score, weight = "PROBABLE", 85, 0.80
    elif body_model and brand_primary:
        level, score, weight = "PROBABLE", 78, 0.72
    elif body_model and brand_any:
        level, score, weight = "CONTEXTUAL", 60, 0.45
    elif brand_primary:
        level, score, weight = "CONTEXTUAL", 52, 0.40
    else:
        level, score, weight = "WEAK", 0, 0.0

    return level, score, model_match, weight, model_field


class DeterministicEngine:
    """Stage 1 restaurado a partir do R5 estável (v3.7.1).

    Regra central do R9.3:
    - candidatos vêm SOMENTE de aliases/regras do JSON;
    - qualquer fonte Tavily pode gerar candidato, como no R5;
    - resolução automática exige identidade explícita do modelo OU repetição;
    - Clef nunca cria candidato novo (gate posterior).
    """
    IDENTITY_RANK = {"CONFLICT": 0, "WEAK": 1, "CONTEXTUAL": 2, "PROBABLE": 3, "STRONG": 4}

    def __init__(self, rules: Rules, cfg: Dict[str, Any]):
        self.rules = rules
        self.cfg = cfg
        self.max_results = int(cfg.get("evidence", {}).get("max_results", 5))
        self.content_chars = int(cfg.get("evidence", {}).get("content_chars", 2400))
        self.identity_cfg = cfg.get("identity", {}) or {}
        self.suffixes = self.identity_cfg.get("regional_suffixes", ["AR", "BR", "B1", "B2", "B3", "B4"])
        self.component_negations = rules.component_context.get("negation_phrases", {}) or {}
        self.min_score = 1.0  # usado pelo gate Clef; 1 ponto = 1 fonte de suporte.

    def _source_quality_flags(self, title: str, content: str) -> List[str]:
        flags: List[str] = []
        title_fold = strip_accents(title).casefold()
        page_fold = strip_accents(f"{title} {content}").casefold()
        if any(safe(x) and contains_alias_prepared(title_fold, x) for x in (self.rules.source_rules.get("negative_title_terms", []) or [])):
            flags.append("NEGATIVE_TITLE_TERM")
        if any(safe(x) and contains_alias_prepared(page_fold, x) for x in (self.rules.source_rules.get("negative_page_terms", []) or [])):
            flags.append("NEGATIVE_PAGE_TERM")
        return flags

    def _title_only_rescue_rows(self, brand: str, model: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Recupera somente TITULOS target-anchored do RAW Search.

        HF3: quando Extract/Target Isolation nao conseguiu produzir curated_results,
        ainda podemos aproveitar um titulo de Search com identidade STRONG/PROBABLE.
        O BODY RAW nunca entra nesta recuperacao. Decisoes explicitamente erradas
        (wrong model / compatibility / related) continuam bloqueadas.
        """
        identity_model = derive_search_model(brand, model, self.cfg)
        collection = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
        hard_rejects = {
            "REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY",
        }
        audit_by_url: Dict[str, str] = {}
        for row in (collection.get("source_audit", []) or []):
            if not isinstance(row, dict):
                continue
            key = _url_key(row.get("url", ""))
            if key:
                audit_by_url[key] = safe(row.get("decision"))

        rank = self.IDENTITY_RANK
        rescued: List[Dict[str, Any]] = []
        for res in (payload.get("results", []) or []):
            if not isinstance(res, dict):
                continue
            title, url = safe(res.get("title")), safe(res.get("url"))
            if not title:
                continue
            prior_decision = audit_by_url.get(_url_key(url), "")
            if prior_decision in hard_rejects:
                continue
            meta = _source_identity_for_result(brand, identity_model, res, self.cfg)
            if meta["level"] not in {"STRONG", "PROBABLE"}:
                continue
            row = dict(res)
            row["content"] = ""  # nunca deixa snippet/body RAW virar evidencia de TYPE
            row["raw_content"] = None
            row["_collection_decision"] = "ACCEPT_TITLE_ONLY_RESCUE"
            row["_collection_reason"] = "Titulo RAW target-anchored recuperado sem usar BODY contaminado"
            flags = [safe(x) for x in (row.get("_collection_quality_flags", []) or []) if safe(x)]
            row["_collection_quality_flags"] = list(dict.fromkeys(flags + ["TITLE_ONLY_RESCUE"]))
            row["_collection_source_mode"] = "RAW_SEARCH_TITLE_ONLY"
            row["_collection_identity"] = meta["level"]
            rescued.append(row)

        def _score(row: Dict[str, Any]) -> Tuple[int, float]:
            level = safe(row.get("_collection_identity"))
            try:
                tv = float(row.get("score", 0) or 0)
            except Exception:
                tv = 0.0
            return rank.get(level, 0), tv

        rescued.sort(key=_score, reverse=True)
        return rescued[: self.max_results]

    def build_sources(self, brand: str, model: str, payload: Dict[str, Any]) -> List[Source]:
        """Stage 1 consome evidencia curada + titulo RAW target-anchored de resgate.

        HF3 nao volta a usar BODY RAW quando _collection existe. Porem, se Search
        encontrou Marca+Modelo no TITLE/URL e a extracao falhou, o titulo pode
        gerar candidatos determinísticos. Isso evita transformar falha de Extract
        em 100% de classificacao manual.
        """
        out: List[Source] = []
        identity_model = derive_search_model(brand, model, self.cfg)
        if isinstance(payload.get("_collection"), dict):
            curated = [x for x in (payload.get("_collection", {}).get("curated_results", []) or []) if isinstance(x, dict)]
            rescue = self._title_only_rescue_rows(brand, model, payload)
            seen_urls = {_url_key(x.get("url", "")) for x in curated if _url_key(x.get("url", ""))}
            source_rows = list(curated)
            source_rows.extend(x for x in rescue if not _url_key(x.get("url", "")) or _url_key(x.get("url", "")) not in seen_urls)
        else:
            source_rows = payload.get("results", []) or []
        for i, res in enumerate([x for x in source_rows if isinstance(x, dict)][: self.max_results], 1):
            title = safe(res.get("title"))
            content = safe(res.get("content"))[: self.content_chars]
            url = safe(res.get("url"))
            level, score, match, sw, match_field = _source_identity_baseline(
                brand, identity_model, title, content, url, self.suffixes,
                int(self.identity_cfg.get("short_model_requires_brand_length", 5) or 5),
            )
            collection_flags = [safe(x) for x in (res.get("_collection_quality_flags", []) or []) if safe(x)]
            flags = list(dict.fromkeys(self._source_quality_flags(title, content) + collection_flags))
            try:
                tv_score = float(res.get("score", 0) or 0)
            except Exception:
                tv_score = 0.0
            out.append(Source(
                source_id=i, title=title, content=content, url=url,
                relevance=level, quality_flags=flags,
                identity_score=score, model_match=match, model_match_field=match_field,
                source_weight=sw,
                collection_decision=safe(res.get("_collection_decision")),
                collection_reason=safe(res.get("_collection_reason")),
                tavily_score=tv_score,
                collection_source_mode=safe(res.get("_collection_source_mode")),
            ))
        return out

    def _collision_allows(self, typ: str, alias: str) -> bool:
        normalized = norm_text(alias)
        owners = sorted(set(self.rules.alias_to_types.get(normalized, [])))
        if len(owners) <= 1:
            return True
        return self.rules.alias_collision_policy.get(normalized) == typ

    def _composite_candidates(self, text: str) -> List[str]:
        folded = strip_accents(text).casefold()
        found: List[str] = []
        for rule in self.rules.composite_rules:
            target = safe(rule.get("target_type"))
            if target not in self.rules.types:
                continue
            if any(contains_alias_prepared(folded, x) for x in (rule.get("none", []) or [])):
                continue
            any_terms = rule.get("any", []) or []
            if any_terms and not any(contains_alias_prepared(folded, x) for x in any_terms):
                continue
            groups = rule.get("all_groups", []) or []
            if groups and not all(any(contains_alias_prepared(folded, x) for x in group) for group in groups if group):
                continue
            if any_terms or groups:
                found.append(target)
        return found

    def _type_hits_for_source(self, src: Source, brand: str = "", model: str = "") -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        # Classificação lexical usa a mesma normalização estrutural da busca:
        # pontuação vira separador, sem fuzzy nem reordenação semântica.
        title_fold = norm_text(src.title)
        body_fold = norm_text(src.content)
        combined = f"{title_fold}\n{body_fold}"
        hits: Dict[str, List[str]] = {}
        fields: Dict[str, List[str]] = defaultdict(list)

        for typ, patterns in self.rules._compiled_aliases.items():
            neg_patterns = self.rules._compiled_negative.get(typ, [])
            if any(p.search(combined) for _, p in neg_patterns):
                continue
            if typ in self.component_negations and any(contains_alias_prepared(combined, x) for x in self.component_negations.get(typ, [])):
                continue

            matched = [alias for alias, p in patterns if self._collision_allows(typ, alias) and p.search(combined)]
            if not matched:
                continue
            max_words = max(len(norm_text(a).split()) for a in matched)
            chosen = sorted({a for a in matched if len(norm_text(a).split()) == max_words})
            hits[typ] = chosen
            if any(p.search(title_fold) for a, p in patterns if a in chosen):
                fields[typ].append("TITLE")
            if any(p.search(body_fold) for a, p in patterns if a in chosen):
                fields[typ].append("BODY")

        # Recovery Ladder p6: TAXONOMY_EVIDENCE_RULES também podem GERAR o
        # candidato de TYPE quando a própria regra já ancora Brand/Model/alias.
        # Antes elas só eram usadas depois que TYPE já existia, criando o dead-end
        # TAXONOMY_GAP_NO_ALIAS mesmo com evidência suficiente.
        brand_key = canonical_display(brand)
        model_for_rule = derive_search_model(brand, model, self.cfg) if model else safe(model)
        for rule in self.rules.taxonomy_evidence_rules:
            typ = safe(rule.get("primary_type"))
            if typ not in self.rules.types:
                continue
            rule_brand = canonical_display(rule.get("brand"))
            if rule_brand and rule_brand != brand_key:
                continue
            model_regex = safe(rule.get("model_regex"))
            model_match_rule = False
            if model_regex:
                try:
                    model_match_rule = bool(re.search(model_regex, model_for_rule, flags=re.I))
                except re.error:
                    model_match_rule = False
                if not model_match_rule:
                    continue
            neg = [safe(x) for x in (rule.get("negative_aliases", []) or []) if safe(x)]
            if neg and any(contains_alias_prepared(combined, x) for x in neg):
                continue
            aliases = [safe(x) for x in (rule.get("aliases", []) or []) if safe(x)]
            matched_aliases = [x for x in aliases if contains_alias_prepared(combined, x)]
            if aliases and not matched_aliases:
                continue
            if not aliases and not model_match_rule:
                continue
            hits.setdefault(typ, [])
            marker = "[TAXONOMY_MODEL_RULE]" if model_match_rule and not matched_aliases else "[TAXONOMY_EVIDENCE_RULE]"
            if marker not in hits[typ]:
                hits[typ].append(marker)
            if matched_aliases:
                if any(contains_alias_prepared(title_fold, x) for x in matched_aliases):
                    fields[typ].append("TITLE")
                if any(contains_alias_prepared(body_fold, x) for x in matched_aliases):
                    fields[typ].append("BODY")
            elif model_match_rule:
                fields[typ].append("MODEL_RULE")

        for typ in self._composite_candidates(combined):
            hits.setdefault(typ, []).append("[COMPOSITE_RULE]")
            fields[typ].append("COMPOSITE")

        # Mesmo guard do R5: componente genérico não rouba tipo físico.
        physical = [t for t in hits if t not in self.rules.component_types]
        if physical:
            for typ in list(hits):
                if typ not in self.rules.component_types:
                    continue
                explicit = [a for a in hits[typ] if a == "[COMPOSITE_RULE]" or len(norm_text(a).split()) >= 2]
                if explicit:
                    hits[typ] = explicit
                else:
                    hits.pop(typ, None)
                    fields.pop(typ, None)

        return hits, {k: list(dict.fromkeys(v)) for k, v in fields.items()}

    def _power_hits_for_source(self, src: Source) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        title_fold = norm_text(src.title)
        body_fold = norm_text(src.content)
        combined = f"{title_fold}\n{body_fold}"
        hits: Dict[str, List[str]] = {}
        fields: Dict[str, List[str]] = defaultdict(list)
        source_types = set(src.type_hits)
        output_voltage_context = bool(source_types.intersection(self.rules.output_voltage_not_input_power_types))

        for power, data in self.rules.power_rules.items():
            if any(contains_alias_prepared(combined, x) for x in (data.get("negative_keywords", []) or [])):
                continue
            by_group: Dict[str, List[str]] = defaultdict(list)
            for key, values in data.items():
                if key in {"negative_keywords", "display_label", "voltage_min", "voltage_max", "voltage_only_requires_context"}:
                    continue
                if not key.endswith("keywords") or not isinstance(values, list):
                    continue
                for value in values:
                    in_title = contains_alias_prepared(title_fold, value)
                    in_body = contains_alias_prepared(body_fold, value)
                    if in_title or in_body:
                        by_group[key].append(value)
                        if in_title:
                            fields[power].append("TITLE")
                        if in_body:
                            fields[power].append("BODY")
            positive = [x for vals in by_group.values() for x in vals]
            if not positive:
                continue
            if output_voltage_context and power in {"BATTERY", "CORDED"} and not by_group.get("strong_keywords"):
                continue
            hits[power] = sorted(set(positive))
        return hits, {k: list(dict.fromkeys(v)) for k, v in fields.items()}

    @staticmethod
    def _evidence_is_target_anchored(src: Source, evidence_fields: Sequence[str]) -> bool:
        """TYPE/POWER só entram no candidato se estiverem ancorados ao SKU alvo."""
        if src.relevance not in {"STRONG", "PROBABLE"}:
            return False
        fields = set(evidence_fields or [])
        if src.model_match_field in {"TITLE", "URL"}:
            return True
        if src.model_match_field == "BODY":
            return "BODY" in fields
        return False

    def _apply_specificity(self, candidates: Iterable[str]) -> List[str]:
        cur = set(candidates)
        for group in self.rules.specificity_groups:
            present = [x for x in group if x in cur]
            if len(present) >= 2:
                winner = present[0]
                for item in present[1:]:
                    cur.discard(item)
                cur.add(winner)
        return sorted(cur)

    def _type_requires_specific_power(self, typ: str) -> bool:
        rows = [r for r in self.rules.taxonomy_rules if safe(r.get("primary_type")) == typ]
        if not rows:
            return False
        powers = {safe(r.get("power")) or "*" for r in rows}
        return "*" not in powers and "UNKNOWN" not in powers

    def classify(self, brand: str, model: str, payload: Dict[str, Any]) -> Stage1Result:
        sources = self.build_sources(brand, model, payload)
        all_type_hits: Dict[str, List[int]] = defaultdict(list)
        all_power_hits: Dict[str, List[int]] = defaultdict(list)

        # R9.3: candidato não nasce mais de ruído de busca ou de repetição entre
        # páginas sem o SKU alvo. TYPE/POWER precisam estar ancorados ao modelo.
        explicit_levels = {"STRONG"}
        for src in sources:
            src.type_hits, src.type_evidence_fields = self._type_hits_for_source(src, brand, model)
            src.power_hits, src.power_evidence_fields = self._power_hits_for_source(src)

            anchored_types = {
                t for t in src.type_hits
                if self._evidence_is_target_anchored(src, src.type_evidence_fields.get(t, []))
            }
            anchored_powers = {
                p for p in src.power_hits
                if self._evidence_is_target_anchored(src, src.power_evidence_fields.get(p, []))
            }

            # Score target-aware: TITLE vale mais que BODY; identidade e qualidade
            # modulam o peso. Isso restaura discriminação sem introduzir fuzzy/IA.
            src.type_scores = {}
            for typ in anchored_types:
                fields = set(src.type_evidence_fields.get(typ, []) or [])
                field_weight = 3.2 if "TITLE" in fields else (1.0 if "COMPOSITE" in fields else 0.45)
                aliases = [a for a in (src.type_hits.get(typ, []) or []) if a != "[COMPOSITE_RULE]"]
                max_words = max([len(norm_text(a).split()) for a in aliases] or [1])
                specificity = 1.0 + min(0.35, max(0, max_words - 1) * 0.07)
                quality = 0.65 if src.quality_flags else 1.0
                src.type_scores[typ] = round(field_weight * max(0.1, src.source_weight) * specificity * quality, 6)

            src.power_scores = {}
            for power in anchored_powers:
                fields = set(src.power_evidence_fields.get(power, []) or [])
                field_weight = 2.6 if "TITLE" in fields else 0.55
                quality = 0.70 if src.quality_flags else 1.0
                src.power_scores[power] = round(field_weight * max(0.1, src.source_weight) * quality, 6)

            for typ in anchored_types:
                all_type_hits[typ].append(src.source_id)
            for power in anchored_powers:
                all_power_hits[power].append(src.source_id)

        type_candidates = self._apply_specificity(all_type_hits.keys())
        power_candidates = sorted(all_power_hits.keys())
        # Após especificidade, métricas só consideram candidatos sobreviventes.
        type_support = {t: len(set(all_type_hits.get(t, []))) for t in type_candidates}
        type_scores = {
            t: round(sum(float(src.type_scores.get(t, 0.0) or 0.0) for src in sources), 6)
            for t in type_candidates
        }
        type_winner, type_share, type_margin, runner = _weighted_stats(type_scores)
        power_support = {p: len(set(all_power_hits.get(p, []))) for p in power_candidates}
        power_scores = {
            p: round(sum(float(src.power_scores.get(p, 0.0) or 0.0) for src in sources), 6)
            for p in power_candidates
        }
        _, power_share, power_margin, _ = _weighted_stats(power_scores)
        reasons: List[str] = []

        product_type: Optional[str] = None
        if len(type_candidates) == 1:
            typ = type_candidates[0]
            support_ids = set(all_type_hits.get(typ, []))
            explicit = any(s.source_id in support_ids and s.relevance in explicit_levels for s in sources)
            repeated = len(support_ids) >= 2
            if explicit:
                product_type = typ
                reasons.append(f"TYPE_UNIQUE_STRONG:{typ}; support={sorted(support_ids)}; strong={explicit}; repeated={repeated}")
            else:
                reasons.append(f"TYPE_CANDIDATE_WITHOUT_STRONG_IDENTITY:{typ}; support={sorted(support_ids)}; repeated={repeated}")
        elif len(type_candidates) > 1:
            reasons.append(f"TYPE_AMBIGUOUS:{type_candidates}")
        else:
            reasons.append("TYPE_UNRESOLVED:NO_JSON_ALIAS")

        power: Optional[str] = None
        if product_type in self.rules.passive_types:
            power = "UNKNOWN"
            reasons.append("POWER_PASSIVE_TYPE")
        elif len(power_candidates) == 1:
            candidate = power_candidates[0]
            support_ids = set(all_power_hits.get(candidate, []))
            explicit = any(s.source_id in support_ids and s.relevance in explicit_levels for s in sources)
            repeated = len(support_ids) >= 2
            if explicit:
                power = candidate
                reasons.append(f"POWER_UNIQUE_STRONG:{candidate}; support={sorted(support_ids)}; strong={explicit}; repeated={repeated}")
            else:
                reasons.append(f"POWER_CANDIDATE_WITHOUT_STRONG_IDENTITY:{candidate}; support={sorted(support_ids)}; repeated={repeated}")
        elif len(power_candidates) > 1:
            reasons.append(f"POWER_AMBIGUOUS:{power_candidates}")
        else:
            reasons.append("POWER_UNRESOLVED")

        # Conflito explícito de alimentação exige adjudicação; não aprovar UNKNOWN.
        if product_type and power is None and not self._type_requires_specific_power(product_type):
            if len(power_candidates) > 1:
                reasons.append("POWER_CONFLICT_REQUIRES_ADJUDICATION")
            else:
                power = "UNKNOWN"
                reasons.append("POWER_UNKNOWN_ALLOWED_BY_TAXONOMY")

        if product_type:
            tax_evidence = taxonomy_evidence_text(sources, product_type)
            cg, fam, tax_reason = self.rules.map_taxonomy(
                product_type, power or "UNKNOWN", tax_evidence, brand, model
            )
        else:
            cg, fam, tax_reason = "", "", ""

        if (product_type and power is not None and len(power_candidates) <= 1
                and (cg, fam) in self.rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS")
                # Generic WATERPUMP fallback proves a broad type, not the commercial FAMILY.
                # Route to JEV with evidence rather than silently auto-resolving.
                and not (product_type == "WATERPUMP" and tax_reason == "TYPE_FALLBACK_PAIR")):
            state = "RESOLVED_RULE"
        elif type_candidates:
            state = "AMBIGUOUS"
        else:
            state = "UNRESOLVED"

        supporters = [s for s in sources if product_type and product_type in s.type_hits]
        best_source_score = max((s.identity_score for s in supporters), default=0)
        identity_best = max((s.relevance for s in supporters), key=lambda x: self.IDENTITY_RANK.get(x, 0), default="WEAK")

        if type_candidates:
            reasons.append("ALLOWED_CLEF_TYPES:" + ",".join(type_candidates))
        else:
            reasons.append("CLEF_BLOCKED:NO_TYPE_CANDIDATE")

        return Stage1Result(
            state=state,
            product_type=product_type,
            power=power,
            type_candidates=type_candidates,
            power_candidates=power_candidates,
            sources=sources,
            reasons=reasons,
            taxonomy=(cg, fam, tax_reason),
            type_scores=type_scores,
            type_share=type_share,
            type_margin=type_margin,
            type_support=type_support,
            power_scores=power_scores,
            power_share=power_share,
            power_margin=power_margin,
            best_source_score=best_source_score,
            identity_best=identity_best,
            conflict_count=0,
        )


def _combo_kit_components(rules: Rules, s1: Stage1Result) -> List[str]:
    """Retorna componentes de um POWER TOOL COMBO KIT explicitamente comprovado.

    Não usa apenas a palavra 'kit': exige sinal de combo no texto target e pelo
    menos DOIS tipos físicos elegíveis explicitamente sustentados no mesmo alvo.
    Assim drill+bateria+carregador continua sendo DRILL, não combo kit.
    """
    policy = rules.raw.get("COMBO_KIT_POLICY", {}) or {}
    if not bool(policy.get("enabled", True)):
        return []
    eligible = {safe(x) for x in (policy.get("eligible_primary_types", []) or []) if safe(x)}
    combo_terms = [safe(x) for x in (policy.get("combo_terms", []) or []) if safe(x)]
    rank = DeterministicEngine.IDENTITY_RANK
    target_sources = [s for s in s1.sources if rank.get(s.relevance, 0) >= rank.get("STRONG", 4)]
    for src in target_sources:
        folded = norm_text(f"{src.title}\n{src.content[:1200]}")
        if not any(contains_alias_prepared(folded, term) for term in combo_terms):
            continue
        explicit = {
            t for t in s1.type_candidates
            if t in eligible
            and float(src.type_scores.get(t, 0.0) or 0.0) > 0.0
            and ("TITLE" in (src.type_evidence_fields.get(t, []) or []) or "BODY" in (src.type_evidence_fields.get(t, []) or []))
        }
        if len(explicit) >= int(policy.get("min_distinct_primary_types", 2) or 2):
            return sorted(explicit)
    return []


def choose_combo_title(rules: Rules, s1: Stage1Result) -> Tuple[str, str]:
    """Título da própria fonte STRONG que comprovou o combo multi-tool."""
    policy = rules.raw.get("COMBO_KIT_POLICY", {}) or {}
    eligible = {safe(x) for x in (policy.get("eligible_primary_types", []) or []) if safe(x)}
    combo_terms = [safe(x) for x in (policy.get("combo_terms", []) or []) if safe(x)]
    min_types = int(policy.get("min_distinct_primary_types", 2) or 2)
    candidates: List[Source] = []
    for src in s1.sources:
        if src.relevance != "STRONG":
            continue
        folded = norm_text(f"{src.title}\n{src.content[:1200]}")
        if not any(contains_alias_prepared(folded, term) for term in combo_terms):
            continue
        explicit = {
            t for t in s1.type_candidates
            if t in eligible and float(src.type_scores.get(t, 0.0) or 0.0) > 0.0
        }
        if len(explicit) >= min_types:
            candidates.append(src)
    if not candidates:
        return "", ""
    candidates.sort(
        key=lambda src: (
            src.identity_score,
            sum(float(src.type_scores.get(t, 0.0) or 0.0) for t in s1.type_candidates),
            0 if src.quality_flags else 1,
        ),
        reverse=True,
    )
    return candidates[0].title, candidates[0].url


def multi_product_target_detected(rules: Rules, s1: Stage1Result) -> bool:
    """Detecta múltiplos produtos, exceto combo kit explicitamente modelado."""
    if _combo_kit_components(rules, s1):
        return False
    rank = DeterministicEngine.IDENTITY_RANK
    target_sources = [s for s in s1.sources if rank.get(s.relevance, 0) >= rank.get("PROBABLE", 3)]
    if not target_sources:
        return False
    physical = {t for t in s1.type_candidates if t not in rules.component_types and t != "POWER TOOL COMBO KIT"}
    for src in target_sources:
        explicit = {
            t for t in physical
            if "TITLE" in (src.type_evidence_fields.get(t, []) or [])
            and float(src.type_scores.get(t, 0.0) or 0.0) > 0.0
        }
        if len(explicit) >= 2:
            return True
    return False


@dataclass
class CandidateFunnelResult:
    candidates: List[str]
    scores: Dict[str, float]
    reason: str
    top_score: float = 0.0
    blocked: bool = False


def candidate_funnel(rules: Rules, s1: Stage1Result) -> CandidateFunnelResult:
    """Stage 1.5 determinístico: reduz o espaço do Clef sem inventar candidatos.

    O funil opera somente sobre TYPEs já encontrados pelo Stage 1 e aplica o
    mesmo conceito de evidência que será exigido depois da resposta do Clef.
    """
    gate = rules.classification_policy.get("llm_resolution_gate", {}) or {}
    policy = gate.get("candidate_funnel", {}) or {}
    if multi_product_target_detected(rules, s1):
        return CandidateFunnelResult([], {}, "MULTI_PRODUCT_TARGET", blocked=True)
    max_candidates = max(1, int(policy.get("max_candidates", 4) or 4))
    rel_floor = float(policy.get("relative_score_floor", 0.45) or 0.45)
    min_score = float(policy.get("minimum_score", 0.70) or 0.70)
    req_identity = safe(policy.get("require_identity_at_least", "PROBABLE")) or "PROBABLE"
    min_sources = max(1, int(policy.get("require_title_support_or_min_sources", 2) or 2))
    block_over = bool(policy.get("block_when_candidates_exceed_max", True))
    rank = DeterministicEngine.IDENTITY_RANK

    eligible: List[Tuple[str, float]] = []
    for typ in s1.type_candidates:
        score = float(s1.type_scores.get(typ, 0.0) or 0.0)
        supporting = [
            src for src in s1.sources
            if float(src.type_scores.get(typ, 0.0) or 0.0) > 0.0
            and rank.get(src.relevance, 0) >= rank.get(req_identity, 3)
        ]
        if not supporting:
            continue
        title_support = any("TITLE" in (src.type_evidence_fields.get(typ, []) or []) for src in supporting)
        independent_sources = len({src.source_id for src in supporting})
        if not title_support and independent_sources < min_sources:
            continue
        if score >= min_score:
            eligible.append((typ, score))

    if not eligible:
        return CandidateFunnelResult([], {}, "NO_FUNNEL_ELIGIBLE_CANDIDATE", blocked=True)

    eligible.sort(key=lambda kv: (-kv[1], kv[0]))
    top = eligible[0][1]
    threshold = max(min_score, top * rel_floor)
    narrowed = [(typ, score) for typ, score in eligible if score >= threshold]

    if len(narrowed) > max_candidates and block_over:
        return CandidateFunnelResult(
            [], dict(narrowed),
            f"TOO_MANY_PLAUSIBLE_CANDIDATES:{len(narrowed)}>{max_candidates}",
            top_score=top, blocked=True,
        )

    narrowed = narrowed[:max_candidates]
    return CandidateFunnelResult(
        [typ for typ, _ in narrowed],
        {typ: score for typ, score in narrowed},
        "FUNNEL_OK",
        top_score=top,
        blocked=False,
    )


def _type_requires_specific_power_rules(rules: Rules, typ: str) -> bool:
    rows = [r for r in rules.taxonomy_rules if safe(r.get("primary_type")) == typ]
    if not rows:
        return False
    powers = {safe(r.get("power")) or "*" for r in rows}
    return "*" not in powers and "UNKNOWN" not in powers


def _single_funnel_candidate_can_resolve(rules: Rules, s1: Stage1Result, typ: str, brand: str = "", model: str = "") -> bool:
    gate = rules.classification_policy.get("llm_resolution_gate", {}) or {}
    policy = gate.get("candidate_funnel", {}) or {}
    if not bool(policy.get("single_candidate_can_resolve_without_llm", True)):
        return False
    rank = DeterministicEngine.IDENTITY_RANK
    req_identity = safe(policy.get("require_identity_at_least", "PROBABLE")) or "PROBABLE"
    supporting = [
        src for src in s1.sources
        if float(src.type_scores.get(typ, 0.0) or 0.0) > 0.0
        and rank.get(src.relevance, 0) >= rank.get(req_identity, 3)
    ]
    if not supporting:
        return False
    title_support = any("TITLE" in (src.type_evidence_fields.get(typ, []) or []) for src in supporting)
    repeated = len({src.source_id for src in supporting}) >= 2
    if not (title_support or repeated):
        return False
    if s1.power is None and _type_requires_specific_power_rules(rules, typ):
        return False
    power = s1.power or "UNKNOWN"
    tax_evidence = taxonomy_evidence_text(s1.sources, typ)
    cg, fam, _ = rules.map_taxonomy(typ, power, tax_evidence, brand, model)
    return (cg, fam) in rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS")


# -----------------------------------------------------------------------------
# Classificação DB / checkpoint
# -----------------------------------------------------------------------------
class ClassificationDB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                rules_hash TEXT NOT NULL,
                model TEXT,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_id TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                rules_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                usage_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS classification_results (
                checkpoint_id TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                rules_hash TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_results_business_key ON classification_results(business_key);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                business_key TEXT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def start_run(self, mode: str, rules_hash: str, model: str) -> str:
        rid = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.conn.execute(
            "INSERT INTO runs(run_id,started_at,status,mode,pipeline_version,rules_hash,model) VALUES(?,?,?,?,?,?,?)",
            (rid, now_iso(), "RUNNING", mode, PIPELINE_VERSION, rules_hash, model),
        )
        self.conn.commit()
        return rid

    def finish_run(self, run_id: str, status: str, note: str = "") -> None:
        self.conn.execute("UPDATE runs SET finished_at=?,status=?,note=? WHERE run_id=?", (now_iso(), status, note, run_id))
        self.conn.commit()

    def update_run_model(self, run_id: str, model: str) -> None:
        self.conn.execute(
            "UPDATE runs SET model=? WHERE run_id=?",
            (model, run_id),
        )
        self.conn.commit()

    def event(self, run_id: str, key: str, event_type: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events(run_id,business_key,created_at,event_type,detail) VALUES(?,?,?,?,?)",
            (run_id, key, now_iso(), event_type, detail[:4000]),
        )
        self.conn.commit()

    @staticmethod
    def checkpoint_id(key: str, raw_hash: str, rules_hash: str, model: str) -> str:
        # Inclui modelo + prompt para impedir reaproveitamento silencioso de
        # resultados de outra política Clef.
        material = f"{PIPELINE_VERSION}|{PROMPT_VERSION}|{model}|{key}|{raw_hash}|{rules_hash}"
        return hashlib.sha256(material.encode()).hexdigest()

    def get_checkpoint(self, key: str, raw_hash: str, rules_hash: str, model: str) -> Optional[Dict[str, Any]]:
        cid = self.checkpoint_id(key, raw_hash, rules_hash, model)
        row = self.conn.execute("SELECT result_json FROM classification_results WHERE checkpoint_id=?", (cid,)).fetchone()
        if not row:
            return None
        try:
            result = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        # Only terminal, complete results may skip classification.
        # REVIEW and unresolved checkpoints remain stored for audit.
        if not isinstance(result, dict):
            return None
        resolution = safe(result.get("Resolution State")).upper()
        if resolution not in {"RESOLVED", "CLASSIFIED_CLEF"}:
            return None
        if not all(safe(result.get(col)) for col in ("O que é (Tipo)", "Category General", "FAMILY", "Título Real")):
            return None
        if safe(result.get("O que é (Tipo)")).upper() in {"REVISAR", "UNKNOWN"}:
            return None
        return result


    def put_checkpoint(self, key: str, brand: str, model: str, raw_hash: str, rules_hash: str, result: Dict[str, Any], llm_model: str) -> None:
        cid = self.checkpoint_id(key, raw_hash, rules_hash, llm_model)
        self.conn.execute(
            """INSERT OR REPLACE INTO classification_results
               (checkpoint_id,business_key,brand,model,raw_hash,rules_hash,pipeline_version,result_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid, key, brand, model, raw_hash, rules_hash, PIPELINE_VERSION, json.dumps(result, ensure_ascii=False, default=str), now_iso()),
        )
        self.conn.commit()  # por produto


    @staticmethod
    def llm_cache_id(key: str, raw_hash: str, rules_hash: str, provider: str, model: str) -> str:
        material = f"{PROMPT_VERSION}|{provider}|{model}|{key}|{raw_hash}|{rules_hash}"
        return hashlib.sha256(material.encode()).hexdigest()

    def get_llm(
        self,
        key: str,
        raw_hash: str,
        rules_hash: str,
        provider: str,
        model: str,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        cid = self.llm_cache_id(key, raw_hash, rules_hash, provider, model)
        row = self.conn.execute(
            "SELECT response_json,usage_json FROM llm_cache WHERE cache_id=?",
            (cid,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["response_json"]), json.loads(row["usage_json"] or "{}")
        except Exception:
            return None

    def put_llm(
        self,
        key: str,
        raw_hash: str,
        rules_hash: str,
        provider: str,
        model: str,
        response: Dict[str, Any],
        usage: Dict[str, Any],
    ) -> None:
        cid = self.llm_cache_id(key, raw_hash, rules_hash, provider, model)
        self.conn.execute(
            """INSERT OR REPLACE INTO llm_cache
               (cache_id,business_key,raw_hash,rules_hash,prompt_version,provider,model,response_json,usage_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, key, raw_hash, rules_hash, PROMPT_VERSION, provider, model,
                json.dumps(response, ensure_ascii=False, default=str),
                json.dumps(usage, ensure_ascii=False, default=str),
                now_iso(),
            ),
        )
        self.conn.commit()



# -----------------------------------------------------------------------------
# Stage 1.5 semântico + Stage 2 Cloudflare Clef
# -----------------------------------------------------------------------------

def _unit_score(value: Any) -> float:
    """Normaliza probability/confidence para [0, 1], tolerando APIs que retornem %."""
    try:
        x = float(value)
    except Exception:
        return 0.0
    if x > 1.0 and x <= 100.0:
        x /= 100.0
    return max(0.0, min(1.0, x))


def _taxonomy_pairs_for_type(rules: Rules, product_type: str) -> set:
    """Todos os pares Category|FAMILY declarados pelo JSON para um PRODUCT_TYPE."""
    typ = safe(product_type)
    pairs = set()
    for row in rules.taxonomy_rules:
        if safe(row.get("primary_type")) != typ:
            continue
        pair = (safe(row.get("category_general")), safe(row.get("family")))
        if pair in rules.valid_pairs:
            pairs.add(pair)
    for row in rules.taxonomy_evidence_rules:
        if safe(row.get("primary_type")) != typ:
            continue
        pair = (safe(row.get("category_general")), safe(row.get("family")))
        if pair in rules.valid_pairs:
            pairs.add(pair)
    fb = (rules.types.get(typ, {}) or {}).get("fallback_pair")
    if isinstance(fb, list) and len(fb) >= 2:
        pair = (safe(fb[0]), safe(fb[1]))
        if pair in rules.valid_pairs:
            pairs.add(pair)
    return pairs


def _pair_label(pair: Tuple[str, str]) -> str:
    return f"{safe(pair[0])} | {safe(pair[1])}"


def _split_pair_label(value: str) -> Tuple[str, str]:
    raw = safe(value)
    if "|" not in raw:
        return "", ""
    a, b = raw.split("|", 1)
    return safe(a), safe(b)


def _clef_preflight_rejection_reason(rules: Rules, s1: Stage1Result) -> str:
    """Clef pode recuperar candidatos sem alias, mas não pode superar falta de identidade."""
    rank = DeterministicEngine.IDENTITY_RANK
    target_sources = [
        src for src in s1.sources
        if rank.get(src.relevance, 0) >= rank.get("PROBABLE", 3)
    ]
    if not target_sources:
        return "LOW_TARGET_EVIDENCE"
    if multi_product_target_detected(rules, s1):
        return "MULTI_PRODUCT_TARGET"
    return ""


def _jev_endpoint(base_url: str) -> str:
    """Normalize the native TypeSafe endpoint without duplicating its path."""
    base = safe(base_url).strip().rstrip("/") or "https://api.typesafe.ai"
    if not base.startswith("https://"):
        raise ValueError("JEV_BASE_URL_INVALID: HTTPS required")
    return base if base.endswith("/v1/systemone") else base + "/v1/systemone"


class ClefFunnelClassifier:
    """Funil semântico local + decisão estruturada Cloudflare Clef.

    Função de cada camada:
    - Stage 1 existente: aliases/regras exatas do JSON;
    - BGE-M3: amplia recall de candidatos sem criar labels fora do JSON;
    - BGE reranker: reduz ruído para poucos candidatos plausíveis;
    - Clef: escolhe TYPE, POWER e Category|FAMILY entre opções fechadas.

    O GOLD nunca participa de candidate generation ou decisão.
    """

    provider = "clef"

    def __init__(
        self,
        rules: Rules,
        cfg: Dict[str, Any],
        db: ClassificationDB,
        provider: str = "clef",
        model: str = DEFAULT_CLEF_MODEL,
    ):
        self.rules = rules
        self.cfg = cfg
        self.db = db
        self.provider = safe(provider).lower() or "clef"
        if self.provider not in {"clef", "jev"}:
            raise ValueError(f"Provedor de classificação inválido: {self.provider}")
        self.model = safe(model) or DEFAULT_CLEF_MODEL
        self.ccfg = cfg.get(self.provider, {}) or {}
        self.fcfg = cfg.get("semantic_funnel", {}) or {}

        self.account_id_env = safe(self.ccfg.get("account_id_env")) or "CLOUDFLARE_ACCOUNT_ID"
        self.api_key_env = (safe(self.ccfg.get("api_key_env")) or "TYPESAFE_API_KEY") if self.provider == "jev" else (safe(self.ccfg.get("api_token_env")) or "CLOUDFLARE_API_TOKEN")
        self.operational_requests_per_minute = float(
            self.ccfg.get("operational_requests_per_minute", 10.0) or 10.0
        )
        self.limiter = RateLimiter(self.operational_requests_per_minute)
        self.timeout_seconds = int(self.ccfg.get("timeout_seconds", 90) or 90)
        self.retry_attempts = max(1, int(self.ccfg.get("retry_attempts", 3) or 3))
        self.retry_backoff_seconds = [
            float(x) for x in (self.ccfg.get("retry_backoff_seconds", [2, 5, 10]) or [2, 5, 10])
        ]
        self.max_calls_per_run = max(0, int(self.ccfg.get("max_api_calls_per_run", 0) or 0))
        self.max_total_tokens_per_run = max(0, int(self.ccfg.get("max_total_tokens_per_run", 0) or 0))
        self.transport_attempts = 0

        # Atributos mantidos para compatibilidade com o formato de execução/cache existente.
        self.max_output_tokens = 0
        self.json_retry_max_completion_tokens = 0
        self.account_requests_per_minute = 0
        self.account_tokens_per_minute = 0
        self.requests_per_day = 0
        self.tokens_per_day = 0

        self.embedding_model_name = safe(self.fcfg.get("embedding_model")) or "BAAI/bge-m3"
        self.reranker_model_name = safe(self.fcfg.get("reranker_model")) or "BAAI/bge-reranker-v2-m3"
        self.local_files_only = bool(self.fcfg.get("local_files_only", False))
        self.offline_mode = bool(self.fcfg.get("offline_mode", self.local_files_only))
        self.embedding_batch_size = max(1, int(self.fcfg.get("embedding_batch_size", 16) or 16))
        self.rerank_batch_size = max(1, int(self.fcfg.get("rerank_batch_size", 8) or 8))
        self.rerank_max_length = max(128, int(self.fcfg.get("rerank_max_length", 224) or 224))
        self.rerank_types = bool(self.fcfg.get("rerank_types", False))
        self.rerank_taxonomy = bool(self.fcfg.get("rerank_taxonomy", True))
        self.type_dense_k = max(4, int(self.fcfg.get("type_dense_k", 8) or 8))
        self.type_pool_k = max(self.type_dense_k, int(self.fcfg.get("type_pool_k", 10) or 10))
        self.type_top_k = max(3, int(self.fcfg.get("type_top_k", 6) or 6))
        self.taxonomy_dense_k = max(4, int(self.fcfg.get("taxonomy_dense_k", 10) or 10))
        self.taxonomy_pool_k = max(self.taxonomy_dense_k, int(self.fcfg.get("taxonomy_pool_k", 14) or 14))
        self.taxonomy_top_k = max(3, int(self.fcfg.get("taxonomy_top_k", 6) or 6))
        self.max_sources = max(1, int(self.fcfg.get("max_sources", 4) or 4))
        self.source_chars = max(160, int(self.fcfg.get("source_chars", 500) or 500))
        self.max_evidence_chars = max(900, int(self.fcfg.get("max_evidence_chars", 2200) or 2200))
        self.direct_stage1_threshold = max(1, int(self.fcfg.get("direct_stage1_threshold", 4) or 4))
        self.embedding_device_cfg = safe(self.fcfg.get("embedding_device")) or "cpu"
        self.reranker_device_cfg = safe(self.fcfg.get("reranker_device")) or "auto"

        self.api_calls = 0
        self.repair_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.semantic_calls = 0
        self.semantic_seconds = 0.0
        self.rerank_seconds = 0.0

        self._torch = None
        self._embedder = None
        self._reranker_tokenizer = None
        self._reranker = None
        self._type_doc_embeddings = None
        self._taxonomy_doc_embeddings = None

        self.type_labels = sorted(self.rules.types.keys())
        self.type_docs = {typ: self._type_document(typ) for typ in self.type_labels}
        self.taxonomy_profiles = self._build_taxonomy_profiles()
        self.taxonomy_labels = sorted(self.taxonomy_profiles.keys())
        self.taxonomy_docs = {
            label: self._taxonomy_document(label) for label in self.taxonomy_labels
        }
        self.cache_model_id = (
            f"{self.model}|prompt:{PROMPT_VERSION}|{self.embedding_model_name}|{self.reranker_model_name}"
            f"|type{self.type_top_k}|tax{self.taxonomy_top_k}"
            f"|rt{int(self.rerank_types)}|rx{self.rerank_max_length}|tp{self.type_pool_k}|xp{self.taxonomy_pool_k}"
        )

        account_id = os.getenv(self.account_id_env, "").strip()
        slug = "@cf/cloudflare/clef-flash" if self.model == "clef-flash" else "@cf/cloudflare/clef"
        self.base_url = _jev_endpoint(safe(self.ccfg.get("base_url"))) if self.provider == "jev" else (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{slug}"
            if account_id else f"https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/run/{slug}"
        )

    def route_identity_fragment(self) -> str:
        return (
            f"{self.provider}:{self.model}+prompt:{PROMPT_VERSION}+embed:{self.embedding_model_name}"
            f"+rerank:{self.reranker_model_name}"
            f"+rt:{int(self.rerank_types)}+rx:{self.rerank_max_length}"
            f"+tp:{self.type_pool_k}+xp:{self.taxonomy_pool_k}"
        )

    def preflight(self) -> Dict[str, Any]:
        missing = []
        for mod in ["requests", "torch", "sentence_transformers", "transformers"]:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if missing:
            raise OperationalPause(
                "CLEF_FUNNEL_DEPENDENCY_MISSING",
                "Dependências ausentes: " + ", ".join(missing),
            )
        if self.provider == "clef" and not os.getenv(self.account_id_env, "").strip():
            raise OperationalPause(
                "CLEF_ACCOUNT_ID_MISSING",
                f"{self.account_id_env} não configurada no .env.",
            )
        if not os.getenv(self.api_key_env, "").strip():
            # Compatibilidade com o nome legado somente para Cloudflare.
            fallback = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip() if self.provider == "clef" else ""
            if not fallback:
                raise OperationalPause(
                    "CLEF_API_TOKEN_MISSING",
                    f"{self.api_key_env} não configurada no .env.",
                )
        if self.local_files_only:
            missing_local_models = [
                value for value in [self.embedding_model_name, self.reranker_model_name]
                if not Path(value).expanduser().exists()
            ]
            if missing_local_models:
                raise OperationalPause(
                    "CLEF_FUNNEL_LOCAL_MODEL_MISSING",
                    "Modelos locais não encontrados: " + ", ".join(missing_local_models),
                )

        account_id = os.getenv(self.account_id_env, "").strip()
        slug = "@cf/cloudflare/clef-flash" if self.model == "clef-flash" else "@cf/cloudflare/clef"
        if self.provider == "clef":
            self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{slug}"
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "operational_rpm": self.operational_requests_per_minute,
            "account_rpm": 0,
            "account_tpm": 0,
            "requests_per_day": 0,
            "tokens_per_day": 0,
            "run_cap": self.max_calls_per_run,
            "token_cap": self.max_total_tokens_per_run,
            "model_discovery": None,
        }

    @staticmethod
    def _resolve_device(requested: str, torch_module) -> str:
        req = safe(requested).casefold()
        if req in {"cpu", "cuda"}:
            if req == "cuda" and not torch_module.cuda.is_available():
                return "cpu"
            return req
        return "cuda" if torch_module.cuda.is_available() else "cpu"

    def _type_document(self, typ: str) -> str:
        meta = self.rules.types.get(typ, {}) or {}
        aliases = [safe(x) for x in (meta.get("aliases", []) or []) if safe(x)][:20]
        pair_names = sorted(_pair_label(x) for x in _taxonomy_pairs_for_type(self.rules, typ))
        chunks = [f"Canonical physical product type: {typ}."]
        if aliases:
            chunks.append("Known names and aliases: " + "; ".join(aliases) + ".")
        if pair_names:
            chunks.append("Business taxonomy mappings: " + "; ".join(pair_names) + ".")
        return " ".join(chunks)

    def _build_taxonomy_profiles(self) -> Dict[str, Dict[str, Any]]:
        profiles: Dict[str, Dict[str, Any]] = {}
        for pair in sorted(self.rules.valid_pairs):
            if not pair[0] or not pair[1] or pair == ("OTHERS", "OTHERS"):
                continue
            label = _pair_label(pair)
            profiles[label] = {
                "category": pair[0], "family": pair[1],
                "types": set(), "aliases": set(), "evidence_aliases": set(),
                "negative_aliases": set(), "powers": set(),
            }

        for row in self.rules.taxonomy_rules:
            pair = (safe(row.get("category_general")), safe(row.get("family")))
            label = _pair_label(pair)
            if label not in profiles:
                continue
            typ = safe(row.get("primary_type"))
            power = safe(row.get("power"))
            if typ:
                profiles[label]["types"].add(typ)
            if power and power != "*":
                profiles[label]["powers"].add(power)

        for typ, meta in self.rules.types.items():
            for pair in _taxonomy_pairs_for_type(self.rules, typ):
                label = _pair_label(pair)
                if label not in profiles:
                    continue
                profiles[label]["types"].add(typ)
                for a in (meta.get("aliases", []) or []):
                    if safe(a):
                        profiles[label]["aliases"].add(safe(a))
                for a in (meta.get("negative_aliases", []) or []):
                    if safe(a):
                        profiles[label]["negative_aliases"].add(safe(a))

        for row in self.rules.taxonomy_evidence_rules:
            pair = (safe(row.get("category_general")), safe(row.get("family")))
            label = _pair_label(pair)
            if label not in profiles:
                continue
            typ = safe(row.get("primary_type"))
            if typ:
                profiles[label]["types"].add(typ)
            for a in (row.get("aliases", []) or []):
                if safe(a):
                    profiles[label]["evidence_aliases"].add(safe(a))
            for a in (row.get("negative_aliases", []) or []):
                if safe(a):
                    profiles[label]["negative_aliases"].add(safe(a))
        return profiles

    def _taxonomy_document(self, label: str) -> str:
        p = self.taxonomy_profiles[label]
        chunks = [
            f"Category General: {p['category']}.",
            f"FAMILY: {p['family']}.",
        ]
        if p["types"]:
            chunks.append("Mapped physical product types: " + "; ".join(sorted(p["types"])) + ".")
        aliases = sorted(p["aliases"], key=lambda x: (-len(norm_text(x).split()), x))[:24]
        if aliases:
            chunks.append("Aliases: " + "; ".join(aliases) + ".")
        evid = sorted(p["evidence_aliases"], key=lambda x: (-len(norm_text(x).split()), x))[:20]
        if evid:
            chunks.append("Subtype evidence terms: " + "; ".join(evid) + ".")
        if p["powers"]:
            chunks.append("Power mappings: " + "; ".join(sorted(p["powers"])) + ".")
        return " ".join(chunks)

    def _evidence_text(self, brand: str, model: str, s1: Stage1Result) -> Tuple[str, List[int]]:
        rank = DeterministicEngine.IDENTITY_RANK
        ordered = sorted(
            s1.sources,
            key=lambda src: (
                rank.get(src.relevance, 0),
                src.identity_score,
                src.source_weight,
                float(src.tavily_score or 0.0),
            ),
            reverse=True,
        )[: self.max_sources]
        chunks = [f"TARGET BRAND: {brand}", f"TARGET MODEL: {model}"]
        refs: List[int] = []
        for src in ordered:
            refs.append(src.source_id)
            content = safe(src.content)[: self.source_chars]
            chunks.append(
                f"SOURCE {src.source_id} [identity={src.relevance}; model_field={src.model_match_field}; "
                f"collection={src.collection_decision or src.collection_source_mode}]: "
                f"TITLE={safe(src.title)}\nCONTENT={content}"
            )
        raw = "\n\n".join(chunks)
        return raw[: self.max_evidence_chars], refs

    @staticmethod
    def _phrase_score(text_folded: str, phrase: str, strong: float) -> float:
        phrase = safe(phrase)
        if not phrase:
            return 0.0
        if contains_alias_prepared(text_folded, phrase):
            return strong + min(5, len(norm_text(phrase).split())) * 4.0
        p_tokens = set(norm_text(phrase).split())
        t_tokens = set(norm_text(text_folded).split())
        if not p_tokens:
            return 0.0
        cov = len(p_tokens & t_tokens) / len(p_tokens)
        if len(p_tokens) == 1 and cov == 1:
            return strong * 0.28
        if cov >= 1.0:
            return strong * 0.55
        if cov >= 0.75:
            return strong * 0.32
        if cov >= 0.50:
            return strong * 0.15
        return 0.0

    def _lexical_type_scores(self, evidence: str, s1: Stage1Result) -> Dict[str, float]:
        folded = strip_accents(evidence).casefold()
        out: Dict[str, float] = {}
        for typ, meta in self.rules.types.items():
            score = 0.0
            score += self._phrase_score(folded, typ, 115.0)
            hits = []
            for a in (meta.get("aliases", []) or []):
                val = self._phrase_score(folded, a, 100.0)
                if val:
                    hits.append(val)
            score += sum(sorted(hits, reverse=True)[:4])
            negatives = sum(
                1 for a in (meta.get("negative_aliases", []) or [])
                if safe(a) and contains_alias_prepared(folded, a)
            )
            score -= min(3, negatives) * 120.0
            if typ in s1.type_candidates:
                # Stage 1 é sinal forte e deve sobreviver à união.
                score += 180.0 + 25.0 * float(s1.type_scores.get(typ, 0.0) or 0.0)
            out[typ] = score
        return out

    def _lexical_taxonomy_scores(self, evidence: str, type_candidates: Sequence[str]) -> Dict[str, float]:
        folded = strip_accents(evidence).casefold()
        top_types = set(type_candidates)
        out: Dict[str, float] = {}
        for label, p in self.taxonomy_profiles.items():
            score = self._phrase_score(folded, p["family"], 130.0)
            ev_hits = [
                self._phrase_score(folded, a, 150.0)
                for a in p["evidence_aliases"]
            ]
            score += sum(sorted((x for x in ev_hits if x), reverse=True)[:4])
            alias_hits = [
                self._phrase_score(folded, a, 90.0)
                for a in p["aliases"]
            ]
            score += sum(sorted((x for x in alias_hits if x), reverse=True)[:4])
            if p["types"] & top_types:
                score += 150.0
            negatives = sum(
                1 for a in p["negative_aliases"]
                if safe(a) and contains_alias_prepared(folded, a)
            )
            score -= min(3, negatives) * 130.0
            out[label] = score
        return out

    def _ensure_models(self) -> None:
        if self._embedder is not None and self._reranker is not None:
            return
        import torch
        from sentence_transformers import SentenceTransformer
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        if self.offline_mode:
            # Impede qualquer tentativa de consulta/download do Hugging Face durante a classificação.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        embed_device = self._resolve_device(self.embedding_device_cfg, torch)
        rerank_device = self._resolve_device(self.reranker_device_cfg, torch)
        LOG.info(
            "SEMANTIC FUNNEL loading | embedding=%s (%s) | reranker=%s (%s) | "
            "local_only=%s | offline=%s | type_rerank=%s | tax_pool=%d | rerank_max_length=%d",
            self.embedding_model_name, embed_device,
            self.reranker_model_name, rerank_device,
            self.local_files_only, self.offline_mode, self.rerank_types,
            self.taxonomy_pool_k, self.rerank_max_length,
        )
        self._embedder = SentenceTransformer(
            self.embedding_model_name,
            device=embed_device,
            local_files_only=self.local_files_only,
        )
        type_docs = [self.type_docs[x] for x in self.type_labels]
        tax_docs = [self.taxonomy_docs[x] for x in self.taxonomy_labels]
        self._type_doc_embeddings = self._embedder.encode(
            type_docs,
            batch_size=self.embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._taxonomy_doc_embeddings = self._embedder.encode(
            tax_docs,
            batch_size=self.embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._reranker_tokenizer = AutoTokenizer.from_pretrained(
            self.reranker_model_name,
            local_files_only=self.local_files_only,
        )
        self._reranker = AutoModelForSequenceClassification.from_pretrained(
            self.reranker_model_name,
            local_files_only=self.local_files_only,
        )
        self._reranker.to(rerank_device)
        self._reranker.eval()
        self._reranker_device = rerank_device

    def _query_embedding(self, query: str):
        # Um único embedding por produto é reutilizado em TYPE e TAXONOMIA.
        return self._embedder.encode(
            [query],
            batch_size=1,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]

    @staticmethod
    def _dense_scores_from_vector(query_embedding, labels: Sequence[str], doc_embeddings) -> Dict[str, float]:
        sims = doc_embeddings @ query_embedding
        return {label: float(sims[i]) for i, label in enumerate(labels)}

    @staticmethod
    def _rrf(rank: Optional[int], k: int = 60) -> float:
        return 0.0 if rank is None else 1.0 / (k + rank)

    def _merge_pool(
        self,
        lexical: Dict[str, float],
        dense: Dict[str, float],
        required: Iterable[str],
        dense_k: int,
        pool_k: int,
    ) -> List[str]:
        lex_ranked = [x[0] for x in sorted(lexical.items(), key=lambda kv: (-kv[1], kv[0]))]
        den_ranked = [x[0] for x in sorted(dense.items(), key=lambda kv: (-kv[1], kv[0]))]
        lex_pos = {x: i for i, x in enumerate(lex_ranked, 1)}
        den_pos = {x: i for i, x in enumerate(den_ranked, 1)}
        pool = set(lex_ranked[:dense_k]) | set(den_ranked[:dense_k]) | {safe(x) for x in required if safe(x)}
        score = {
            x: self._rrf(lex_pos.get(x)) + self._rrf(den_pos.get(x))
            for x in pool
        }
        return sorted(
            pool,
            key=lambda x: (-score.get(x, 0.0), -lexical.get(x, 0.0), -dense.get(x, -9.0), x),
        )[:pool_k]

    def _rerank_labels(self, query: str, labels: Sequence[str], docs: Dict[str, str], top_k: int) -> Tuple[List[str], Dict[str, float]]:
        if not labels:
            return [], {}
        started = time.monotonic()
        torch = self._torch
        scores: List[float] = []
        for start in range(0, len(labels), self.rerank_batch_size):
            batch = list(labels[start:start + self.rerank_batch_size])
            enc = self._reranker_tokenizer(
                [query] * len(batch),
                [docs[x] for x in batch],
                padding=True,
                truncation=True,
                max_length=self.rerank_max_length,
                return_tensors="pt",
            )
            enc = {k: v.to(self._reranker_device) for k, v in enc.items()}
            with torch.inference_mode():
                logits = self._reranker(**enc, return_dict=True).logits
                if logits.ndim == 2 and logits.shape[1] > 1:
                    vals = logits[:, -1].float().view(-1)
                else:
                    vals = logits.float().view(-1)
            scores.extend(vals.detach().cpu().numpy().astype(float).tolist())
        self.rerank_seconds += time.monotonic() - started
        ranked = sorted(zip(labels, scores), key=lambda kv: (-kv[1], kv[0]))
        return [x[0] for x in ranked[:top_k]], {x[0]: float(x[1]) for x in ranked}

    def _semantic_candidates(self, brand: str, model: str, s1: Stage1Result) -> Dict[str, Any]:
        self._ensure_models()
        started = time.monotonic()
        rerank_before = self.rerank_seconds
        evidence, refs = self._evidence_text(brand, model, s1)

        query_embedding = self._query_embedding(evidence)
        type_lex = self._lexical_type_scores(evidence, s1)
        type_dense = self._dense_scores_from_vector(query_embedding, self.type_labels, self._type_doc_embeddings)
        type_pool = self._merge_pool(
            type_lex, type_dense, s1.type_candidates,
            self.type_dense_k, self.type_pool_k,
        )

        # TYPE: BGE + lexical/RRF já têm recall alto e são baratos. O cross-encoder
        # fica desligado por padrão nesta etapa para não duplicar o maior custo em CPU.
        if self.rerank_types and len(type_pool) > self.type_top_k:
            type_top, type_rerank = self._rerank_labels(
                evidence, type_pool, self.type_docs, self.type_top_k
            )
        else:
            type_top = list(type_pool[: self.type_top_k])
            type_rerank = {}

        deterministic_keep = [
            typ for typ, _ in sorted(
                [(t, float(s1.type_scores.get(t, 0.0) or 0.0)) for t in s1.type_candidates],
                key=lambda kv: (-kv[1], kv[0]),
            )[: self.direct_stage1_threshold]
            if typ in self.rules.types
        ]
        for typ in deterministic_keep:
            if typ not in type_top:
                type_top.append(typ)

        # TAXONOMIA: concentra o cross-encoder onde ele mais agrega — separar FAMILYs
        # vizinhas. Pool pequeno, contexto curto e apenas um reranking por produto.
        mapped_required: List[str] = []
        for typ in type_top:
            mapped_required.extend(_pair_label(x) for x in _taxonomy_pairs_for_type(self.rules, typ))
        tax_lex = self._lexical_taxonomy_scores(evidence, type_top)
        tax_dense = self._dense_scores_from_vector(query_embedding, self.taxonomy_labels, self._taxonomy_doc_embeddings)
        tax_pool = self._merge_pool(
            tax_lex, tax_dense, mapped_required,
            self.taxonomy_dense_k, self.taxonomy_pool_k,
        )
        if self.rerank_taxonomy and len(tax_pool) > self.taxonomy_top_k:
            tax_top, tax_rerank = self._rerank_labels(
                evidence, tax_pool, self.taxonomy_docs, self.taxonomy_top_k
            )
        else:
            tax_top = list(tax_pool[: self.taxonomy_top_k])
            tax_rerank = {}

        deterministic_pairs: List[str] = []
        for typ in deterministic_keep:
            deterministic_pairs.extend(
                _pair_label(pair) for pair in _taxonomy_pairs_for_type(self.rules, typ)
            )
        for label in deterministic_pairs:
            if label in self.taxonomy_profiles and label not in tax_top:
                tax_top.append(label)

        elapsed = time.monotonic() - started
        rerank_elapsed = self.rerank_seconds - rerank_before
        self.semantic_calls += 1
        self.semantic_seconds += elapsed
        return {
            "evidence": evidence,
            "refs": refs,
            "type_candidates": type_top,
            "taxonomy_candidates": tax_top,
            "type_rerank_scores": type_rerank,
            "taxonomy_rerank_scores": tax_rerank,
            "semantic_seconds": elapsed,
            "rerank_seconds": rerank_elapsed,
            "type_pool_size": len(type_pool),
            "taxonomy_pool_size": len(tax_pool),
        }

    @staticmethod
    def _choice_criteria_type(typ: str, rules: Rules) -> str:
        meta = rules.types.get(typ, {}) or {}
        aliases = [safe(x) for x in (meta.get("aliases", []) or []) if safe(x)][:4]
        txt = f"Canonical physical product type: {typ}."
        if aliases:
            txt += " Common aliases: " + "; ".join(aliases) + "."
        return txt

    def _choice_criteria_taxonomy(self, label: str) -> str:
        p = self.taxonomy_profiles[label]
        txt = f"Valid business taxonomy pair. Category General={p['category']}; FAMILY={p['family']}."
        if p["types"]:
            txt += " Mapped types: " + "; ".join(sorted(p["types"])) + "."
        ev = sorted(p["evidence_aliases"], key=lambda x: (-len(norm_text(x).split()), x))[:6]
        if ev:
            txt += " Subtype evidence: " + "; ".join(ev) + "."
        return txt

    @staticmethod
    def _parse_choice(answers: Dict[str, Any], question_id: str) -> Dict[str, Any]:
        ans = answers.get(question_id) or {}
        probs_raw = ans.get("probabilities") or {}
        probs: Dict[str, float] = {}
        for k, v in probs_raw.items():
            try:
                probs[safe(k)] = _unit_score(v)
            except Exception:
                continue
        ranked = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
        choice = safe(ans.get("choice")) or (ranked[0][0] if ranked else "")
        top1 = next((v for k, v in ranked if norm_text(k) == norm_text(choice)), ranked[0][1] if ranked else 0.0)
        top2_label, top2 = "", 0.0
        for k, v in ranked:
            if norm_text(k) != norm_text(choice):
                top2_label, top2 = k, v
                break
        return {
            "choice": choice,
            "probability": _unit_score(top1),
            "confidence": _unit_score(ans.get("confidence")),
            "top2": top2_label,
            "top2_probability": _unit_score(top2),
            "margin": max(0.0, _unit_score(top1) - _unit_score(top2)),
            "probabilities": probs,
        }

    def classify(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        cached = self.db.get_llm(
            key, raw_hash, self.rules.hash, self.provider, self.cache_model_id
        )
        if cached is not None:
            data, usage = cached
            return data, True, usage

        if self.max_calls_per_run and self.api_calls >= self.max_calls_per_run:
            raise OperationalPause("CLEF_RUN_CAP", f"Limite local de {self.max_calls_per_run} chamadas Clef/run atingido.")
        if self.max_total_tokens_per_run and self.total_tokens >= self.max_total_tokens_per_run:
            raise OperationalPause("CLEF_LOCAL_TOKEN_CAP", f"Cap local de {self.max_total_tokens_per_run} tokens/run atingido.")

        sem = self._semantic_candidates(brand, model, s1)
        type_candidates = sem["type_candidates"]
        tax_candidates = sem["taxonomy_candidates"]
        if not type_candidates or not tax_candidates:
            raise OperationalPause(
                "CLEF_SEMANTIC_FUNNEL_EMPTY",
                "Funil BGE/reranker não produziu candidatos fechados.",
            )

        power_candidates = list(dict.fromkeys(
            [safe(x) for x in s1.power_candidates if safe(x)]
            + ([safe(s1.power)] if safe(s1.power) else [])
            + list(self.rules.allowed_powers)
        ))
        power_candidates = [x for x in power_candidates if x]
        if "UNKNOWN" not in power_candidates:
            power_candidates.append("UNKNOWN")

        type_criteria = {
            typ: self._choice_criteria_type(typ, self.rules)
            for typ in type_candidates
        }
        type_criteria["OTHER"] = "The physical product is identifiable, but none of the listed canonical PRODUCT_TYPE options is correct."
        type_criteria["INSUFFICIENT"] = "The supplied target evidence is not sufficient to identify the physical product type reliably."

        power_criteria = {x: f"Primary power-source classification: {x}." for x in power_candidates}
        if "UNKNOWN" in power_criteria:
            power_criteria["UNKNOWN"] = "The supplied evidence does not establish the primary power source reliably."

        tax_criteria = {
            label: self._choice_criteria_taxonomy(label)
            for label in tax_candidates
        }
        tax_criteria["OTHER"] = "The product is identifiable, but none of the listed valid Category General | FAMILY pairs fits it."
        tax_criteria["INSUFFICIENT"] = "The evidence is insufficient to choose a business taxonomy pair reliably."

        payload = {
            "model": self.model,
            "state": {
                "brand": brand,
                "model": model,
                "target_evidence": sem["evidence"],
            },
            "questions": {
                "product_type": {
                    "type": "choice",
                    "instructions": (
                        "Identify the actual physical target product. Choose the most appropriate canonical PRODUCT_TYPE. "
                        "Do not confuse power source, voltage, accessories, replacement parts, compatible products, merchant wording, "
                        "or a secondary bundled item with the main target product. Use OTHER only if no listed type fits; "
                        "use INSUFFICIENT when the target evidence is too weak."
                    ),
                    "criteria": type_criteria,
                },
                "power_source": {
                    "type": "choice",
                    "instructions": (
                        "Choose the primary power source of the target product. Output voltage is not proof of BATTERY or CORDED. "
                        "Use UNKNOWN when the evidence does not establish the input power source."
                    ),
                    "criteria": power_criteria,
                },
                "taxonomy_pair": {
                    "type": "choice",
                    "instructions": (
                        "Choose the single valid business Category General | FAMILY pair that best classifies the target product. "
                        "Use the physical product function and supported subtype evidence. Do not choose a more specific FAMILY without evidence. "
                        "Never invent a pair outside the options. Use OTHER if none fits and INSUFFICIENT when evidence is too weak."
                    ),
                    "criteria": tax_criteria,
                },
            },
        }

        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
        import requests

        token = os.getenv(self.api_key_env, "").strip()
        if self.provider == "jev":
            url = self.base_url
        else:
            token = token or os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
            account_id = os.getenv(self.account_id_env, "").strip()
            slug = "@cf/cloudflare/clef-flash" if self.model == "clef-flash" else "@cf/cloudflare/clef"
            url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{slug}"
        transient = {429, 500, 502, 503, 504}
        body: Dict[str, Any] = {}
        last_detail = ""
        self.api_calls += 1
        for attempt in range(self.retry_attempts):
            if self.max_calls_per_run and self.transport_attempts >= self.max_calls_per_run:
                raise OperationalPause("CLEF_HTTP_CAP", "Teto HTTP atingido, inclusive retries/reparos.")
            self.transport_attempts += 1
            self.limiter.wait()
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}
                if resp.status_code in transient and attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                if not resp.ok:
                    code = "CLEF_RATE_LIMIT" if resp.status_code == 429 else ("CLEF_TRANSIENT" if resp.status_code in transient else "CLEF_HTTP_ERROR")
                    raise OperationalPause(code, f"Cloudflare HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:1200]}")
                if body.get("success") is False:
                    raise OperationalPause("CLEF_API_ERROR", json.dumps(body, ensure_ascii=False)[:1200])
                break
            except requests.RequestException as exc:
                last_detail = safe(exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                raise OperationalPause("CLEF_TRANSIENT", last_detail) from exc
        else:
            raise OperationalPause("CLEF_TRANSIENT", last_detail or "Falha desconhecida Clef")

        result = body if self.provider == "jev" else (body.get("result") or {})
        answers = result.get("answers") or {}
        type_ans = self._parse_choice(answers, "product_type")
        power_ans = self._parse_choice(answers, "power_source")
        tax_ans = self._parse_choice(answers, "taxonomy_pair")
        cg, fam = _split_pair_label(tax_ans["choice"])

        type_choice = safe(type_ans["choice"])
        tax_choice = safe(tax_ans["choice"])
        valid_type = type_choice in self.rules.types and type_choice in type_candidates
        valid_pair = (cg, fam) in self.rules.valid_pairs and tax_choice in tax_candidates
        decision = "RESOLVED" if valid_type and valid_pair else "INSUFFICIENT_EVIDENCE"
        if type_choice in {"OTHER", "INSUFFICIENT"} or tax_choice in {"OTHER", "INSUFFICIENT"}:
            decision = "INSUFFICIENT_EVIDENCE"

        data = {
            "decision": decision,
            "product_type": type_choice if valid_type else "REVISAR",
            "power": safe(power_ans["choice"]) if safe(power_ans["choice"]) in self.rules.allowed_powers else "UNKNOWN",
            "taxonomy_pair": tax_choice,
            "category_general": cg if valid_pair else "",
            "family": fam if valid_pair else "",
            "evidence_source_ids": sem["refs"],
            "reason_short": (
                f"{self.provider.upper()} structured choice; type={type_choice or 'NONE'}; taxonomy={tax_choice or 'NONE'}"
            ),
        }

        usage_raw = result.get("usage") or {}
        input_tokens = int(usage_raw.get("input_tokens") or 0)
        output_tokens = int(usage_raw.get("output_tokens") or 0)
        self.prompt_tokens += input_tokens
        self.completion_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens

        usage = {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "json_parse_retries": 0,
            "failed_parse_tokens": 0,
            "evidence_refs_valid": True,
            "context_types": type_candidates,
            "rate_limit_headers": {},
            "semantic_type_candidates": type_candidates,
            "semantic_taxonomy_candidates": tax_candidates,
            "semantic_type_rerank_scores": sem["type_rerank_scores"],
            "semantic_taxonomy_rerank_scores": sem["taxonomy_rerank_scores"],
            "semantic_seconds": float(sem.get("semantic_seconds", 0.0) or 0.0),
            "rerank_seconds": float(sem.get("rerank_seconds", 0.0) or 0.0),
            "semantic_type_pool_size": int(sem.get("type_pool_size", 0) or 0),
            "semantic_taxonomy_pool_size": int(sem.get("taxonomy_pool_size", 0) or 0),
            "clef_type_probability": type_ans["probability"],
            "clef_type_confidence": type_ans["confidence"],
            "clef_type_top2": type_ans["top2"],
            "clef_type_top2_probability": type_ans["top2_probability"],
            "clef_type_margin": type_ans["margin"],
            "clef_power_probability": power_ans["probability"],
            "clef_power_confidence": power_ans["confidence"],
            "clef_taxonomy_probability": tax_ans["probability"],
            "clef_taxonomy_confidence": tax_ans["confidence"],
            "clef_taxonomy_top2": tax_ans["top2"],
            "clef_taxonomy_top2_probability": tax_ans["top2_probability"],
            "clef_taxonomy_margin": tax_ans["margin"],
        }
        self.db.put_llm(
            key, raw_hash, self.rules.hash, self.provider, self.cache_model_id,
            data, usage,
        )
        return data, False, usage


    def repair_type_taxonomy_conflict(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
        initial_data: Dict[str, Any],
        initial_usage: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        """Uma única correção Clef para conflito TYPE x taxonomia.

        Não faz nova busca, não refaz o BGE/reranker e não inventa labels. O Clef
        recebe somente combinações TYPE + Category|FAMILY que já são válidas no
        regras_negocio.json e escolhe uma opção conjunta coerente.
        """
        repair_cfg = (self.cfg.get(self.provider, {}) or {}).get("repair", (self.cfg.get("clef", {}) or {}).get("repair", {})) or {}
        if not bool(repair_cfg.get("enabled", True)):
            return {"decision": "INSUFFICIENT_EVIDENCE"}, False, {"clef_repair": False}

        repair_model_id = self.cache_model_id + "|repair:joint-v1"
        cached = self.db.get_llm(
            key, raw_hash, self.rules.hash, self.provider, repair_model_id
        )
        if cached is not None:
            data, usage = cached
            usage = dict(usage or {})
            usage["clef_repair"] = True
            usage["clef_repair_cache"] = True
            return data, True, usage

        if self.max_calls_per_run and self.api_calls >= self.max_calls_per_run:
            raise OperationalPause("CLEF_RUN_CAP", f"Limite local de {self.max_calls_per_run} chamadas Clef/run atingido.")
        if self.max_total_tokens_per_run and self.total_tokens >= self.max_total_tokens_per_run:
            raise OperationalPause("CLEF_LOCAL_TOKEN_CAP", f"Cap local de {self.max_total_tokens_per_run} tokens/run atingido.")

        evidence, refs = self._evidence_text(brand, model, s1)
        raw_types = [
            safe(initial_data.get("product_type")),
            safe(initial_usage.get("clef_type_top2")),
            *[safe(x) for x in (initial_usage.get("semantic_type_candidates", []) or [])],
        ]
        type_candidates: List[str] = []
        for typ in raw_types:
            if typ in self.rules.types and typ not in type_candidates:
                type_candidates.append(typ)

        max_types = max(2, int(repair_cfg.get("max_types", 5) or 5))
        max_joint = max(4, int(repair_cfg.get("max_joint_options", 14) or 14))
        type_candidates = type_candidates[:max_types]

        joint_criteria: Dict[str, str] = {}
        for typ in type_candidates:
            for pair in sorted(_taxonomy_pairs_for_type(self.rules, typ)):
                label = f"{typ} || {_pair_label(pair)}"
                if label in joint_criteria:
                    continue
                joint_criteria[label] = (
                    self._choice_criteria_type(typ, self.rules)
                    + " "
                    + self._choice_criteria_taxonomy(_pair_label(pair))
                )
                if len(joint_criteria) >= max_joint:
                    break
            if len(joint_criteria) >= max_joint:
                break

        if not joint_criteria:
            return {"decision": "INSUFFICIENT_EVIDENCE"}, False, {"clef_repair": True}

        joint_criteria["INSUFFICIENT"] = (
            "The available target evidence is insufficient to choose one coherent physical product type "
            "and business taxonomy pair reliably."
        )
        initial_type = safe(initial_data.get("product_type"))
        initial_pair = safe(initial_data.get("taxonomy_pair"))
        payload = {
            "model": self.model,
            "state": {
                "brand": brand,
                "model": model,
                "target_evidence": evidence,
                "previous_type_choice": initial_type,
                "previous_taxonomy_choice": initial_pair,
                "repair_reason": "TYPE_TAXONOMY_CONFLICT",
            },
            "questions": {
                "joint_classification": {
                    "type": "choice",
                    "instructions": (
                        "Repair the previous inconsistent result by choosing ONE coherent joint option. "
                        "Each option contains a canonical PRODUCT_TYPE and a business Category General | FAMILY pair "
                        "that is valid for that type. Determine whether the target is the complete physical product, "
                        "a replacement part, accessory, kit, or another canonical type from the evidence. "
                        "Do not preserve the previous type merely because it was selected before. "
                        "Use INSUFFICIENT only when the evidence cannot reliably distinguish the coherent options."
                    ),
                    "criteria": joint_criteria,
                }
            },
        }

        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
        import requests

        token = os.getenv(self.api_key_env, "").strip()
        if self.provider == "jev":
            url = self.base_url
        else:
            token = token or os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
            account_id = os.getenv(self.account_id_env, "").strip()
            slug = "@cf/cloudflare/clef-flash" if self.model == "clef-flash" else "@cf/cloudflare/clef"
            url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{slug}"
        transient = {429, 500, 502, 503, 504}
        body: Dict[str, Any] = {}
        last_detail = ""
        self.api_calls += 1
        self.repair_calls += 1
        for attempt in range(self.retry_attempts):
            if self.max_calls_per_run and self.transport_attempts >= self.max_calls_per_run:
                raise OperationalPause("CLEF_HTTP_CAP", "Teto HTTP atingido, inclusive retries/reparos.")
            self.transport_attempts += 1
            self.limiter.wait()
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}
                if resp.status_code in transient and attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                if not resp.ok:
                    code = "CLEF_RATE_LIMIT" if resp.status_code == 429 else ("CLEF_TRANSIENT" if resp.status_code in transient else "CLEF_HTTP_ERROR")
                    raise OperationalPause(code, f"Cloudflare repair HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:1200]}")
                if body.get("success") is False:
                    raise OperationalPause("CLEF_API_ERROR", json.dumps(body, ensure_ascii=False)[:1200])
                break
            except requests.RequestException as exc:
                last_detail = safe(exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                raise OperationalPause("CLEF_TRANSIENT", last_detail) from exc
        else:
            raise OperationalPause("CLEF_TRANSIENT", last_detail or "Falha desconhecida Clef repair")

        result = body if self.provider == "jev" else (body.get("result") or {})
        answers = result.get("answers") or {}
        joint_ans = self._parse_choice(answers, "joint_classification")
        joint_choice = safe(joint_ans.get("choice"))
        typ, pair_label = "", ""
        if "||" in joint_choice:
            typ, pair_label = [safe(x) for x in joint_choice.split("||", 1)]
        cg, fam = _split_pair_label(pair_label)
        compatible = (cg, fam) in _taxonomy_pairs_for_type(self.rules, typ)
        resolved = (
            joint_choice != "INSUFFICIENT"
            and typ in self.rules.types
            and (cg, fam) in self.rules.valid_pairs
            and compatible
        )

        data = {
            "decision": "RESOLVED" if resolved else "INSUFFICIENT_EVIDENCE",
            "product_type": typ if resolved else "REVISAR",
            "power": safe(initial_data.get("power")) if safe(initial_data.get("power")) in self.rules.allowed_powers else "UNKNOWN",
            "taxonomy_pair": pair_label if resolved else "",
            "category_general": cg if resolved else "",
            "family": fam if resolved else "",
            "evidence_source_ids": refs,
            "reason_short": (
                f"{self.provider.upper()} joint repair; previous={initial_type} + {initial_pair}; choice={joint_choice or 'NONE'}"
            ),
        }
        usage_raw = result.get("usage") or {}
        input_tokens = int(usage_raw.get("input_tokens") or 0)
        output_tokens = int(usage_raw.get("output_tokens") or 0)
        self.prompt_tokens += input_tokens
        self.completion_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens
        usage = {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "clef_repair": True,
            "clef_repair_cache": False,
            "clef_repair_probability": joint_ans["probability"],
            "clef_repair_confidence": joint_ans["confidence"],
            "clef_repair_top2": joint_ans["top2"],
            "clef_repair_margin": joint_ans["margin"],
            "clef_type_probability": joint_ans["probability"],
            "clef_type_confidence": joint_ans["confidence"],
            "clef_type_top2": joint_ans["top2"],
            "clef_type_margin": joint_ans["margin"],
            "clef_taxonomy_probability": joint_ans["probability"],
            "clef_taxonomy_confidence": joint_ans["confidence"],
            "clef_taxonomy_top2": joint_ans["top2"],
            "clef_taxonomy_margin": joint_ans["margin"],
            "semantic_type_candidates": initial_usage.get("semantic_type_candidates", []) or [],
            "semantic_taxonomy_candidates": initial_usage.get("semantic_taxonomy_candidates", []) or [],
            "semantic_seconds": 0.0,
            "rerank_seconds": 0.0,
        }
        self.db.put_llm(
            key, raw_hash, self.rules.hash, self.provider, repair_model_id,
            data, usage,
        )
        return data, False, usage


def choose_title(sources: List[Source], refs: Sequence[int]) -> Tuple[str, str]:
    by_id = {s.source_id: s for s in sources}
    ordered = [by_id[x] for x in refs if x in by_id]
    if not ordered:
        rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1, "CONFLICT": 0}
        ordered = sorted(
            sources,
            key=lambda s: (rank.get(s.relevance, 0), s.identity_score, s.source_weight),
            reverse=True,
        )
    if not ordered:
        return "", ""
    return ordered[0].title, ordered[0].url



def choose_title_for_type(
    sources: List[Source], typ: str, refs: Sequence[int] = (), min_identity: str = ""
) -> Tuple[str, str]:
    """Escolhe o título da fonte que realmente sustenta o TYPE.

    Quando ``min_identity`` é informado (RESOLVED/AUTO_APROVADO), não há
    fallback para um título genérico: o título precisa vir da própria fonte
    classificadora e cumprir o gate de identidade.
    """
    rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1, "CONFLICT": 0}
    refset = {int(x) for x in refs if isinstance(x, int) and not isinstance(x, bool)}
    candidates = [s for s in sources if float(s.type_scores.get(typ, 0.0) or 0.0) > 0.0]
    if min_identity:
        floor = rank.get(safe(min_identity).upper(), 99)
        candidates = [s for s in candidates if rank.get(s.relevance, 0) >= floor]
    if refset:
        cited_support = [s for s in candidates if s.source_id in refset]
        if cited_support:
            candidates = cited_support
        elif min_identity:
            return "", ""
    if not candidates:
        return ("", "") if min_identity else choose_title(sources, refs)
    candidates.sort(
        key=lambda src: (
            rank.get(src.relevance, 0),
            1 if "TITLE" in (src.type_evidence_fields.get(typ, []) or []) else 0,
            float(src.type_scores.get(typ, 0.0) or 0.0),
            src.identity_score,
            0 if src.quality_flags else 1,
        ),
        reverse=True,
    )
    return candidates[0].title, candidates[0].url


def choose_reference_title(
    raw: RawEvidence, brand: str, model: str, cfg: Dict[str, Any]
) -> Tuple[str, str, str, str]:
    """Melhor titulo de Search para apoio humano; nunca vira evidencia sozinho.

    Retorna (title, url, identity, collection_decision). Titulo de referencia pode
    ser WEAK e portanto serve apenas para diagnostico/manual. A classificacao
    automatica usa somente curated_results ou TITLE_ONLY_RESCUE STRONG/PROBABLE.
    """
    payload = raw.payload or {}
    results = [x for x in (payload.get("results", []) or []) if isinstance(x, dict)]
    if not results:
        return "", "", "", ""
    search_model = derive_search_model(brand, model, cfg)
    collection = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
    audit_by_url: Dict[str, str] = {}
    for row in (collection.get("source_audit", []) or []):
        if isinstance(row, dict) and _url_key(row.get("url", "")):
            audit_by_url[_url_key(row.get("url", ""))] = safe(row.get("decision"))
    rank = DeterministicEngine.IDENTITY_RANK
    scored = []
    hard_rejects = {"REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"}
    for res in results:
        title, url = safe(res.get("title")), safe(res.get("url"))
        if not title:
            continue
        meta = _source_identity_for_result(brand, search_model, res, cfg)
        decision = audit_by_url.get(_url_key(url), "")
        try:
            tv = float(res.get("score", 0) or 0)
        except Exception:
            tv = 0.0
        scored.append((
            0 if decision in hard_rejects else 1,
            rank.get(meta["level"], 0),
            tv, title, url, meta["level"], decision
        ))
    if not scored:
        return "", "", "", ""
    scored.sort(reverse=True)
    best = scored[0]
    return best[3], best[4], best[5], best[6]


def taxonomy_consensus_for_review(
    rules: Rules, s1: Stage1Result, brand: str, model: str
) -> Tuple[str, str, str]:
    """Preenche Category/FAMILY quando todos os TYPE candidatos convergem.

    Nao resolve o TYPE e nao remove Review Required. Ex.: DRILL + SCREWDRIVER
    podem permanecer ambíguos, mas ambos pertencem a OTHERS | POWER TOOLS.
    """
    candidates = list(dict.fromkeys(s1.type_candidates or []))
    if not candidates:
        return "", "", ""
    power = s1.power or "UNKNOWN"
    mapped: List[Tuple[str, str, str]] = []
    for typ in candidates:
        evidence = taxonomy_evidence_text(s1.sources, typ)
        cg, fam, why = rules.map_taxonomy(typ, power, evidence, brand, model)
        if (cg, fam) not in rules.valid_pairs or (cg, fam) == ("OTHERS", "OTHERS"):
            return "", "", ""
        mapped.append((cg, fam, why))
    pairs = {(x[0], x[1]) for x in mapped}
    if len(pairs) != 1:
        return "", "", ""
    cg, fam = next(iter(pairs))
    return cg, fam, "TAXONOMY_CONSENSUS:" + ",".join(candidates)




def record_for_raw_state(raw: RawEvidence) -> Dict[str, Any]:
    status_map = {
        "RAW_MISSING": ("PENDING_COLLECTION", "Produto ausente do cache Search"),
        "RAW_EMPTY": ("REVIEW_NO_RESULTS", "Consulta Search realizada sem resultados; revisão sem recoleta automática"),
        "RAW_CORRUPT": ("CACHE_ERROR", "RAW Search corrompido"),
        "RAW_COLLISION": ("CACHE_COLLISION", "Colisão de chave de negócio"),
    }
    status, msg = status_map.get(raw.state, ("CACHE_ERROR", raw.reason))
    return {
        "Marca": raw.brand,
        "Modelo": raw.model,
        "Raw State": raw.state,
        "Resolution State": status,
        "Stage": "0",
        "O que é (Tipo)": "REVISAR",
        "Alimentação": "UNKNOWN",
        "Category General": "",
        "FAMILY": "",
        "Título Real": "",
        "Fonte URL": "",
        "Review Required": "SIM",
        "Reason Code": raw.state,
        "Motivo": f"{msg}. {raw.reason}".strip(),
        "Pipeline Version": PIPELINE_VERSION,
        "Processed At": now_iso(),
    }



def record_for_uncurated_cache(raw: RawEvidence) -> Dict[str, Any]:
    """Bloqueia classificacao R9.5 sobre cache legado ainda nao curado."""
    return {
        "Marca": raw.brand,
        "Modelo": raw.model,
        "Raw State": raw.state,
        "Resolution State": "PENDING_CURATION",
        "Stage": "COLLECTION",
        "O que é (Tipo)": "REVISAR",
        "Alimentação": "UNKNOWN",
        "Category General": "",
        "FAMILY": "",
        "Título Real": "",
        "Fonte URL": "",
        "Review Required": "SIM",
        "Reason Code": "LEGACY_CACHE_NOT_CURATED",
        "Motivo": (
            "R9.5 exige Collection Quality antes da classificacao. "
            "Cache RAW legado sem _collection; execute a opcao 6 (0 creditos) "
            "ou uma nova coleta R9.5 antes de classificar."
        ),
        "Pipeline Version": PIPELINE_VERSION,
        "Processed At": now_iso(),
        "Clef Provider": "",
        "Clef Model": "",
        "Clef Cache": "",
        "Clef Prompt Tokens": 0,
        "Clef Completion Tokens": 0,
        "Clef Total Tokens": 0,
    }



def _merge_clef_usage(initial: Dict[str, Any], repair: Dict[str, Any]) -> Dict[str, Any]:
    """Combina métricas da primeira decisão Clef com a única chamada de repair."""
    out = dict(initial or {})
    rep = dict(repair or {})
    for key in ["prompt_tokens", "completion_tokens", "total_tokens"]:
        out[key] = int(initial.get(key, 0) or 0) + int(rep.get(key, 0) or 0)
    for key in [
        "clef_type_probability", "clef_type_confidence", "clef_type_top2", "clef_type_margin",
        "clef_taxonomy_probability", "clef_taxonomy_confidence", "clef_taxonomy_top2", "clef_taxonomy_margin",
        "clef_repair", "clef_repair_cache", "clef_repair_probability", "clef_repair_confidence",
        "clef_repair_top2", "clef_repair_margin",
    ]:
        if key in rep:
            out[key] = rep[key]
    return out


def _clef_auto_gate(
    data: Dict[str, Any],
    usage: Dict[str, Any],
    rules: Rules,
    s1: Stage1Result,
    cfg: Dict[str, Any],
) -> Tuple[bool, str]:
    """Gate conservador de AUTO. Clef pode sugerir em REVIEW sem passar neste gate."""
    if safe(data.get("decision")) != "RESOLVED":
        return False, f"CLEF_{safe(data.get('decision')) or 'UNRESOLVED'}"
    typ = safe(data.get("product_type"))
    cg = safe(data.get("category_general"))
    fam = safe(data.get("family"))
    if typ not in rules.types:
        return False, "CLEF_TYPE_INVALID"
    if (cg, fam) not in rules.valid_pairs or (cg, fam) == ("OTHERS", "OTHERS"):
        return False, "CLEF_TAXONOMY_INVALID"

    policy = (cfg.get("clef", {}) or {}).get("auto_approval", {}) or {}
    if bool(policy.get("require_type_taxonomy_compatible", True)):
        allowed_pairs = _taxonomy_pairs_for_type(rules, typ)
        if allowed_pairs and (cg, fam) not in allowed_pairs:
            return False, "CLEF_TYPE_TAXONOMY_CONFLICT"

    if bool(policy.get("require_strong_identity", True)) and s1.identity_best != "STRONG":
        return False, "CLEF_IDENTITY_NOT_STRONG"

    tax_prob = _unit_score(usage.get("clef_taxonomy_probability"))
    tax_conf = _unit_score(usage.get("clef_taxonomy_confidence"))
    tax_margin = _unit_score(usage.get("clef_taxonomy_margin"))
    type_prob = _unit_score(usage.get("clef_type_probability"))
    type_conf = _unit_score(usage.get("clef_type_confidence"))

    min_tax_prob = float(policy.get("min_taxonomy_probability", 0.70) or 0.0)
    min_tax_conf = float(policy.get("min_taxonomy_confidence", 0.50) or 0.0)
    min_tax_margin = float(policy.get("min_taxonomy_margin", 0.20) or 0.0)
    min_type_prob = float(policy.get("min_type_probability", 0.55) or 0.0)
    min_type_conf = float(policy.get("min_type_confidence", 0.25) or 0.0)

    if tax_prob < min_tax_prob:
        return False, f"CLEF_TAX_PROB_LOW:{tax_prob:.3f}<{min_tax_prob:.3f}"
    if tax_conf < min_tax_conf:
        return False, f"CLEF_TAX_CONF_LOW:{tax_conf:.3f}<{min_tax_conf:.3f}"
    if tax_margin < min_tax_margin:
        return False, f"CLEF_TAX_MARGIN_LOW:{tax_margin:.3f}<{min_tax_margin:.3f}"
    if type_prob < min_type_prob:
        return False, f"CLEF_TYPE_PROB_LOW:{type_prob:.3f}<{min_type_prob:.3f}"
    if type_conf < min_type_conf:
        return False, f"CLEF_TYPE_CONF_LOW:{type_conf:.3f}<{min_type_conf:.3f}"
    return True, "CLEF_AUTO_GATE_OK"


def _best_target_title(s1: Stage1Result, refs: Sequence[int], require_strong: bool) -> Tuple[str, str]:
    refset = {int(x) for x in refs if isinstance(x, int) and not isinstance(x, bool)}
    rank = DeterministicEngine.IDENTITY_RANK
    candidates = [
        src for src in s1.sources
        if (not refset or src.source_id in refset)
        and (not require_strong or src.relevance == "STRONG")
    ]
    if not candidates and require_strong:
        return "", ""
    if not candidates:
        candidates = list(s1.sources)
    if not candidates:
        return "", ""
    candidates.sort(
        key=lambda src: (
            rank.get(src.relevance, 0), src.identity_score,
            0 if src.quality_flags else 1, src.source_weight, float(src.tavily_score or 0.0),
        ),
        reverse=True,
    )
    return candidates[0].title, candidates[0].url


def finalize_product(
    brand: str,
    model: str,
    raw: RawEvidence,
    s1: Stage1Result,
    rules: Rules,
    cfg: Dict[str, Any],
    clef: Optional[ClefFunnelClassifier],
    db: ClassificationDB,
    run_id: str,
) -> Dict[str, Any]:
    key = business_key(brand, model)
    combo_components = _combo_kit_components(rules, s1)
    funnel = candidate_funnel(rules, s1) if s1.type_candidates else CandidateFunnelResult([], {}, "NO_STAGE1_CANDIDATE", blocked=True)

    ref_title, ref_url, ref_identity, ref_decision = choose_reference_title(raw, brand, model, cfg)
    collection = (raw.payload or {}).get("_collection") if isinstance((raw.payload or {}).get("_collection"), dict) else {}
    collection_state = safe(collection.get("state"))
    curated_count = len([x for x in (collection.get("curated_results", []) or []) if isinstance(x, dict)])
    raw_result_count = len([x for x in ((raw.payload or {}).get("results", []) or []) if isinstance(x, dict)])
    title_rescue_count = sum(1 for src in s1.sources if safe(src.collection_decision) == "ACCEPT_TITLE_ONLY_RESCUE")
    rescue_only = (
        curated_count == 0
        and title_rescue_count > 0
        and collection_state in {"NO_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE", "PARTIAL_TARGET_EVIDENCE"}
    )
    consensus_cg, consensus_fam, consensus_reason = taxonomy_consensus_for_review(rules, s1, brand, model)
    if s1.state == "RESOLVED_RULE" and not rescue_only:
        next_action = "CONCLUIDO"
    elif s1.state == "RESOLVED_RULE" and rescue_only:
        next_action = "CLEF_RESCUE_DOUBLE_CHECK"
    elif not s1.type_candidates and ref_identity in {"STRONG", "PROBABLE"}:
        next_action = "JSON_GAP_OU_CLEF_GAP"
    elif not s1.type_candidates and raw_result_count:
        next_action = "REVISAR_EVIDENCIA_CACHE"
    elif not s1.type_candidates:
        next_action = "REVISAR_EVIDENCIA_CACHE"
    elif consensus_cg and consensus_fam:
        next_action = "TYPE_REVIEW_FAMILY_JA_DEFINIDA"
    else:
        next_action = "CLEF_COM_CANDIDATOS_OU_REVISAO"

    common = {
        "Marca": brand,
        "Modelo": model,
        "Raw State": raw.state,
        "Collection State": collection_state,
        "Raw Search Results": raw_result_count,
        "Curated Sources": curated_count,
        "Title Rescue Sources": title_rescue_count,
        "Título Referência": ref_title,
        "Fonte Referência URL": ref_url,
        "Título Referência Identity": ref_identity,
        "Título Referência Decision": ref_decision,
        "Próxima Ação": next_action,
        "Stage1 Type Candidates": " | ".join(s1.type_candidates),
        "Stage1.5 Type Candidates": " | ".join(funnel.candidates),
        "Stage1 Type Scores": json.dumps(s1.type_scores, ensure_ascii=False, sort_keys=True),
        "Stage1 Power Candidates": " | ".join(s1.power_candidates),
        "Classification Identity": s1.identity_best,
        "Rules Hash": rules.hash,
        "Pipeline Version": PIPELINE_VERSION,
        "Processed At": now_iso(),
        "Classification Outcome": "",
        "Auto Eligible": "NÃO",
        "Double Check Priority": "",
    }

    def _output_title(primary_title: str = "", primary_url: str = "") -> Tuple[str, str]:
        # Base Final exibe somente o título que sustentou a classificação.
        # Se não houve classificação, preserva o melhor título Search para revisão humana.
        return (safe(primary_title) or ref_title, safe(primary_url) or ref_url)

    if combo_components:
        typ = "POWER TOOL COMBO KIT"
        power = s1.power or "UNKNOWN"
        tax_evidence = "\n".join(f"{x.title} {x.content[:1200]}" for x in s1.sources)
        cg, fam, tax_reason = rules.map_taxonomy(typ, power, tax_evidence, brand, model)
        title, url = choose_combo_title(rules, s1)
        return {
            **common,
            "Próxima Ação": "CONCLUIDO",
            "Resolution State": "RESOLVED",
            "Stage": "COMBO_RULE",
            "O que é (Tipo)": typ,
            "Alimentação": power,
            "Category General": cg,
            "FAMILY": fam,
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "NÃO",
            "Classification Outcome": "RULE_CLASSIFIED",
            "Auto Eligible": "SIM",
            "Double Check Priority": "BAIXA",
            "Reason Code": "POWER_TOOL_COMBO_KIT",
            "Motivo": f"Combo kit explícito com múltiplas ferramentas principais: {', '.join(combo_components)} | {tax_reason}",
            "Clef Provider": "",
            "Clef Model": "",
            "Clef Cache": "",
            "Clef Prompt Tokens": 0,
            "Clef Completion Tokens": 0,
            "Clef Total Tokens": 0,
        }

    if s1.state == "RESOLVED_RULE" and not rescue_only:
        typ = s1.product_type or "REVISAR"
        power = s1.power or "UNKNOWN"
        tax_evidence = taxonomy_evidence_text(s1.sources, typ)
        cg, fam, tax_reason = rules.map_taxonomy(typ, power, tax_evidence, brand, model)
        title, url = choose_title_for_type(s1.sources, typ, min_identity="STRONG")
        return {
            **common,
            "Próxima Ação": "CONCLUIDO",
            "Resolution State": "RESOLVED",
            "Stage": "RULE",
            "O que é (Tipo)": typ,
            "Alimentação": power,
            "Category General": cg,
            "FAMILY": fam,
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "NÃO",
            "Classification Outcome": "RULE_CLASSIFIED",
            "Auto Eligible": "SIM",
            "Double Check Priority": "BAIXA",
            "Reason Code": "RULE_UNIQUE",
            "Motivo": " | ".join(s1.reasons + [tax_reason]),
            "Clef Provider": "",
            "Clef Model": "",
            "Clef Cache": "",
            "Clef Prompt Tokens": 0,
            "Clef Completion Tokens": 0,
            "Clef Total Tokens": 0,
        }

    # IMPORTANTE v4.0.5: o funil NÃO fecha casos AMBIGUOUS/UNRESOLVED.
    # Mesmo que BGE/regras deixem apenas um candidato, se Stage 1 não foi
    # RESOLVED_RULE o Clef é o adjudicador obrigatório quando há evidência útil.

    # Clef é o Stage 2 padrão. NO_TARGET_EVIDENCE sem qualquer title-rescue
    # target-anchored é desviado antes de carregar/executar BGE/reranker.
    if clef is not None:
        if collection_state == "NO_TARGET_EVIDENCE" and curated_count == 0 and title_rescue_count == 0:
            clef_preflight = "NO_TARGET_EVIDENCE"
        else:
            clef_preflight = _clef_preflight_rejection_reason(rules, s1)
        if clef_preflight:
            title, url = _output_title(*choose_title(s1.sources, []))
            reason_text = {
                "LOW_TARGET_EVIDENCE":
                    "Clef não chamado: nenhuma fonte PROBABLE/STRONG do SKU alvo; classificação sem identidade seria arriscada.",
                "MULTI_PRODUCT_TARGET":
                    "Clef não chamado: evidência target descreve múltiplos produtos físicos sem combo explicitamente modelado.",
                "NO_TARGET_EVIDENCE":
                    "Clef/BGE não chamados: cache curado sem evidência target e sem title-rescue seguro; item direcionado à revisão das fontes existentes.",
            }.get(clef_preflight, "Clef bloqueado pelo gate de identidade.")
            return {
                **common,
                "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
                "Resolution State": "REVIEW_CACHE_EVIDENCE",
                "Classification Outcome": "REVIEW_CACHE_EVIDENCE",
                "Stage": "COLLECTION_GATE",
                "O que é (Tipo)": s1.product_type or "REVISAR",
                "Alimentação": s1.power or "UNKNOWN",
                "Category General": consensus_cg,
                "FAMILY": consensus_fam,
                "Título Real": title,
                "Fonte URL": url,
                "Review Required": "SIM",
                "Auto Eligible": "NÃO",
                "Double Check Priority": "ALTA",
                "Reason Code": clef_preflight,
                "Motivo": reason_text + " Nenhuma segunda consulta automática foi executada.",
                "Clef Provider": "",
                "Clef Model": "",
                "Clef Cache": "",
                "Clef Prompt Tokens": 0,
                "Clef Completion Tokens": 0,
                "Clef Total Tokens": 0,
            }

        try:
            data, cache_hit, usage = clef.classify(key, raw.raw_hash, brand, model, s1)
        except OperationalPause as exc:
            if exc.code == "CLEF_SEMANTIC_FUNNEL_EMPTY":
                title, url = _output_title(*choose_title(s1.sources, []))
                return {
                    **common,
                    "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
                    "Resolution State": "REVIEW_CACHE_EVIDENCE",
                    "Classification Outcome": "REVIEW_CACHE_EVIDENCE",
                    "Stage": "SEMANTIC_FUNNEL_EMPTY",
                    "O que é (Tipo)": s1.product_type or "REVISAR",
                    "Alimentação": s1.power or "UNKNOWN",
                    "Category General": consensus_cg,
                    "FAMILY": consensus_fam,
                    "Título Real": title,
                    "Fonte URL": url,
                    "Review Required": "SIM",
                    "Auto Eligible": "NÃO",
                    "Double Check Priority": "ALTA",
                    "Reason Code": exc.code,
                    "Motivo": f"{safe(exc)} Nenhuma segunda consulta automática foi executada.",
                    "Clef Provider": "",
                    "Clef Model": "",
                    "Clef Cache": "",
                    "Clef Prompt Tokens": 0,
                    "Clef Completion Tokens": 0,
                    "Clef Total Tokens": 0,
                }
            raise
        actual_provider = safe(usage.get("provider")) or "clef"
        actual_model = safe(usage.get("model")) or safe(getattr(clef, "model", "clef"))
        refs = data.get("evidence_source_ids", []) or []
        typ = safe(data.get("product_type"))
        power = safe(data.get("power")) or "UNKNOWN"
        cg = safe(data.get("category_general"))
        fam = safe(data.get("family"))
        auto_ok, gate_reason = _clef_auto_gate(data, usage, rules, s1, cfg)
        if rescue_only and auto_ok:
            auto_ok = False
            gate_reason = "CLEF_RESCUE_ONLY_DOUBLE_CHECK"
        title, url = _best_target_title(s1, refs, require_strong=auto_ok)
        title, url = _output_title(title, url)

        base_clef = {
            **common,
            "Clef Provider": actual_provider.upper(),
            "Clef Model": actual_model,
            "Clef Cache": "SIM" if cache_hit else "NÃO",
            "Clef Prompt Tokens": int(usage.get("prompt_tokens", 0) or 0),
            "Clef Completion Tokens": int(usage.get("completion_tokens", 0) or 0),
            "Clef Total Tokens": int(usage.get("total_tokens", 0) or 0),
            "Semantic Type Candidates": " | ".join(usage.get("semantic_type_candidates", []) or []),
            "Semantic Taxonomy Candidates": " || ".join(usage.get("semantic_taxonomy_candidates", []) or []),
            "Clef Type Probability": _unit_score(usage.get("clef_type_probability")),
            "Clef Type Confidence": _unit_score(usage.get("clef_type_confidence")),
            "Clef Type Top2": safe(usage.get("clef_type_top2")),
            "Clef Type Margin": _unit_score(usage.get("clef_type_margin")),
            "Clef Taxonomy Probability": _unit_score(usage.get("clef_taxonomy_probability")),
            "Clef Taxonomy Confidence": _unit_score(usage.get("clef_taxonomy_confidence")),
            "Clef Taxonomy Top2": safe(usage.get("clef_taxonomy_top2")),
            "Clef Taxonomy Margin": _unit_score(usage.get("clef_taxonomy_margin")),
            "Clef Repair": "NÃO",
            "Clef Repair Cache": "",
        }
        clef_valid = (
            safe(data.get("decision")) == "RESOLVED"
            and typ in rules.types
            and (cg, fam) in rules.valid_pairs
            and (cg, fam) != ("OTHERS", "OTHERS")
        )

        # v4.0.5: NÃO bloquear a classificação Clef por s1.identity_best == WEAK.
        # Esse campo mede a identidade das fontes que sustentaram um TYPE no Stage 1;
        # em casos semanticamente ambíguos que dependem de BGE/Clef ele pode permanecer
        # WEAK mesmo quando existem fontes target PROBABLE/STRONG. O preflight acima já
        # exige evidência target mínima antes de chamar Clef. A exigência STRONG continua
        # valendo exclusivamente para AUTO_APROVADO via _clef_auto_gate.

        # Se a primeira resposta selecionou TYPE e taxonomia incompatíveis, uma
        # única chamada Clef de repair escolhe uma opção CONJUNTA já validada pelo
        # regras_negocio.json. Não há nova busca nem novo BGE/reranker.
        repair_cfg = (cfg.get("clef", {}) or {}).get("repair", {}) or {}
        if clef_valid and gate_reason == "CLEF_TYPE_TAXONOMY_CONFLICT" and bool(repair_cfg.get("enabled", True)):
            repair_data, repair_cache_hit, repair_usage = clef.repair_type_taxonomy_conflict(
                key, raw.raw_hash, brand, model, s1, data, usage
            )
            if safe(repair_data.get("decision")) == "RESOLVED":
                data = repair_data
                usage = _merge_clef_usage(usage, repair_usage)
                refs = data.get("evidence_source_ids", []) or refs
                typ = safe(data.get("product_type"))
                power = safe(data.get("power")) or power
                cg = safe(data.get("category_general"))
                fam = safe(data.get("family"))
                auto_ok, gate_reason = _clef_auto_gate(data, usage, rules, s1, cfg)
                if rescue_only and auto_ok:
                    auto_ok = False
                    gate_reason = "CLEF_RESCUE_ONLY_DOUBLE_CHECK"
                title, url = _best_target_title(s1, refs, require_strong=auto_ok)
                title, url = _output_title(title, url)
                base_clef.update({
                    "Clef Cache": "SIM" if (cache_hit and repair_cache_hit) else "NÃO",
                    "Clef Prompt Tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "Clef Completion Tokens": int(usage.get("completion_tokens", 0) or 0),
                    "Clef Total Tokens": int(usage.get("total_tokens", 0) or 0),
                    "Clef Type Probability": _unit_score(usage.get("clef_type_probability")),
                    "Clef Type Confidence": _unit_score(usage.get("clef_type_confidence")),
                    "Clef Type Top2": safe(usage.get("clef_type_top2")),
                    "Clef Type Margin": _unit_score(usage.get("clef_type_margin")),
                    "Clef Taxonomy Probability": _unit_score(usage.get("clef_taxonomy_probability")),
                    "Clef Taxonomy Confidence": _unit_score(usage.get("clef_taxonomy_confidence")),
                    "Clef Taxonomy Top2": safe(usage.get("clef_taxonomy_top2")),
                    "Clef Taxonomy Margin": _unit_score(usage.get("clef_taxonomy_margin")),
                    "Clef Repair": "SIM",
                    "Clef Repair Cache": "SIM" if repair_cache_hit else "NÃO",
                })
                clef_valid = (
                    typ in rules.types
                    and (cg, fam) in rules.valid_pairs
                    and (cg, fam) in _taxonomy_pairs_for_type(rules, typ)
                )
            else:
                return {
                    **base_clef,
                    "Raw State": "RAW_OK",
                    "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
                    "Resolution State": "REVIEW_CACHE_EVIDENCE",
                    "Classification Outcome": "REVIEW_CACHE_EVIDENCE",
                    "Stage": "CLEF_REPAIR_INSUFFICIENT",
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": power if power in rules.allowed_powers else "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": title,
                    "Fonte URL": url,
                    "Review Required": "SIM",
                    "Auto Eligible": "NÃO",
                    "Double Check Priority": "ALTA",
                    "Reason Code": "CLEF_REPAIR_INSUFFICIENT",
                    "Motivo": (
                        f"Primeira resposta teve TYPE x TAXONOMIA incompatíveis ({typ} + {cg} | {fam}); "
                        "a única chamada Clef Repair não conseguiu escolher uma combinação coerente. "
                        "Nenhuma segunda busca automática foi executada."
                    ),
                    "Evidence Source IDs": ",".join(map(str, refs)),
                    "Clef Repair": "SIM",
                    "Clef Repair Cache": "SIM" if repair_cache_hit else "NÃO",
                }
        if clef_valid:
            # Resultado estruturado válido é sempre preservado. O gate AUTO apenas
            # define se pode ser aceito sem double-check; não apaga a classificação.
            priority = "BAIXA" if auto_ok else (
                "ALTA" if any(x in gate_reason for x in ["IDENTITY", "CONFLICT", "MARGIN_LOW"]) else "MEDIA"
            )
            return {
                **base_clef,
                "Raw State": "RAW_OK",
                "Próxima Ação": "CONCLUIDO" if auto_ok else "DOUBLE_CHECK",
                "Resolution State": "RESOLVED" if auto_ok else "CLASSIFIED_CLEF",
                "Classification Outcome": "CLEF_CLASSIFIED_AUTO" if auto_ok else "CLEF_CLASSIFIED",
                "Stage": "CLEF_REPAIR" if safe(base_clef.get("Clef Repair")) == "SIM" else "CLEF",
                "O que é (Tipo)": typ,
                "Alimentação": power if power in rules.allowed_powers else "UNKNOWN",
                "Category General": cg,
                "FAMILY": fam,
                "Título Real": title,
                "Fonte URL": url,
                "Review Required": "NÃO" if auto_ok else "SIM",
                "Auto Eligible": "SIM" if auto_ok else "NÃO",
                "Double Check Priority": priority,
                "Reason Code": "CLEF_AUTO_APROVADO" if auto_ok else gate_reason,
                "Motivo": (
                    f"{safe(data.get('reason_short'))} | {gate_reason} | "
                    f"tax_prob={_unit_score(usage.get('clef_taxonomy_probability')):.3f} | "
                    f"tax_conf={_unit_score(usage.get('clef_taxonomy_confidence')):.3f} | "
                    f"tax_margin={_unit_score(usage.get('clef_taxonomy_margin')):.3f}"
                ),
                "Evidence Source IDs": ",".join(map(str, refs)),
            }

        # Clef foi consultado, mas não conseguiu produzir TYPE + par taxonômico
        # válidos. Isso é falta de evidência/aderência para esta consulta, não uma
        # solicitação de recoleta: encaminhar as fontes existentes para revisão.
        return {
            **base_clef,
            "Raw State": "RAW_OK",
            "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
            "Resolution State": "REVIEW_CACHE_EVIDENCE",
            "Classification Outcome": "REVIEW_CACHE_EVIDENCE",
            "Stage": "CLEF_INSUFFICIENT",
            "O que é (Tipo)": typ if typ in rules.types else "REVISAR",
            "Alimentação": power if power in rules.allowed_powers else "UNKNOWN",
            "Category General": cg if (cg, fam) in rules.valid_pairs else "",
            "FAMILY": fam if (cg, fam) in rules.valid_pairs else "",
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "NÃO",
            "Auto Eligible": "NÃO",
            "Double Check Priority": "ALTA",
            "Reason Code": f"CLEF_{safe(data.get('decision')) or 'INSUFFICIENT'}",
            "Motivo": (
                f"{safe(data.get('reason_short'))} | Clef não fechou TYPE + taxonomia válidos. "
                "Nenhuma segunda consulta automática foi executada."
            ),
            "Evidence Source IDs": ",".join(map(str, refs)),
        }

    # Execução explicitamente sem Clef: mantém resultado conservador em REVIEW.
    title, url = _output_title(*choose_title(s1.sources, []))
    return {
        **common,
        "Resolution State": "REVIEW",
        "Stage": "FUNNEL",
        "O que é (Tipo)": s1.product_type or "REVISAR",
        "Alimentação": s1.power or "UNKNOWN",
        "Category General": consensus_cg,
        "FAMILY": consensus_fam,
        "Título Real": title,
        "Fonte URL": url,
        "Review Required": "SIM",
        "Reason Code": f"CLEF_DISABLED_{s1.state}",
        "Motivo": "Clef desabilitado para esta execução; " + " | ".join(s1.reasons + ([consensus_reason] if consensus_reason else [])),
        "Clef Provider": "",
        "Clef Model": "",
        "Clef Cache": "",
        "Clef Prompt Tokens": 0,
        "Clef Completion Tokens": 0,
        "Clef Total Tokens": 0,
    }


# -----------------------------------------------------------------------------
# Baseline e exportação
# -----------------------------------------------------------------------------
BASELINE_FINAL_COLUMNS = [
    "Marca",
    "Modelo",
    "O que é (Tipo)",
    "Alimentação",
    "Category General",
    "FAMILY",
    "Título Real",
    "Próxima Ação",
    "Status",
    "Revisão",
    "Revisão B.I",
    # Campos operacionais aditivos: nunca substituem os campos Gold.
    "Status Coleta",
    "Status Identidade",
    "Status Classificação",
    "Título Referência",
    "Fonte URL",
    "Fonte Referência URL",
    "Motivo",
    "Reason Code",
    "Collection State",
    "Resolution State",
    "Classification Identity",
    "Título Referência Identity",
    "Sugestão Category General",
    "Sugestão FAMILY",
    "Fontes Pesquisa",
    "Resumo Evidências",
    "Fontes Encontradas",
    "Qualidade Referência",
]

BASELINE_MASTER_FIELDS = [
    "O que é (Tipo)",
    "Alimentação",
    "Category General",
    "FAMILY",
    "Título Real",
    "Título Referência",
    "Próxima Ação",
    "Sugestão Category General",
    "Sugestão FAMILY",
    "Fonte URL",
    "Fonte Referência URL",
    "Raw State",
    "Collection State",
    "Classification Identity",
    "Título Referência Identity",
    "Raw Search Results",
    "Curated Sources",
    "Title Rescue Sources",
    "Status",
    "Status Processamento",
    "Revisão",
    "Revisão B.I",
    "Resolution State",
    "Classification Outcome",
    "Review Required",
    "Auto Eligible",
    "Double Check Priority",
    "Reason Code",
    "Motivo",
    "Stage",
    "Rules Version",
    "Rules Hash",
    "Pipeline Version",
    "Processed At",
    "Clef Provider",
    "Clef Model",
    "Semantic Type Candidates",
    "Semantic Taxonomy Candidates",
    "Clef Type Probability",
    "Clef Type Confidence",
    "Clef Type Top2",
    "Clef Type Margin",
    "Clef Taxonomy Probability",
    "Clef Taxonomy Confidence",
    "Clef Taxonomy Top2",
    "Clef Taxonomy Margin",
]

_REVIEW_REJECT_STATES = {
    "REVISAR",
    "REVIEW",
    "PENDENTE_COLETA",
    "PENDING_COLLECTION",
    "PENDING_RECOLLECTION",
    "NEEDS_RECOLLECTION",
    "CACHE_ERROR",
    "CACHE_COLLISION",
    "NÃO_CLASSIFICADO",
    "NAO_CLASSIFICADO",
}

_BASELINE_ACCEPT_STATES = {
    "AUTO_APROVADO",
    "PROVÁVEL",
    "PROVAVEL",
    "VALIDADO_MANUAL",
}


def _is_manual_status(value: Any) -> bool:
    return safe(value).upper() == "MANUAL"


def _is_bi_ok_value(value: Any) -> bool:
    """Único marcador humano de aprovação final usado como HARD LOCK."""
    return safe(value).upper() == "OK"


def _row_bi_locked(row: Any, df: Any) -> bool:
    col = find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)
    return bool(col and _is_bi_ok_value(row.get(col)))


def _read_business_sheet(path: Path) -> Tuple[Any, str]:
    """Lê a primeira aba que realmente possui Marca+Modelo.

    Evita confundir a aba Resumo de relatórios com a base de negócio.
    """
    import pandas as pd

    xls = pd.ExcelFile(path)
    try:
        preferred = [
            x for x in ["Base Final", "Consolidado", "Resultados"]
            if x in xls.sheet_names
        ]
        order = preferred + [x for x in xls.sheet_names if x not in preferred]
        last_error: Optional[Exception] = None
        for sheet in order:
            try:
                df = pd.read_excel(path, sheet_name=sheet, dtype=object)
                find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
                find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
                return df, sheet
            except Exception as exc:
                last_error = exc
                continue
        raise ValueError(
            f"Nenhuma aba com Marca+Modelo encontrada em {path.name}: {last_error}"
        )
    finally:
        xls.close()


def _baseline_row_accepted(row: Any, df: Any) -> bool:
    """Define se a linha é baseline/skip.

    HARD LOCKS:
    - Revisão B.I = OK: autoridade humana final, mesmo se a linha parecer incompleta.
    - Status = Manual: preservado integralmente.

    Para Script sem OK, REVIEW/PENDING nunca vira baseline só por existir na Base Final.
    """
    if _row_bi_locked(row, df):
        return True

    status_col = find_col(df, ["Status"], required=False)
    if status_col and _is_manual_status(row.get(status_col)):
        return True

    review_required_col = find_col(df, ["Review Required"], required=False)
    if review_required_col and safe(row.get(review_required_col)).upper() == "SIM":
        return False

    resolution_col = find_col(df, ["Resolution State"], required=False)
    resolution = safe(row.get(resolution_col)).upper() if resolution_col else ""
    if resolution and resolution != "RESOLVED":
        return False

    proc_col = find_col(df, ["Status Processamento"], required=False)
    proc = safe(row.get(proc_col)).upper() if proc_col else ""
    if proc and proc not in {"PROCESSADO", "COMPLETED"}:
        return False

    review_col = find_col(df, ["Revisão", "Revisao"], required=False)
    review = safe(row.get(review_col)).upper() if review_col else ""
    if review in _REVIEW_REJECT_STATES:
        return False

    technical_gate_present = any([
        bool(review_required_col and safe(row.get(review_required_col))),
        bool(resolution_col and safe(row.get(resolution_col))),
        bool(proc_col and safe(row.get(proc_col))),
    ])
    if not technical_gate_present and review not in _BASELINE_ACCEPT_STATES:
        return False

    type_col = find_col(df, ["O que é (Tipo)", "Tipo", "Product Type"], required=False)
    cg_col = find_col(df, ["Category General"], required=False)
    fam_col = find_col(df, ["FAMILY", "Family"], required=False)

    typ = safe(row.get(type_col)).upper() if type_col else ""
    cg = safe(row.get(cg_col)).upper() if cg_col else ""
    fam = safe(row.get(fam_col)).upper() if fam_col else ""

    if not typ or typ in {"REVISAR", "UNKNOWN"}:
        return False
    if not cg or not fam:
        return False
    if (cg, fam) == ("OTHERS", "OTHERS"):
        return False
    return True

def _accepted_keys_from_baseline_file(
    path: Optional[Path],
) -> Tuple[set, set, set]:
    """Retorna (aceitos, manuais, bi_ok_locked) de um baseline."""
    accepted: set = set()
    manual: set = set()
    bi_locked: set = set()

    if not path or not path.exists():
        return accepted, manual, bi_locked

    try:
        df, _ = _read_business_sheet(path)
    except Exception as exc:
        LOG.warning("Baseline %s não pôde ser lida: %s", path, exc)
        return accepted, manual, bi_locked

    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    status = find_col(df, ["Status"], required=False)
    rbi = find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)

    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        key = business_key(brand, model)

        if rbi and _is_bi_ok_value(row.get(rbi)):
            bi_locked.add(key)
            accepted.add(key)
            continue
        if status and _is_manual_status(row.get(status)):
            manual.add(key)
            accepted.add(key)
            continue
        if _baseline_row_accepted(row, df):
            accepted.add(key)

    return accepted, manual, bi_locked

def load_baseline_inventory(
    master_path: Optional[Path],
    final_path: Optional[Path],
) -> Dict[str, set]:
    """Lê Consolidado/Base Final e separa baseline de HARD LOCK humano."""
    master_accepted, master_manual, master_bi = _accepted_keys_from_baseline_file(master_path)
    final_accepted, final_manual, final_bi = _accepted_keys_from_baseline_file(final_path)

    manual = master_manual | final_manual
    bi_locked = master_bi | final_bi
    hard_lock = manual | bi_locked
    processed = master_accepted | final_accepted | hard_lock

    return {
        "processed": processed,
        "manual": manual,
        "bi_locked": bi_locked,
        "hard_lock": hard_lock,
        "master_accepted": master_accepted,
        "final_accepted": final_accepted,
        "master_manual": master_manual,
        "final_manual": final_manual,
        "master_bi_locked": master_bi,
        "final_bi_locked": final_bi,
    }

def load_already_classified(
    master_path: Optional[Path],
    final_path: Optional[Path],
) -> Tuple[set, set]:
    """Compatibilidade: retorna união aceita + manuais."""
    inv = load_baseline_inventory(master_path, final_path)
    return inv["processed"], inv["manual"]


def _ensure_master_columns(df: Any) -> Tuple[Any, str, str]:
    import pandas as pd

    out = df.copy()
    brand_col = find_col(
        out,
        ["Marca", "Fabricante", "Brand", "Manufacturer"],
        required=False,
    )
    model_col = find_col(
        out,
        ["Modelo", "Model", "SKU", "Part Number"],
        required=False,
    )

    if not brand_col:
        brand_col = "Marca"
        out[brand_col] = pd.NA
    if not model_col:
        model_col = "Modelo"
        out[model_col] = pd.NA

    # Unifica Family/FAMILY sem apagar valor existente.
    family_col = find_col(out, ["FAMILY"], required=False)
    legacy_family = find_col(out, ["Family"], required=False)
    if not family_col:
        out["FAMILY"] = out[legacy_family] if legacy_family else pd.NA
    elif legacy_family and legacy_family != family_col:
        mask = out[family_col].isna() | (
            out[family_col].astype(str).str.strip() == ""
        )
        out.loc[mask, family_col] = out.loc[mask, legacy_family]

    for col in BASELINE_MASTER_FIELDS:
        if col not in out.columns:
            out[col] = pd.NA

    return out, brand_col, model_col


def _master_key_index(
    df: Any,
    brand_col: str,
    model_col: str,
) -> Dict[str, int]:
    """Índice deduplicado. BI_OK vence Manual; Manual vence linha automática."""
    out: Dict[str, int] = {}
    status_col = find_col(df, ["Status"], required=False)
    rbi_col = find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)

    def rank(row: Any) -> int:
        if rbi_col and _is_bi_ok_value(row.get(rbi_col)):
            return 3
        if status_col and _is_manual_status(row.get(status_col)):
            return 2
        if _baseline_row_accepted(row, df):
            return 1
        return 0

    for idx, row in df.iterrows():
        brand, model = safe(row.get(brand_col)), safe(row.get(model_col))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        if key not in out:
            out[key] = int(idx)
            continue
        old_idx = out[key]
        old_rank = rank(df.loc[old_idx])
        new_rank = rank(row)
        if new_rank >= old_rank:
            out[key] = int(idx)
    return out

def _copy_business_values(
    src_row: Any,
    src_df: Any,
    dst_df: Any,
    dst_idx: int,
) -> None:
    aliases = {
        "O que é (Tipo)": ["O que é (Tipo)", "Tipo", "Product Type"],
        "Alimentação": ["Alimentação"],
        "Category General": ["Category General"],
        "FAMILY": ["FAMILY", "Family"],
        "Título Real": ["Título Real"],
        "Título Referência": ["Título Referência"],
        "Próxima Ação": ["Próxima Ação"],
        "Fonte URL": ["Fonte URL"],
        "Fonte Referência URL": ["Fonte Referência URL"],
        "Collection State": ["Collection State"],
        "Raw Search Results": ["Raw Search Results"],
        "Curated Sources": ["Curated Sources"],
        "Title Rescue Sources": ["Title Rescue Sources"],
        "Status": ["Status"],
        "Status Processamento": ["Status Processamento"],
        "Revisão": ["Revisão", "Revisao"],
        # Revisão B.I é exclusivamente humana e nunca passa pela cópia automática.
        "Resolution State": ["Resolution State"],
        "Review Required": ["Review Required"],
        "Reason Code": ["Reason Code"],
        "Motivo": ["Motivo"],
        "Stage": ["Stage"],
        "Rules Version": ["Rules Version"],
        "Rules Hash": ["Rules Hash"],
        "Pipeline Version": ["Pipeline Version"],
        "Processed At": ["Processed At"],
        "Clef Provider": ["Clef Provider"],
        "Clef Model": ["Clef Model"],
    }

    for dst_col, names in aliases.items():
        if dst_col not in dst_df.columns:
            continue
        src_col = find_col(src_df, names, required=False)
        if src_col and safe(src_row.get(src_col)):
            dst_df.at[dst_idx, dst_col] = src_row.get(src_col)


def _copy_bi_locked_row_exact(
    src_row: Any,
    src_df: Any,
    dst_df: Any,
    dst_idx: int,
) -> None:
    """Copia a linha aprovada pelo B.I sem preencher vazios nem recalcular campos."""
    aliases = {
        "O que é (Tipo)": ["O que é (Tipo)", "Tipo", "Product Type"],
        "Alimentação": ["Alimentação"],
        "Category General": ["Category General"],
        "FAMILY": ["FAMILY", "Family"],
        "Título Real": ["Título Real"],
        "Status": ["Status"],
        "Revisão": ["Revisão", "Revisao"],
        "Revisão B.I": ["Revisão B.I", "Revisao B.I"],
    }
    for dst_col, names in aliases.items():
        if dst_col not in dst_df.columns:
            continue
        src_col = find_col(src_df, names, required=False)
        if src_col:
            dst_df.at[dst_idx, dst_col] = src_row.get(src_col)


def _load_bi_lock_snapshot(final_path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    """Snapshot textual de todas as 10 colunas da Base Final com Revisão B.I=OK."""
    if not final_path or not final_path.exists():
        return {}
    try:
        df, _ = _read_business_sheet(final_path)
    except Exception as exc:
        LOG.warning("Snapshot HARD LOCK não pôde ser lido: %s", exc)
        return {}
    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    rbi = find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)
    if not rbi:
        return {}
    aliases = {
        "O que é (Tipo)": ["O que é (Tipo)", "Tipo", "Product Type"],
        "Alimentação": ["Alimentação"],
        "Category General": ["Category General"],
        "FAMILY": ["FAMILY", "Family"],
        "Título Real": ["Título Real"],
        "Status": ["Status"],
        "Revisão": ["Revisão", "Revisao"],
        "Revisão B.I": ["Revisão B.I", "Revisao B.I"],
    }
    out: Dict[str, Dict[str, str]] = {}
    for _, row in df.iterrows():
        if not _is_bi_ok_value(row.get(rbi)):
            continue
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        snap = {"Marca": brand, "Modelo": model}
        for dst, names in aliases.items():
            col = find_col(df, names, required=False)
            snap[dst] = safe(row.get(col)) if col else ""
        out[business_key(brand, model)] = snap
    return out


def _assert_bi_lock_snapshot(df: Any, snapshot: Dict[str, Dict[str, str]], label: str) -> None:
    """Anti-regressão física: aborta a gravação se qualquer campo aprovado mudou."""
    if not snapshot:
        return
    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    aliases = {
        "O que é (Tipo)": ["O que é (Tipo)", "Tipo", "Product Type"],
        "Alimentação": ["Alimentação"],
        "Category General": ["Category General"],
        "FAMILY": ["FAMILY", "Family"],
        "Título Real": ["Título Real"],
        "Status": ["Status"],
        "Revisão": ["Revisão", "Revisao"],
        "Revisão B.I": ["Revisão B.I", "Revisao B.I"],
    }
    by_key = {}
    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if brand and model:
            key = business_key(brand, model)
            existing = by_key.get(key)
            if existing is None or (_row_bi_locked(row, df) and not _row_bi_locked(existing, df)):
                by_key[key] = row
    errors = []
    for key, expected in snapshot.items():
        row = by_key.get(key)
        if row is None:
            errors.append(f"{key}: linha desapareceu")
            continue
        for field, names in aliases.items():
            col = find_col(df, names, required=False)
            actual = safe(row.get(col)) if col else ""
            if actual != expected.get(field, ""):
                errors.append(f"{key}:{field} '{expected.get(field, '')}' -> '{actual}'")
        if len(errors) >= 20:
            break
    if errors:
        raise RuntimeError(
            "BI_OK_HARD_LOCK_VIOLATION em " + label + ": " + " | ".join(errors)
        )


def _load_review_bi_snapshot(
    final_path: Optional[Path],
) -> Tuple[Dict[str, Any], bool]:
    """Captura Revisão B.I exatamente como está na Base Final, inclusive vazios."""
    if not final_path or not final_path.exists():
        return {}, False
    try:
        df, _ = _read_business_sheet(final_path)
    except Exception as exc:
        LOG.warning("Snapshot Revisão B.I não pôde ser lido: %s", exc)
        return {}, False
    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    rb = find_col(df, ["Revisão B.I", "Revisao B.I"], required=False)
    snapshot: Dict[str, Any] = {}
    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        value = row.get(rb) if rb else None
        if key not in snapshot or _is_bi_ok_value(value):
            snapshot[key] = value
    return snapshot, True


def _apply_review_bi_snapshot(
    df: Any,
    brand_col: str,
    model_col: str,
    snapshot: Dict[str, Any],
    source_exists: bool,
    bi_lock_snapshot: Optional[Dict[str, Dict[str, str]]] = None,
) -> Any:
    """Reaplica B.I=OK somente ao par original aprovado na Base Final."""
    import pandas as pd
    if "Revisão B.I" not in df.columns:
        df["Revisão B.I"] = pd.NA
    if not source_exists:
        return df
    # Nunca replica OK para todas as linhas que compartilham chave normalizada.
    # Primeiro posiciona cada aprovação no par exato Marca+Modelo da Base Final.
    locks = bi_lock_snapshot or {}
    df["Revisão B.I"] = pd.NA
    for idx, row in df.iterrows():
        brand, model = safe(row.get(brand_col)), safe(row.get(model_col))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        approved = locks.get(key)
        if approved:
            if (brand, model) == (approved["Marca"], approved["Modelo"]):
                df.at[idx, "Revisão B.I"] = approved["Revisão B.I"]
        elif key in snapshot:
            df.at[idx, "Revisão B.I"] = snapshot[key]
    return df


def _sanitize_legacy_unresolved_taxonomy(df: Any) -> Tuple[Any, int]:
    """Limpa taxonomia residual de Script não resolvido; HARD LOCK fica intocado."""
    import pandas as pd
    out = df.copy()
    status_col = find_col(out, ["Status"], required=False)
    rbi_col = find_col(out, ["Revisão B.I", "Revisao B.I"], required=False)
    type_col = find_col(out, ["O que é (Tipo)", "Tipo", "Product Type"], required=False)
    cg_col = find_col(out, ["Category General"], required=False)
    fam_col = find_col(out, ["FAMILY", "Family"], required=False)
    if not cg_col or not fam_col:
        return out, 0
    # Candidate columns carry strings even when an entire column starts empty.
    # pandas 2.x otherwise infers float64 for an all-NA column and raises
    # TypeError when a taxonomy label is first written.
    for suggestion_col in ("Sugestão Category General", "Sugestão FAMILY"):
        if suggestion_col not in out.columns:
            out[suggestion_col] = pd.Series(pd.NA, index=out.index, dtype="object")
        else:
            out[suggestion_col] = out[suggestion_col].astype("object")
    cleaned = 0
    for idx, row in out.iterrows():
        if rbi_col and _is_bi_ok_value(row.get(rbi_col)):
            continue
        if status_col and _is_manual_status(row.get(status_col)):
            continue
        typ = safe(row.get(type_col)).upper() if type_col else ""
        cg = safe(row.get(cg_col)).upper()
        fam = safe(row.get(fam_col)).upper()
        if (cg, fam) != ("OTHERS", "OTHERS") and typ not in {"REVISAR", "UNKNOWN"}:
            continue
        if _baseline_row_accepted(row, out):
            continue
        if safe(row.get(cg_col)) or safe(row.get(fam_col)):
            # Nunca descartar candidatos: retirar apenas dos campos aprováveis.
            for source_col, suggestion_col in ((cg_col, "Sugestão Category General"),
                                               (fam_col, "Sugestão FAMILY")):
                if safe(row.get(source_col)) and not safe(row.get(suggestion_col)):
                    out.at[idx, suggestion_col] = row.get(source_col)
            out.at[idx, cg_col] = pd.NA
            out.at[idx, fam_col] = pd.NA
            cleaned += 1
    return out, cleaned

def _backup_baseline_files(
    master_path: Optional[Path],
    final_path: Optional[Path],
    backup_root: Path,
) -> Optional[Path]:
    """Cria backup antes de qualquer regravação dos Excel."""
    from datetime import datetime
    import shutil
    existing=[p for p in (master_path,final_path) if p and p.exists()]
    if not existing:
        return None
    folder=backup_root/datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True,exist_ok=False)
    for p in existing:
        shutil.copy2(p,folder/p.name)
    (folder/'manifest.json').write_text(json.dumps({
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'files':[str(p.resolve()) for p in existing],
        'reason':'automatic_before_baseline_write',
    },ensure_ascii=False,indent=2),encoding='utf-8')
    return folder


def _audit_baseline_conflicts(
    final_path: Optional[Path],
    report_path: Path,
) -> Dict[str, int]:
    """Gera CSV de conflitos objetivos sem modificar Excel e sem usar Clef."""
    import csv
    stats={'audit_rows':0,'review_bi_conflict':0,'others_others':0}
    if not final_path or not final_path.exists():
        return stats
    try:
        df,_=_read_business_sheet(final_path)
    except Exception as exc:
        LOG.warning("Auditoria baseline não pôde ler Base Final: %s",exc)
        return stats
    b=find_col(df,["Marca","Fabricante","Brand","Manufacturer"])
    m=find_col(df,["Modelo","Model","SKU","Part Number"])
    typc=find_col(df,["O que é (Tipo)","Tipo","Product Type"],required=False)
    pc=find_col(df,["Alimentação"],required=False)
    cgc=find_col(df,["Category General"],required=False)
    fc=find_col(df,["FAMILY","Family"],required=False)
    revc=find_col(df,["Revisão","Revisao"],required=False)
    rbic=find_col(df,["Revisão B.I","Revisao B.I"],required=False)
    statusc=find_col(df,["Status"],required=False)
    findings=[]
    for _,row in df.iterrows():
        brand,model=safe(row.get(b)),safe(row.get(m))
        if not brand or not model:
            continue
        typ=safe(row.get(typc)) if typc else ''
        power=safe(row.get(pc)) if pc else ''
        cg=safe(row.get(cgc)) if cgc else ''
        fam=safe(row.get(fc)) if fc else ''
        rev=safe(row.get(revc)).upper() if revc else ''
        rbi=safe(row.get(rbic)) if rbic else ''
        status=safe(row.get(statusc)) if statusc else ''
        issues=[]
        bi_ok = _is_bi_ok_value(rbi)
        # Revisão B.I=OK é autoridade humana final. Estados técnicos antigos
        # (REVISAR/OTHERS|OTHERS) não são tratados como conflito nem saneados.
        if (not bi_ok) and cg.upper()=='OTHERS' and fam.upper()=='OTHERS':
            issues.append('OTHERS_OTHERS_LEGACY'); stats['others_others'] += 1
        if rbi and (not bi_ok) and rev in _REVIEW_REJECT_STATES:
            issues.append('REVISAO_BI_CONFLITA_COM_REVIEW_ATUAL'); stats['review_bi_conflict'] += 1
        if issues:
            findings.append([brand,model,typ,power,cg,fam,status,rev,rbi,' | '.join(issues)])
    stats['audit_rows']=len(findings)
    if findings:
        report_path.parent.mkdir(parents=True,exist_ok=True)
        with report_path.open('w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh,delimiter=';')
            w.writerow(['Marca','Modelo','O que é (Tipo)','Alimentação','Category General','FAMILY','Status','Revisão','Revisão B.I','Diagnóstico'])
            w.writerows(findings)
    return stats


def _sync_final_into_master(
    master_df: Any,
    final_path: Optional[Path],
    brand_col: str,
    model_col: str,
) -> Tuple[Any, Dict[str, int]]:
    """Base Final é autoridade humana; BI_OK é copiado exatamente e congelado."""
    import pandas as pd

    stats = {
        "final_manual_promoted": 0,
        "final_bi_lock_promoted": 0,
        "final_baseline_preserved": 0,
        "final_rows_imported": 0,
    }
    if not final_path or not final_path.exists():
        return master_df, stats

    try:
        final_df, _ = _read_business_sheet(final_path)
    except Exception as exc:
        LOG.warning("Base Final não pôde ser sincronizada: %s", exc)
        return master_df, stats

    fb = find_col(final_df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    fm = find_col(final_df, ["Modelo", "Model", "SKU", "Part Number"])
    fs = find_col(final_df, ["Status"], required=False)
    frbi = find_col(final_df, ["Revisão B.I", "Revisao B.I"], required=False)
    ms = find_col(master_df, ["Status"], required=False)
    key_index = _master_key_index(master_df, brand_col, model_col)

    for _, src in final_df.iterrows():
        brand, model = safe(src.get(fb)), safe(src.get(fm))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        src_bi_ok = bool(frbi and _is_bi_ok_value(src.get(frbi)))
        src_manual = bool(fs and _is_manual_status(src.get(fs)))
        src_accepted = _baseline_row_accepted(src, final_df)

        if key in key_index:
            idx = key_index[key]

            # O registro B.I=OK deve ocupar exatamente o par original aprovado,
            # mesmo que outra grafia compartilhe a business_key.
            if src_bi_ok and (safe(master_df.at[idx, brand_col]), safe(master_df.at[idx, model_col])) != (brand, model):
                exact = [
                    i for i in master_df.index
                    if safe(master_df.at[i, brand_col]) == brand
                    and safe(master_df.at[i, model_col]) == model
                ]
                if exact:
                    idx = exact[-1]
                else:
                    new_row = {col: pd.NA for col in master_df.columns}
                    new_row[brand_col] = brand
                    new_row[model_col] = model
                    idx = len(master_df)
                    master_df.loc[idx] = new_row
                    stats["final_rows_imported"] += 1
                key_index[key] = idx

            # BI_OK é absoluto: a Base Final vence o Consolidado inclusive nos vazios.
            if src_bi_ok:
                _copy_bi_locked_row_exact(src, final_df, master_df, idx)
                stats["final_bi_lock_promoted"] += 1
                continue

            # Uma variante não aprovada nunca rebaixa campos da linha B.I.
            master_bi = find_col(master_df, ["Revisão B.I", "Revisao B.I"], required=False)
            if master_bi and _is_bi_ok_value(master_df.at[idx, master_bi]):
                continue
            if frbi:
                master_df.at[idx, "Revisão B.I"] = src.get(frbi)

            master_manual = bool(ms and _is_manual_status(master_df.at[idx, ms]))
            if master_manual:
                continue

            if src_manual or src_accepted:
                _copy_business_values(src, final_df, master_df, idx)
                if src_manual:
                    master_df.at[idx, "Status"] = "Manual"
                    stats["final_manual_promoted"] += 1
                else:
                    # A Base Final e autoridade para uma classificacao tecnicamente
                    # aceita. Um estado antigo de REVIEW/PENDING no Consolidado
                    # nao pode rebaixar um registro aceito durante a reconciliacao.
                    # src_accepted ja passou pelo gate de taxonomia completa,
                    # excluindo os casos sem classificacao/hard locks humanos.
                    master_df.at[idx, "Resolution State"] = "RESOLVED"
                    master_df.at[idx, "Status Processamento"] = "PROCESSADO"
                    master_df.at[idx, "Review Required"] = "NÃO"
                    stats["final_baseline_preserved"] += 1
            continue

        row = {col: pd.NA for col in master_df.columns}
        row[brand_col] = brand
        row[model_col] = model
        idx = len(master_df)
        master_df.loc[idx] = row
        key_index[key] = idx
        if src_bi_ok:
            _copy_bi_locked_row_exact(src, final_df, master_df, idx)
            stats["final_bi_lock_promoted"] += 1
        else:
            _copy_business_values(src, final_df, master_df, idx)
            if frbi:
                master_df.at[idx, "Revisão B.I"] = src.get(frbi)
            if src_accepted and not src_manual:
                master_df.at[idx, "Resolution State"] = "RESOLVED"
                master_df.at[idx, "Status Processamento"] = "PROCESSADO"
                master_df.at[idx, "Review Required"] = "NÃO"
        stats["final_rows_imported"] += 1

    return master_df, stats

def _record_is_accepted(rec: Dict[str, Any]) -> bool:
    if safe(rec.get("Resolution State")).upper() != "RESOLVED":
        return False
    if safe(rec.get("Review Required")).upper() == "SIM":
        return False
    typ = safe(rec.get("O que é (Tipo)")).upper()
    cg = safe(rec.get("Category General")).upper()
    fam = safe(rec.get("FAMILY")).upper()
    if not typ or typ in {"REVISAR", "UNKNOWN"}:
        return False
    if not cg or not fam or (cg, fam) == ("OTHERS", "OTHERS"):
        return False
    identity = safe(rec.get("Classification Identity")).upper()
    if identity and identity != "STRONG":
        return False
    return True


def _record_process_state(rec: Dict[str, Any]) -> Tuple[str, str]:
    if _record_is_accepted(rec):
        return "PROCESSADO", "AUTO_APROVADO"

    resolution = safe(rec.get("Resolution State")).upper()
    if resolution == "CLASSIFIED_CLEF":
        # Classificação completa produzida pelo Clef; falta apenas double-check humano.
        return "PROCESSADO", "PROVÁVEL"
    if resolution == "PENDING_COLLECTION":
        return resolution, "PENDENTE_COLETA"
    if resolution in {"PENDING_RECOLLECTION", "NEEDS_RECOLLECTION",
                      "CACHE_ERROR", "CACHE_COLLISION", "REVIEW_NO_RESULTS",
                      "REVIEW_CACHE_EVIDENCE"}:
        return "REVISAR", "REVISAR"

    return "REVISAR", "REVISAR"


def _upsert_run_rows_into_master(
    master_df: Any,
    rows: List[Dict[str, Any]],
    brand_col: str,
    model_col: str,
    rules: Rules,
    reprocess: bool,
    force_reprocess_keys: Optional[set] = None,
) -> Tuple[Any, Dict[str, int]]:
    import pandas as pd

    stats = {
        "baseline_promoted": 0,
        "review_recorded": 0,
        "manual_lock_preserved": 0,
        "bi_ok_lock_preserved": 0,
        "baseline_downgrade_blocked": 0,
    }

    force_reprocess_keys = set(force_reprocess_keys or set())
    key_index = _master_key_index(master_df, brand_col, model_col)
    status_col = find_col(master_df, ["Status"], required=False)
    rbi_col = find_col(master_df, ["Revisão B.I", "Revisao B.I"], required=False)

    for rec in rows:
        brand, model = safe(rec.get("Marca")), safe(rec.get("Modelo"))
        if not brand or not model:
            continue
        key = business_key(brand, model)

        if key in key_index:
            idx = key_index[key]
            # Regra R6: --reprocess NUNCA vence aprovação humana B.I.
            if rbi_col and _is_bi_ok_value(master_df.at[idx, rbi_col]):
                stats["bi_ok_lock_preserved"] += 1
                continue
            if status_col and _is_manual_status(master_df.at[idx, status_col]):
                stats["manual_lock_preserved"] += 1
                continue

            existing_accepted = _baseline_row_accepted(master_df.loc[idx], master_df)
            new_accepted = _record_is_accepted(rec)
            if existing_accepted and not new_accepted and key not in force_reprocess_keys:
                stats["baseline_downgrade_blocked"] += 1
                continue
        else:
            row = {col: pd.NA for col in master_df.columns}
            row[brand_col] = brand
            row[model_col] = model
            idx = len(master_df)
            master_df.loc[idx] = row
            key_index[key] = idx

        proc_state, review_state = _record_process_state(rec)

        for col, value in rec.items():
            if col in {"Marca", "Modelo", "Revisão B.I", "Revisao B.I"}:
                continue
            if col not in master_df.columns:
                master_df[col] = pd.NA
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            master_df.at[idx, col] = value

        master_df.at[idx, "Status"] = master_df.at[idx, "Status"] if safe(master_df.at[idx, "Status"]) else "Script"
        master_df.at[idx, "Status Processamento"] = proc_state
        master_df.at[idx, "Revisão"] = review_state
        # Nunca escreve Revisão B.I. Novo/reprocessado permanece vazio até o double check.
        master_df.at[idx, "Rules Version"] = rules.version
        master_df.at[idx, "Rules Hash"] = rules.hash
        master_df.at[idx, "Pipeline Version"] = PIPELINE_VERSION

        if _record_is_accepted(rec):
            stats["baseline_promoted"] += 1
        else:
            stats["review_recorded"] += 1

    return master_df, stats

def _derive_final_review(row: Any, df: Any) -> Any:
    # HARD LOCK: não recalcula nem normaliza Revisão de uma linha B.I=OK.
    review_col = find_col(df, ["Revisão", "Revisao"], required=False)
    if _row_bi_locked(row, df):
        return row.get(review_col) if review_col else ""

    status_col = find_col(df, ["Status"], required=False)
    if status_col and _is_manual_status(row.get(status_col)):
        return "VALIDADO_MANUAL"
    if _baseline_row_accepted(row, df):
        value = safe(row.get(review_col)).upper() if review_col else ""
        return value if value in {"AUTO_APROVADO", "PROVÁVEL", "PROVAVEL"} else "AUTO_APROVADO"

    resolution_col = find_col(df, ["Resolution State"], required=False)
    resolution = safe(row.get(resolution_col)).upper() if resolution_col else ""
    if resolution == "CLASSIFIED_CLEF":
        return "PROVÁVEL"
    if resolution == "PENDING_EVIDENCE_CHECK":
        return "PENDENTE"
    if resolution == "PENDING_CLASSIFICATION":
        return "PENDENTE_CLASSIFICACAO"
    # Revisão indica próximo trabalho; não deve mascarar evidência RAW_OK.
    raw_state = safe(row.get("Raw State")).upper()
    collection_state = safe(row.get("Collection State")).upper()
    has_cache_raw = raw_state in {"RAW_OK", "RAW_OK_MERGED", "OK"} or collection_state in {
        "READY_FOR_CLASSIFICATION", "PARTIAL_TARGET_EVIDENCE", "TARGET_EVIDENCE",
        "LOW_TARGET_EVIDENCE", "NO_TARGET_EVIDENCE", "CACHE_RAW_SEM_CURADORIA",
    }
    if has_cache_raw:
        # RAW existente nunca solicita uma nova busca por falha de taxonomia.
        return ("REVISAR" if collection_state in {"NO_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE"}
                else "PENDENTE_CLASSIFICACAO")
    if raw_state == "RAW_EMPTY" or resolution == "REVIEW_NO_RESULTS":
        return "REVISAR"
    if resolution in {"PENDING_COLLECTION", "PENDING_RECOLLECTION",
                      "NEEDS_RECOLLECTION", "CACHE_ERROR", "CACHE_COLLISION"}:
        return "PENDENTE_COLETA" if raw_state == "RAW_MISSING" else "REVISAR"

    # Linha conhecida, mas ainda nunca enriquecida: fica PENDENTE, não REVISAR.
    type_col = find_col(df, ["O que é (Tipo)", "Tipo", "Product Type"], required=False)
    cg_col = find_col(df, ["Category General"], required=False)
    fam_col = find_col(df, ["FAMILY", "Family"], required=False)
    title_col = find_col(df, ["Título Real"], required=False)
    proc_col = find_col(df, ["Status Processamento"], required=False)
    has_any_business = any(
        safe(row.get(c)) for c in [type_col, cg_col, fam_col, title_col] if c
    )
    has_processing = bool(proc_col and safe(row.get(proc_col)))
    if not has_any_business and not has_processing:
        return "PENDENTE"
    return "REVISAR"

def _atomic_write_dataframe(df: Any, path: Path, sheet_name: str) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f".tmp_{os.getpid()}" + path.suffix)
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name[:31] or "Consolidado", index=False)
        os.replace(tmp, path)
    except PermissionError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise PermissionError(
            f"Feche '{path.name}' no Excel e rode novamente; "
            "o arquivo não foi substituído."
        ) from exc
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise



def _manual_review_bundle(raw: RawEvidence) -> Dict[str, str]:
    """Exporta fontes do cache para revisão humana, mesmo rejeitadas; não aprova AUTO."""
    payload = raw.payload or {}
    coll = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    audits = {_url_key(r.get("url", "")): r for r in (coll.get("source_audit") or [])
              if isinstance(r, dict) and _url_key(r.get("url", ""))}
    evidence = [r for r in (coll.get("evidence_cache") or []) if isinstance(r, dict)]
    evidence_by_url = {_url_key(r.get("url", "")): r for r in evidence if _url_key(r.get("url", ""))}
    rows = []
    source = results[:10] if results else evidence[:10]
    for n, item in enumerate(source, 1):
        url = safe(item.get("url"))
        ev = evidence_by_url.get(_url_key(url), item if not results else {})
        au = audits.get(_url_key(url), {})
        rows.append({
            "n": n,
            "title": (safe(item.get("title")) or safe(ev.get("title")))[:500],
            "url": url[:1200],
            "identity": safe(ev.get("identity") or au.get("identity_after")) or "NAO_VERIFICADA",
            "decision": safe(au.get("decision") or ev.get("decision")) or "NAO_AVALIADA",
            "tier": safe(ev.get("tier")) or "NAO_CURADA",
            "snippet": (safe(item.get("content")) or safe(ev.get("snippet")) or safe(ev.get("evidence_excerpt")))[:800],
        })
    hard_reject = {"REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"}
    rank = {"STRONG": 4, "PROBABLE": 3, "CONTEXTUAL": 2, "WEAK": 1}
    titled = [r for r in rows if r["title"]]
    titled.sort(key=lambda r: (
        r["decision"] not in hard_reject,
        rank.get(r["identity"], 0),
        r["tier"] == "PRIMARY",
        -r["n"],
    ), reverse=True)
    best = titled[0] if titled else {}
    summary = [
        f'[{r["n"]}] {r["title"] or "(sem titulo)"} | {r["identity"]} | {r["decision"]} | {r["url"]} | {r["snippet"]}'
        for r in rows
    ]
    return {
        "Título Referência": safe(best.get("title")),
        "Fonte Referência URL": safe(best.get("url")),
        "Título Referência Identity": safe(best.get("identity")),
        "Qualidade Referência": safe(best.get("decision")),
        "Fontes Pesquisa": json.dumps(rows, ensure_ascii=False)[:32000],
        "Resumo Evidências": "\n".join(summary)[:32000],
        "Fontes Encontradas": str(len(rows)),
    }


def _export_base_final(
    master_df: Any,
    final_path: Path,
    review_bi_snapshot: Optional[Dict[str, Any]] = None,
    review_bi_source_exists: bool = False,
    bi_lock_snapshot: Optional[Dict[str, Dict[str, str]]] = None,
    input_path: Optional[Path] = None,
    cache_path: Optional[Path] = None,
) -> int:
    """Reconstrói Base Final preservando BI_OK byte-semanticamente nos campos humanos."""
    import pandas as pd

    work, brand_col, model_col = _ensure_master_columns(master_df)
    valid = work[brand_col].map(lambda x: bool(safe(x))) & work[model_col].map(lambda x: bool(safe(x)))
    work = work.loc[valid].copy()
    work["__key"] = [business_key(safe(b), safe(m)) for b, m in zip(work[brand_col], work[model_col])]

    status_col = find_col(work, ["Status"], required=False)
    rbi_col = find_col(work, ["Revisão B.I", "Revisao B.I"], required=False)
    work["__bi_lock_rank"] = (
        work[rbi_col].map(lambda x: 1 if _is_bi_ok_value(x) else 0) if rbi_col else 0
    )
    work["__manual_rank"] = (
        work[status_col].map(lambda x: 1 if _is_manual_status(x) else 0) if status_col else 0
    )
    work["__accepted_rank"] = work.apply(lambda row: 1 if _baseline_row_accepted(row, work) else 0, axis=1)
    work["__row_rank"] = range(len(work))
    work = work.sort_values(
        ["__key", "__bi_lock_rank", "__manual_rank", "__accepted_rank", "__row_rank"]
    ).drop_duplicates("__key", keep="last")

    out = pd.DataFrame(index=work.index)
    out["Marca"] = work[brand_col]
    out["Modelo"] = work[model_col]
    for col in ["O que é (Tipo)", "Alimentação", "Category General", "FAMILY", "Título Real"]:
        src = find_col(work, [col, "Family"] if col == "FAMILY" else [col], required=False)
        out[col] = work[src] if src else pd.NA

    if review_bi_source_exists:
        review_bi_snapshot = review_bi_snapshot or {}
        out["Revisão B.I"] = [review_bi_snapshot.get(key, pd.NA) for key in work["__key"]]
    else:
        src_rbi = find_col(work, ["Revisão B.I", "Revisao B.I"], required=False)
        out["Revisão B.I"] = work[src_rbi] if src_rbi else pd.NA

    def final_status(row: Any) -> Any:
        if _row_bi_locked(row, work):
            return row.get(status_col) if status_col else ""
        if status_col and _is_manual_status(row.get(status_col)):
            return "Manual"
        return "Script"

    out["Status"] = work.apply(final_status, axis=1)
    out["Revisão"] = work.apply(lambda row: _derive_final_review(row, work), axis=1)

    # Estados ortogonais: metadados auditáveis, jamais aprovam uma taxonomia.
    def operational_status(row: Any) -> Dict[str, str]:
        raw_state = safe(row.get("Raw State")).upper()
        collection_state = safe(row.get("Collection State")).upper()
        resolution = safe(row.get("Resolution State")).upper()
        title = safe(row.get("Título Real")) or safe(row.get("Título Referência"))
        typ = safe(row.get("O que é (Tipo)")).upper()
        cg = safe(row.get("Category General"))
        family = safe(row.get("FAMILY"))
        suggestions = safe(row.get("Sugestão Category General")) or safe(row.get("Sugestão FAMILY"))
        identity = safe(row.get("Classification Identity")).upper()
        ref_identity = safe(row.get("Título Referência Identity")).upper()
        if raw_state in {"RAW_OK", "RAW_OK_MERGED", "OK"} or collection_state in {
            "TARGET_EVIDENCE", "READY_FOR_CLASSIFICATION", "PARTIAL_TARGET_EVIDENCE",
            "LOW_TARGET_EVIDENCE", "NO_TARGET_EVIDENCE", "CACHE_RAW_SEM_CURADORIA"
        }:
            collected = ("COLETADO_EVIDENCIA_PRONTA" if collection_state in {
                "READY_FOR_CLASSIFICATION", "TARGET_EVIDENCE"
            } else "COLETADO_EVIDENCIA_PARCIAL" if collection_state == "PARTIAL_TARGET_EVIDENCE"
            else "COLETADO_SEM_EVIDENCIA_ALVO" if collection_state in {
                "LOW_TARGET_EVIDENCE", "NO_TARGET_EVIDENCE"
            } else "COLETADO_RAW_DISPONIVEL")
        elif raw_state in {"RAW_EMPTY", "EMPTY"}:
            collected = "COLETA_VAZIA"
        elif raw_state in {"CACHE_ERROR", "CACHE_COLLISION", "RAW_CORRUPT"}:
            collected = "ERRO_CACHE"
        else:
            collected = "EVIDENCIA_EXISTENTE_SEM_ESTADO" if title else "COLETA_NAO_COMPROVADA"
        # STRONG da referência só comprova a referência, não a classificação.
        if ref_identity == "STRONG" or identity == "STRONG":
            identity_status = "MARCA_MODELO_CONFIRMADOS"
        elif ref_identity == "PROBABLE" or identity == "PROBABLE":
            identity_status = "IDENTIDADE_PROVAVEL"
        elif ref_identity in {"CONFLICT", "WEAK"} or identity in {"CONFLICT", "WEAK"}:
            identity_status = "IDENTIDADE_NAO_CONFIAVEL"
        else:
            identity_status = "IDENTIDADE_NAO_VERIFICADA" if title else "SEM_EVIDENCIA_IDENTIDADE"
        if typ not in {"", "REVISAR", "UNKNOWN", "N/A"} and cg and family:
            classification = "COMPLETA_NAO_HOMOLOGADA"
        elif (typ not in {"", "REVISAR", "UNKNOWN", "N/A"} or cg or family or suggestions):
            classification = "PARCIAL"
        elif title or raw_state in {"RAW_OK", "RAW_OK_MERGED"}:
            classification = "PENDENTE_CLASSIFICACAO"
        else:
            classification = "SEM_CLASSIFICACAO"
        if resolution == "RESOLVED" and safe(row.get("Review Required")).upper() != "SIM":
            classification = "RESOLVIDA_PELO_PIPELINE"
        return {"Status Coleta": collected, "Status Identidade": identity_status,
                "Status Classificação": classification}

    operational = work.apply(operational_status, axis=1)
    for name in ("Status Coleta", "Status Identidade", "Status Classificação"):
        out[name] = operational.map(lambda fields: fields[name])

    # HF3.1p2: schema-forward baseline export.
    # Diagnostic columns introduced after older baseline files were created may
    # legitimately be absent from ``work``. Copy them when available and
    # create any remaining final-schema column as NA before the strict slice.
    # This keeps old baselines forward-compatible and prevents KeyError when
    # BASELINE_FINAL_COLUMNS evolves.
    for col in ["Título Referência", "Próxima Ação", "Fonte URL",
                "Fonte Referência URL", "Motivo", "Reason Code",
                "Collection State", "Resolution State", "Classification Identity",
                "Título Referência Identity", "Sugestão Category General",
                "Sugestão FAMILY"]:
        src = find_col(work, [col], required=False)
        out[col] = work[src] if src else pd.NA

    for col in BASELINE_FINAL_COLUMNS:
        if col not in out.columns:
            src = find_col(work, [col], required=False)
            out[col] = work[src] if src else pd.NA

    out = out[BASELINE_FINAL_COLUMNS].reset_index(drop=True)
    # Quando colunas textuais estao completamente vazias, pandas pode
    # inferir float64. A reconciliacao cache->Excel precisa escrever strings
    # (status, titulos, URLs, fontes) sem coercao invalida em pandas recentes.
    # Estes campos do schema Final sao textuais, inclusive 'Fontes Encontradas'
    # que armazena a contagem como representacao de texto.
    out = out.astype("object")

    _assert_bi_lock_snapshot(out, bi_lock_snapshot or {}, "Base Final gerada")

    # Contrato de cobertura: cada par exato deve ser persistido, inclusive aliases
    # de execuções anteriores. Chave compacta só serve para buscar classificação.
    # O registro B.I. permanece byte-semanticamente protegido no par de origem.
    def present(value):
        if value is None:
            return False
        try:
            if bool(pd.isna(value)):
                return False
        except (TypeError, ValueError):
            pass
        return bool(safe(value))

    taxonomy = ["O que é (Tipo)", "Alimentação", "Category General", "FAMILY", "Título Real",
                "Título Referência", "Fonte URL", "Fonte Referência URL",
                "Motivo", "Reason Code", "Collection State", "Resolution State",
                "Classification Identity", "Título Referência Identity",
                "Sugestão Category General", "Sugestão FAMILY"]
    required = ["O que é (Tipo)", "Category General", "FAMILY"]
    prior_exact = {}
    if final_path.exists():
        prior_df, _ = _read_business_sheet(final_path)
        pb = find_col(prior_df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
        pm = find_col(prior_df, ["Modelo", "Model", "SKU", "Part Number"])
        for _, prev in prior_df.iterrows():
            pair = (safe(prev.get(pb)), safe(prev.get(pm)))
            if pair[0] and pair[1]:
                prior_exact[pair] = prev

    base = {}
    for _, existing in out.iterrows():
        pair = (safe(existing["Marca"]), safe(existing["Modelo"]))
        base[pair] = {col: existing.get(col, pd.NA) for col in BASELINE_FINAL_COLUMNS}

    def classified(rec):
        return (all(present(rec.get(col)) for col in required)
                and safe(rec.get("O que é (Tipo)")).upper() not in {"REVISAR", "UNKNOWN", "N/A"}
                and present(rec.get("Título Real")))

    # Escolhe evidência completa antes de linhas pendentes; B.I. tem autoridade,
    # mas nunca se converte numa aprovação humana para uma variante.
    by_key = {}
    for rec in base.values():
        key = business_key(safe(rec["Marca"]), safe(rec["Modelo"]))
        score = (int(classified(rec)), int(_is_bi_ok_value(rec.get("Revisão B.I"))),
                 int(safe(rec.get("Collection State")) in {
                     "TARGET_EVIDENCE", "READY_FOR_CLASSIFICATION",
                     "PARTIAL_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE",
                     "NO_TARGET_EVIDENCE", "CACHE_RAW_SEM_CURADORIA"
                 }),
                 sum(bool(present(rec.get(c))) for c in taxonomy))
        if key not in by_key or score > by_key[key][0]:
            by_key[key] = (score, rec)
    for pair, prev in prior_exact.items():
        key = business_key(*pair)
        rec = {col: prev.get(col, pd.NA) for col in BASELINE_FINAL_COLUMNS}
        score = (int(classified(rec)), int(_is_bi_ok_value(rec.get("Revisão B.I"))),
                 int(safe(rec.get("Collection State")) in {
                     "TARGET_EVIDENCE", "READY_FOR_CLASSIFICATION",
                     "PARTIAL_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE",
                     "NO_TARGET_EVIDENCE", "CACHE_RAW_SEM_CURADORIA"
                 }),
                 sum(bool(present(rec.get(c))) for c in taxonomy))
        if key not in by_key or score > by_key[key][0]:
            by_key[key] = (score, rec)

    requested = set(prior_exact)
    input_pairs = set()
    if input_path and input_path.exists():
        input_pairs = {
            (item.brand_original, item.model_original)
            for item in read_input_rows(input_path) if item.valid
        }
        requested.update(input_pairs)

    # Detecta conflitos reais de taxonomia em variantes da mesma chave.
    signatures = {}
    for rec in list(base.values()) + [
        {col: prev.get(col, pd.NA) for col in BASELINE_FINAL_COLUMNS}
        for prev in prior_exact.values()
    ]:
        if not classified(rec):
            continue
        key = business_key(safe(rec["Marca"]), safe(rec["Modelo"]))
        signature = tuple(safe(rec.get(col)).upper() for col in required)
        signatures.setdefault(key, set()).add(signature)
    conflict_keys = {key for key, values in signatures.items() if len(values) > 1}

    created = 0
    enriched = 0
    inherited = 0
    for pair in sorted(requested | set(base)):
        rec = base.get(pair)
        if rec is None:
            # Preserva exatamente os dados já disponíveis em outra variante
            # previamente exportada, inclusive correções manuais.
            prev = prior_exact.get(pair)
            rec = ({col: prev.get(col, pd.NA) for col in BASELINE_FINAL_COLUMNS}
                   if prev is not None else {col: pd.NA for col in BASELINE_FINAL_COLUMNS})
            rec["Marca"], rec["Modelo"] = pair
            base[pair] = rec
            created += 1
        if _is_bi_ok_value(rec.get("Revisão B.I")) or (present(rec.get("Status")) and safe(rec.get("Status")).upper() == "MANUAL"):
            continue
        src = by_key.get(business_key(*pair))
        if src is None:
            if not present(rec.get("Status")):
                rec["Status"] = "Script"
            if not present(rec.get("Revisão")):
                rec["Revisão"] = "PENDENTE"
            if not present(rec.get("Próxima Ação")):
                rec["Próxima Ação"] = "REVISAR_MANUAL_SEM_EVIDENCIA"
            continue
        source = src[1]
        # Uma variante não protegida não pode continuar PENDENTE_COLETA
        # quando sua business_key possui RAW confirmado no cache.
        src_collection = safe(source.get("Collection State"))
        if src_collection in {
            "TARGET_EVIDENCE", "READY_FOR_CLASSIFICATION",
            "PARTIAL_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE",
            "NO_TARGET_EVIDENCE", "CACHE_RAW_SEM_CURADORIA"
        }:
            rec["Collection State"] = src_collection
            rec["Status Coleta"] = source.get("Status Coleta")
            if safe(source.get("Status Identidade")):
                rec["Status Identidade"] = source["Status Identidade"]
            if (safe(rec.get("Revisão")).upper() in {
                "PENDENTE", "PENDENTE_COLETA", "PENDENTE_CLASSIFICACAO",
                "PENDENTE_RECOLETA"
            } and safe(source.get("Revisão")).upper() in {
                "PENDENTE_CLASSIFICACAO", "PENDENTE_RECOLETA"
            }):
                rec["Revisão"] = source["Revisão"]
                rec["Próxima Ação"] = source.get("Próxima Ação")
                rec["Resolution State"] = source.get("Resolution State")
        changed = False
        for col in taxonomy:
            if not present(rec.get(col)) and present(source.get(col)):
                rec[col] = source[col]
                changed = True
        if changed:
            enriched += 1
        if not present(rec.get("Status")):
            rec["Status"] = "Script"
        if classified(rec) and pair != (safe(source["Marca"]), safe(source["Modelo"])):
            key = business_key(*pair)
            # Herda a classificação validada para grafias equivalentes,
            # sem copiar o campo humano Revisão B.I.
            if (_is_bi_ok_value(source.get("Revisão B.I"))
                    and key not in conflict_keys
                    and tuple(safe(rec.get(col)).upper() for col in required)
                        == tuple(safe(source.get(col)).upper() for col in required)):
                rec["Revisão"] = "AUTO_APROVADO"
                rec["Próxima Ação"] = "CONCLUIDO_POR_EQUIVALENCIA_BI"
                inherited += 1
            elif key in conflict_keys:
                rec["Revisão"] = "PROVÁVEL"
                rec["Próxima Ação"] = "REVISAR_CONFLITO_EQUIVALENCIA"
            elif safe(source.get("Revisão")).upper() == "AUTO_APROVADO":
                rec["Revisão"] = "AUTO_APROVADO"
                rec["Próxima Ação"] = "CONCLUIDO_POR_EQUIVALENCIA_TECNICA"
            else:
                # Uma única revisão por business_key: a fonte canônica mantém
                # DOUBLE_CHECK; aliases não abrem novas tarefas de revisão.
                rec["Revisão"] = "PROVÁVEL"
                rec["Próxima Ação"] = "AGUARDAR_REVISAO_PRODUTO_CANONICO"
        elif not present(rec.get("Revisão")):
            rec["Revisão"] = "PENDENTE"
            rec["Próxima Ação"] = "REVISAR_MANUAL_SEM_CLASSIFICACAO"

    out = pd.DataFrame(base.values(), columns=BASELINE_FINAL_COLUMNS)
    # CONTRATO PROCV: toda ocorrência da entrada deve existir fisicamente na
    # aba Base Final. Deduplicação aplica-se SOMENTE a Search/classificação.
    # Reconstrução determinística: extras são recriados da entrada a cada run,
    # jamais acumulados a partir das duplicatas do Excel anterior.
    from collections import Counter
    input_rows_all = read_input_rows(input_path) if input_path and input_path.exists() else []
    input_counts = Counter((item.brand_original, item.model_original) for item in input_rows_all)
    output_counts = Counter((safe(b), safe(m)) for b, m in zip(out["Marca"], out["Modelo"]))
    by_exact_output = {(safe(row["Marca"]), safe(row["Modelo"])): row
                       for _, row in out.iterrows()}
    supplemental = []
    for pair, required_count in input_counts.items():
        missing_occurrences = max(0, required_count - output_counts.get(pair, 0))
        if not missing_occurrences:
            continue
        exemplar = by_exact_output.get(pair)
        for _ in range(missing_occurrences):
            if exemplar is not None:
                copy = exemplar.copy().to_dict()
            else:
                copy = {col: pd.NA for col in BASELINE_FINAL_COLUMNS}
                copy["Marca"], copy["Modelo"] = pair
                copy["Status"] = "Script"
                copy["Revisão"] = "PENDENTE"
                copy["Próxima Ação"] = "CORRIGIR_CADASTRO_SEM_IDENTIDADE"
            supplemental.append(copy)
    if supplemental:
        out = pd.concat([out, pd.DataFrame(supplemental, columns=BASELINE_FINAL_COLUMNS)], ignore_index=True)
    # A reconstrução acima (pd.DataFrame(base.values())) e a concatenação podem
    # reinferir colunas inteiramente vazias como float64. O dtype deve ser
    # garantido neste DataFrame FINAL, imediatamente antes das atribuições
    # de hipóteses textuais em CLASSIFICATION INTEGRITY.
    # Preservar strings apos concat de linhas adicionais da entrada.
    out = out.astype("object")
    physical_counts = Counter((safe(b), safe(m)) for b, m in zip(out["Marca"], out["Modelo"]))
    missing_lines = {pair: n - physical_counts.get(pair, 0) for pair, n in input_counts.items()
                     if physical_counts.get(pair, 0) < n}
    if missing_lines:
        raise RuntimeError(f"BASE_FINAL_INPUT_ROW_COVERAGE_FAILED: {len(missing_lines)} pares sem todas as ocorrencias")
    LOG.info("BASE FINAL INPUT ROW COVERAGE OK | input_rows=%d | extra_rows=%d | missing=0",
             len(input_rows_all), len(supplemental))
    # Corrige estados falsamente aprovados em linhas não protegidas por revisão humana.
    # Nunca altera classificações manuais ou Revisão B.I=OK.
    repaired_approval = 0
    stripped_partial = 0
    for idx, row in out.iterrows():
        if _is_bi_ok_value(row.get("Revisão B.I")) or safe(row.get("Status")).upper() == "MANUAL":
            continue
        complete = classified(row)
        approved = safe(row.get("Revisão")).upper() == "AUTO_APROVADO"
        if approved and not complete:
            out.at[idx, "Revisão"] = "REVISAR"
            out.at[idx, "Próxima Ação"] = "REVISAR_CLASSIFICACAO_INCOMPLETA"
            repaired_approval += 1
        has_pair = present(row.get("Category General")) or present(row.get("FAMILY"))
        has_type = present(row.get("O que é (Tipo)")) and safe(row.get("O que é (Tipo)")).upper() not in {"REVISAR", "UNKNOWN"}
        if has_pair and not has_type:
            # Preserva a hipótese para revisão, sem a tornar classificação.
            for original, candidate in (("Category General", "Sugestão Category General"),
                                        ("FAMILY", "Sugestão FAMILY")):
                if present(row.get(original)) and not present(row.get(candidate)):
                    out.at[idx, candidate] = row.get(original)
                out.at[idx, original] = pd.NA
            out.at[idx, "Status Classificação"] = "PARCIAL"
            stripped_partial += 1
    LOG.info("BASE FINAL CLASSIFICATION INTEGRITY | false_approvals_repaired=%d | partial_pairs_cleared=%d",
             repaired_approval, stripped_partial)
    # Executa o hard-lock também sobre a base final reconstruída.
    _assert_bi_lock_snapshot(out, bi_lock_snapshot or {}, "Base Final com variantes")
    exported_pairs = {(safe(b), safe(m)) for b, m in zip(out["Marca"], out["Modelo"])}
    missing = input_pairs - exported_pairs
    if missing:
        raise RuntimeError(f"BASE_FINAL_INPUT_COVERAGE_FAILED: {len(missing)} pares ausentes")
    # Duplicatas exatas da entrada são intencionais para PROCV; o contrato
    # de multiplicidade acima substitui a antiga proibição global.
    LOG.info(
        "BASE FINAL PROCV COVERAGE OK | input_pairs=%d | prior_pairs=%d | "
        "aliases_added=%d | variants_enriched=%d | missing=0",
        len(input_pairs), len(prior_exact), created, enriched,
    )
    LOG.info("BASE FINAL EQUIVALENCIA BI | herdados=%d | conflitos=%d", inherited, len(conflict_keys))

    # Espelha o SQLite em todos os produtos não aceitos da Base Final,
    # inclusive aliases que não receberam os campos do Consolidado.
    if cache_path and cache_path.exists():
        store = TavilyReadOnlyStore(cache_path)
        try:
            for idx, rec in out.iterrows():
                brand, model = safe(rec.get("Marca")), safe(rec.get("Modelo"))
                if (_is_bi_ok_value(rec.get("Revisão B.I"))
                        or safe(rec.get("Status")).upper() == "MANUAL"
                        or _baseline_row_accepted(rec, out)):
                    continue
                raw = store.get(brand, model)
                if raw.state == "RAW_EMPTY":
                    out.at[idx, "Status Coleta"] = "COLETA_VAZIA"
                    out.at[idx, "Collection State"] = "RAW_EMPTY"
                    if not classified(rec):
                        out.at[idx, "Revisão"] = "REVISAR"
                        out.at[idx, "Próxima Ação"] = "REVISAR_SEM_RESULTADOS"
                        out.at[idx, "Resolution State"] = "REVIEW_NO_RESULTS"
                elif raw.state in {"RAW_OK", "RAW_OK_MERGED"}:
                    collection = (raw.payload or {}).get("_collection")
                    collection = collection if isinstance(collection, dict) else {}
                    state = safe(collection.get("state")) or "CACHE_RAW_SEM_CURADORIA"
                    out.at[idx, "Collection State"] = state
                    if state in {"READY_FOR_CLASSIFICATION", "TARGET_EVIDENCE"}:
                        out.at[idx, "Status Coleta"] = "COLETADO_EVIDENCIA_PRONTA"
                    elif state == "PARTIAL_TARGET_EVIDENCE":
                        out.at[idx, "Status Coleta"] = "COLETADO_EVIDENCIA_PARCIAL"
                    elif state in {"LOW_TARGET_EVIDENCE", "NO_TARGET_EVIDENCE"}:
                        out.at[idx, "Status Coleta"] = "COLETADO_SEM_EVIDENCIA_ALVO"
                    else:
                        out.at[idx, "Status Coleta"] = "COLETADO_RAW_DISPONIVEL"
                    # Materializa todas as fontes consultadas, inclusive rejeitadas:
                    # exibição de evidência não equivale a aprovação taxonômica.
                    bundle = _manual_review_bundle(raw)
                    for field in ("Fontes Pesquisa", "Resumo Evidências",
                                  "Fontes Encontradas", "Qualidade Referência"):
                        if bundle.get(field):
                            out.at[idx, field] = bundle[field]
                    for field in ("Título Referência", "Fonte Referência URL",
                                  "Título Referência Identity"):
                        if not safe(rec.get(field)) and bundle.get(field):
                            out.at[idx, field] = bundle[field]
                    # Título Real só recebe referência com identidade comprovada.
                    # Outros títulos permanecem visíveis em Título Referência/Fontes.
                    identity = safe(bundle.get("Título Referência Identity")).upper()
                    decision = safe(bundle.get("Qualidade Referência")).upper()
                    if (not safe(rec.get("Título Real")) and identity == "STRONG"
                            and not decision.startswith("REJECT")
                            and safe(bundle.get("Título Referência"))):
                        out.at[idx, "Título Real"] = bundle["Título Referência"]
                        if not safe(rec.get("Fonte URL")):
                            out.at[idx, "Fonte URL"] = bundle["Fonte Referência URL"]
                    if not classified(rec):
                        requires_review = state in {"LOW_TARGET_EVIDENCE", "NO_TARGET_EVIDENCE"}
                        out.at[idx, "Revisão"] = "REVISAR" if requires_review else "PENDENTE_CLASSIFICACAO"
                        out.at[idx, "Próxima Ação"] = "REVISAR_EVIDENCIA_CACHE" if requires_review else "CLASSIFICAR_CACHE"
                        out.at[idx, "Resolution State"] = "REVIEW_CACHE_EVIDENCE" if requires_review else "PENDING_CLASSIFICATION"
        finally:
            store.close()

    # Métricas de cobertura manual: observam somente registros não aceitos.
    pending_for_review = out.loc[
        ~out.apply(lambda r: _baseline_row_accepted(r, out), axis=1)
    ]
    manual_reference_titles = sum(
        bool(safe(r.get("Título Real")) or safe(r.get("Título Referência")))
        for _, r in pending_for_review.iterrows()
    )
    manual_sources = sum(
        bool(safe(r.get("Fontes Pesquisa")))
        for _, r in pending_for_review.iterrows()
    )
    LOG.info(
        "BASE FINAL MANUAL EVIDENCE | pendentes=%d | com_titulo=%d | com_fontes=%d | sem_titulo=%d",
        len(pending_for_review), manual_reference_titles, manual_sources,
        len(pending_for_review) - manual_reference_titles,
    )

    # GATE pré-gravação: presença no Excel não comprova estado de coleta.
    # O SQLite é a fonte oficial e RAW_OK nunca pode sair PENDENTE_COLETA.
    if cache_path and cache_path.exists() and input_rows_all:
        store = TavilyReadOnlyStore(cache_path)
        try:
            status_index = {}
            for _, rec in out.iterrows():
                pair = (safe(rec.get("Marca")), safe(rec.get("Modelo")))
                status_index.setdefault(pair, []).append(rec)
            expected = {"raw_ok": 0, "raw_empty": 0, "skipped_human": 0}
            conflicts = []
            for item in input_rows_all:
                if not item.valid:
                    continue
                records = status_index.get((item.brand_original, item.model_original), [])
                if not records:
                    conflicts.append((item.brand_original, item.model_original, "AUSENTE"))
                    continue
                raw = store.get(item.brand_original, item.model_original)
                if raw.state not in {"RAW_OK", "RAW_OK_MERGED", "RAW_EMPTY"}:
                    continue
                for record in records:
                    if _is_bi_ok_value(record.get("Revisão B.I")) or safe(record.get("Status")).upper() == "MANUAL":
                        expected["skipped_human"] += 1
                        continue
                    collection_status = safe(record.get("Status Coleta")).upper()
                    review_status = safe(record.get("Revisão")).upper()
                    if raw.state in {"RAW_OK", "RAW_OK_MERGED"}:
                        expected["raw_ok"] += 1
                        if not collection_status.startswith("COLETADO_") or review_status == "PENDENTE_COLETA":
                            conflicts.append((item.brand_original, item.model_original,
                                              f"RAW_OK x {collection_status} / {review_status}"))
                    else:
                        expected["raw_empty"] += 1
                        if collection_status != "COLETA_VAZIA":
                            conflicts.append((item.brand_original, item.model_original,
                                              f"RAW_EMPTY x {collection_status}"))
            LOG.info("BASE FINAL CACHE CONTRACT | raw_ok=%d | raw_empty=%d | human_skipped=%d | conflicts=%d",
                     expected["raw_ok"], expected["raw_empty"], expected["skipped_human"], len(conflicts))
            if conflicts:
                raise RuntimeError(
                    "BASE_FINAL_CACHE_CONTRACT_FAILED: "
                    f"{len(conflicts)} divergencias cache/Excel; primeiros={conflicts[:5]}. "
                    "Exportacao bloqueada para preservar baseline."
                )
        finally:
            store.close()

    tmp = final_path.with_name(final_path.stem + f".tmp_{os.getpid()}" + final_path.suffix)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
            out.to_excel(writer, sheet_name="Base Final", index=False)
        os.replace(tmp, final_path)
    except PermissionError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise PermissionError(
            f"Feche '{final_path.name}' no Excel e rode novamente; a Base Final não foi substituída."
        ) from exc
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return len(out)

def _register_input_keys_in_master(master_df: Any, input_path: Optional[Path]) -> Tuple[Any, int]:
    """Cadastro total da entrada; não atribui classificação sem evidência.

    Nunca altera registros existentes, inclusive B.I=OK e classificação manual.
    """
    import pandas as pd

    if not input_path or not input_path.exists():
        return master_df, 0
    master_df, brand_col, model_col = _ensure_master_columns(master_df)
    present = set(_master_key_index(master_df, brand_col, model_col))
    added = 0
    for item in read_input_rows(input_path):
        if not item.valid or item.key in present:
            continue
        row = {col: pd.NA for col in master_df.columns}
        row[brand_col] = item.brand_original
        row[model_col] = item.model_original
        row["Status"] = "Script"
        row["Status Processamento"] = "PENDING_EVIDENCE_CHECK"
        row["Revisão"] = "PENDENTE"
        row["Resolution State"] = "PENDING_EVIDENCE_CHECK"
        row["Review Required"] = "SIM"
        row["Próxima Ação"] = "VERIFICAR_CACHE_ANTES_DE_COLETAR"
        row["Reason Code"] = "INPUT_PENDING_EVIDENCE_CHECK"
        row["Motivo"] = "Registro da entrada ainda sem classificação validada."
        master_df.loc[len(master_df)] = row
        present.add(item.key)
        added += 1
    return master_df, added


def _hydrate_input_collection_from_cache(
    master_df: Any,
    input_path: Optional[Path],
    cache_path: Optional[Path],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """Reconcilia evidência do SQLite com os SKUs da entrada, sem classificar.

    Coleta != identidade != taxonomia. Nunca altera Gold, Manual ou itens aceitos.
    """
    stats = {"cache_keys": 0, "cache_raw_ok": 0, "cache_raw_empty": 0,
             "cache_pending_corrected": 0, "cache_reference_titles": 0}
    if not input_path or not input_path.exists() or not cache_path or not cache_path.exists():
        return stats
    bcol = find_col(master_df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    mcol = find_col(master_df, ["Modelo", "Model", "SKU", "Part Number"])
    key_index = _master_key_index(master_df, bcol, mcol)
    status_col = find_col(master_df, ["Status"], required=False)
    rbi_col = find_col(master_df, ["Revisão B.I", "Revisao B.I"], required=False)
    review_col = find_col(master_df, ["Revisão", "Revisao"], required=False) or "Revisão"
    needed = ("Raw State", "Collection State", "Título Referência",
              "Fonte Referência URL", "Título Referência Identity",
              "Título Referência Decision", "Status Processamento",
              "Próxima Ação", "Resolution State", review_col,
              "Fontes Pesquisa", "Resumo Evidências",
              "Fontes Encontradas", "Qualidade Referência",
              "Título Real", "Fonte URL")
    for col in needed:
        if col not in master_df.columns:
            master_df[col] = ""
        # Colunas antigas inteiramente vazias podem ter dtype float64 no pandas.
        master_df[col] = master_df[col].astype("object")

    store = TavilyReadOnlyStore(cache_path)
    try:
        seen = set()
        for item in read_input_rows(input_path):
            if not item.valid or item.key in seen:
                continue
            seen.add(item.key)
            idx = key_index.get(item.key)
            if idx is None:
                continue
            old = master_df.loc[idx]
            if ((rbi_col and _is_bi_ok_value(old.get(rbi_col)))
                    or (status_col and _is_manual_status(old.get(status_col)))
                    or _baseline_row_accepted(old, master_df)):
                continue
            raw = store.get(item.brand_original, item.model_original)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED", "RAW_EMPTY"}:
                continue
            stats["cache_keys"] += 1
            master_df.at[idx, "Raw State"] = raw.state
            if raw.state == "RAW_EMPTY":
                stats["cache_raw_empty"] += 1
                master_df.at[idx, "Collection State"] = "RAW_EMPTY"
                # Uma consulta vazia é diferente de produto nunca consultado.
                if safe(old.get("Resolution State")).upper() not in {"RESOLVED", "CLASSIFIED_CLEF"}:
                    master_df.at[idx, "Resolution State"] = "REVIEW_NO_RESULTS"
                    master_df.at[idx, "Status Processamento"] = "REVIEW_NO_RESULTS"
                    master_df.at[idx, "Próxima Ação"] = "REVISAR_SEM_RESULTADOS"
                    master_df.at[idx, review_col] = "REVISAR"
                continue
            stats["cache_raw_ok"] += 1
            coll = (raw.payload or {}).get("_collection")
            coll = coll if isinstance(coll, dict) else {}
            state = safe(coll.get("state")) or "CACHE_RAW_SEM_CURADORIA"
            master_df.at[idx, "Collection State"] = state
            old_review = safe(old.get(review_col)).upper()
            old_process = safe(old.get("Status Processamento")).upper()
            old_resolution = safe(old.get("Resolution State")).upper()
            pending = old_review in {"", "PENDENTE", "PENDENTE_COLETA", "PENDENTE_RECOLETA", "PENDENTE_CLASSIFICACAO"}
            pending = pending or old_process in {"PENDING_COLLECTION", "PENDING_RECOLLECTION", "PENDING_CLASSIFICATION"}
            pending = pending or old_resolution in {"", "PENDING_EVIDENCE_CHECK", "PENDING_COLLECTION", "PENDING_RECOLLECTION", "NEEDS_RECOLLECTION", "PENDING_CLASSIFICATION"}
            if pending and old_resolution not in {"CLASSIFIED_CLEF", "RESOLVED"}:
                if state in {"NO_TARGET_EVIDENCE", "LOW_TARGET_EVIDENCE"}:
                    master_df.at[idx, review_col] = "REVISAR"
                    master_df.at[idx, "Resolution State"] = "REVIEW_CACHE_EVIDENCE"
                    master_df.at[idx, "Status Processamento"] = "REVIEW_CACHE_EVIDENCE"
                    master_df.at[idx, "Próxima Ação"] = "REVISAR_EVIDENCIA_CACHE"
                else:
                    master_df.at[idx, review_col] = "PENDENTE_CLASSIFICACAO"
                    master_df.at[idx, "Resolution State"] = "PENDING_CLASSIFICATION"
                    master_df.at[idx, "Status Processamento"] = "PENDING_CLASSIFICATION"
                    master_df.at[idx, "Próxima Ação"] = "CLASSIFICAR_CACHE"
                stats["cache_pending_corrected"] += 1
            # Independente da classificação, todos os títulos e fontes
            # existentes no SQLite precisam ficar visíveis no Consolidado.
            bundle = _manual_review_bundle(raw)
            for field in ("Fontes Pesquisa", "Resumo Evidências",
                          "Fontes Encontradas", "Qualidade Referência"):
                if bundle.get(field):
                    master_df.at[idx, field] = bundle[field]
            for field in ("Título Referência", "Fonte Referência URL",
                          "Título Referência Identity"):
                if not safe(old.get(field)) and bundle.get(field):
                    master_df.at[idx, field] = bundle[field]
            title = safe(bundle.get("Título Referência"))
            identity = safe(bundle.get("Título Referência Identity")).upper()
            decision = safe(bundle.get("Qualidade Referência")).upper()
            if cfg is not None and not title:
                try:
                    title, url, identity, decision = choose_reference_title(
                        raw, item.brand_original, item.model_original, cfg
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    LOG.warning("CACHE TITLE REFERENCE SKIPPED | %s %s | %s",
                                item.brand_original, item.model_original, exc)
                    title = ""
                if title and not safe(old.get("Título Referência")):
                    master_df.at[idx, "Título Referência"] = title
                    master_df.at[idx, "Fonte Referência URL"] = url
                    master_df.at[idx, "Título Referência Identity"] = identity
            if title:
                stats["cache_reference_titles"] += 1
                if (identity == "STRONG" and not decision.startswith("REJECT")
                        and not safe(old.get("Título Real"))):
                    master_df.at[idx, "Título Real"] = title
                    if not safe(old.get("Fonte URL")):
                        master_df.at[idx, "Fonte URL"] = safe(
                            master_df.at[idx, "Fonte Referência URL"])
    finally:
        store.close()
    return stats


def persist_baseline_excels(
    rows: List[Dict[str, Any]],
    master_path: Optional[Path],
    final_path: Optional[Path],
    input_path: Optional[Path],
    rules: Rules,
    reprocess: bool = False,
    force_reprocess_keys: Optional[set] = None,
    cache_path: Optional[Path] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """Grava Consolidado -> Base Final sem jamais alterar linhas Revisão B.I=OK."""
    import pandas as pd

    if not master_path and not final_path:
        return {}

    # Fail-closed: jamais reconstruir uma Base Final vazia quando o Consolidado
    # ainda contém Gold/classificações aceitas. O backup deve ser restaurado antes.
    if master_path and master_path.exists() and final_path:
        guard_inventory = load_baseline_inventory(master_path, final_path)
        master_accepted = len(guard_inventory["master_accepted"])
        final_accepted = len(guard_inventory["final_accepted"])
        if master_accepted and (not final_path.exists() or not final_accepted):
            # OneDrive pode criar uma cópia com sufixo aleatório e retirar o
            # nome canônico. Não escolher/restaurar automaticamente um conflito.
            candidates = (
                sorted(
                    p.name for p in final_path.parent.glob(final_path.stem + "-*" + final_path.suffix)
                    if p.is_file()
                ) if not final_path.exists() else []
            )
            hint = (
                " Possiveis copias conflitantes: " + ", ".join(candidates[:5])
                + ". Preserve as copias e restaure o nome canonico apos conferir."
                if candidates else ""
            )
            raise RuntimeError(
                "BASELINE_FINAL_MISSING_OR_EMPTY: Consolidado possui "
                f"{master_accepted} classificacoes aceitas, mas Base Final possui "
                f"{final_accepted}. Exportacao BLOQUEADA antes de qualquer escrita. "
                "Verifique caminho/nome da Base Final antes de restaurar backup." + hint
            )

    review_bi_snapshot, review_bi_source_exists = _load_review_bi_snapshot(final_path)
    bi_lock_snapshot = _load_bi_lock_snapshot(final_path)

    source_path: Optional[Path] = None
    master_sheet = "Consolidado"
    if master_path and master_path.exists():
        source_path = master_path
    elif input_path and input_path.exists():
        source_path = input_path
    elif final_path and final_path.exists():
        source_path = final_path

    if source_path and source_path != input_path:
        master_df, master_sheet = _read_business_sheet(source_path)
    else:
        # A entrada é lista de SKUs, não uma baseline já classificada.
        # Partir de um cadastro vazio evita declarar itens como existentes sem status.
        master_df = pd.DataFrame(columns=["Marca", "Modelo"])

    master_df, brand_col, model_col = _ensure_master_columns(master_df)
    master_df, sync_stats = _sync_final_into_master(master_df, final_path, brand_col, model_col)
    master_df, brand_col, model_col = _ensure_master_columns(master_df)
    master_df, input_registered = _register_input_keys_in_master(master_df, input_path)
    cache_stats = _hydrate_input_collection_from_cache(master_df, input_path, cache_path, cfg)
    if cache_path is not None:
        LOG.info("CACHE -> BASELINE | %s", " | ".join(f"{k}={v}" for k, v in cache_stats.items()))
    master_df, merge_stats = _upsert_run_rows_into_master(
        master_df, rows, brand_col, model_col, rules, reprocess, force_reprocess_keys
    )

    master_df = _apply_review_bi_snapshot(
        master_df, brand_col, model_col, review_bi_snapshot, review_bi_source_exists,
        bi_lock_snapshot=bi_lock_snapshot
    )
    master_df, legacy_taxonomy_cleaned = _sanitize_legacy_unresolved_taxonomy(master_df)

    # Antes de tocar nos arquivos, prova que o HARD LOCK continua idêntico.
    _assert_bi_lock_snapshot(master_df, bi_lock_snapshot, "Consolidado em memória")
    # Os aceitos existentes não podem desaparecer durante uma mera sincronização.
    if not force_reprocess_keys and master_path and final_path:
        before_accepted = load_baseline_inventory(master_path, final_path)["processed"]
        mb = find_col(master_df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
        mm = find_col(master_df, ["Modelo", "Model", "SKU", "Part Number"])
        after_accepted = {
            business_key(safe(row.get(mb)), safe(row.get(mm)))
            for _, row in master_df.iterrows()
            if _baseline_row_accepted(row, master_df)
        }
        dropped = before_accepted - after_accepted
        if dropped:
            raise RuntimeError(
                "BASELINE_ACCEPTED_PRESERVATION_FAILED: "
                f"{len(dropped)} classificacoes aceitas seriam removidas. "
                "Nenhum Excel foi regravado."
            )

    # Verificar acesso a AMBOS os arquivos antes de gravar o primeiro.
    # Em Windows, Excel costuma negar abertura para escrita de um workbook aberto.
    # Falhar antes evita Consolidado atualizado e Base Final desatualizada.
    for target in (master_path, final_path):
        if target is None or not target.exists():
            continue
        try:
            with target.open("r+b"):
                pass
        except PermissionError as exc:
            raise PermissionError(
                f"BASELINE_FILE_LOCKED: feche '{target.name}' no Excel antes da sincronizacao; "
                "nenhuma exportacao foi iniciada."
            ) from exc

    stats = {
        **sync_stats,
        **merge_stats,
        **cache_stats,
        "input_registered": input_registered,
        "legacy_taxonomy_cleaned": legacy_taxonomy_cleaned,
        "review_bi_snapshot_rows": len(review_bi_snapshot),
        "bi_ok_hard_lock_rows": len(bi_lock_snapshot),
    }

    if master_path:
        _atomic_write_dataframe(
            master_df, master_path, master_sheet if source_path == master_path else "Consolidado"
        )
        stats["master_rows"] = len(master_df)

    if final_path:
        stats["final_rows"] = _export_base_final(
            master_df, final_path, review_bi_snapshot, review_bi_source_exists, bi_lock_snapshot,
            input_path=input_path,
            cache_path=cache_path,
        )

    inv_after = load_baseline_inventory(master_path, final_path)
    stats["master_accepted_total"] = len(inv_after["master_accepted"])
    stats["final_accepted_total"] = len(inv_after["final_accepted"])
    stats["baseline_union_total"] = len(inv_after["processed"])
    stats["bi_ok_hard_lock_total"] = len(inv_after["bi_locked"])
    stats["baseline_missing_in_final"] = len(inv_after["processed"] - inv_after["final_accepted"])
    return stats

def reconcile_baseline_before_run(
    master_path: Optional[Path],
    final_path: Optional[Path],
    input_path: Optional[Path],
    rules: Rules,
) -> Dict[str, int]:
    """Garante que todo item usado como SKIP exista fisicamente na Base Final."""
    before = load_baseline_inventory(master_path, final_path)
    missing_before = before["processed"] - before["final_accepted"]

    stats = {
        "pre_master_accepted": len(before["master_accepted"]),
        "pre_final_accepted": len(before["final_accepted"]),
        "pre_union_accepted": len(before["processed"]),
        "pre_missing_in_final": len(missing_before),
        "pre_reconciled": 0,
    }

    if not missing_before:
        return stats

    sync_stats = persist_baseline_excels(
        rows=[],
        master_path=master_path,
        final_path=final_path,
        input_path=input_path,
        rules=rules,
        reprocess=False,
    )

    after = load_baseline_inventory(master_path, final_path)
    missing_after = after["processed"] - after["final_accepted"]

    stats.update({
        "pre_reconciled": 1,
        "post_master_accepted": len(after["master_accepted"]),
        "post_final_accepted": len(after["final_accepted"]),
        "post_union_accepted": len(after["processed"]),
        "post_missing_in_final": len(missing_after),
        "pre_sync_final_rows": int(sync_stats.get("final_rows", 0) or 0),
    })

    if missing_after:
        raise RuntimeError(
            "BASELINE_RECONCILIATION_FAILED: "
            f"{len(missing_after)} item(ns) seriam pulados pelo pipeline, "
            "mas ainda não constam como aceitos na Base Final. "
            "Classificação não iniciada para evitar chamar Clef com baseline inconsistente."
        )

    return stats


def export_excel(rows: List[Dict[str, Any]], out_path: Path, summary: Dict[str, Any]) -> None:
    import pandas as pd
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    rev = df[df.get("Review Required", pd.Series(dtype=str)).astype(str).str.upper().eq("SIM")].copy() if not df.empty else pd.DataFrame()
    summ = pd.DataFrame([{"Métrica": k, "Valor": v} for k, v in summary.items()])
    tmp = out_path.with_name(out_path.stem + f".tmp_{os.getpid()}" + out_path.suffix)
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        summ.to_excel(w, sheet_name="Resumo", index=False)
        df.to_excel(w, sheet_name="Resultados", index=False)
        rev.to_excel(w, sheet_name="Revisar", index=False)
    os.replace(tmp, out_path)


# -----------------------------------------------------------------------------
# Operação
# -----------------------------------------------------------------------------
def build_classification_cohort(
    store: "TavilyReadOnlyStore",
    processed: set,
    manual: set,
    hard_lock: Optional[set] = None,
    max_items: int = 0,
    reprocess: bool = False,
    allowed_keys: Optional[set] = None,
    allow_safe_auxiliary_rescue: bool = True,
) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str, str]], Dict[str, int], str]:
    """Seleciona itens realmente classificáveis para a opção CLASSIFICAÇÃO.

    v4.0.6: ``--max-items`` passa a significar quantidade de TARGETS que serão
    classificados, e não as primeiras N linhas lexicográficas do catálogo.
    HARD LOCK / já processados são removidos ANTES do limite. Itens com
    READY_FOR_CLASSIFICATION vêm primeiro, seguidos por PARTIAL_TARGET_EVIDENCE.
    NO_TARGET_EVIDENCE sem curated evidence não consome o piloto de classificação;
    permanece para a frente específica de coleta/recoleta.
    """
    hard_lock = set(hard_lock or manual)
    ranked: List[Tuple[int, str, str, str, str]] = []
    stats = {
        "cohort_size": 0,
        "cohort_already_classified": 0,
        "cohort_manual_lock": 0,
        "cohort_bi_ok_lock": 0,
        "cohort_pending": 0,
        "classification_ready_available": 0,
        "classification_partial_available": 0,
        "classification_rescue_available": 0,
        "classification_safe_aux_rescue_available": 0,
        "classification_outside_input_skipped": 0,
        "classification_no_evidence_skipped": 0,
        "classification_raw_unavailable_skipped": 0,
    }

    allowed_filter = None if allowed_keys is None else set(allowed_keys)
    for brand, model, key in store.catalog():
        if allowed_filter is not None and key not in allowed_filter:
            stats["classification_outside_input_skipped"] += 1
            continue
        if key in hard_lock:
            if key in manual:
                stats["cohort_manual_lock"] += 1
            else:
                stats["cohort_bi_ok_lock"] += 1
            continue
        if not reprocess and key in processed:
            stats["cohort_already_classified"] += 1
            continue

        raw = store.get(brand, model)
        if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
            stats["classification_raw_unavailable_skipped"] += 1
            continue
        payload = raw.payload or {}
        collection = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
        state = safe(collection.get("state"))
        curated_count = len([x for x in (collection.get("curated_results", []) or []) if isinstance(x, dict)])
        evidence_cache = [x for x in (collection.get("evidence_cache", []) or []) if isinstance(x, dict)]
        safe_aux_count = sum(
            1 for x in evidence_cache
            if safe(x.get("tier")) == "AUXILIARY_REVIEW_ONLY"
            and safe(x.get("identity")) in {"STRONG", "PROBABLE"}
            and bool(x.get("eligible_for_llm_rescue", True))
            and safe(x.get("decision")) not in {
                "REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"
            }
        )

        # READY é o material ideal para o stress do classificador. PARTIAL ainda
        # possui evidência curada e deve vir em seguida. Estados legados com
        # curated_results > 0 continuam classificáveis. NO_TARGET_EVIDENCE sem
        # curated_results fica fora desta fila; não faz sentido gastar o limite
        # do piloto para apenas produzir NEEDS_RECOLLECTION.
        if state == "READY_FOR_CLASSIFICATION":
            priority = 0
            stats["classification_ready_available"] += 1
        elif state == "PARTIAL_TARGET_EVIDENCE":
            priority = 1
            stats["classification_partial_available"] += 1
        elif curated_count > 0:
            priority = 2
            stats["classification_rescue_available"] += 1
        elif allow_safe_auxiliary_rescue and safe_aux_count > 0:
            # Somente rota de rescue: Stage 1 poderá usar TITLE_ONLY_RESCUE.
            # Isso não transforma AUXILIARY em PRIMARY e não reduz o gate STRONG de AUTO.
            priority = 3
            stats["classification_safe_aux_rescue_available"] += 1
        else:
            stats["classification_no_evidence_skipped"] += 1
            continue
        ranked.append((priority, business_key(brand, model), brand, model, key))

    ranked.sort(key=lambda x: (x[0], x[1]))
    if max_items:
        ranked = ranked[:max_items]
    targets = [(brand, model, key) for _, _, brand, model, key in ranked]
    cohort = list(targets)
    stats["cohort_size"] = len(cohort)
    stats["cohort_pending"] = len(targets)
    cohort_material = "\n".join(x[2] for x in cohort)
    cohort_hash = hashlib.sha256(cohort_material.encode("utf-8")).hexdigest()[:12]
    return cohort, targets, stats, cohort_hash


def build_pilot_cohort(
    catalog: Sequence[Tuple[str, str, str]],
    processed: set,
    manual: set,
    hard_lock: Optional[set] = None,
    max_items: int = 0,
    reprocess: bool = False,
) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str, str]], Dict[str, int], str]:
    """Define cohort e garante que HARD LOCK nunca vira target, nem com --reprocess."""
    cohort = list(catalog)
    if max_items:
        cohort = cohort[:max_items]
    hard_lock = set(hard_lock or manual)

    stats = {
        "cohort_size": len(cohort),
        "cohort_already_classified": 0,
        "cohort_manual_lock": 0,
        "cohort_bi_ok_lock": 0,
        "cohort_pending": 0,
    }
    targets: List[Tuple[str, str, str]] = []
    for brand, model, key in cohort:
        if key in hard_lock:
            if key in manual:
                stats["cohort_manual_lock"] += 1
            else:
                stats["cohort_bi_ok_lock"] += 1
            continue
        if not reprocess and key in processed:
            stats["cohort_already_classified"] += 1
            continue
        targets.append((brand, model, key))

    stats["cohort_pending"] = len(targets)
    cohort_material = "\n".join(x[2] for x in cohort)
    cohort_hash = hashlib.sha256(cohort_material.encode("utf-8")).hexdigest()[:12]
    return cohort, targets, stats, cohort_hash

def _expected_cohort_accepted_after_run(
    cohort_keys: set,
    final_accepted_at_start: set,
    rows: List[Dict[str, Any]],
    force_reprocess_keys: Optional[set] = None,
) -> set:
    """Projeta o conjunto aceito após persistência usando a política do baseline."""
    expected = set(cohort_keys) & set(final_accepted_at_start)
    force_keys = set(force_reprocess_keys or set())

    for rec in rows:
        brand = safe(rec.get("Marca"))
        model = safe(rec.get("Modelo"))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        if key not in cohort_keys:
            continue

        # CLASSIFIED_CLEF e classificacao proposta, nao baseline aceito.
        # O Consolidado persiste esses registros como PROVAVEL/DOUBLE_CHECK;
        # _baseline_row_accepted exige RESOLVED e rejeita CLASSIFIED_CLEF.
        # O gate precisa comparar aceitos com aceitos, sem contar candidatos.
        if _record_is_accepted(rec):
            expected.add(key)
        elif key in force_keys:
            expected.discard(key)

    return expected


def inventory(
    store: TavilyReadOnlyStore,
    processed: set,
    manual: set,
    bi_locked: Optional[set] = None,
    output: Optional[Path] = None,
) -> Dict[str, Any]:
    bi_locked = set(bi_locked or set())
    counts = defaultdict(int)
    rows = []
    for brand, model, key in store.catalog():
        raw = store.get(brand, model)
        counts[raw.state] += 1
        if key in bi_locked:
            state = "BI_OK_HARD_LOCK"
        elif key in manual:
            state = "MANUAL_LOCK"
        elif key in processed:
            state = "ALREADY_CLASSIFIED"
        else:
            state = "PENDING_CLASSIFICATION"
        counts[state] += 1
        rows.append({"Marca": brand, "Modelo": model, "Raw State": raw.state, "Classification State": state, "Reason": raw.reason})
    counts["CACHE_PRODUCTS"] = len(rows)
    counts["MERGED_KEY_VARIANTS"] = len(store.merged_variants)
    if output:
        import pandas as pd
        with pd.ExcelWriter(output, engine="openpyxl") as w:
            pd.DataFrame([{"Métrica": k, "Valor": v} for k, v in counts.items()]).to_excel(w, sheet_name="Resumo", index=False)
            pd.DataFrame(rows).to_excel(w, sheet_name="Catalogo", index=False)
    return dict(counts)

def _load_master_rule_metadata(master_path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    """Lê metadados técnicos do Consolidado por Business Key.

    A Base Final deliberadamente não expõe Rules Version/Hash. Por isso a
    recuperação cruza a autoridade humana da Base Final com a proveniência
    técnica registrada no Consolidado.
    """
    out: Dict[str, Dict[str, str]] = {}
    if not master_path or not master_path.exists():
        return out
    try:
        df, _ = _read_business_sheet(master_path)
    except Exception as exc:
        LOG.warning("Metadados de Rules não puderam ser lidos do Consolidado: %s", exc)
        return out

    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    rv = find_col(df, ["Rules Version"], required=False)
    rh = find_col(df, ["Rules Hash"], required=False)
    pp = find_col(df, ["Pipeline Version"], required=False)

    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        out[key] = {
            "rules_version": safe(row.get(rv)) if rv else "",
            "rules_hash": safe(row.get(rh)) if rh else "",
            "pipeline_version": safe(row.get(pp)) if pp else "",
        }
    return out


def _rule_metadata_is_current(meta: Dict[str, str], rules: Rules) -> bool:
    """Hash é autoridade; versão é fallback somente quando o hash não existe."""
    old_hash = safe(meta.get("rules_hash"))
    if old_hash:
        return old_hash == rules.hash
    old_version = safe(meta.get("rules_version"))
    if old_version:
        return old_version == rules.version
    return False


def _cache_recovery_reason(
    row: Any,
    df: Any,
    key: str,
    master_meta: Dict[str, Dict[str, str]],
    rules: Rules,
) -> str:
    """Retorna motivo de reprocessamento ou string vazia para preservar.

    Regra R8:
    - Revisão B.I=OK nunca entra.
    - Manual nunca entra.
    - AUTO_APROVADO sem OK entra novamente quando Rules Hash/Version do
      Consolidado é antigo ou desconhecido.
    - Demais linhas não aceitas continuam sendo pendência normal.
    """
    if _row_bi_locked(row, df):
        return ""

    status_col = find_col(df, ["Status"], required=False)
    review_col = find_col(df, ["Revisão", "Revisao"], required=False)
    if status_col and _is_manual_status(row.get(status_col)):
        return ""
    if review_col and safe(row.get(review_col)).upper() == "MANUAL":
        return ""

    accepted = _baseline_row_accepted(row, df)
    review = safe(row.get(review_col)).upper() if review_col else ""

    # CLASSIFIED_CLEF atual já tem uma proposta completa para double-check; não
    # deve voltar automaticamente à fila de recuperação em toda execução.
    resolution_col = find_col(df, ["Resolution State"], required=False)
    pipeline_col = find_col(df, ["Pipeline Version"], required=False)
    resolution = safe(row.get(resolution_col)).upper() if resolution_col else ""
    pipeline_version = safe(row.get(pipeline_col)) if pipeline_col else ""
    meta = master_meta.get(key, {})
    # A Base Final nao expoe Pipeline Version: consultar a proveniencia do
    # Consolidado evita enfileirar novamente propostas ja aguardando double-check.
    metadata_pipeline_version = safe(meta.get("pipeline_version"))
    version_current = (
        pipeline_version == PIPELINE_VERSION
        or metadata_pipeline_version == PIPELINE_VERSION
    )
    if (
        resolution == "CLASSIFIED_CLEF"
        and review in {"PROVÁVEL", "PROVAVEL"}
        and _rule_metadata_is_current(meta, rules)
        and version_current
    ):
        return ""
    # REVIEW_CACHE_EVIDENCE precisa permanecer visivel na recuperacao.
    # Uma revisao nao e uma classificacao concluida. A fila usa elegibilidade
    # de evidencia + ledger para evitar repeticao paga do mesmo SKU.
    if resolution == "REVIEW_NO_RESULTS":
        return "REVISAO_SEM_RESULTADOS"

    if accepted:
        if review == "AUTO_APROVADO":
            meta = master_meta.get(key, {})
            if not _rule_metadata_is_current(meta, rules):
                return "AUTO_APROVADO_REGRA_OBSOLETA"
        return ""

    return "PENDENCIA_ATUAL"


def _row_needs_cache_recovery(
    row: Any,
    df: Any,
    key: str,
    master_meta: Dict[str, Dict[str, str]],
    rules: Rules,
) -> bool:
    return bool(_cache_recovery_reason(row, df, key, master_meta, rules))


def _export_recovery_report(rows: List[Dict[str, Any]], path: Path, sheet: str) -> None:
    import pandas as pd
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f".tmp_{os.getpid()}" + path.suffix)
    pd.DataFrame(rows).to_excel(tmp, sheet_name=sheet[:31], index=False)
    os.replace(tmp, path)


def build_cache_recovery_plan(
    final_path: Optional[Path],
    master_path: Optional[Path],
    store: TavilyReadOnlyStore,
    with_cache_path: Path,
    without_cache_path: Path,
    rules: Rules,
    cfg: Dict[str, Any],
    max_items: int = 0,
    include_stale_auto: bool = False,
    exclude_keys: Optional[set] = None,
) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
    """Pendencias do cache; aprovados tecnicos antigos somente com opt-in."""
    source = final_path if final_path and final_path.exists() else master_path
    if not source or not source.exists():
        raise FileNotFoundError("Cache Recovery exige Base Final ou Consolidado existente.")
    df, _ = _read_business_sheet(source)
    b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    fields = {
        "O que é (Tipo)": find_col(df, ["O que é (Tipo)", "Tipo", "Product Type"], required=False),
        "Alimentação": find_col(df, ["Alimentação"], required=False),
        "Category General": find_col(df, ["Category General"], required=False),
        "FAMILY": find_col(df, ["FAMILY", "Family"], required=False),
        "Título Real": find_col(df, ["Título Real"], required=False),
        "Status": find_col(df, ["Status"], required=False),
        "Revisão": find_col(df, ["Revisão", "Revisao"], required=False),
        "Revisão B.I": find_col(df, ["Revisão B.I", "Revisao B.I"], required=False),
    }

    master_meta = _load_master_rule_metadata(master_path)

    with_cache: List[Dict[str, Any]] = []
    without_cache: List[Dict[str, Any]] = []
    target_ranked: List[Tuple[int, str, str, str]] = []
    seen = set()
    locked = good = stale_auto = stale_unknown_meta = 0
    review_total = review_with_cache = review_without_cache = 0
    evidence_not_classifiable = 0
    collection_counts: Dict[str, int] = defaultdict(int)

    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        if key in seen:
            continue
        seen.add(key)
        review_value = safe(row.get(fields["Revisão"])).upper() if fields["Revisão"] else ""
        is_review = review_value in {"REVISAR", "PENDENTE_RECOLETA", "PENDENTE_CLASSIFICACAO"}
        if is_review:
            review_total += 1
        if _row_bi_locked(row, df):
            locked += 1
            continue

        recovery_reason = _cache_recovery_reason(
            row, df, key, master_meta, rules
        )
        if not recovery_reason or (recovery_reason == "AUTO_APROVADO_REGRA_OBSOLETA" and not include_stale_auto):
            good += 1
            continue
        if recovery_reason == "AUTO_APROVADO_REGRA_OBSOLETA":
            stale_auto += 1
            meta = master_meta.get(key, {})
            if not safe(meta.get("rules_hash")) and not safe(meta.get("rules_version")):
                stale_unknown_meta += 1

        raw = store.get(brand, model)
        meta = master_meta.get(key, {})
        coll = (raw.payload or {}).get("_collection") if isinstance((raw.payload or {}).get("_collection"), dict) else {}
        coll_state = safe(coll.get("state")) or ("SEM_COLLECTION" if raw.state in {"RAW_OK", "RAW_OK_MERGED"} else "")
        curated_count = len([x for x in (coll.get("curated_results", []) or []) if isinstance(x, dict)])
        raw_count = len([x for x in ((raw.payload or {}).get("results", []) or []) if isinstance(x, dict)])
        if coll_state:
            collection_counts[coll_state] += 1
        collection_counts["CACHE_CURATED_GT0" if curated_count else "CACHE_CURATED_ZERO"] += 1

        rec: Dict[str, Any] = {
            "Marca": brand,
            "Modelo": model,
            "Business Key": key,
            **{name: (row.get(col) if col else "") for name, col in fields.items()},
            "Estado Cache": raw.state,
            "Collection State": coll_state,
            "Raw Search Results": raw_count,
            "Curated Sources": curated_count,
            "Motivo Cache": raw.reason,
            "Motivo Recuperação": recovery_reason,
            "Rules Version Anterior": safe(meta.get("rules_version")),
            "Rules Hash Anterior": safe(meta.get("rules_hash")),
            "Rules Version Atual": rules.version,
            "Rules Hash Atual": rules.hash,
        }
        if raw.state in {"RAW_OK", "RAW_OK_MERGED"}:
            if is_review:
                review_with_cache += 1
            if recovery_reason == "AUTO_APROVADO_REGRA_OBSOLETA":
                rec["Ação"] = "REPROCESSAR_REGRA_OBSOLETA"
            elif curated_count:
                rec["Ação"] = "REPROCESSAR_CACHE_CURADO"
            elif raw_count:
                rec["Ação"] = "TITLE_RESCUE_OU_RECOLETA"
            else:
                rec["Ação"] = "RECOLETA_NECESSARIA"
            with_cache.append(rec)
            # Títulos e fontes aparecem no relatório para TODOS. A chamada LLM
            # só é permitida com evidência classficável (curada/auxiliar segura).
            aux = [x for x in (coll.get("evidence_cache", []) or []) if isinstance(x, dict)]
            aux_safe = any(
                safe(x.get("tier")) == "AUXILIARY_REVIEW_ONLY"
                and safe(x.get("identity")) in {"STRONG", "PROBABLE"}
                and bool(x.get("eligible_for_llm_rescue", True))
                and safe(x.get("decision")) not in {
                    "REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"
                }
                for x in aux
            )
            if coll_state == "READY_FOR_CLASSIFICATION":
                priority = 0
            elif coll_state == "PARTIAL_TARGET_EVIDENCE":
                priority = 1
            elif curated_count > 0:
                priority = 2
            elif aux_safe:
                priority = 3
            else:
                priority = None
            if priority is not None:
                target_ranked.append((priority, brand, model, key))
            else:
                evidence_not_classifiable += 1
                rec["Ação"] = "REVISAR_EVIDENCIA_CACHE_SEM_CHAMADA_LLM"
        else:
            if is_review:
                review_without_cache += 1
            rec["Ação"] = ("PENDENTE_COLETA" if raw.state == "RAW_MISSING"
                           else "REVISAR_CACHE_EXISTENTE")
            without_cache.append(rec)

    _export_recovery_report(with_cache, with_cache_path, "Pendentes_Com_Cache")
    _export_recovery_report(without_cache, without_cache_path, "Pendentes_Sem_Cache")

    target_ranked.sort(key=lambda x: (x[0], business_key(x[1], x[2])))
    target_all = [(brand, model, key) for _, brand, model, key in target_ranked]
    # Retomada: uma chave tentada neste plano de execucao nao deve
    # consumir novamente tokens nem ocupar as mesmas primeiras posicoes.
    excluded = set(exclude_keys or set())
    available = [item for item in target_all if item[2] not in excluded]
    targets = available[:max_items] if max_items else available
    stats = {
        "recovery_ledger_excluded": len(target_all) - len(available),
        "recovery_remaining": len(available),
        "review_total_base": review_total,
        "review_com_cache": review_with_cache,
        "review_sem_cache": review_without_cache,
        "cache_sem_evidencia_classificavel": evidence_not_classifiable,
        "linhas_unicas_analisadas": len(seen),
        "bi_ok_hard_lock": locked,
        "baseline_atual_ignorado": good,
        "auto_aprovado_regra_obsoleta": stale_auto,
        "auto_aprovado_sem_metadado_regra": stale_unknown_meta,
        "pendentes_com_cache": len(with_cache),
        "pendentes_sem_cache": len(without_cache),
        "targets_desta_execucao": len(targets),
        "tavily_calls": 0,
        **dict(collection_counts),
    }
    return targets, stats


def run_cache_recovery(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Opção 4: reaproveita somente cache_tavily.sqlite; Tavily permanece desligada."""
    cache_path = Path(args.cache or cfg["files"]["cache"])
    master_path = Path(args.master or cfg["files"].get("master", "")) if (args.master or cfg["files"].get("master")) else None
    final_path = Path(args.final or cfg["files"].get("final", "")) if (args.final or cfg["files"].get("final")) else None
    with_path = Path(cfg.get("files", {}).get("pending_with_cache", "Pendentes_Com_Cache.xlsx"))
    without_path = Path(cfg.get("files", {}).get("pending_without_cache", "Pendentes_Sem_Cache.xlsx"))

    ledger_path = Path(args.recovery_ledger) if safe(getattr(args, "recovery_ledger", "")) else None
    attempted_keys = set()
    if ledger_path and ledger_path.exists():
        attempted_keys = {
            line.strip() for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }

    store = TavilyReadOnlyStore(cache_path)
    try:
        targets, plan_stats = build_cache_recovery_plan(
            final_path, master_path, store, with_path, without_path, rules, cfg, args.max_items,
            include_stale_auto=bool(getattr(args, "include_stale_auto", False)),
            exclude_keys=attempted_keys,
        )
    finally:
        store.close()

    LOG.info("CACHE RECOVERY | %s", " | ".join(f"{k}={v}" for k, v in plan_stats.items()))
    LOG.info("Relatórios: %s | %s", with_path, without_path)
    if not targets:
        LOG.info("Nenhuma pendência com RAW_OK para reprocessar.")
        return

    # O conjunto contém pendências e AUTO_APROVADO técnico obsoleto.
    # O pipeline revalida HARD LOCK antes do primeiro item.
    run_pipeline(args, cfg, rules, explicit_targets=targets)
    # Grava ledger SOMENTE depois de salvar baseline e passar pelos gates.
    # Falhas/interruptoes nao avancam a fila e podem ser retomadas.
    if ledger_path:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as ledger:
            for _, _, key in targets:
                ledger.write(key + "\n")


def run_pipeline(
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    rules: Rules,
    explicit_targets: Optional[Sequence[Tuple[str, str, str]]] = None,
) -> None:
    cache_path = Path(args.cache or cfg["files"]["cache"])
    master_path = Path(args.master or cfg["files"].get("master", "")) if (args.master or cfg["files"].get("master")) else None
    final_path = Path(args.final or cfg["files"].get("final", "")) if (args.final or cfg["files"].get("final")) else None
    input_path = Path(args.input or cfg["files"].get("input", "")) if (args.input or cfg["files"].get("input")) else None
    db_path = Path(args.db or cfg["files"]["classification_db"])
    output_path = Path(args.output or cfg["files"]["output"])
    inventory_path = Path(args.inventory_output or cfg["files"]["inventory_output"])

    baseline_pre_stats: Dict[str, int] = {}
    if args.mode in {"classify", "full", "recover"}:
        backup_root = Path(cfg.get("files", {}).get("baseline_backup_dir", "Backups_Baseline"))
        backup_folder = _backup_baseline_files(master_path, final_path, backup_root)
        if backup_folder:
            LOG.info("BACKUP BASELINE OK | pasta=%s", backup_folder)

        audit_path = Path(cfg.get("files", {}).get("baseline_audit", "Auditoria_Baseline_R5.csv"))
        audit_stats = _audit_baseline_conflicts(final_path, audit_path)
        if audit_stats.get("audit_rows", 0):
            LOG.warning(
                "AUDITORIA BASELINE | conflitos=%d | Revisão B.I x REVIEW=%d | "
                "OTHERS|OTHERS=%d | arquivo=%s",
                audit_stats["audit_rows"], audit_stats["review_bi_conflict"],
                audit_stats["others_others"], audit_path,
            )
        else:
            LOG.info("AUDITORIA BASELINE OK | sem conflitos objetivos")

        baseline_pre_stats = reconcile_baseline_before_run(
            master_path=master_path,
            final_path=final_path,
            input_path=input_path,
            rules=rules,
        )
        LOG.info(
            "BASELINE CHECK | Consolidado aceitos=%d | Base Final aceitos=%d | "
            "união=%d | faltando na Base Final=%d | reconciliada=%s",
            int(baseline_pre_stats.get(
                "post_master_accepted",
                baseline_pre_stats.get("pre_master_accepted", 0),
            )),
            int(baseline_pre_stats.get(
                "post_final_accepted",
                baseline_pre_stats.get("pre_final_accepted", 0),
            )),
            int(baseline_pre_stats.get(
                "post_union_accepted",
                baseline_pre_stats.get("pre_union_accepted", 0),
            )),
            int(baseline_pre_stats.get(
                "post_missing_in_final",
                baseline_pre_stats.get("pre_missing_in_final", 0),
            )),
            "SIM" if baseline_pre_stats.get("pre_reconciled") else "NÃO",
        )

    baseline_inventory = load_baseline_inventory(master_path, final_path)
    # Em Cache Recovery, explicit_targets é um conjunto deliberadamente autorizado
    # a substituir AUTO_APROVADO técnico antigo. HARD LOCK humano continua sendo
    # revalidado separadamente e nunca é vencido por esta lista.
    force_reprocess_keys = (
        {x[2] for x in explicit_targets}
        if explicit_targets is not None and bool(getattr(args, "reprocess", False))
        and bool(getattr(args, "include_stale_auto", False))
        else set()
    )
    processed = baseline_inventory["processed"]
    manual = baseline_inventory["manual"]
    bi_locked = baseline_inventory["bi_locked"]
    hard_lock = baseline_inventory["hard_lock"]
    store = TavilyReadOnlyStore(cache_path)

    try:
        if args.mode == "inventory":
            counts = inventory(store, processed, manual, bi_locked, inventory_path)
            LOG.info("INVENTÁRIO: %s", " | ".join(f"{k}={v}" for k, v in counts.items()))
            LOG.info("Arquivo: %s", inventory_path)
            return

        llm_provider = safe(getattr(args, "llm_provider", "")).lower() or safe(cfg.get("classification_v42", {}).get("default_provider")).lower() or "clef"
        if llm_provider not in {"jev", "clef", "local"}:
            raise RuntimeError(f"CLASSIFICATION_PROVIDER_INVALID: {llm_provider}")
        clef_model = (
            os.getenv("CLOUDFLARE_CLEF_MODEL", "").strip()
            or safe(cfg.get("clef", {}).get("model"))
            or DEFAULT_CLEF_MODEL
        )
        if safe(getattr(args, "clef_model", "")):
            clef_model = safe(args.clef_model)

        if llm_provider == "jev":
            clef_model = safe(cfg.get("jev", {}).get("model")) or "jev-latest"
        if llm_provider != "local":
            provider_cfg = dict(cfg.get(llm_provider, {}) or {})
            # Zero means unrestricted; explicit CLI/config limits are still respected.
            max_calls = int(getattr(args, "max_api_calls", 0) or provider_cfg.get("max_api_calls_per_run", 0) or 0)
            max_tokens = int(getattr(args, "max_tokens", 0) or provider_cfg.get("max_total_tokens_per_run", 0) or 0)
            if max_calls < 0 or max_tokens < 0:
                raise ValueError("Limites HTTP/tokens nao podem ser negativos.")
            provider_cfg["max_api_calls_per_run"] = max_calls
            provider_cfg["max_total_tokens_per_run"] = max_tokens
            cfg[llm_provider] = provider_cfg
            LOG.info("LLM BUDGET | provider=%s | http_cap=%s | tokens_cap=%s",
                     llm_provider, max_calls or "SEM_LIMITE", max_tokens or "SEM_LIMITE")
        clef_identity = f"{llm_provider}:{clef_model if llm_provider != 'local' else 'rules-only'}"
        db = ClassificationDB(db_path)
        run_id = db.start_run(args.mode, rules.hash, clef_identity)
        engine = DeterministicEngine(rules, cfg)

        use_clef = llm_provider != "local" and not bool(getattr(args, "no_clef", False))
        clef: Optional[ClefFunnelClassifier] = None

        rows: List[Dict[str, Any]] = []
        stats = defaultdict(int)

        if explicit_targets is None:
            if args.mode == "classify":
                flow_cfg = cfg.get("classification_flow", {}) or {}
                restrict_to_input = bool(flow_cfg.get("restrict_classify_to_input", True))
                allowed_keys: Optional[set] = None
                if restrict_to_input:
                    if input_path is None or not input_path.exists():
                        raise RuntimeError(
                            "CLASSIFICATION_INPUT_SCOPE: arquivo de entrada não encontrado; "
                            "classificação foi bloqueada para não consumir itens históricos do cache."
                        )
                    allowed_keys = {
                        row.key for row in read_input_rows(input_path)
                        if row.valid and row.key
                    }
                    if not allowed_keys:
                        raise RuntimeError(
                            "CLASSIFICATION_INPUT_SCOPE: arquivo de entrada não possui nenhuma Marca+Modelo válida."
                        )
                    LOG.info(
                        "CLASSIFICATION INPUT SCOPE | arquivo=%s | business_keys=%d | historico_cache_excluido=SIM",
                        input_path, len(allowed_keys),
                    )
                cohort, targets, cohort_stats, cohort_hash = build_classification_cohort(
                    store,
                    processed=processed,
                    manual=manual,
                    hard_lock=hard_lock,
                    max_items=args.max_items,
                    reprocess=args.reprocess,
                    allowed_keys=allowed_keys,
                    allow_safe_auxiliary_rescue=bool(flow_cfg.get("allow_safe_auxiliary_rescue", True)),
                )
            else:
                cohort, targets, cohort_stats, cohort_hash = build_pilot_cohort(
                    store.catalog(),
                    processed=processed,
                    manual=manual,
                    hard_lock=hard_lock,
                    max_items=args.max_items,
                    reprocess=args.reprocess,
                )
        else:
            # Cache Recovery já montou pendências + auto-aprovados obsoletos. Revalida o HARD LOCK
            # no instante imediatamente anterior à classificação.
            cohort = list(explicit_targets)
            targets = [x for x in cohort if x[2] not in hard_lock]
            cohort_stats = {
                "cohort_size": len(cohort),
                "cohort_already_classified": 0,
                "cohort_manual_lock": sum(1 for x in cohort if x[2] in manual),
                "cohort_bi_ok_lock": sum(1 for x in cohort if x[2] in bi_locked),
                "cohort_pending": len(targets),
            }
            material = "\n".join(x[2] for x in cohort)
            cohort_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        for k, v in cohort_stats.items():
            stats[k] = v
        # Mantém nomes históricos no resumo/log.
        stats["already_classified"] = cohort_stats["cohort_already_classified"]
        stats["manual_lock"] = cohort_stats["cohort_manual_lock"]
        stats["bi_ok_lock"] = cohort_stats.get("cohort_bi_ok_lock", 0)

        cohort_keys = {x[2] for x in cohort}
        cohort_master_accepted = len(
            cohort_keys & baseline_inventory["master_accepted"]
        )
        cohort_final_accepted_keys_at_start = (
            cohort_keys & baseline_inventory["final_accepted"]
        )
        cohort_final_accepted = len(cohort_final_accepted_keys_at_start)
        stats["cohort_master_accepted"] = cohort_master_accepted
        stats["cohort_final_accepted_at_start"] = cohort_final_accepted

        LOG.info(
            "COHORT=%d | pendentes=%d | preservados=%d | "
            "Consolidado=%d | Base Final=%d | Manual=%d | BI_OK=%d | cohort=%s",
            len(cohort),
            len(targets),
            cohort_stats["cohort_already_classified"],
            cohort_master_accepted,
            cohort_final_accepted,
            cohort_stats["cohort_manual_lock"],
            cohort_stats.get("cohort_bi_ok_lock", 0),
            cohort_hash,
        )
        if args.mode == "classify" and explicit_targets is None:
            LOG.info(
                "CLASSIFICATION QUEUE | targets=%d | ready=%d | partial=%d | legacy_curated=%d | "
                "safe_aux_rescue=%d | fora_input=%d | no_evidence_fora_da_fila=%d | raw_indisponivel=%d | "
                "limite_aplicado_apos_filtros=SIM",
                len(targets),
                cohort_stats.get("classification_ready_available", 0),
                cohort_stats.get("classification_partial_available", 0),
                cohort_stats.get("classification_rescue_available", 0),
                cohort_stats.get("classification_safe_aux_rescue_available", 0),
                cohort_stats.get("classification_outside_input_skipped", 0),
                cohort_stats.get("classification_no_evidence_skipped", 0),
                cohort_stats.get("classification_raw_unavailable_skipped", 0),
            )

        if use_clef and targets:
            try:
                clef = ClefFunnelClassifier(
                    rules=rules, cfg=cfg, db=db, provider=llm_provider, model=clef_model
                )
                pf = clef.preflight()
                clef_identity = clef.route_identity_fragment()
                db.update_run_model(run_id, clef_identity)
                LOG.info(
                    "LLM PRE-FLIGHT OK | provider=%s | model=%s | endpoint=%s | ritmo=%.1f req/min",
                    llm_provider, clef.model, pf["base_url"], pf["operational_rpm"],
                )
                LOG.info(
                    "ROTA LLM ATIVA | provider=%s | regra forte fecha; demais usam BGE/reranker -> provedor selecionado.", llm_provider
                )
            except OperationalPause as exc:
                db.event(run_id, "", exc.code, safe(exc))
                db.finish_run(run_id, "FAILED", f"{exc.code}: {exc}")
                db.close()
                raise RuntimeError(
                    f"{exc.code}: {exc}\nClassificação NÃO iniciada."
                ) from exc
        elif use_clef:
            LOG.info(
                "CLEF PRE-FLIGHT DISPENSADO | cohort sem itens pendentes; nenhuma chamada Clef necessária."
            )

        paused_reason = ""

        try:
            for idx, (brand, model_, key) in enumerate(targets, 1):
                raw = store.get(brand, model_)

                if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
                    rec = record_for_raw_state(raw)
                    stats[raw.state.lower()] += 1
                    db.put_checkpoint(
                        key, brand, model_, raw.raw_hash, rules.hash, rec, clef_identity
                    )
                    rows.append(rec)
                    continue

                require_curated = bool(
                    (cfg.get("collection_quality", {}) or {}).get(
                        "require_curated_evidence_for_classification", True
                    )
                )
                has_collection = isinstance((raw.payload or {}).get("_collection"), dict)
                if require_curated and not has_collection:
                    rec = record_for_uncurated_cache(raw)
                    stats["collection_uncurated"] += 1
                    db.put_checkpoint(
                        key, brand, model_, raw.raw_hash, rules.hash, rec, clef_identity
                    )
                    rows.append(rec)
                    continue

                cp = None
                if not args.no_checkpoint:
                    cp = db.get_checkpoint(
                        key,
                        raw.raw_hash,
                        rules.hash,
                        clef_identity,
                    )

                if cp is not None:
                    rows.append(cp)
                    stats["checkpoint_hit"] += 1
                    continue

                s1 = engine.classify(brand, model_, raw.payload or {})
                stats[f"stage1_{s1.state.lower()}"] += 1

                try:
                    rec = finalize_product(
                        brand, model_, raw, s1, rules, cfg, clef, db, run_id
                    )
                except OperationalPause as exc:
                    db.event(run_id, key, exc.code, safe(exc))
                    if exc.code.endswith("_OUTPUT_PARSE") or exc.code.endswith("_JSON_TOKEN_LIMIT"):
                        title, url = choose_title(s1.sources, [])
                        rec = {
                            "Marca": brand, "Modelo": model_, "Raw State": raw.state,
                            "Stage1 Type Candidates": " | ".join(s1.type_candidates),
                            "Stage1 Power Candidates": " | ".join(s1.power_candidates),
        "Classification Identity": s1.identity_best,
                            "Rules Hash": rules.hash, "Pipeline Version": PIPELINE_VERSION,
                            "Processed At": now_iso(), "Resolution State": "REVIEW",
                            "Stage": "CLEF_FORMAT_REVIEW", "O que é (Tipo)": s1.product_type or "REVISAR",
                            "Alimentação": s1.power or "UNKNOWN", "Category General": "", "FAMILY": "",
                            "Título Real": title, "Fonte URL": url, "Review Required": "SIM",
                            "Reason Code": exc.code,
                            "Motivo": f"Clef falhou no formato para este item; execução continuou. {safe(exc)}",
                            "Clef Provider": "", "Clef Model": "", "Clef Cache": "",
                            "Clef Prompt Tokens": 0, "Clef Completion Tokens": 0, "Clef Total Tokens": 0,
                        }
                        stats["clef_format_review"] += 1
                        LOG.warning("%s | item enviado a REVIEW; execução continuará.", exc.code)
                    else:
                        stats["api_paused"] += 1
                        paused_reason = f"{exc.code}: {exc}"
                        LOG.error("%s", paused_reason)
                        break

                if should_checkpoint_record(rec):
                    db.put_checkpoint(
                        key, brand, model_, raw.raw_hash, rules.hash, rec, clef_identity
                    )

                rows.append(rec)
                stats[f"final_{safe(rec.get('Resolution State')).lower()}"] += 1
                stats[f"stage_{safe(rec.get('Stage')).lower()}"] += 1

                if safe(rec.get("Clef Cache")).upper() == "SIM":
                    stats["clef_cache_hit"] += 1
                stage_upper = safe(rec.get("Stage")).upper()
                resolution_upper = safe(rec.get("Resolution State")).upper()
                if stage_upper in {"CLEF", "CLEF_REPAIR"}:
                    stats["clef_result"] += 1
                    if safe(rec.get("Reason Code")) == "CLEF_AUTO_APROVADO":
                        stats["clef_auto"] += 1
                    elif resolution_upper == "CLASSIFIED_CLEF":
                        stats["clef_classified"] += 1
                if resolution_upper == "NEEDS_RECOLLECTION":
                    stats["needs_recollection"] += 1
                if stage_upper == "CLEF_REPAIR":
                    stats["clef_repair_success"] += 1
                if stage_upper == "CLEF_REPAIR_INSUFFICIENT":
                    stats["clef_repair_insufficient"] += 1
                if stage_upper == "IDENTITY_GATE":
                    stats["weak_identity_recollection"] += 1
                if stage_upper in {"COLLECTION_GATE", "SEMANTIC_FUNNEL_EMPTY"}:
                    stats["clef_blocked_no_evidence"] += 1

                if idx % 10 == 0 or idx == len(targets):
                    sem_avg = (clef.semantic_seconds / clef.semantic_calls) if clef is not None and clef.semantic_calls else 0.0
                    rerank_avg = (clef.rerank_seconds / clef.semantic_calls) if clef is not None and clef.semantic_calls else 0.0
                    LOG.info(
                        "PROGRESSO %d/%d | rule=%d ambiguous=%d unresolved=%d | "
                        "clef_auto=%d clef_classified=%d clef_api=%d clef_repair=%d clef_cache=%d | "
                        "needs_recollection=%d weak_identity=%d repair_fail=%d blocked_no_evidence=%d exception_review=%d checkpoint=%d | "
                        "tokens=%d | sem_avg=%.2fs rerank_avg=%.2fs",
                        idx, len(targets),
                        stats["stage1_resolved_rule"], stats["stage1_ambiguous"], stats["stage1_unresolved"],
                        stats["clef_auto"], stats["clef_classified"],
                        clef.api_calls if clef is not None else 0, clef.repair_calls if clef is not None else 0, stats["clef_cache_hit"],
                        stats["needs_recollection"], stats["weak_identity_recollection"], stats["clef_repair_insufficient"], stats["clef_blocked_no_evidence"],
                        stats["final_review"], stats["checkpoint_hit"],
                        clef.total_tokens if clef is not None else 0, sem_avg, rerank_avg,
                    )

            baseline_stats: Dict[str, int] = {}
            baseline_warning = ""
            try:
                baseline_stats = persist_baseline_excels(
                    rows=rows,
                    master_path=master_path,
                    final_path=final_path,
                    input_path=input_path,
                    rules=rules,
                    reprocess=args.reprocess,
                    force_reprocess_keys=force_reprocess_keys,
                    cache_path=cache_path,
                    cfg=cfg,
                )
                if baseline_stats:
                    post_inv = load_baseline_inventory(master_path, final_path)
                    final_cohort_accepted = len(
                        cohort_keys & post_inv["final_accepted"]
                    )
                    expected_cohort_keys = _expected_cohort_accepted_after_run(
                        cohort_keys=cohort_keys,
                        final_accepted_at_start=cohort_final_accepted_keys_at_start,
                        rows=rows,
                        force_reprocess_keys=force_reprocess_keys,
                    )
                    expected_cohort_accepted = len(expected_cohort_keys)
                    stats["cohort_final_accepted_at_end"] = final_cohort_accepted
                    stats["cohort_expected_accepted_at_end"] = expected_cohort_accepted

                    LOG.info(
                        "BASELINE EXCEL ATUALIZADA | %s",
                        " | ".join(
                            f"{k}={v}" for k, v in baseline_stats.items()
                        ),
                    )
                    LOG.info(
                        "COHORT BASE FINAL | aceitos=%d/%d | esperado=%d | review=%d",
                        final_cohort_accepted,
                        len(cohort),
                        expected_cohort_accepted,
                        int(stats.get("final_review", 0) or 0),
                    )

                    if final_path and final_cohort_accepted != expected_cohort_accepted:
                        raise RuntimeError(
                            "BASELINE_COHORT_MISMATCH: "
                            f"Base Final possui {final_cohort_accepted} aceito(s) "
                            f"no cohort, mas eram esperados {expected_cohort_accepted}. "
                            "Classificacao_v3 e checkpoints foram preservados; "
                            "a Base Final precisa ser reconciliada antes da próxima execução."
                        )
            except Exception as exc:
                # Classificação e checkpoints não são perdidos só porque o Excel
                # está aberto ou houve falha de gravação.
                baseline_warning = f"{type(exc).__name__}: {exc}"
                LOG.error(
                    "BASELINE EXCEL NÃO ATUALIZADA: %s",
                    baseline_warning,
                )

            summary = {
                "Pipeline Version": PIPELINE_VERSION,
                "Rules Version": rules.version,
                "Rules Hash": rules.hash,
                "Clef Provider": "CLEF" if clef is not None else ("NOT_NEEDED" if use_clef else "DISABLED"),
                "Clef Model": clef.route_identity_fragment() if clef is not None else ("NOT_NEEDED" if use_clef else "DISABLED"),
                "Produtos no cache": len(store.catalog()),
                "Elegíveis": len(targets),
                "Pilot Cohort Hash": cohort_hash,
                "Clef API Calls": clef.api_calls if clef is not None else 0,
                "Clef Prompt Tokens": clef.prompt_tokens if clef is not None else 0,
                "Clef Completion Tokens": clef.completion_tokens if clef is not None else 0,
                "Clef Total Tokens": clef.total_tokens if clef is not None else 0,
                "Semantic Calls": clef.semantic_calls if clef is not None else 0,
                "Semantic Seconds": round(clef.semantic_seconds, 3) if clef is not None else 0,
                "Rerank Seconds": round(clef.rerank_seconds, 3) if clef is not None else 0,
                "Baseline Warning": baseline_warning,
                **baseline_pre_stats,
                **dict(stats),
                **baseline_stats,
            }

            export_excel(rows, output_path, summary)
            run_status = (
                "PAUSED"
                if paused_reason
                else ("COMPLETED_WITH_BASELINE_WARNING" if baseline_warning else "COMPLETED")
            )
            note = paused_reason or json.dumps(summary, ensure_ascii=False)
            db.finish_run(run_id, run_status, note)

            if paused_reason:
                LOG.warning(
                    "CLASSIFICAÇÃO PAUSADA COM SEGURANÇA. Resultado parcial: %s",
                    output_path,
                )
                LOG.warning(
                    "Rode novamente depois; checkpoints e cache Clef evitam repetir itens concluídos."
                )
            elif baseline_warning:
                LOG.error(
                    "EXECUÇÃO BLOQUEADA: baseline divergente. Resultado parcial: %s | motivo=%s",
                    output_path, baseline_warning,
                )
            else:
                LOG.info("CONCLUÍDO. Resultado: %s", output_path)

            LOG.info(
                "Resumo: %s",
                " | ".join(f"{k}={v}" for k, v in stats.items()),
            )
            # Logs, planilhas e checkpoints podem ter sido gravados, mas a
            # execução NÃO pode retornar exit code 0 se o baseline divergiu.
            if baseline_warning:
                raise RuntimeError("BASELINE_PERSISTENCE_FAILED: " + baseline_warning)
            # Um cap de API ou pausa operacional pode deixar o lote incompleto.
            # Nao gravar todas as chaves no ledger de retomada nessa situacao.
            if paused_reason:
                raise RuntimeError("RECOVERY_BATCH_PAUSED: " + paused_reason)

        except KeyboardInterrupt:
            try:
                partial_baseline = persist_baseline_excels(
                    rows=rows,
                    master_path=master_path,
                    final_path=final_path,
                    input_path=input_path,
                    rules=rules,
                    reprocess=args.reprocess,
                    force_reprocess_keys=force_reprocess_keys,
                    cache_path=cache_path,
                    cfg=cfg,
                )
                if partial_baseline:
                    LOG.warning(
                        "Interrompido, mas baseline Excel foi atualizada: %s",
                        " | ".join(
                            f"{k}={v}" for k, v in partial_baseline.items()
                        ),
                    )
            except Exception as baseline_exc:
                LOG.error(
                    "Interrompido; baseline Excel não pôde ser atualizada: %s",
                    baseline_exc,
                )

            db.finish_run(
                run_id,
                "INTERRUPTED",
                "Interrompido pelo usuário; checkpoints persistidos por produto",
            )
            LOG.warning(
                "Interrompido. Checkpoints concluídos permanecem persistidos."
            )
            raise
        except BaseException as exc:
            db.finish_run(run_id, "FAILED", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            db.close()
    finally:
        store.close()


def run_quality_gates(rules: Rules, cfg: Dict[str, Any]) -> Dict[str, int]:
    """Executa regressões sintéticas + cobertura dos casos reais validados.

    REAL_VALIDATED_CASES nunca classificam um produto em runtime. Servem apenas
    para detectar quando um tipo humano-validado deixou de ser recuperável pelas
    regras em evidência que contém alias explícito desse mesmo tipo.
    """
    qg = rules.quality_gates or {}
    synthetic = qg.get("SYNTHETIC_CASES", []) or []
    identity_cases = qg.get("IDENTITY_CASES", []) or []
    real = qg.get("REAL_VALIDATED_CASES", []) or []
    if not synthetic:
        raise RuntimeError("QUALITY_GATES_INVALID: SYNTHETIC_CASES vazio")
    if not identity_cases:
        raise RuntimeError("QUALITY_GATES_INVALID: IDENTITY_CASES vazio")
    if not real:
        raise RuntimeError("QUALITY_GATES_INVALID: REAL_VALIDATED_CASES vazio")

    engine = DeterministicEngine(rules, cfg)
    errors: List[str] = []

    for i, case in enumerate(synthetic, 1):
        cid = safe(case.get("id")) or f"synthetic_{i}"
        brand = safe(case.get("brand")) or "TEST"
        model = safe(case.get("model")) or f"M{i}"
        results = case.get("results") or [{
            "title": safe(case.get("title")),
            "content": safe(case.get("content")),
            "url": safe(case.get("url")) or "https://example.com/regression",
        }]
        if not isinstance(results, list):
            errors.append(f"{cid}: results inválido")
            continue
        result = engine.classify(brand, model, {"results": results})
        expect = case.get("expect", {}) or {}

        def check(label: str, actual: Any, expected: Any) -> None:
            if expected is not None and actual != expected:
                errors.append(f"{cid}: {label}={actual!r}, esperado={expected!r}")

        if "state" in expect: check("state", result.state, safe(expect.get("state")))
        if "product_type" in expect: check("product_type", result.product_type, expect.get("product_type"))
        if "power" in expect: check("power", result.power, expect.get("power"))
        if "category_general" in expect: check("category_general", result.taxonomy[0], safe(expect.get("category_general")))
        if "family" in expect: check("family", result.taxonomy[1], safe(expect.get("family")))
        for typ in expect.get("must_include_type_candidates", []) or []:
            if typ not in result.type_candidates: errors.append(f"{cid}: candidato obrigatório ausente: {typ}")
        for typ in expect.get("forbidden_type_candidates", []) or []:
            if typ in result.type_candidates: errors.append(f"{cid}: candidato proibido presente: {typ}")
        for power in expect.get("forbidden_power_candidates", []) or []:
            if power in result.power_candidates: errors.append(f"{cid}: power proibido presente: {power}")

    identity_passed = 0
    rank = DeterministicEngine.IDENTITY_RANK
    for i, case in enumerate(identity_cases, 1):
        cid = safe(case.get("id")) or f"identity_{i}"
        srcs = engine.build_sources(
            safe(case.get("brand")), safe(case.get("model")),
            {"results": [{"title": safe(case.get("title")), "content": safe(case.get("content")), "url": safe(case.get("url")) or "https://example.com/identity"}]},
        )
        if not srcs:
            errors.append(f"{cid}: fonte não construída")
            continue
        src = srcs[0]
        expect = case.get("expect", {}) or {}
        min_level = safe(expect.get("min_level"))
        max_level = safe(expect.get("max_level"))
        model_match = safe(expect.get("model_match"))
        if min_level and rank.get(src.relevance, 0) < rank.get(min_level, 0):
            errors.append(f"{cid}: identity={src.relevance}, mínimo={min_level}")
            continue
        if max_level and rank.get(src.relevance, 0) > rank.get(max_level, 4):
            errors.append(f"{cid}: identity={src.relevance}, máximo={max_level}")
            continue
        if model_match and src.model_match != model_match:
            errors.append(f"{cid}: model_match={src.model_match}, esperado={model_match}")
            continue
        identity_passed += 1

    real_active = 0
    real_explicit = 0
    real_passed = 0
    real_reference_only = 0
    for i, case in enumerate(real, 1):
        if not bool(case.get("teacher_enabled", True)):
            continue
        real_active += 1
        typ = safe(case.get("gold_type"))
        cg = safe(case.get("gold_category"))
        fam = safe(case.get("gold_family"))
        cid = f"REAL:{safe(case.get('brand'))}|{safe(case.get('model'))}"
        if typ not in rules.types:
            errors.append(f"{cid}: gold_type não existe no JSON: {typ}")
            continue
        if cg and fam and (cg, fam) not in rules.valid_pairs:
            errors.append(f"{cid}: gold taxonomy inexistente: {cg}|{fam}")
            continue
        source_title = safe(case.get("source_title"))
        evidence = safe(case.get("evidence_text"))
        aliases = (rules.types.get(typ, {}) or {}).get("aliases", []) or []
        title_explicit = any(contains_alias(source_title, a) for a in aliases if safe(a))
        probe_sources = engine.build_sources(
            safe(case.get("brand")) or "TEST",
            safe(case.get("model")) or f"REAL{i}",
            {"results": [{"title": source_title, "content": evidence, "url": "https://example.com/real-case"}]},
        )
        usable_title_source = bool(
            probe_sources
            and probe_sources[0].source_weight > 0
            and probe_sources[0].relevance not in {"WEAK", "CONFLICT"}
        )
        if not title_explicit or not usable_title_source:
            # Caso real sem identidade + tipo explícitos no título permanece como
            # referência humana. Não transformamos ruído de BODY em obrigação do motor.
            real_reference_only += 1
            continue
        real_explicit += 1
        r = engine.classify(
            safe(case.get("brand")) or "TEST",
            safe(case.get("model")) or f"REAL{i}",
            {"results": [{"title": source_title, "content": evidence, "url": "https://example.com/real-case"}]},
        )
        if typ not in r.type_candidates and r.product_type != typ:
            errors.append(f"{cid}: tipo validado {typ} deixou de ser candidato determinístico")
        else:
            real_passed += 1

    if errors:
        raise RuntimeError(
            f"QUALITY_GATE_FAILED: {len(errors)} falha(s). " + " | ".join(errors[:25])
        )
    return {
        "synthetic_cases": len(synthetic),
        "synthetic_passed": len(synthetic),
        "identity_cases": len(identity_cases),
        "identity_passed": identity_passed,
        "real_active": real_active,
        "real_explicit": real_explicit,
        "real_passed": real_passed,
        "real_reference_only": real_reference_only,
    }



def validate_classification_flow_policy(cfg: Dict[str, Any]) -> List[str]:
    """Fail-closed para o contrato operacional da v4.0.7.

    O funil prepara candidatos; Clef decide toda cauda não resolvida por regra
    quando há evidência target utilizável. Resultado Clef válido é preservado
    mesmo abaixo do gate AUTO. REVIEW humano não é rota padrão.
    """
    flow = cfg.get("classification_flow", {}) or {}
    errors: List[str] = []
    required_true = (
        "clef_required_after_non_rule",
        "preserve_valid_clef_classification_below_auto_gate",
        "no_automatic_secondary_search",
        "repair_type_taxonomy_conflict",
        "review_is_last_resort",
        "restrict_classify_to_input",
        "allow_safe_auxiliary_rescue",
    )
    for key in required_true:
        if not bool(flow.get(key, False)):
            errors.append(f"CLASSIFICATION_FLOW_POLICY: {key} deve ser true")
    # s1.identity_best mede suporte de TYPE do Stage 1 e não a identidade global
    # do target; portanto não pode bloquear uma decisão Clef válida.
    if bool(flow.get("weak_identity_requires_recollection", False)):
        errors.append("CLASSIFICATION_FLOW_POLICY: weak_identity_requires_recollection deve ser false")
    if int(flow.get("max_clef_repair_attempts", 0) or 0) != 1:
        errors.append("CLASSIFICATION_FLOW_POLICY: max_clef_repair_attempts deve ser exatamente 1")
    repair = (cfg.get("clef", {}) or {}).get("repair", {}) or {}
    if not bool(repair.get("enabled", False)):
        errors.append("CLEF_REPAIR_POLICY: clef.repair.enabled deve ser true")
    if int(repair.get("max_attempts", 0) or 0) != 1:
        errors.append("CLEF_REPAIR_POLICY: clef.repair.max_attempts deve ser exatamente 1")
    return errors

def validate_all(rules: Rules, cfg: Dict[str, Any], cache_override: str = "") -> None:
    errors = rules.validate()
    if errors:
        for e in errors:
            LOG.error(e)
        raise RuntimeError(f"Validação falhou: {len(errors)} erro(s)")

    identity_runtime_errors = validate_identity_runtime_contract(cfg)
    if identity_runtime_errors:
        for e in identity_runtime_errors:
            LOG.error(e)
        raise RuntimeError("Contrato runtime de identidade inválido; execução bloqueada.")
    LOG.info("IDENTITY RUNTIME CONTRACT OK | cases=%d | profile=%s", len((cfg.get("identity", {}) or {}).get("runtime_regression_contract", []) or []), IDENTITY_PROFILE_VERSION)

    qg = run_quality_gates(rules, cfg)
    LOG.info(
        "QUALITY GATES OK | synthetic=%d | identity=%d | real_explicit=%d/%d | reference_only=%d",
        qg["synthetic_cases"], qg["identity_passed"], qg["real_passed"], qg["real_active"], qg["real_reference_only"],
    )

    # Contrato operacional v4.0.7.3: uma consulta automática por produto e Clef
    # obrigatório para toda cauda não resolvida por regra com evidência útil.
    cost_errors = validate_collection_cost_policy(cfg)
    if cost_errors:
        for e in cost_errors:
            LOG.error(e)
        raise RuntimeError("Política de coleta inválida; execução bloqueada.")
    flow_errors = validate_classification_flow_policy(cfg)
    if flow_errors:
        for e in flow_errors:
            LOG.error(e)
        raise RuntimeError("Contrato de classificação v4.0.7.3 inválido; execução bloqueada.")
    LOG.info(
        "FLOW CONTRACT OK | 1_search_only=True | clef_after_non_rule=True | "
        "preserve_clef_classification=True | weak_stage1_identity_recollect=False | joint_repair=True | review_last_resort=True"
    )

    sf = cfg.get("semantic_funnel", {}) or {}
    local_only = bool(sf.get("local_files_only", False))
    offline = bool(sf.get("offline_mode", local_only))
    embedding_model = safe(sf.get("embedding_model"))
    reranker_model = safe(sf.get("reranker_model"))
    if local_only:
        local_errors = []
        if not offline:
            local_errors.append("semantic_funnel.offline_mode deve ser true quando local_files_only=true")
        for label, value in [("embedding_model", embedding_model), ("reranker_model", reranker_model)]:
            if not value:
                local_errors.append(f"semantic_funnel.{label} vazio")
            elif not Path(value).expanduser().exists():
                local_errors.append(f"semantic_funnel.{label} não encontrado: {value}")
        if local_errors:
            for e in local_errors:
                LOG.error(e)
            raise RuntimeError("BGE local/offline inválido; classificação bloqueada.")
        LOG.info(
            "BGE LOCAL CONFIG OK | embedding=%s | reranker=%s | local_only=%s | offline=%s | "
            "type_rerank=%s | taxonomy_rerank=%s | rerank_max_length=%s",
            embedding_model, reranker_model, local_only, offline,
            bool(sf.get("rerank_types", False)), bool(sf.get("rerank_taxonomy", True)),
            int(sf.get("rerank_max_length", 224) or 224),
        )

    cache = Path(cache_override or cfg["files"]["cache"])
    if cache.exists():
        store = TavilyReadOnlyStore(cache)
        try:
            LOG.info("Cache Search OK: %d produtos | merged_variants=%d", len(store.catalog()), len(store.merged_variants))
        finally:
            store.close()
    else:
        LOG.warning("Cache Search não encontrado no caminho padrão: %s", cache)
    LOG.info("VALIDAÇÃO OK | rules=%s | types=%d | powers=%d", rules.version, len(rules.types), len(rules.allowed_powers))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pipeline de Classificação de Produtos v4 Clef")
    p.add_argument("--mode", choices=["validate", "inventory", "collect", "curate", "classify", "full", "recover", "repair", "evidence-export", "sync-input"], default="validate")
    p.add_argument("--config", default="pipeline_config.json")
    p.add_argument("--rules", default="regras_negocio.json")
    p.add_argument("--collection-policy", default="")
    p.add_argument("--input", default="")
    p.add_argument("--cache", default="")
    p.add_argument("--master", default="")
    p.add_argument("--final", default="")
    p.add_argument("--db", default="")
    p.add_argument("--output", default="")
    p.add_argument("--inventory-output", default="")
    p.add_argument("--precollection-output", default="")
    p.add_argument("--evidence-output", default="")
    p.add_argument("--clef-model", dest="clef_model", default="")
    p.add_argument("--llm-provider", choices=["jev", "clef", "local"], default="")
    p.add_argument("--no-clef", dest="no_clef", action="store_true")
    p.add_argument("--no-checkpoint", action="store_true")
    p.add_argument("--reprocess", action="store_true")
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--max-api-calls", type=int, default=0, help="Limite HTTP LLM incluindo retries e reparos.")
    p.add_argument("--max-tokens", type=int, default=0, help="Limite de tokens LLM reportados.")
    p.add_argument("--include-stale-auto", action="store_true", help="Recuperar tambem autoaprovados antigos.")
    p.add_argument("--recovery-ledger", default="", help="Arquivo de chaves ja tentadas pela recuperacao, para execucao sequencial em lotes.")
    p.add_argument("--max-tavily-new", "--max-search-new", dest="max_tavily_new", type=int, default=0)
    p.add_argument("--search-provider", choices=["tavily", "serper", "brightdata"], default="")
    p.add_argument("--recollect-empty", action="store_true")
    p.add_argument("--recollect-low-evidence", action="store_true")
    p.add_argument("--curate-all-cache", action="store_true", help="No modo curate, ignora o arquivo de entrada e percorre todo o cache")
    p.add_argument("--force-curate", action="store_true", help="Reavaliar a curadoria atual sem repetir Search; preserva historico do SQLite.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def _menu_limit(prompt: str) -> int:
    """ENTER/TODOS=sem limite de produtos."""
    while True:
        raw = input(prompt).strip().upper()
        if not raw or raw == "TODOS":
            return 0
        if raw.isdecimal() and int(raw) > 0:
            return int(raw)
        print("Informe inteiro positivo ou pressione ENTER para todos.")


def _menu_search_provider() -> str:
    """Seleção numérica explícita; nunca iniciar coleta com provider inválido."""
    options = {"1": "tavily", "2": "serper", "3": "brightdata"}
    while True:
        print("\nAPI DE COLETA / SEARCH")
        print("1 - TAVILY")
        print("2 - SERPER")
        print("3 - BRIGHT DATA")
        raw = input("Selecione [1/2/3] (ENTER = 1 / TAVILY): ").strip()
        if not raw:
            raw = "1"
        if raw in options:
            choice = options[raw]
            print(f"API selecionada: {choice.upper()}")
            return choice
        print("Opção inválida. Escolha 1, 2 ou 3.")


def _menu_llm_provider() -> str:
    options = {"1": "jev", "2": "clef", "3": "local"}
    while True:
        print("\nCLASSIFICADOR")
        print("1 - JEV / TypeSafe AI")
        print("2 - Cloudflare Clef")
        print("3 - Somente regras locais (sem LLM)")
        choice = input("Selecione [1/2/3] (ENTER = 1 / JEV): ").strip() or "1"
        if choice in options:
            return options[choice]
        print("Opção inválida: escolha 1, 2 ou 3.")


def interactive_menu(base: Path) -> argparse.Namespace:
    parser = build_parser()
    print("=" * 86)
    print(f" PIPELINE ENRIQUECEDOR DE PRODUTOS {PIPELINE_VERSION} | {BUILD_LABEL}")
    print(" Search Provider -> Identity -> Curated Evidence -> Rules -> BGE Funnel -> Reranker -> Clef")
    print(" COLLECTION HARD RULE: 1 produto = no máximo 1 request | sem retry/fallback/extract/advanced")
    print(" Revisão B.I=OK = HARD LOCK ABSOLUTO | novos itens mantêm Revisão B.I vazia")
    print("=" * 86)
    print(f"1 - COLETA / EXTRAÇÃO {COLLECTION_LABEL} (1 REQUEST/PRODUTO)")
    print("2 - CLASSIFICAÇÃO")
    print("3 - PROCESSO COMPLETO")
    print("4 - RECUPERAR PENDÊNCIAS DO CACHE (SEM BUSCA)")
    print("5 - AUDITORIA / REPARO BASE (SEM CLEF)")
    print("6 - CURAR CACHE LEGADO (SEM BUSCA / 0 CRÉDITOS)")
    print("7 - EXPORTAR EVIDENCE BUNDLE (SEM BUSCA / 0 CRÉDITOS)")
    print("8 - SAIR")
    op = input("Opção: ").strip()
    if op == "1":
        provider = _menu_search_provider()
        # Diferente do piloto de classificacao (ENTER=100), coleta em branco
        # significa SEM LIMITE. A lista exata de requests e confirmada antes
        # de qualquer chamada; RAW ja consultado nunca entra automaticamente.
        raw_limit = input("Limite de produtos para coleta (ENTER = todos; 0 buscas para itens ja consultados): ").strip().upper()
        if raw_limit and raw_limit != "TODOS" and (not raw_limit.isdecimal() or int(raw_limit) <= 0):
            raise ValueError("Limite de coleta deve ser inteiro positivo ou TODOS/ENTER")
        limite = int(raw_limit) if raw_limit and raw_limit != "TODOS" else 0
        argv = ["--mode", "collect", "--search-provider", provider]
        if limite:
            argv += ["--max-search-new", str(limite)]
        return parser.parse_args(argv)
    if op == "2":
        limite = _menu_limit("Limite de produtos para piloto (ENTER = todos): ")
        llm = _menu_llm_provider()
        argv = ["--mode", "classify", "--llm-provider", llm]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "3":
        provider = _menu_search_provider()
        limite_search = _menu_limit("Limite de produtos na coleta (ENTER = todos): ")
        limite_class = _menu_limit("Limite de produtos na classificação (ENTER = todos): ")
        llm = _menu_llm_provider()
        argv = ["--mode", "full", "--search-provider", provider, "--llm-provider", llm]
        low = input("Recoletar também RAW existente com evidência target fraca? (S/N): ").strip().upper()
        if low == "S":
            argv += ["--recollect-low-evidence"]
        if limite_search:
            argv += ["--max-search-new", str(limite_search)]
        if limite_class:
            argv += ["--max-items", str(limite_class)]
        return parser.parse_args(argv)
    if op == "4":
        limite = _menu_limit("Limite de pendências COM CACHE para reprocessar (ENTER = todas): ")
        llm = _menu_llm_provider()
        argv = ["--mode", "recover", "--llm-provider", llm]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "5":
        return parser.parse_args(["--mode", "repair"])
    if op == "6":
        limite = _menu_limit("Limite de produtos do arquivo de entrada (ENTER = todos): ")
        argv = ["--mode", "curate"]
        all_cache = input("Curar TODO o cache em vez do arquivo de entrada atual? (S/N, ENTER = N): ").strip().upper()
        if all_cache == "S":
            argv += ["--curate-all-cache"]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "7":
        limite = _menu_limit("Limite de produtos para exportar (ENTER = todos): ")
        argv = ["--mode", "evidence-export"]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    raise SystemExit(0)



def validate_identity_runtime_contract(cfg: Dict[str, Any]) -> List[str]:
    """Valida Search Model e identidade estrutural antes de consumir Search."""
    identity = cfg.get("identity", {}) or {}
    cases = identity.get("runtime_regression_contract", []) or []
    if not isinstance(cases, list) or not cases:
        return ["IDENTITY_RUNTIME_CONTRACT: runtime_regression_contract ausente/vazio"]

    structural_cfg = identity.get("structural_sku_core", {}) or {}
    configured_regexes = tuple(safe(x) for x in (structural_cfg.get("edge_decorator_regexes", []) or []) if safe(x))
    if configured_regexes and configured_regexes != DEFAULT_STRUCTURAL_EDGE_DECORATOR_REGEXES:
        return [
            "IDENTITY_RUNTIME_CONTRACT: edge_decorator_regexes diverge do perfil estrutural versionado"
        ]
    if bool(structural_cfg.get("fuzzy_matching", False)):
        return ["IDENTITY_RUNTIME_CONTRACT: fuzzy_matching deve permanecer false"]

    errors: List[str] = []
    suffixes = identity.get("regional_suffixes", []) or []
    rank = {"WEAK": 0, "CONTEXTUAL": 1, "PROBABLE": 2, "STRONG": 3}
    for idx, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"IDENTITY_RUNTIME_CONTRACT[{idx}]: caso inválido")
            continue
        brand = safe(case.get("brand"))
        model = safe(case.get("model"))
        if not brand or not model:
            errors.append(f"IDENTITY_RUNTIME_CONTRACT[{idx}]: brand/model ausente")
            continue

        expected_search = safe(case.get("expected_search_model"))
        if expected_search:
            actual_search = derive_search_model(brand, model, cfg)
            if canonical_display(actual_search) != canonical_display(expected_search):
                errors.append(
                    f"IDENTITY_RUNTIME_CONTRACT[{idx}]: SearchModel {brand} {model} "
                    f"esperado={expected_search} atual={actual_search}"
                )

        expected_variant = safe(case.get("expected_identity_variant"))
        if expected_variant:
            variants = {value for value, _ in _model_identity_variants(model, suffixes)}
            if compact(expected_variant) not in variants:
                errors.append(
                    f"IDENTITY_RUNTIME_CONTRACT[{idx}]: CORE {brand} {model} "
                    f"esperado={expected_variant} variantes={sorted(variants)}"
                )

        expected_identity = safe(case.get("expected_source_identity"))
        expected_identity_max = safe(case.get("expected_source_identity_max"))
        if expected_identity or expected_identity_max:
            res = {
                "title": safe(case.get("title")),
                "url": safe(case.get("url")),
                "content": safe(case.get("content")),
            }
            meta = _source_identity_for_result(brand, model, res, cfg)
            actual_level = safe(meta.get("level")) or "WEAK"
            if expected_identity and actual_level != expected_identity:
                errors.append(
                    f"IDENTITY_RUNTIME_CONTRACT[{idx}]: identity esperado={expected_identity} atual={actual_level}"
                )
            if expected_identity_max and rank.get(actual_level, 0) > rank.get(expected_identity_max, 0):
                errors.append(
                    f"IDENTITY_RUNTIME_CONTRACT[{idx}]: identity máximo={expected_identity_max} atual={actual_level}"
                )

    return errors


def validate_release_contract(cfg: Dict[str, Any], rules: Any, policy: Dict[str, Any]) -> List[str]:
    """Fail-closed para impedir mistura silenciosa de código/config/policy/rules."""
    contract = cfg.get("release_contract", {}) or {}
    actual = {
        "pipeline_version": PIPELINE_VERSION,
        "collection_engine_version": COLLECTION_ENGINE_VERSION,
        "collection_policy_version": safe(policy.get("VERSION")),
        "rules_version": safe(getattr(rules, "version", "")),
        "identity_profile_version": IDENTITY_PROFILE_VERSION,
        "evidence_cache_schema": EVIDENCE_CACHE_SCHEMA_VERSION,
    }
    errors: List[str] = []
    for key, value in actual.items():
        expected = safe(contract.get(key))
        if not expected:
            errors.append(f"RELEASE_CONTRACT: {key} ausente no pipeline_config.json")
        elif expected != safe(value):
            errors.append(f"RELEASE_CONTRACT: {key} esperado={expected} atual={value}")
    return errors


def export_evidence_bundle(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    """Exporta JSONL reutilizável por chat/LLM sem Search e sem classificação."""
    cache_path = Path(args.cache or cfg["files"]["cache"])
    output_path = Path(
        args.evidence_output
        or (cfg.get("files", {}) or {}).get("evidence_export_jsonl", "Evidence_Bundle_v4_0_7_3.jsonl")
    )
    store = TavilyReadOnlyStore(cache_path)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    exported = 0
    with_sources = 0
    try:
        catalog = store.catalog()
        if args.max_items:
            catalog = catalog[:int(args.max_items)]
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for brand, model, key in catalog:
                raw = store.get(brand, model)
                if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                    continue
                payload = raw.payload
                collection = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
                entries = [x for x in (collection.get("evidence_cache", []) or []) if isinstance(x, dict)]
                if not entries:
                    entries = _build_evidence_cache_entries(
                        brand,
                        derive_search_model(brand, model, cfg),
                        [x for x in (payload.get("results", []) or []) if isinstance(x, dict)],
                        [x for x in (collection.get("source_audit", []) or []) if isinstance(x, dict)],
                        [x for x in (collection.get("curated_results", []) or []) if isinstance(x, dict)],
                        cfg,
                        safe(collection.get("provider"))
                        or safe((payload.get("_search_meta") or {}).get("provider"))
                        or "cache",
                    )
                quality = _evidence_cache_quality(entries)
                if entries:
                    with_sources += 1
                bundle = {
                    "schema": "product-evidence-bundle-v1",
                    "pipeline_version": PIPELINE_VERSION,
                    "brand": brand,
                    "model": model,
                    "business_key": key,
                    "raw_state": raw.state,
                    "collection_state": safe(collection.get("state")),
                    "search_query": safe((payload.get("_search_meta") or {}).get("query")),
                    "quality": quality,
                    "sources": entries,
                    "llm_context": _llm_evidence_context(brand, model, entries, max_sources=6),
                    "review_context_unverified": _llm_unverified_review_context(
                        brand,
                        model,
                        entries,
                        max_sources=int((cfg.get("classification_flow", {}) or {}).get("max_unverified_review_sources", 4) or 4),
                    ),
                }
                fh.write(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")) + "\n")
                exported += 1
        os.replace(tmp, output_path)
        LOG.info(
            "EVIDENCE EXPORT | produtos=%d | com_fontes=%d | search_calls=0 | arquivo=%s",
            exported, with_sources, output_path,
        )
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        store.close()


def run_baseline_repair(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Audita e saneia baseline local sem Tavily e sem Clef."""
    master_path = Path(args.master or cfg["files"].get("master", "")) if (args.master or cfg["files"].get("master")) else None
    final_path = Path(args.final or cfg["files"].get("final", "")) if (args.final or cfg["files"].get("final")) else None
    input_path = Path(args.input or cfg["files"].get("input", "")) if (args.input or cfg["files"].get("input")) else None

    backup_root = Path(cfg.get("files", {}).get("baseline_backup_dir", "Backups_Baseline"))
    backup_folder = _backup_baseline_files(master_path, final_path, backup_root)
    if backup_folder:
        LOG.info("BACKUP BASELINE OK | pasta=%s", backup_folder)

    audit_path = Path(cfg.get("files", {}).get("baseline_audit", "Auditoria_Baseline_R5.csv"))
    audit_stats = _audit_baseline_conflicts(final_path, audit_path)
    LOG.info(
        "AUDITORIA BASELINE | conflitos=%d | Revisão B.I x REVIEW=%d | OTHERS|OTHERS=%d | arquivo=%s",
        audit_stats.get("audit_rows", 0),
        audit_stats.get("review_bi_conflict", 0),
        audit_stats.get("others_others", 0),
        audit_path,
    )

    stats = persist_baseline_excels(
        rows=[],
        master_path=master_path,
        final_path=final_path,
        input_path=input_path,
        rules=rules,
        reprocess=False,
        cache_path=Path(args.cache or cfg["files"]["cache"]),
        cfg=cfg,
    )
    LOG.info(
        "REPARO BASE CONCLUÍDO SEM CLEF | %s",
        " | ".join(f"{k}={v}" for k, v in stats.items()),
    )
    LOG.info("Revisão B.I não foi recalculada nem preenchida pelo script.")


def main() -> int:
    base = Path(__file__).resolve().parent
    args = interactive_menu(base) if len(sys.argv) == 1 else build_parser().parse_args()
    setup_logging(args.verbose)
    os.chdir(base)
    cfg_path = (base / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    rules_path = (base / args.rules).resolve() if not Path(args.rules).is_absolute() else Path(args.rules)
    env_info = load_env_file(base / ".env")
    cfg = read_json(cfg_path)
    collection_policy_name = args.collection_policy or safe((cfg.get("files", {}) or {}).get("collection_policy")) or "collection_quality_policy.json"
    collection_policy_path = (base / collection_policy_name).resolve() if not Path(collection_policy_name).is_absolute() else Path(collection_policy_name)
    collection_policy = read_json(collection_policy_path)
    collection_errors = validate_collection_policy(collection_policy)
    if collection_errors:
        for e in collection_errors:
            LOG.error(e)
        return 2
    cfg["_collection_policy"] = collection_policy
    cfg["_collection_policy_path"] = str(collection_policy_path)
    cost_policy_errors = validate_collection_cost_policy(cfg)
    if cost_policy_errors:
        for e in cost_policy_errors:
            LOG.error(e)
        return 2
    rules = Rules(rules_path)
    release_errors = validate_release_contract(cfg, rules, collection_policy)
    if release_errors:
        for e in release_errors:
            LOG.error(e)
        return 2
    identity_runtime_errors = validate_identity_runtime_contract(cfg)
    if identity_runtime_errors:
        for e in identity_runtime_errors:
            LOG.error(e)
        return 2

    if env_info["exists"]:
        LOG.info(
            ".env carregado: %s | CLEF=%s | TAVILY=%s | SERPER=%s | BRIGHTDATA=%s | BRIGHTDATA_ZONE=%s",
            env_info["path"],
            "CONFIGURADA" if bool(os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()) and bool((os.getenv("CLOUDFLARE_API_TOKEN", "").strip() or os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip())) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("TAVILY_API_KEY", "").strip()) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("SERPER_API_KEY", "").strip()) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("BRIGHTDATA_API_KEY", "").strip()) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("BRIGHTDATA_ZONE", "").strip()) else "AUSENTE",
        )
    else:
        LOG.warning(".env não encontrado em: %s", env_info["path"])

    LOG.info("Pipeline=%s | Rules=%s | semantic_hash=%s | quality_hash=%s | CollectionPolicy=%s", PIPELINE_VERSION, rules.version, rules.hash[:12], rules.quality_hash[:12], safe(collection_policy.get("VERSION")))
    errors = rules.validate()
    if errors:
        for e in errors[:50]:
            LOG.error(e)
        return 2
    if args.mode == "validate":
        validate_all(rules, cfg, args.cache)
        return 0
    if args.mode == "repair":
        run_baseline_repair(args, cfg, rules)
        return 0
    if args.mode == "evidence-export":
        export_evidence_bundle(args, cfg)
        return 0
    if args.mode in {"classify", "full", "recover"}:
        qg = run_quality_gates(rules, cfg)
        flow_errors = validate_classification_flow_policy(cfg)
        if flow_errors:
            for e in flow_errors:
                LOG.error(e)
            return 2
        LOG.info(
            "QUALITY GATES OK | synthetic=%d | identity=%d | real_explicit=%d/%d | classificação liberada",
            qg["synthetic_cases"], qg["identity_passed"], qg["real_passed"], qg["real_active"],
        )
        LOG.info(
            "FLOW CONTRACT OK | 1_search_only=True | clef_after_non_rule=True | "
            "preserve_clef_classification=True | weak_stage1_identity_recollect=False | joint_repair=True | review_last_resort=True"
        )
    # A existência do SKU nas bases finais não depende de Search nem de classificação.
    # Executa uma única sincronização segura antes de coletar/classificar.
    if args.mode in {"collect", "full", "sync-input"}:
        files_cfg = cfg.get("files", {}) or {}
        input_sync = Path(args.input or files_cfg.get("input", "Base de Produtos.xlsx"))
        master_sync = Path(files_cfg.get("master", "Consolidado Base de Produtos Enriquecida.xlsx"))
        final_sync = Path(files_cfg.get("final", "Base Final Produtos Enriquecida.xlsx"))
        if not input_sync.exists():
            LOG.error("Sincronização não iniciada: Base de entrada ausente: %s", input_sync)
            return 2
        try:
            _backup_baseline_files(
                master_sync, final_sync,
                Path(files_cfg.get("baseline_backup_dir", "Backups_Baseline")),
            )
            sync_stats = persist_baseline_excels(
                rows=[], master_path=master_sync, final_path=final_sync,
                input_path=input_sync, rules=rules,
                cache_path=Path(args.cache or files_cfg.get("cache", "cache_tavily.sqlite")),
                cfg=cfg,
            )
            LOG.info("INPUT BASELINE SYNC OK | inserted=%s | final_rows=%s",
                     sync_stats.get("input_registered", 0), sync_stats.get("final_rows", 0))
        except Exception as exc:
            LOG.error("INPUT BASELINE SYNC FAILED; interrompendo para preservar os dados: %s", exc)
            return 2

    if args.mode == "sync-input":
        return 0
    if args.mode == "inventory":
        run_pipeline(args, cfg, rules)
        return 0
    if args.mode == "collect":
        provider = _resolve_search_provider(args, cfg)
        _provider_credentials(provider, cfg)
        completed = collect_products(args, cfg, rules)
        if completed:
            files_cfg = cfg.get("files", {}) or {}
            persist_baseline_excels(
                rows=[],
                master_path=Path(files_cfg.get("master", "Consolidado Base de Produtos Enriquecida.xlsx")),
                final_path=Path(files_cfg.get("final", "Base Final Produtos Enriquecida.xlsx")),
                input_path=Path(args.input or files_cfg.get("input", "Base de Produtos.xlsx")),
                rules=rules,
                cache_path=Path(args.cache or files_cfg.get("cache", "cache_tavily.sqlite")),
                cfg=cfg,
            )
        return 0 if completed else 3
    if args.mode == "curate":
        curate_cache_without_tavily(args, cfg, rules)
        return 0
    if args.mode == "classify":
        run_pipeline(args, cfg, rules)
        return 0
    if args.mode == "recover":
        run_cache_recovery(args, cfg, rules)
        return 0
    if args.mode == "full":
        provider = _resolve_search_provider(args, cfg)
        _provider_credentials(provider, cfg)
        completed = collect_products(args, cfg, rules)
        if not completed:
            LOG.warning("PROCESSO COMPLETO interrompido após a coleta. Classificação não iniciada.")
            return 3
        run_pipeline(args, cfg, rules)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
