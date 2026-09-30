#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIPELINE DE ENRIQUECIMENTO DE PRODUTOS v3.9.5-hf3.1 / Collection R9.7 Cache-First Free-Tier

Arquitetura:
    Stage 0A - Search Provider único (Tavily/Serper/Bright Data) - MAX 1 request
    Stage 0B - Source Sieve: Query Integrity + Target Identity + Source Purpose
    Stage 0C - Evidence Enrichment: Tavily raw_content na mesma Search ou fetch local
    Stage 0D - Target Isolation + Collection Quality Gate
    Stage 1 - Regras determinísticas do JSON
    Stage 1.5 - Candidate Funnel determinístico
    Stage 2A - Product Definition Pilot: LLM define o produto principal a partir da evidência
    Stage 2B - Funil taxonômico reconcilia a definição da LLM com PRODUCT_TYPE canônico
    Stage 3 - NVIDIA (model failover) -> Groq
    Stage 4 - Taxonomia determinística do JSON

Princípios:
- Search RAW do provider selecionado descobre fontes; somente fontes aprovadas no Source Sieve podem ser aprofundadas.
- regras_negocio.json é a fonte única semântica: regras + quality gates.
- Stage 1 usa o comportamento comprovado do R5: aliases JSON formam candidatos; resolução automática exige evidência explícita/repetida.
- O LLM recebe contexto relevante extraído do JSON por produto.
- O LLM escolhe apenas PRODUCT_TYPE e POWER dentro do contrato fechado.
- Category General e FAMILY sempre vêm do JSON, nunca do LLM.
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
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlencode, urlparse

PIPELINE_VERSION = "3.9.5-hf3.1"
PROMPT_VERSION = "2026-09-11-v15-evidence-arbiter-funnel"
SEMANTIC_PROMPT_VERSION = "2026-09-30-v4-product-definition-explicit-schema"
SEMANTIC_MAP_PROMPT_VERSION = "2026-09-29-v1-semantic-taxonomy-mapper"
DEFAULT_MODEL = "openai/gpt-oss-120b"
BUILD_LABEL = "v3.9.5-HF3.1p9 R9.7 Cache-First Free-Tier"
COLLECTION_ENGINE_VERSION = "2026-09-30-r9.7-cache-first-free-tier-v1"
MAX_COLLECTION_REQUESTS_PER_PRODUCT = 1
ALLOWED_SEARCH_PROVIDERS = {"tavily", "serper", "brightdata"}

SEMANTIC_DEFINITION_SYSTEM_PROMPT = (
    "Define the primary product represented by the supplied evidence, using the most specific technical/commercial product name that the evidence supports. "
    "Use ONLY the supplied evidence. Do not classify Category General or FAMILY and do not consult outside knowledge. "
    "Do not force the product into the internal taxonomy and do not generalize when the evidence explicitly supports a more specific product name. "
    "Return a short, specific, uppercase English product_type_raw. Preserve meaningful subtype words such as AXIAL, ROTARY, EXPLOSION-PROOF, MODULE, ASSEMBLY, PRESS TOOL, etc. "
    "Do not answer with only an industry, use case, function, material, or generic class when the evidence names the actual product. "
    "The Brand field can be an importer/trading/company name and may differ from the commercial product brand. An exact model/part number in the evidence can still identify the target, but NEVER accept a different or near model. "
    "product_role=COMPONENT_PART when the item itself is a component/spare/module/blade/fixed part; ACCESSORY only when it is sold as an accessory; ASSEMBLY when the evidence explicitly describes an assembly; otherwise WHOLE_PRODUCT. "
    "evidence_phrase MUST be a short verbatim phrase copied from one cited source and must directly support product_type_raw. "
    "If the supplied evidence does not tie the requested model to a definable product, return INSUFFICIENT_EVIDENCE."
)

LOG = logging.getLogger("pipeline_v3")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe(v: Any) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and v != v:
            return ""
    except Exception:
        pass
    return str(v).strip()


def _json_schema_contract_errors(data: Any, schema: Dict[str, Any]) -> List[str]:
    """Validação mínima do contrato JSON usado pelas chamadas semânticas.

    Não pretende implementar JSON Schema completo; cobre exatamente o contrato
    utilizado pelo piloto (object/required/additionalProperties/type/enum/array).
    Isso é necessário porque alguns providers aceitam apenas json_object e podem
    devolver JSON sintaticamente válido, porém fora do schema solicitado.
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["ROOT_NOT_OBJECT"]

    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    for key in required:
        if key not in data:
            errors.append(f"MISSING_REQUIRED:{key}")

    if schema.get("additionalProperties") is False:
        for key in data:
            if key not in properties:
                errors.append(f"UNEXPECTED_PROPERTY:{key}")

    def _type_ok(value: Any, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, dict)
        if expected == "boolean":
            return isinstance(value, bool)
        return True

    for key, spec in properties.items():
        if key not in data:
            continue
        value = data[key]
        expected = safe(spec.get("type"))
        if expected and not _type_ok(value, expected):
            errors.append(f"TYPE:{key}:{expected}")
            continue
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            errors.append(f"ENUM:{key}:{value}")
        if expected == "array" and isinstance(value, list):
            item_spec = spec.get("items", {}) or {}
            item_type = safe(item_spec.get("type"))
            item_enum = item_spec.get("enum")
            for idx, item in enumerate(value):
                if item_type and not _type_ok(item, item_type):
                    errors.append(f"ITEM_TYPE:{key}[{idx}]:{item_type}")
                if isinstance(item_enum, list) and item not in item_enum:
                    errors.append(f"ITEM_ENUM:{key}[{idx}]:{item}")
    return errors


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
    suffixes = {canonical_display(x) for x in (suffix_map.get(brand_key, []) or []) if safe(x)}

    work = re.sub(r"\s+", " ", original).strip()
    tokens = work.split()
    if len(tokens) > 1 and canonical_display(tokens[-1]) in suffixes:
        work = " ".join(tokens[:-1]).strip()

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
        [safe(x) for x in (revision_map.get(brand_key, []) or []) if safe(x)],
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

    embedded_brands = {canonical_display(x) for x in (icfg.get("embedded_sku_brands", []) or [])}
    min_len = int(icfg.get("embedded_sku_min_length", 5) or 5)
    if brand_key in embedded_brands:
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
    """Controla rajadas por requests/minute no processo local."""
    def __init__(self, requests_per_minute: float):
        rpm = max(0.0, float(requests_per_minute or 0))
        self.interval = (60.0 / rpm) if rpm > 0 else 0.0
        self._last = 0.0

    def wait(self, label: str = "") -> float:
        if self.interval <= 0:
            return 0.0
        now = time.monotonic()
        remaining = (self._last + self.interval) - now
        if self._last and remaining > 0:
            if label:
                LOG.info("%s PACING | aguardando %.1fs para respeitar %.2f RPM", label, remaining, 60.0 / self.interval)
            time.sleep(remaining)
        self._last = time.monotonic()
        return max(0.0, remaining)


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
        self.semantic_policy = self.classification_policy.get("semantic_recovery", {}) or {}
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
            if pair == ("OTHERS", "OTHERS"):
                allowed_oo = set(self.type_policy.get("allow_explicit_others_others_types", []) or [])
                if t not in allowed_oo:
                    errors.append(f"TAXONOMY_EVIDENCE_RULE OTHERS|OTHERS não autorizado para type={t}")
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
            allowed_oo = set(self.type_policy.get("allow_explicit_others_others_types", []) or [])
            if not allowed_oo:
                errors.append("FORBIDDEN_TAXONOMY_DEAD_END: OTHERS|OTHERS exige TYPE_POLICY.allow_explicit_others_others_types")

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
                errors.append(f"LLM_SAFETY_GATE_DISABLED: {key} deve ser true")
        if safe(llm_gate.get("require_identity_at_least")) not in {"PROBABLE", "STRONG"}:
            errors.append("LLM_SAFETY_GATE_WEAK: require_identity_at_least deve ser PROBABLE ou STRONG")
        semantic_policy = self.classification_policy.get("semantic_recovery", {}) or {}
        if semantic_policy:
            if safe(semantic_policy.get("min_identity", "PROBABLE")) not in {"PROBABLE", "STRONG"}:
                errors.append("CLASSIFICATION_POLICY.semantic_recovery.min_identity deve ser PROBABLE ou STRONG")
            role_overrides = semantic_policy.get("role_overrides", {}) or {}
            for role, typ in role_overrides.items():
                if safe(typ) not in self.types:
                    errors.append(f"SEMANTIC_ROLE_OVERRIDE_UNKNOWN_TYPE: {role}->{typ}")
            bridges = semantic_policy.get("raw_type_alias_to_canonical", {}) or {}
            for raw_alias, typ in bridges.items():
                if not safe(raw_alias) or safe(typ) not in self.types:
                    errors.append(f"SEMANTIC_RAW_BRIDGE_INVALID: {raw_alias!r}->{typ!r}")

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

        allowed_classification = {"llm_resolution_gate", "semantic_recovery"}
        for key in self.classification_policy:
            if key not in allowed_classification:
                errors.append(f"UNKNOWN_CLASSIFICATION_POLICY_FIELD: {key}")

        allowed_type_policy = {
            "component_primary_types", "passive_power_types", "specificity_groups",
            "output_voltage_not_input_power_types", "output_voltage_rule",
            "replacement_compatibility_rule", "source_conflict_rule",
            "agricultural_mower_rule", "allow_explicit_others_others_types",
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
            "negative_aliases", "priority", "brand", "model_regex", "semantic_only",
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
        Stage 0 / Collection Quality Gate; a LLM continua sem decidir taxonomia.
        """
        evidence_fold = strip_accents(evidence_text).casefold()
        evidence_candidates = []
        for r in self.taxonomy_evidence_rules:
            if safe(r.get("primary_type")) != product_type:
                continue
            if bool(r.get("semantic_only", False)) and "semantic_type_raw" not in evidence_fold:
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
        apenas provider/modelo, permitindo benchmark Groq x NVIDIA no futuro.
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
        df = pd.read_excel(path, dtype=object)
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
        valid = bool(brand and model)
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
    """Normaliza/deduplica/cruza cache. NÃO consulta Tavily."""
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
                rep.action = "RECOLETAR"
                rep.reason = "RAW corrompido"
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
        "REJECT_WEAK_IDENTITY", "REJECT_EXTRACT_FAILED",
    }
    missing = sorted(required - decisions)
    if missing:
        errors.append(f"COLLECTION_POLICY.source_decisions faltando={missing}")
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


def _looks_like_other_sku_token(token: str, start: int, text: str, model: str, policy: Dict[str, Any]) -> bool:
    raw = safe(token).upper().strip(".,:;()[]{}")
    compact_token = compact(raw)
    if not compact_token:
        return False
    target_compacts = {x[0] for x in _model_identity_variants(model, [])}
    if compact_token in target_compacts:
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


def _other_sku_spans(text: str, model: str, policy: Dict[str, Any]) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    # Conservador: so tokens com estrutura de codigo; palavras comuns nao viram fronteira.
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9._/-]{4,})(?![A-Za-z0-9])", safe(text)):
        token = m.group(1)
        if _looks_like_other_sku_token(token, m.start(), text, model, policy):
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
    title_url_target = bool(_target_spans(f"{title}\n{url}", model, cfg))
    body_target = bool(_target_spans(raw, model, cfg))
    search_target = bool(_target_spans(search_content, model, cfg))
    if not (title_url_target or body_target or search_target):
        return {"decision": "REJECT_WRONG_MODEL", "reason": "SKU alvo nao aparece em TITLE/URL/conteudo extraido", "content": "", "flags": ["NO_TARGET_MODEL"], "other_skus": []}

    other_spans = _other_sku_spans(raw, model, policy)
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
                if not _other_sku_spans(chunk, model, policy) and not _term_present(chunk, related_terms):
                    isolated_regions.append(chunk)
            continue
        other = _other_sku_spans(chunk, model, policy)
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

    residual_other = _other_sku_spans(isolated, model, policy)
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
        identity = _source_identity_for_result(brand, search_model, row, cfg)
        title = safe(row.get("title"))
        snippet = safe(row.get("content"))
        url = safe(row.get("url"))
        search_text = f"{title}\n{snippet}".strip()
        flags: List[str] = []

        decision = "ACCEPT_SOURCE_SIEVE"
        reason = "Identidade target STRONG/PROBABLE e sem propósito negativo detectado"
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
        snippet = safe(res.get("content"))
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
    }]

    # Não persistir raw_content integral da Tavily no RAW cache: ele pode ser muito
    # grande. O trecho target-isolado já está em curated_results e é suficiente para
    # classificação/auditoria. Mantemos apenas o indicador de disponibilidade.
    persisted_results: List[Dict[str, Any]] = []
    for row in normalized:
        r = dict(row)
        r["_raw_content_available"] = bool(safe(r.get("raw_content")))
        r["raw_content"] = None
        persisted_results.append(r)

    tavily_credits = float(usage.get("reported_credits") or 0.0) if provider == "tavily" else 0.0
    return {
        "results": persisted_results,
        "_search_meta": {
            "provider": provider,
            "engine_version": COLLECTION_ENGINE_VERSION,
            "brand_original": brand,
            "model_original": model,
            "search_model": search_model,
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
            "curated_results": curated,
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
    """Aplica R9.5 ao cache legado sem nova chamada Tavily.

    Usa raw_content quando existente; caso contrario, usa somente os chunks de
    Search ja persistidos. Nunca inventa conteudo faltante.
    """
    policy = _collection_policy(cfg)
    search_model = derive_search_model(brand, model, cfg)
    raw_results = [x for x in (payload.get("results", []) or []) if isinstance(x, dict)]
    curated: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    for res in raw_results:
        identity = _source_identity_for_result(brand, search_model, res, cfg)
        raw_content = safe(res.get("raw_content"))
        isolation = _isolate_target_block(
            brand, search_model, safe(res.get("title")), safe(res.get("url")),
            raw_content, safe(res.get("content")), cfg, policy,
        )
        decision = safe(isolation.get("decision"))
        if decision.startswith("ACCEPT_"):
            row = _make_curated_result(res, isolation, "CACHE_RAW", "CACHE_REBUILD_NO_TAVILY")
            row["_collection_identity"] = _source_identity_for_result(brand, search_model, row, cfg)["level"]
            curated.append(row)
        audit.append({
            "url": safe(res.get("url")), "title": safe(res.get("title")), "tavily_score": res.get("score"),
            "identity_before": identity["level"], "model_match_field_before": identity["model_match_field"],
            "decision": decision, "reason": safe(isolation.get("reason")),
            "quality_flags": list(isolation.get("flags", []) or []), "other_skus": list(isolation.get("other_skus", []) or []),
            "isolated_chars": len(safe(isolation.get("content"))), "extract_depth": "CACHE_RAW",
            "source_mode": "CACHE_REBUILD_NO_TAVILY",
        })
    curated = _dedupe_curated(curated)
    curated.sort(key=lambda x: (safe(x.get("_collection_identity")) != "STRONG", -float(x.get("score", 0) or 0)))
    state = _collection_state(brand, search_model, curated, cfg, policy)
    out = dict(payload)
    out["_collection"] = {
        "version": safe(policy.get("VERSION")) or "R9.5",
        "state": state,
        "search_model": search_model,
        "curated_results": curated[:max(1, int((cfg.get("evidence", {}) or {}).get("max_results", 5) or 5))],
        "source_audit": audit,
        "query_audit": [{"stage": "CACHE_REBUILD", "query": "", "search_depth": "CACHE_RAW", "results": len(raw_results), "extract_candidates": 0, "accepted_total_after_stage": len(curated), "collection_state_after_stage": state, "search_credits_total": 0, "extract_credits_total": 0}],
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
    print(" R9.6p6 PRÉ-COLETA — 1 SEARCH / SOURCE SIEVE / EVIDENCE ENRICHMENT")
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
    run_id = db.start_run("collect", rules.hash, f"SEARCH_R9_6:{provider}")
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
                writer.put(
                    brand, model, payload,
                    archive_reason=(
                        f"RECOLLECT_{provider.upper()}_R9_6"
                        if prior_state in {"RAW_OK", "RAW_OK_MERGED"}
                        else f"COLLECT_{provider.upper()}_R9_6"
                    ),
                )
                curated = coll.get("curated_results", []) or []
                raw_n = len(payload.get("results", []) or [])
                reported_credits = usage.get("reported_credits")
                product_rows.append({
                    "Marca": brand, "Modelo": model, "Search Model": safe(coll.get("search_model")),
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
                    run_id, key, "SEARCH_R9_6_SAVED",
                    f"provider={provider}; state={state}; raw={raw_n}; curated={len(curated)}; requests={req_count}; credits={reported_credits}",
                )
                LOG.info(
                    "COLETA R9.6 %d/%d | %s %s | provider=%s | state=%s | raw=%d curated=%d | requests=%d | credits=%s",
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
                writer.put(brand, model, failure, archive_reason=f"{provider.upper()}_REQUEST_ERROR_R9_6")
                total_requests += 1
                db.event(run_id, key, "SEARCH_R9_6_ERROR_NO_RETRY", safe(exc))
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
            "COLLECTION R9.6 FINAL | provider=%s | produtos=%d | requests=%d | hard_cap=%d/produto | relatório=%s",
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
    """Migra/curta RAW legado para R9.5 sem consumir creditos Tavily."""
    cache_path = Path(args.cache or cfg["files"]["cache"])
    report_path = Path(cfg.get("files", {}).get("collection_report_cache_rebuild", "Curadoria_Cache_R9_5.xlsx"))
    store = TavilyReadOnlyStore(cache_path)
    writer = TavilyWritableStore(cache_path)
    products: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    queries: List[Dict[str, Any]] = []
    try:
        catalog = store.catalog()
        if args.max_items:
            catalog = catalog[:int(args.max_items)]
        for idx, (brand, model, key) in enumerate(catalog, 1):
            raw = store.get(brand, model)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                continue
            existing = raw.payload.get("_collection") if isinstance(raw.payload, dict) else None
            if isinstance(existing, dict) and safe(existing.get("version")) == safe((_collection_policy(cfg)).get("VERSION")):
                continue
            rebuilt = _curate_existing_payload(brand, model, raw.payload, cfg)
            coll = rebuilt.get("_collection", {}) or {}
            writer.put(brand, model, rebuilt, archive_reason="CACHE_CURATED_R9_5_NO_TAVILY")
            products.append({
                "Marca": brand, "Modelo": model, "Search Model": coll.get("search_model"),
                "Collection State": coll.get("state"), "RAW Results": len(rebuilt.get("results", []) or []),
                "Curated Sources": len(coll.get("curated_results", []) or []), "Tavily Credits": 0, "Data": now_iso(),
            })
            for s in coll.get("source_audit", []) or []:
                sources.append({
                    "Marca": brand, "Modelo": model, "URL": s.get("url"), "Title": s.get("title"),
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
        LOG.info("CURADORIA CACHE R9.5 CONCLUIDA | produtos=%d | creditos Tavily=0 | arquivo=%s", len(products), report_path)
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


def _model_identity_variants(model: str, regional_suffixes: Sequence[str]) -> List[Tuple[str, str]]:
    """Variações equivalentes de SKU sem fuzzy/edit-distance.

    Mantém o comportamento estável R5 e acrescenta apenas equivalências
    estruturais determinísticas: separadores, sufixo regional e revisão decimal.
    Retorna (compact_variant, reason).
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
    - LLM nunca cria candidato novo (gate posterior).
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
        self.min_score = 1.0  # usado pelo gate LLM; 1 ponto = 1 fonte de suporte.

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
            if bool(rule.get("semantic_only", False)):
                continue
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

        # Se a taxonomia do TYPE independe de alimentação, UNKNOWN é uma decisão
        # válida e não motivo para gastar LLM.
        if product_type and power is None and not self._type_requires_specific_power(product_type):
            power = "UNKNOWN"
            reasons.append("POWER_UNKNOWN_ALLOWED_BY_TAXONOMY")

        if product_type:
            tax_evidence = taxonomy_evidence_text(sources, product_type)
            cg, fam, tax_reason = self.rules.map_taxonomy(
                product_type, power or "UNKNOWN", tax_evidence, brand, model
            )
        else:
            cg, fam, tax_reason = "", "", ""

        if product_type and power is not None and (cg, fam) in self.rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS"):
            state = "RESOLVED_RULE"
        elif type_candidates:
            state = "AMBIGUOUS"
        else:
            state = "UNRESOLVED"

        supporters = [s for s in sources if product_type and product_type in s.type_hits]
        best_source_score = max((s.identity_score for s in supporters), default=0)
        identity_best = max((s.relevance for s in supporters), key=lambda x: self.IDENTITY_RANK.get(x, 0), default="WEAK")

        if type_candidates:
            reasons.append("ALLOWED_LLM_TYPES:" + ",".join(type_candidates))
        else:
            reasons.append("LLM_BLOCKED:NO_TYPE_CANDIDATE")

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
    """Stage 1.5 determinístico: reduz o espaço da LLM sem inventar candidatos.

    O funil opera somente sobre TYPEs já encontrados pelo Stage 1 e aplica o
    mesmo conceito de evidência que será exigido depois da resposta da LLM.
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
# R9.6p8 - Semantic Product Pilot
# -----------------------------------------------------------------------------
def _semantic_tokens(text: str) -> set:
    return {x for x in norm_text(text).split() if len(x) >= 2}


def semantic_taxonomy_candidates(
    rules: Rules,
    raw_type: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Recupera candidatos canônicos sem expor toda a taxonomia à LLM.

    A busca é somente local sobre PRODUCT_TYPES/aliases/famílias do JSON.
    Não usa embeddings, internet ou fuzzy de Marca+Modelo.
    """
    raw_norm = norm_text(raw_type)
    if not raw_norm:
        return []
    raw_tokens = _semantic_tokens(raw_norm)
    labels_by_type: Dict[str, set] = defaultdict(set)
    for typ, spec in rules.types.items():
        labels_by_type[typ].add(typ)
        for alias in (spec.get("aliases", []) or []):
            if safe(alias):
                labels_by_type[typ].add(safe(alias))
        fp = spec.get("fallback_pair")
        if isinstance(fp, list) and len(fp) == 2 and safe(fp[1]):
            labels_by_type[typ].add(safe(fp[1]))
    for row in rules.taxonomy_rules:
        typ = safe(row.get("primary_type"))
        fam = safe(row.get("family"))
        if typ in rules.types and fam:
            labels_by_type[typ].add(fam)
    for row in rules.taxonomy_evidence_rules:
        typ = safe(row.get("primary_type"))
        fam = safe(row.get("family"))
        if typ in rules.types and fam:
            labels_by_type[typ].add(fam)

    scored: List[Tuple[float, int, str, str]] = []
    for typ, labels in labels_by_type.items():
        best_score = 0.0
        best_label = ""
        best_len = 0
        for label in labels:
            ln = norm_text(label)
            if not ln:
                continue
            lt = _semantic_tokens(ln)
            if raw_norm == ln:
                score = 1.0
            elif ln in raw_norm and len(lt) >= 1:
                score = 0.94 + min(0.04, 0.01 * len(lt))
            elif raw_norm in ln and len(raw_tokens) >= 1:
                score = 0.90
            else:
                union = raw_tokens | lt
                jacc = (len(raw_tokens & lt) / len(union)) if union else 0.0
                seq = SequenceMatcher(None, raw_norm, ln).ratio()
                score = 0.62 * jacc + 0.38 * seq
            if score > best_score or (score == best_score and len(ln) > best_len):
                best_score = score
                best_label = label
                best_len = len(ln)
        scored.append((best_score, best_len, typ, best_label))

    scored.sort(key=lambda x: (x[0], x[1], rules.specificity_rank.get(x[2], 0)), reverse=True)
    floor = float(rules.semantic_policy.get("candidate_min_score", 0.28) or 0.28)
    out = []
    for score, _, typ, label in scored:
        if score < floor:
            continue
        out.append({"type": typ, "score": round(score, 4), "matched_label": label})
        if len(out) >= max(1, int(limit)):
            break
    return out


def semantic_resolve_canonical(
    rules: Rules,
    raw_type: str,
    product_role: str,
) -> Tuple[str, List[Dict[str, Any]], str]:
    """Normaliza TYPE_RAW da LLM para o TYPE canônico controlado pelo JSON."""
    policy = rules.semantic_policy or {}
    role = safe(product_role).upper()
    role_overrides = {safe(k).upper(): safe(v) for k, v in (policy.get("role_overrides", {}) or {}).items()}
    if role in role_overrides and role_overrides[role] in rules.types:
        typ = role_overrides[role]
        return typ, [{"type": typ, "score": 1.0, "matched_label": f"ROLE:{role}"}], "ROLE_OVERRIDE"

    raw_norm = norm_text(raw_type)
    bridges = {norm_text(k): safe(v) for k, v in (policy.get("raw_type_alias_to_canonical", {}) or {}).items()}
    if raw_norm in bridges and bridges[raw_norm] in rules.types:
        typ = bridges[raw_norm]
        return typ, [{"type": typ, "score": 1.0, "matched_label": raw_type}], "RAW_TYPE_BRIDGE"

    candidates = semantic_taxonomy_candidates(
        rules,
        raw_type,
        limit=int(policy.get("max_taxonomy_candidates", 5) or 5),
    )
    if not candidates:
        return "", [], "NO_TAXONOMY_CANDIDATE"
    top = candidates[0]
    second = candidates[1]["score"] if len(candidates) > 1 else 0.0
    threshold = float(policy.get("deterministic_match_threshold", 0.90) or 0.90)
    margin = float(policy.get("deterministic_margin", 0.08) or 0.08)
    if float(top["score"]) >= threshold and (float(top["score"]) - float(second)) >= margin:
        return safe(top["type"]), candidates, "DETERMINISTIC_SEMANTIC_MATCH"
    return "", candidates, "NEEDS_LLM_TAXONOMY_VALIDATION"


def semantic_grounding_ok(
    rules: Rules,
    s1: Stage1Result,
    data: Dict[str, Any],
) -> Tuple[bool, str]:
    decision = safe(data.get("decision")).upper()
    if decision not in {"IDENTIFIED", "INSUFFICIENT_EVIDENCE", "CONFLICT"}:
        return False, "SEMANTIC_DECISION_INVALID"
    if decision != "IDENTIFIED":
        return True, "OK"
    raw_type = safe(data.get("product_type_raw"))
    if not raw_type:
        return False, "SEMANTIC_TYPE_EMPTY"
    power = safe(data.get("power")).upper() or "UNKNOWN"
    if power not in rules.allowed_powers:
        return False, "SEMANTIC_POWER_INVALID"
    refs = data.get("evidence_source_ids", []) or []
    if not isinstance(refs, list) or not refs:
        return False, "SEMANTIC_EVIDENCE_REFS_EMPTY"
    by_id = {s.source_id: s for s in s1.sources}
    if any((not isinstance(x, int) or x not in by_id) for x in refs):
        return False, "SEMANTIC_EVIDENCE_REFS_INVALID"
    min_identity = safe(rules.semantic_policy.get("min_identity", "PROBABLE")).upper() or "PROBABLE"
    rank = DeterministicEngine.IDENTITY_RANK
    cited = [by_id[x] for x in refs]
    if not any(rank.get(src.relevance, 0) >= rank.get(min_identity, 3) for src in cited):
        return False, "SEMANTIC_IDENTITY_TOO_WEAK"
    if bool(rules.semantic_policy.get("require_grounded_evidence_phrase", True)):
        phrase = norm_text(data.get("evidence_phrase"))
        if len(phrase) < 4:
            return False, "SEMANTIC_EVIDENCE_PHRASE_EMPTY"
        grounded = any(phrase in norm_text(src.text) for src in cited)
        if not grounded:
            return False, "SEMANTIC_EVIDENCE_PHRASE_NOT_GROUNDED"
    return True, "OK"


def semantic_taxonomy_result(
    rules: Rules,
    s1: Stage1Result,
    canonical_type: str,
    raw_type: str,
    power: str,
    refs: Sequence[int],
    brand: str,
    model: str,
) -> Tuple[str, str, str]:
    by_id = {s.source_id: s for s in s1.sources}
    chunks = [by_id[x].text for x in refs if x in by_id and by_id[x].text]
    # O TYPE_RAW da LLM funciona como ponte semântica para evidence rules,
    # mas nunca cria Category/FAMILY fora do JSON.
    chunks.append(f"SEMANTIC_TYPE_RAW: {safe(raw_type)}")
    evidence = "\n---SOURCE---\n".join(chunks)
    return rules.map_taxonomy(canonical_type, power or "UNKNOWN", evidence, brand, model)


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
        # resultados de outra política LLM.
        material = f"{PIPELINE_VERSION}|{PROMPT_VERSION}|{model}|{key}|{raw_hash}|{rules_hash}"
        return hashlib.sha256(material.encode()).hexdigest()

    def get_checkpoint(self, key: str, raw_hash: str, rules_hash: str, model: str) -> Optional[Dict[str, Any]]:
        cid = self.checkpoint_id(key, raw_hash, rules_hash, model)
        row = self.conn.execute("SELECT result_json FROM classification_results WHERE checkpoint_id=?", (cid,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None


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

    def delete_llm(
        self,
        key: str,
        raw_hash: str,
        rules_hash: str,
        provider: str,
        model: str,
    ) -> None:
        cid = self.llm_cache_id(key, raw_hash, rules_hash, provider, model)
        self.conn.execute("DELETE FROM llm_cache WHERE cache_id=?", (cid,))
        self.conn.commit()

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
# OpenAI-compatible Stage 2 — Groq
# -----------------------------------------------------------------------------
class DualWindowLimiter:
    """Limiter conservador para RPM + TPM usando janela móvel de 60 segundos.

    O consumo de tokens da próxima chamada é estimado antes do POST. A API continua
    sendo a autoridade final; headers e 429 são tratados separadamente.
    """

    def __init__(self, rpm: int, tpm: int, safety_margin: float = 0.90):
        self.rpm = max(1, int(rpm))
        self.tpm = max(1, int(tpm))
        self.margin = min(1.0, max(0.50, float(safety_margin)))
        self.effective_rpm = max(1, int(self.rpm * self.margin))
        self.effective_tpm = max(1, int(self.tpm * self.margin))
        self.requests: deque = deque()
        self.tokens: deque = deque()

    def _prune(self, now: float) -> None:
        while self.requests and now - self.requests[0] >= 60.0:
            self.requests.popleft()
        while self.tokens and now - self.tokens[0][0] >= 60.0:
            self.tokens.popleft()

    def wait(self, estimated_tokens: int) -> None:
        estimated_tokens = max(1, int(estimated_tokens))
        while True:
            now = time.monotonic()
            self._prune(now)

            used_tokens = sum(x[1] for x in self.tokens)
            req_ok = len(self.requests) < self.effective_rpm
            tok_ok = used_tokens + estimated_tokens <= self.effective_tpm

            # Uma única chamada maior que o budget efetivo não pode ficar em loop eterno.
            if estimated_tokens > self.effective_tpm:
                raise OperationalPause(
                    "GROQ_SINGLE_REQUEST_TOO_LARGE",
                    f"Prompt estimado={estimated_tokens} tokens excede TPM operacional="
                    f"{self.effective_tpm}. Reduza contexto/evidências."
                )

            if req_ok and tok_ok:
                self.requests.append(now)
                self.tokens.append((now, estimated_tokens))
                return

            waits = []
            if not req_ok and self.requests:
                waits.append(max(0.1, 60.0 - (now - self.requests[0]) + 0.05))
            if not tok_ok and self.tokens:
                waits.append(max(0.1, 60.0 - (now - self.tokens[0][0]) + 0.05))
            wait_for = min(waits) if waits else 1.0
            LOG.info(
                "GROQ LIMITER: aguardando %.1fs | req_window=%d/%d | token_est_window=%d/%d",
                wait_for, len(self.requests), self.effective_rpm,
                used_tokens, self.effective_tpm,
            )
            time.sleep(wait_for)


class OpenAICompatibleClassifier:
    """Stage 2 via provider OpenAI-compatible.

    Providers:
    - nvidia     / openai/gpt-oss-20b (primário, com failover de modelo)
    - groq       / openai/gpt-oss-120b
    - openrouter / opcional; fora da rota padrão enquanto bloqueado pelo T.I.
    """

    def __init__(
        self,
        rules: Rules,
        cfg: Dict[str, Any],
        db: ClassificationDB,
        provider: str,
        model: str,
    ):
        self.rules = rules
        self.cfg = cfg
        self.db = db
        self.provider = provider.casefold().strip()
        self.model = model

        if self.provider not in {"groq", "nvidia", "openrouter"}:
            raise OperationalPause(
                "LLM_PROVIDER_UNSUPPORTED",
                f"Provider '{provider}' não está habilitado nesta versão."
            )

        pcfg = cfg.get(self.provider, {}) or {}

        if self.provider == "groq":
            self.api_key_env = "GROQ_API_KEY"
            self.base_url = (
                os.getenv("GROQ_BASE_URL", "").strip()
                or safe(pcfg.get("base_url"))
                or "https://api.groq.com/openai/v1"
            ).rstrip("/")
            self.model_candidates = [self.model]

        elif self.provider == "nvidia":
            self.api_key_env = "NVIDIA_API_KEY"
            self.base_url = (
                os.getenv("NVIDIA_BASE_URL", "").strip()
                or safe(pcfg.get("base_url"))
                or "https://integrate.api.nvidia.com/v1"
            ).rstrip("/")

            env_candidates = [
                x.strip()
                for x in os.getenv("NVIDIA_MODEL_CANDIDATES", "").split(",")
                if x.strip()
            ]
            config_candidates = [
                safe(x)
                for x in (pcfg.get("model_candidates", []) or [])
                if safe(x)
            ]
            defaults = [
                "openai/gpt-oss-20b",
                "openai/gpt-oss-120b",
            ]
            ordered = [self.model] + env_candidates + config_candidates + defaults
            self.model_candidates = []
            for candidate in ordered:
                candidate = safe(candidate)
                if candidate and candidate not in self.model_candidates:
                    self.model_candidates.append(candidate)
            # Preflight preencherá somente os modelos realmente servidos agora.
            self.available_model_candidates: List[str] = []

        else:  # OpenRouter
            self.api_key_env = "OPENROUTER_API_KEY"
            self.base_url = (
                os.getenv("OPENROUTER_BASE_URL", "").strip()
                or safe(pcfg.get("base_url"))
                or "https://openrouter.ai/api/v1"
            ).rstrip("/")

            # Mantemos um modelo fixo para não misturar qualidade entre modelos.
            # O OpenRouter já faz failover entre provedores que servem o MESMO modelo.
            self.require_free_model = bool(pcfg.get("require_free_model", True))
            self.model_candidates = [self.model]

        self.account_requests_per_minute = int(
            pcfg.get("requests_per_minute", 30 if self.provider == "groq" else 0)
        )
        self.account_tokens_per_minute = int(
            pcfg.get("tokens_per_minute", 8000 if self.provider == "groq" else 0)
        )

        self.operational_requests_per_minute = float(
            pcfg.get("operational_requests_per_minute", 3.0)
        )
        self.limiter = RateLimiter(self.operational_requests_per_minute)

        # Groq aplica simultaneamente RPM e TPM. O RateLimiter sozinho controla
        # apenas a cadência de requests; o segundo guard reserva também o budget
        # estimado de tokens na janela móvel de 60s para evitar 429 por TPM.
        self.groq_window_limiter: Optional[DualWindowLimiter] = None
        if (
            self.provider == "groq"
            and self.account_requests_per_minute > 0
            and self.account_tokens_per_minute > 0
        ):
            self.groq_window_limiter = DualWindowLimiter(
                self.account_requests_per_minute,
                self.account_tokens_per_minute,
                safety_margin=float(pcfg.get("tpm_safety_margin", 0.88)),
            )

        self.requests_per_day = int(
            pcfg.get("requests_per_day", 1000 if self.provider == "groq" else 0)
        )
        self.tokens_per_day = int(
            pcfg.get("tokens_per_day", 200000 if self.provider == "groq" else 0)
        )
        self.max_calls_per_run = max(
            0, int(pcfg.get("max_api_calls_per_run", 1000))
        )
        self.max_total_tokens_per_run = max(
            0, int(pcfg.get("max_total_tokens_per_run", 0))
        )

        # NVIDIA precisa falhar rápido para não travar lotes grandes.
        # Groq/OpenRouter preservam os limites históricos.
        default_timeout = 60 if self.provider == "nvidia" else 90
        default_retries = 2 if self.provider == "nvidia" else 4
        default_backoff = [2, 5] if self.provider == "nvidia" else [2, 5, 10, 20]
        self.timeout_seconds = max(
            10, int(pcfg.get("timeout_seconds", default_timeout))
        )
        self.retry_attempts = max(
            1, int(pcfg.get("retry_attempts", default_retries))
        )
        self.retry_backoff = list(
            pcfg.get("retry_backoff_seconds", default_backoff)
            or default_backoff
        )
        self.max_output_tokens = max(
            64, int(pcfg.get("max_output_tokens", 1024))
        )
        self.json_retry_max_completion_tokens = max(
            self.max_output_tokens,
            int(pcfg.get("json_retry_max_completion_tokens", 2048)),
        )
        self.reasoning_effort = safe(
            pcfg.get("reasoning_effort", "low")
        ) or "low"

        self.evidence_content_chars = max(
            250, int(pcfg.get("evidence_content_chars", 500))
        )
        self.max_llm_sources = max(
            1, int(pcfg.get("max_llm_sources", 2))
        )
        self.max_aliases_per_type = max(
            3, int(pcfg.get("max_aliases_per_type", 6))
        )
        self.max_negative_aliases_per_type = max(
            2, int(pcfg.get("max_negative_aliases_per_type", 4))
        )

        self.api_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"

    def _discover_nvidia_model(self) -> Dict[str, Any]:
        """Consulta a API NVIDIA e prepara a cadeia de modelos servidos agora."""
        import requests

        url = self.base_url + "/models"
        headers = {
            "Authorization": f"Bearer {os.environ[self.api_key_env].strip()}",
            "Accept": "application/json",
        }

        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=min(self.timeout_seconds, 30),
            )
        except requests.RequestException as exc:
            raise OperationalPause(
                "NVIDIA_MODEL_DISCOVERY_NETWORK",
                f"Falha ao consultar {url}: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        if resp.status_code != 200:
            raise self._operational_error(
                resp.status_code,
                safe(resp.text)[:3000],
            )

        try:
            payload = resp.json()
            available = {
                safe(row.get("id"))
                for row in (payload.get("data", []) or [])
                if safe(row.get("id"))
            }
        except Exception as exc:
            raise OperationalPause(
                "NVIDIA_MODEL_DISCOVERY_PARSE",
                f"Resposta inválida de GET /models: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        if not available:
            raise OperationalPause(
                "NVIDIA_NO_MODELS",
                "GET /v1/models não retornou modelos disponíveis.",
            )

        requested = self.model
        served_candidates = [
            candidate
            for candidate in self.model_candidates
            if candidate in available
        ]
        if not served_candidates:
            interesting = sorted(
                x for x in available
                if any(k in x.casefold() for k in ["nemotron", "llama", "gpt-oss"])
            )[:30]
            raise OperationalPause(
                "NVIDIA_NO_COMPATIBLE_MODEL",
                "Nenhum candidato configurado está disponível na API NVIDIA. "
                f"Candidatos={self.model_candidates}. "
                f"Disponíveis relacionados={interesting}",
            )

        self.available_model_candidates = served_candidates

        if requested in served_candidates:
            selected = requested
            auto_selected = False
        else:
            selected = served_candidates[0]
            auto_selected = True
            self.model = selected
            LOG.warning(
                "NVIDIA MODEL AUTO-SELECT | configurado=%s indisponível | selecionado=%s",
                requested,
                selected,
            )

        return {
            "requested_model": requested,
            "selected_model": selected,
            "auto_selected": auto_selected,
            "available_count": len(available),
            "candidate_models": list(served_candidates),
        }

    def advance_nvidia_model(self, reason_code: str) -> bool:
        """Troca para o próximo modelo NVIDIA já validado pelo preflight."""
        if self.provider != "nvidia":
            return False

        candidates = list(getattr(self, "available_model_candidates", []) or [])
        if not candidates:
            return False

        try:
            current_index = candidates.index(self.model)
        except ValueError:
            current_index = -1

        for candidate in candidates[current_index + 1:]:
            candidate = safe(candidate)
            if not candidate or candidate == self.model:
                continue
            old = self.model
            self.model = candidate
            LOG.warning(
                "NVIDIA MODEL FAILOVER | %s -> %s | motivo=%s",
                old,
                candidate,
                reason_code,
            )
            return True
        return False

    def route_identity_fragment(self) -> str:
        """Identidade auditável da política de modelos do provider."""
        if self.provider == "nvidia":
            candidates = list(
                getattr(self, "available_model_candidates", []) or self.model_candidates
            )
            return "nvidia:[" + ",".join(candidates) + "]"
        return f"{self.provider}:{self.model}"

    def _discover_groq_model(self) -> Dict[str, Any]:
        """Valida credencial e modelo Groq antes de entrar na rota do piloto.

        p8.4: possuir GROQ_API_KEY no .env não é suficiente; a chave precisa
        autenticar de fato. Um 401/403 aqui desabilita o fallback antes da
        primeira inferência, evitando derrubar o lote depois de uma falha NVIDIA.
        """
        import requests

        headers = {
            "Authorization": f"Bearer {os.environ[self.api_key_env].strip()}",
            "Accept": "application/json",
        }
        models_url = self.base_url + "/models"
        try:
            resp = requests.get(
                models_url,
                headers=headers,
                timeout=min(self.timeout_seconds, 30),
            )
        except requests.RequestException as exc:
            raise OperationalPause(
                "GROQ_PREFLIGHT_NETWORK",
                f"Falha de rede ao validar Groq em GET /models: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        detail = safe(resp.text)[:1500]
        if resp.status_code in {401, 403}:
            raise OperationalPause(
                "GROQ_AUTH",
                f"Groq rejeitou a credencial no preflight (HTTP {resp.status_code}). Atualize GROQ_API_KEY ou remova Groq da rota do piloto.",
            )
        if resp.status_code == 429:
            raise OperationalPause(
                "GROQ_RATE_LIMIT",
                f"Groq indisponível no preflight por rate/quota limit: {detail}",
            )
        if resp.status_code != 200:
            raise OperationalPause(
                f"GROQ_PREFLIGHT_HTTP_{resp.status_code}",
                f"Groq GET /models falhou no preflight: {detail}",
            )

        try:
            payload = resp.json() or {}
            available = {
                safe(row.get("id"))
                for row in (payload.get("data", []) or [])
                if safe(row.get("id"))
            }
        except Exception as exc:
            raise OperationalPause(
                "GROQ_PREFLIGHT_PARSE",
                f"Groq GET /models retornou JSON inválido: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        if self.model not in available:
            raise OperationalPause(
                "GROQ_MODEL_UNAVAILABLE",
                f"Modelo Groq configurado não está disponível: {self.model}",
            )

        return {
            "requested_model": self.model,
            "selected_model": self.model,
            "available_count": len(available),
            "auth_validated": True,
        }

    def _discover_openrouter_model(self) -> Dict[str, Any]:
        """Valida chave/modelo diretamente no catálogo vivo do OpenRouter.

        Para este projeto o default é fixo em openai/gpt-oss-120b:free. Não usa
        openrouter/free, porque esse router pode trocar de modelo e dificultar a
        auditoria da taxa de acerto.
        """
        import requests

        headers = {
            "Authorization": f"Bearer {os.environ[self.api_key_env].strip()}",
            "Accept": "application/json",
        }
        models_url = self.base_url + "/models"
        key_url = self.base_url + "/key"

        try:
            resp = requests.get(
                models_url,
                headers=headers,
                timeout=min(self.timeout_seconds, 30),
            )
        except requests.RequestException as exc:
            raise OperationalPause(
                "OPENROUTER_MODEL_DISCOVERY_NETWORK",
                f"Falha ao consultar {models_url}: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        if resp.status_code != 200:
            raise self._operational_error(resp.status_code, safe(resp.text)[:3000])

        try:
            payload = resp.json()
            rows = payload.get("data", []) or []
            by_id = {
                safe(row.get("id")): row
                for row in rows
                if safe(row.get("id"))
            }
        except Exception as exc:
            raise OperationalPause(
                "OPENROUTER_MODEL_DISCOVERY_PARSE",
                f"Resposta inválida de GET /models: {type(exc).__name__}: {safe(exc)}",
            ) from exc

        requested = self.model
        row = by_id.get(requested)
        if row is None:
            related = sorted(
                mid for mid in by_id
                if any(k in mid.casefold() for k in ["gpt-oss-120b", "gpt-oss-20b", "nemotron"])
                and mid.endswith(":free")
            )[:30]
            raise OperationalPause(
                "OPENROUTER_MODEL_UNAVAILABLE",
                f"Modelo configurado não aparece em GET /models: {requested}. "
                f"Free relacionados disponíveis={related}",
            )

        pricing = row.get("pricing", {}) or {}
        prompt_price = safe(pricing.get("prompt"))
        completion_price = safe(pricing.get("completion"))
        request_price = safe(pricing.get("request"))
        is_free_slug = requested.endswith(":free")
        def _price_is_zero(value: str) -> bool:
            value = safe(value)
            if not value:
                return True
            try:
                return float(value) == 0.0
            except Exception:
                return False

        is_zero_priced = all(
            _price_is_zero(x)
            for x in [prompt_price, completion_price, request_price]
        )
        if getattr(self, "require_free_model", True) and not (is_free_slug and is_zero_priced):
            raise OperationalPause(
                "OPENROUTER_MODEL_NOT_FREE",
                f"O projeto exige endpoint gratuito, mas o modelo configurado não foi validado como free: {requested}",
            )

        supported = {safe(x) for x in (row.get("supported_parameters", []) or [])}
        structured = "response_format" in supported
        if not structured:
            raise OperationalPause(
                "OPENROUTER_STRUCTURED_OUTPUT_UNSUPPORTED",
                f"Modelo {requested} não anuncia response_format em GET /models; "
                "não será usado para evitar regressão de JSON.",
            )

        key_info: Dict[str, Any] = {}
        try:
            kres = requests.get(
                key_url,
                headers=headers,
                timeout=min(self.timeout_seconds, 30),
            )
            if kres.status_code == 200:
                kd = (kres.json() or {}).get("data", {}) or {}
                key_info = {
                    "is_free_tier": bool(kd.get("is_free_tier", False)),
                    "limit_remaining": kd.get("limit_remaining"),
                    "usage_daily": kd.get("usage_daily"),
                }
        except Exception:
            # /key é telemetria auxiliar; falha aqui não invalida a inferência.
            key_info = {}

        return {
            "requested_model": requested,
            "selected_model": requested,
            "auto_selected": False,
            "available_count": len(by_id),
            "free_validated": True,
            "structured_output": True,
            "key_info": key_info,
        }

    def preflight(self) -> Dict[str, Any]:
        """Pre-flight local; NVIDIA/OpenRouter validam modelo no catálogo vivo."""
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            raise OperationalPause(
                f"{self.provider.upper()}_REQUESTS_MISSING",
                "Pacote requests não instalado. Instale requirements.txt.",
            ) from exc

        if not os.getenv(self.api_key_env, "").strip():
            raise OperationalPause(
                f"{self.provider.upper()}_API_KEY_MISSING",
                f"{self.api_key_env} não configurada no .env."
            )

        if not self.base_url.startswith("https://"):
            raise OperationalPause(
                f"{self.provider.upper()}_BASE_URL_INVALID",
                f"Base URL inválida: {self.base_url!r}"
            )

        discovery = None
        if self.provider == "nvidia":
            discovery = self._discover_nvidia_model()
        elif self.provider == "groq":
            discovery = self._discover_groq_model()
        elif self.provider == "openrouter":
            discovery = self._discover_openrouter_model()

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "account_rpm": self.account_requests_per_minute,
            "account_tpm": self.account_tokens_per_minute,
            "operational_rpm": self.operational_requests_per_minute,
            "requests_per_day": self.requests_per_day,
            "tokens_per_day": self.tokens_per_day,
            "run_cap": self.max_calls_per_run,
            "token_cap": self.max_total_tokens_per_run,
            "model_discovery": discovery,
        }


    def schema(self, s1: Stage1Result, allowed_types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Structured Output fechado nos candidatos sobreviventes do Stage 1.5.

        Groq/OpenRouter nem conseguem gerar TYPE/POWER fora do conjunto
        determinístico; NVIDIA continua protegida pelo validator Python.
        """
        return {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["RESOLVED", "INSUFFICIENT_EVIDENCE", "CONFLICT"],
                },
                "product_type": {
                    "type": "string",
                    "enum": sorted(set(allowed_types or s1.type_candidates) | {"REVISAR"}),
                },
                "power": {
                    "type": "string",
                    "enum": sorted(set(s1.power_candidates) | {"UNKNOWN"}),
                },
                "evidence_source_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "reason_short": {"type": "string"},
            },
            "required": [
                "decision",
                "product_type",
                "power",
                "evidence_source_ids",
                "reason_short",
            ],
            "additionalProperties": False,
        }

    @staticmethod
    def _tokens(text: str) -> set:
        return {
            x for x in re.findall(r"\w+", norm_text(text), flags=re.UNICODE)
            if len(x) >= 3
        }

    def _context_type_names(self, brand: str, model: str, s1: Stage1Result) -> List[str]:
        """Stage 1.5: LLM vê somente os candidatos sobreviventes do funil."""
        funnel = candidate_funnel(self.rules, s1)
        return [typ for typ in funnel.candidates if typ in self.rules.types]

    def _rules_context(
        self, brand: str, model: str, s1: Stage1Result
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Pacote mínimo de regras do JSON para o fallback LLM."""
        selected = self._context_type_names(brand, model, s1)
        selected_set = set(selected)

        evidence_text = "\n".join(
            [brand, model]
            + [
                f"{s.title}\n{s.content[:self.evidence_content_chars]}"
                for s in s1.sources[: self.max_llm_sources]
            ]
        )
        e_tokens = self._tokens(evidence_text)

        def rank_aliases(values: List[str], limit: int) -> List[str]:
            ranked = []
            for alias in values or []:
                alias = safe(alias)
                if not alias:
                    continue
                a_tokens = self._tokens(alias)
                overlap = len(a_tokens & e_tokens)
                literal = 1 if norm_text(alias) in norm_text(evidence_text) else 0
                ranked.append((literal, overlap, len(a_tokens), alias))
            ranked.sort(reverse=True)
            return [x[-1] for x in ranked[:limit]]

        type_rules: Dict[str, Any] = {}
        for typ in selected:
            raw = self.rules.types[typ]
            type_rules[typ] = {
                "aliases": rank_aliases(
                    list(raw.get("aliases", []) or []),
                    self.max_aliases_per_type,
                ),
                "negative_aliases": rank_aliases(
                    list(raw.get("negative_aliases", []) or []),
                    self.max_negative_aliases_per_type,
                ),
            }

        collision_all = (
            self.rules.raw.get("RESOLUTION_POLICY", {}) or {}
        ).get("alias_collision_policy", {}) or {}
        ntext = norm_text(evidence_text)
        collision_relevant = {
            alias: target
            for alias, target in collision_all.items()
            if safe(target) in selected_set or norm_text(alias) in ntext
        }

        context = {
            "ALLOWED_PRODUCT_TYPES": list(selected),
            "RELEVANT_TYPE_RULES": type_rules,
            "POWER_RULES": {p: self.rules.power_rules[p] for p in s1.power_candidates if p in self.rules.power_rules},
            "TYPE_POLICY": self.rules.type_policy,
            "COMPONENT_CONTEXT_RULES": self.rules.component_context,
            "SOURCE_POLICY": {
                "notes": safe(self.rules.source_rules.get("notes")),
                "replacement_compatibility_rule": safe(
                    self.rules.type_policy.get("replacement_compatibility_rule")
                ),
                "source_conflict_rule": safe(
                    self.rules.type_policy.get("source_conflict_rule")
                ),
                "output_voltage_rule": safe(
                    self.rules.type_policy.get("output_voltage_rule")
                ),
                "agricultural_mower_rule": safe(
                    self.rules.type_policy.get("agricultural_mower_rule")
                ),
            },
            "ALIAS_COLLISIONS_RELEVANT": collision_relevant,
            "ALLOWED_POWER": sorted(set(s1.power_candidates) | {"UNKNOWN"}),
        }
        return context, selected

    def _messages(
        self, brand: str, model: str, s1: Stage1Result
    ) -> Tuple[List[Dict[str, str]], List[str], Dict[int, int]]:
        rules_context, selected_types = self._rules_context(brand, model, s1)

        # Stage 1 atual usa identidade graduada. Os rótulos antigos
        # BRAND_MODEL/MODEL/BRAND/QUERY_CONTEXT não podem ordenar estas fontes.
        relevance_rank = {
            "STRONG": 0,
            "PROBABLE": 1,
            "CONTEXTUAL": 2,
            "WEAK": 3,
            "CONFLICT": 4,
        }
        ranked_sources = sorted(
            s1.sources,
            key=lambda s: (
                relevance_rank.get(s.relevance, 9),
                1 if s.quality_flags else 0,
                0 if (s.type_hits or s.power_hits) else 1,
                s.source_id,
            ),
        )[: self.max_llm_sources]

        # IDs do prompt são SEMPRE 1..N. Isso elimina a ambiguidade criada
        # quando as fontes ranqueadas eram, por exemplo, [1, 3, 5].
        prompt_to_source_id: Dict[int, int] = {}
        sources = []
        for prompt_id, s in enumerate(ranked_sources, 1):
            prompt_to_source_id[prompt_id] = s.source_id
            sources.append(
                {
                    "id": prompt_id,
                    "relevance": s.relevance,
                    "quality_flags": s.quality_flags,
                    "title": s.title,
                    "content": s.content[: self.evidence_content_chars],
                }
            )

        system = (
            "You are an evidence arbiter, not an open-ended product classifier. "
            "The deterministic engine already produced the only product types you may choose from. "
            "Use only the supplied Tavily evidence; do not use outside knowledge. "
            "Do not create, translate, repair, broaden, or infer a product type outside ALLOWED_PRODUCT_TYPES. "
            "Do not classify Category General or FAMILY. "
            "Choose RESOLVED only when exactly one allowed type is clearly supported by target evidence. "
            "If evidence supports multiple products, a kit/combo, a compatible/replacement item, another model, "
            "or is insufficient, return INSUFFICIENT_EVIDENCE or CONFLICT. "
            "Power must be UNKNOWN or one exact value from ALLOWED_POWER. "
            "Cite at least one supplied evidence source id that textually supports the chosen product_type. "
            "Evidence IDs are local and contiguous (1..N); never invent another ID. "
            "Respect relevance and quality_flags. A phrase such as 'without battery', 'tool only', "
            "or 'battery sold separately' does not mean the physical product is a battery. "
            "Return only the requested JSON object."
        )

        payload = {
            "product": {"brand": brand, "model": model},
            "stage1": {
                "state": s1.state,
                "type_candidates": selected_types,
                "power_candidates": s1.power_candidates,
            },
            "business_rules": rules_context,
            "tavily_evidence": sources,
            "output": {
                "fields": [
                    "decision",
                    "product_type",
                    "power",
                    "evidence_source_ids",
                    "reason_short",
                ]
            },
        }

        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            },
        ], selected_types, prompt_to_source_id

    @staticmethod
    def _is_json_completion_limit_error(status: int, detail: str) -> bool:
        if status != 400:
            return False
        folded = safe(detail).casefold()
        return (
            "json_validate_failed" in folded
            and "max completion tokens reached" in folded
        )

    def _quota_code(self, status: int, detail: str) -> Optional[str]:
        if status != 429:
            return None

        folded = safe(detail).casefold()

        if self.provider == "groq":
            if "tokens per day" in folded or "(tpd)" in folded:
                return "GROQ_TPD_EXHAUSTED"
            return None

        if self.provider == "nvidia":
            # NVIDIA pode variar a mensagem de quota/crédito.
            if any(
                phrase in folded
                for phrase in [
                    "quota exhausted",
                    "quota exceeded",
                    "credits exhausted",
                    "credit balance",
                    "daily quota",
                    "daily limit",
                ]
            ):
                return "NVIDIA_QUOTA_EXHAUSTED"
            return None

        if self.provider == "openrouter":
            # RPM/provider congestion é retryable. Só troca/pausa como quota
            # quando a mensagem aponta explicitamente para limite diário.
            if any(phrase in folded for phrase in [
                "requests per day",
                "request per day",
                "daily limit",
                "daily quota",
                "free-models-per-day",
                "free models per day",
            ]):
                return "OPENROUTER_QUOTA_EXHAUSTED"
            return None

        return None

    def _operational_error(
        self, status: int, detail: str
    ) -> OperationalPause:
        p = self.provider.upper()

        if status == 401:
            return OperationalPause(
                f"{p}_AUTH", f"{p} 401 AUTH: {detail}"
            )
        if status == 403:
            return OperationalPause(
                f"{p}_PERMISSION", f"{p} 403 PERMISSION: {detail}"
            )
        if status == 402 and self.provider == "openrouter":
            return OperationalPause(
                "OPENROUTER_CREDIT_LIMIT",
                f"OPENROUTER 402 CREDIT/LIMIT: {detail}",
            )
        if status == 429:
            quota_code = self._quota_code(status, detail)
            if quota_code:
                return OperationalPause(quota_code, detail)
            return OperationalPause(
                f"{p}_RATE_LIMIT", f"{p} 429 RATE_LIMIT: {detail}"
            )
        if self.provider == "nvidia" and status == 410:
            return OperationalPause(
                "NVIDIA_MODEL_EOL",
                f"NVIDIA informou modelo indisponível/EOL: {self.model} | {detail}",
            )
        if status in {408, 500, 502, 503, 504}:
            return OperationalPause(
                f"{p}_TRANSIENT", f"{p} {status} TRANSIENT: {detail}"
            )

        return OperationalPause(
            f"{p}_FATAL", f"{p} HTTP {status}: {detail}"
        )


    def _post(self, payload: Dict[str, Any]):
        import requests

        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {os.environ[self.api_key_env].strip()}",
            "Content-Type": "application/json",
        }
        if self.provider == "openrouter":
            # Opcionais segundo a documentação; úteis apenas para atribuição.
            referer = os.getenv("OPENROUTER_SITE_URL", "").strip()
            app_title = os.getenv("OPENROUTER_APP_TITLE", "").strip()
            if referer:
                headers["HTTP-Referer"] = referer
            if app_title:
                headers["X-OpenRouter-Title"] = app_title

        for attempt in range(1, self.retry_attempts + 1):
            if attempt > 1:
                # Retry HTTP também é um novo request real.
                self._wait_provider_limits(payload)
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self.retry_attempts:
                    raise OperationalPause(
                        f"{self.provider.upper()}_TRANSIENT",
                        f"Falha de rede após {attempt} tentativa(s): "
                        f"{type(exc).__name__}: {safe(exc)}",
                    ) from exc
                wait = float(
                    self.retry_backoff[
                        min(attempt - 1, len(self.retry_backoff) - 1)
                    ]
                )
                LOG.warning(
                    "%s rede/transiente | tentativa=%d/%d | retry em %.1fs",
                    self.provider.upper(),
                    attempt,
                    self.retry_attempts,
                    wait,
                )
                time.sleep(wait)
                continue

            self._sync_tpm_from_headers(resp)

            if resp.status_code == 200:
                return resp

            detail = safe(resp.text)[:3000]

            quota_code = self._quota_code(resp.status_code, detail)
            if quota_code:
                # Não aguarda 10/15 minutos quando o TPD acabou:
                # entrega imediatamente ao próximo provider.
                raise OperationalPause(quota_code, detail)

            # Specific GPT-OSS structured-output failure:
            # reasoning/output budget ended before a complete JSON document existed.
            # This is recoverable by increasing ONLY the completion budget for the
            # same product. It must not become REVIEW and must not stop the batch.
            if self._is_json_completion_limit_error(resp.status_code, detail):
                token_field = (
                    "max_completion_tokens"
                    if self.provider == "groq"
                    else "max_tokens"
                )
                current_cap = int(
                    payload.get(token_field, self.max_output_tokens)
                )
                if (
                    current_cap < self.json_retry_max_completion_tokens
                    and attempt < self.retry_attempts
                ):
                    new_cap = min(
                        self.json_retry_max_completion_tokens,
                        max(1024, current_cap * 2),
                    )
                    payload[token_field] = new_cap
                    LOG.warning(
                        "%s JSON RETRY | produto não concluído dentro de %d tokens "
                        "| novo max_completion_tokens=%d | tentativa=%d/%d",
                        self.provider.upper(),
                        current_cap,
                        new_cap,
                        attempt + 1,
                        self.retry_attempts,
                    )
                    time.sleep(1.0)
                    continue

                raise OperationalPause(
                    f"{self.provider.upper()}_JSON_TOKEN_LIMIT",
                    f"{self.provider.upper()} não concluiu o JSON mesmo com "
                    f"max_completion_tokens={current_cap}: {detail}",
                )

            retryable = resp.status_code in {408, 429, 500, 502, 503, 504}
            if not retryable or attempt >= self.retry_attempts:
                raise self._operational_error(resp.status_code, detail)

            retry_after = safe(resp.headers.get("Retry-After"))
            try:
                wait = max(1.0, float(retry_after)) if retry_after else float(
                    self.retry_backoff[
                        min(attempt - 1, len(self.retry_backoff) - 1)
                    ]
                )
            except Exception:
                wait = float(
                    self.retry_backoff[
                        min(attempt - 1, len(self.retry_backoff) - 1)
                    ]
                )

            detail_short = re.sub(r"\s+", " ", detail)[:600]
            LOG.warning(
                "%s HTTP %s | tentativa=%d/%d | retry em %.1fs | %s",
                self.provider.upper(),
                resp.status_code,
                attempt,
                self.retry_attempts,
                wait,
                detail_short,
            )
            time.sleep(wait)

        raise OperationalPause(
            f"{self.provider.upper()}_TRANSIENT",
            f"Falha transiente {self.provider.upper()} sem resposta final."
        )

    @staticmethod
    def _decode_model_json(content: str) -> Dict[str, Any]:
        """Decodifica a resposta do modelo sem alterar o conteúdo semântico.

        Aceita:
        - JSON puro;
        - JSON cercado por ```json ... ```;
        - texto residual antes/depois de um único objeto JSON.

        NÃO tenta "consertar" campos ou inventar classificação.
        """
        raw = safe(content).strip()
        if not raw:
            raise ValueError("Resposta sem conteúdo.")

        # Remove markdown fences, caso o modelo ignore a instrução de JSON puro.
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\\s*", "", raw, flags=re.I)
            raw = re.sub(r"\\s*```$", "", raw).strip()

        # 1) tentativa direta
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Resposta JSON não é objeto.")
            return data
        except json.JSONDecodeError:
            pass

        # 2) extrai o primeiro objeto delimitado por { ... }.
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            candidate = raw[first:last + 1]
            data = json.loads(candidate)
            if not isinstance(data, dict):
                raise ValueError("Resposta JSON extraída não é objeto.")
            return data

        raise ValueError("Nenhum objeto JSON válido encontrado.")

    @staticmethod
    def _header_snapshot(resp) -> Dict[str, str]:
        names = [
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-reset-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
            "retry-after",
        ]
        return {
            k: safe(resp.headers.get(k))
            for k in names
            if safe(resp.headers.get(k))
        }

    def _estimate_request_tokens(self, payload: Dict[str, Any]) -> int:
        """Estimativa conservadora usada somente para pacing local do Groq.

        O rate-limit do Groq considera o request inteiro, inclusive o orçamento
        máximo de completion. Usamos ~3 chars/token para não depender de tokenizer
        externo e adicionamos pequena margem estrutural.
        """
        messages = payload.get("messages", []) or []
        serialized = json.dumps(
            messages, ensure_ascii=False, separators=(",", ":"), default=str
        )
        prompt_estimate = max(1, (len(serialized) + 2) // 3)
        completion_budget = int(
            payload.get("max_completion_tokens")
            or payload.get("max_tokens")
            or self.max_output_tokens
            or 0
        )
        return prompt_estimate + max(0, completion_budget) + 128

    def _wait_provider_limits(self, payload: Dict[str, Any]) -> None:
        """Aplica pacing RPM e, no Groq, também a janela TPM."""
        self.limiter.wait(self.provider.upper())
        if self.groq_window_limiter is not None:
            estimated = self._estimate_request_tokens(payload)
            self.groq_window_limiter.wait(estimated)

    def _sync_tpm_from_headers(self, resp) -> None:
        """Headers permanecem registrados; o pacing preventivo é local."""
        return

    @staticmethod
    def _normalize_evidence_refs(
        data: Dict[str, Any],
        prompt_to_source_id: Dict[int, int],
    ) -> Tuple[Dict[str, Any], bool]:
        """Normaliza refs do LLM sem inventar evidência.

        NVIDIA JSON mode às vezes devolve "1" como string. Aceitamos apenas
        inteiros ou strings estritamente numéricas que apontem para IDs locais
        realmente presentes no prompt. Depois convertemos para source_id real.
        """
        out = dict(data or {})
        refs = out.get("evidence_source_ids", [])

        if refs is None:
            refs = []
        if not isinstance(refs, list):
            return out, False

        normalized_local: List[int] = []
        for raw in refs:
            if isinstance(raw, bool):
                return out, False
            if isinstance(raw, int):
                ref = raw
            elif isinstance(raw, float) and raw.is_integer():
                ref = int(raw)
            elif isinstance(raw, str) and raw.strip().isdigit():
                ref = int(raw.strip())
            else:
                return out, False

            if ref not in prompt_to_source_id:
                return out, False
            if ref not in normalized_local:
                normalized_local.append(ref)

        out["evidence_source_ids"] = [
            prompt_to_source_id[x] for x in normalized_local
        ]
        return out, True

    def _structured_json_call(
        self,
        *,
        cache_scope: str,
        key: str,
        raw_hash: str,
        schema_name: str,
        schema: Dict[str, Any],
        messages: List[Dict[str, str]],
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        """Chamada estruturada reutilizável para o piloto semântico.

        O namespace de cache é separado da classificação legada para impedir
        colisão entre respostas de prompts com contratos diferentes.
        """
        scoped_rules_hash = hashlib.sha256(
            f"{self.rules.hash}|{cache_scope}".encode("utf-8")
        ).hexdigest()
        cached = self.db.get_llm(key, raw_hash, scoped_rules_hash, self.provider, self.model)
        if cached is not None:
            data, usage = cached
            contract_errors = _json_schema_contract_errors(data, schema)
            if not contract_errors:
                return data, True, usage
            LOG.warning(
                "%s SEMANTIC CACHE INVALID | schema=%s | errors=%s | descartando e refazendo chamada",
                self.provider.upper(), schema_name, ",".join(contract_errors[:6]),
            )
            self.db.delete_llm(key, raw_hash, scoped_rules_hash, self.provider, self.model)

        if self.max_calls_per_run and self.api_calls >= self.max_calls_per_run:
            raise OperationalPause(
                f"{self.provider.upper()}_RUN_CAP",
                f"Limite local de {self.max_calls_per_run} chamadas {self.provider.upper()}/run atingido.",
            )
        if self.max_total_tokens_per_run and self.total_tokens >= self.max_total_tokens_per_run:
            raise OperationalPause(
                f"{self.provider.upper()}_LOCAL_TOKEN_CAP",
                f"Cap real de {self.max_total_tokens_per_run} tokens/run atingido. Consumidos={self.total_tokens}.",
            )

        self.api_calls += 1
        if self.provider in {"groq", "openrouter"}:
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
                "temperature": 0,
                "stream": False,
            }
            if self.provider == "groq":
                payload["reasoning_effort"] = self.reasoning_effort
                payload["max_completion_tokens"] = self.max_output_tokens
            else:
                payload["max_tokens"] = self.max_output_tokens
        else:
            # NVIDIA aceita JSON Object Mode, mas não recebe o JSON Schema pelo
            # response_format como Groq/OpenRouter. p8.3 validava um contrato que
            # o modelo nunca tinha visto explicitamente, o que podia gerar JSON
            # válido porém com campos ausentes. p8.4 injeta o schema completo no
            # último turno do prompt e continua validando em Python.
            nvidia_messages = [dict(m) for m in messages]
            schema_instruction = (
                "\n\nOUTPUT CONTRACT - return ONLY one JSON object that matches this JSON Schema exactly. "
                "Every required field must be present; use only the declared enum values; do not add fields.\n"
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
            if nvidia_messages and safe(nvidia_messages[-1].get("role")).casefold() == "user":
                nvidia_messages[-1]["content"] = safe(nvidia_messages[-1].get("content")) + schema_instruction
            else:
                nvidia_messages.append({"role": "user", "content": schema_instruction.strip()})
            payload = {
                "model": self.model,
                "messages": nvidia_messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "stream": False,
            }
            # NVIDIA GPT-OSS expõe reasoning_effort no endpoint de Chat Completions.
            # Manter LOW reduz raciocínio residual e reserva o orçamento de saída
            # para o JSON final do contrato semântico.
            if self.model.startswith("openai/gpt-oss-"):
                payload["reasoning_effort"] = self.reasoning_effort
            if self.model == "nvidia/nemotron-3.5-lightning-30b-a3b":
                payload["chat_template_kwargs"] = {"enable_thinking": False}

        parse_max_attempts = 2
        failed_parse_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        data: Optional[Dict[str, Any]] = None
        usage: Optional[Dict[str, Any]] = None
        last_error: Optional[Exception] = None

        for parse_attempt in range(1, parse_max_attempts + 1):
            self._wait_provider_limits(payload)
            resp = self._post(payload)
            try:
                envelope = resp.json()
                raw_usage = envelope.get("usage", {}) or {}
                current_usage = {
                    "prompt_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                }
                content = safe(envelope["choices"][0]["message"]["content"])
                data = self._decode_model_json(content)
                contract_errors = _json_schema_contract_errors(data, schema)
                if contract_errors:
                    raise ValueError("SEMANTIC_SCHEMA_INVALID: " + "; ".join(contract_errors[:8]))
                usage = {
                    **current_usage,
                    "rate_limit_headers": self._header_snapshot(resp),
                    "json_parse_retries": parse_attempt - 1,
                    "failed_parse_tokens": failed_parse_tokens["total_tokens"],
                }
                break
            except Exception as exc:
                last_error = exc
                try:
                    envelope_for_usage = resp.json()
                    raw_failed = envelope_for_usage.get("usage", {}) or {}
                    for k in failed_parse_tokens:
                        failed_parse_tokens[k] += int(raw_failed.get(k, 0) or 0)
                except Exception:
                    pass
                if parse_attempt < parse_max_attempts:
                    retry_payload = dict(payload)
                    retry_messages = list(payload.get("messages", []))
                    retry_messages.append({
                        "role": "user",
                        "content": "Return ONLY one syntactically valid JSON object matching the requested schema. No markdown or commentary.",
                    })
                    retry_payload["messages"] = retry_messages
                    payload = retry_payload
                    self.api_calls += 1
                    time.sleep(1.0)
                    continue
                raise OperationalPause(
                    f"{self.provider.upper()}_OUTPUT_PARSE",
                    f"Falha ao interpretar JSON semântico após {parse_max_attempts} tentativa(s): {type(exc).__name__}: {safe(exc)}",
                ) from exc

        if data is None or usage is None:
            raise OperationalPause(
                f"{self.provider.upper()}_OUTPUT_PARSE",
                f"Resposta semântica sem JSON válido: {safe(last_error)}",
            )

        self.prompt_tokens += usage["prompt_tokens"] + failed_parse_tokens["prompt_tokens"]
        self.completion_tokens += usage["completion_tokens"] + failed_parse_tokens["completion_tokens"]
        self.total_tokens += usage["total_tokens"] + failed_parse_tokens["total_tokens"]
        self.db.put_llm(
            key, raw_hash, scoped_rules_hash, self.provider, self.model, data, usage
        )
        return data, False, usage

    def semantic_contract_smoke(self) -> Dict[str, Any]:
        """Executa UMA inferência real antes do piloto para provar o contrato JSON.

        Diferente de GET /models, este teste valida o caminho que de fato falhou
        no p8.3: autenticação + inferência + JSON parse + aderência ao schema.
        Em NVIDIA, testa os candidatos servidos em ordem e fixa o primeiro que
        cumprir o contrato. Nenhum resultado do smoke permanece no cache.
        """
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["IDENTIFIED", "INSUFFICIENT_EVIDENCE", "CONFLICT"]},
                "product_type_raw": {"type": "string"},
                "product_role": {"type": "string", "enum": ["WHOLE_PRODUCT", "COMPONENT_PART", "ACCESSORY", "ASSEMBLY", "UNKNOWN"]},
                "domain_hint": {"type": "string", "enum": ["GARDEN_OUTDOOR", "INDUSTRIAL_HVAC", "AUTOMOTIVE", "AGRICULTURAL", "POWER_TOOL", "PUMP_FLUID", "ELECTRONICS", "OTHER", "UNKNOWN"]},
                "power": {"type": "string", "enum": sorted(set(self.rules.allowed_powers))},
                "evidence_source_ids": {"type": "array", "items": {"type": "integer"}},
                "evidence_phrase": {"type": "string"},
                "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                "reason_short": {"type": "string"},
            },
            "required": ["decision", "product_type_raw", "product_role", "domain_hint", "power", "evidence_source_ids", "evidence_phrase", "confidence", "reason_short"],
            "additionalProperties": False,
        }
        expected_phrase = "TEST PRODUCT"
        messages = [
            {
                "role": "system",
                "content": (
                    "Provider contract smoke test. Return only the requested JSON object. "
                    "Do not explain or add markdown."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return a valid contract object for the literal evidence TEST PRODUCT. "
                    "Use decision=IDENTIFIED, product_type_raw=TEST PRODUCT, "
                    "product_role=WHOLE_PRODUCT, domain_hint=OTHER, power=UNKNOWN, "
                    "evidence_source_ids=[1], evidence_phrase=TEST PRODUCT, "
                    "confidence=HIGH, reason_short=provider contract smoke."
                ),
            },
        ]

        original_model = self.model
        candidates = [self.model]
        if self.provider == "nvidia":
            candidates = list(getattr(self, "available_model_candidates", []) or self.model_candidates or [self.model])

        errors: List[str] = []
        for candidate in candidates:
            self.model = candidate
            smoke_scope = SEMANTIC_PROMPT_VERSION + "-live-provider-smoke"
            smoke_key = "__SEMANTIC_PROVIDER_SMOKE__"
            smoke_raw_hash = hashlib.sha256((self.provider + "|" + candidate + "|" + expected_phrase).encode("utf-8")).hexdigest()
            scoped_rules_hash = hashlib.sha256(
                f"{self.rules.hash}|{smoke_scope}".encode("utf-8")
            ).hexdigest()
            # Força inferência AO VIVO em toda execução; cache não pode mascarar provider quebrado.
            self.db.delete_llm(smoke_key, smoke_raw_hash, scoped_rules_hash, self.provider, candidate)
            started = time.monotonic()
            try:
                data, _hit, usage = self._structured_json_call(
                    cache_scope=smoke_scope,
                    key=smoke_key,
                    raw_hash=smoke_raw_hash,
                    schema_name="semantic_provider_contract_smoke",
                    schema=schema,
                    messages=messages,
                )
                if safe(data.get("decision")).upper() != "IDENTIFIED":
                    raise OperationalPause(
                        f"{self.provider.upper()}_STRUCTURED_SMOKE_INVALID",
                        f"Smoke respondeu decision={safe(data.get('decision'))!r}.",
                    )
                if safe(data.get("product_type_raw")).upper() != expected_phrase:
                    raise OperationalPause(
                        f"{self.provider.upper()}_STRUCTURED_SMOKE_INVALID",
                        f"Smoke respondeu product_type_raw={safe(data.get('product_type_raw'))!r}.",
                    )
                return {
                    "provider": self.provider,
                    "model": self.model,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "total_tokens": int((usage or {}).get("total_tokens", 0) or 0),
                }
            except OperationalPause as exc:
                errors.append(f"{candidate}:{exc.code}")
                LOG.warning(
                    "LLM LIVE SMOKE FAIL | provider=%s | model=%s | code=%s | %s",
                    self.provider.upper(), candidate, exc.code, safe(exc),
                )
            finally:
                self.db.delete_llm(smoke_key, smoke_raw_hash, scoped_rules_hash, self.provider, candidate)

        self.model = original_model
        raise OperationalPause(
            f"{self.provider.upper()}_STRUCTURED_SMOKE_FAILED",
            "Nenhum modelo do provider completou uma inferência JSON válida. " + " | ".join(errors),
        )

    def semantic_identify(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        policy = self.rules.semantic_policy or {}
        max_sources = max(1, int(policy.get("max_sources", 3) or 3))
        rank = DeterministicEngine.IDENTITY_RANK
        min_identity = safe(policy.get("min_identity", "PROBABLE")).upper() or "PROBABLE"
        eligible_sources = [
            src for src in s1.sources
            if rank.get(src.relevance, 0) >= rank.get(min_identity, 3)
        ]
        sources_ranked = sorted(
            eligible_sources,
            key=lambda src: (
                rank.get(src.relevance, 0),
                src.identity_score,
                src.source_weight,
                1 if src.title else 0,
            ),
            reverse=True,
        )[:max_sources]
        prompt_to_source_id: Dict[int, int] = {}
        prompt_sources = []
        for i, src in enumerate(sources_ranked, 1):
            prompt_to_source_id[i] = src.source_id
            prompt_sources.append({
                "id": i,
                "identity": src.relevance,
                "model_match": src.model_match,
                "title": src.title,
                "content": src.content[: self.evidence_content_chars],
            })

        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["IDENTIFIED", "INSUFFICIENT_EVIDENCE", "CONFLICT"]},
                "product_type_raw": {"type": "string"},
                "product_role": {"type": "string", "enum": ["WHOLE_PRODUCT", "COMPONENT_PART", "ACCESSORY", "ASSEMBLY", "UNKNOWN"]},
                "domain_hint": {"type": "string", "enum": ["GARDEN_OUTDOOR", "INDUSTRIAL_HVAC", "AUTOMOTIVE", "AGRICULTURAL", "POWER_TOOL", "PUMP_FLUID", "ELECTRONICS", "OTHER", "UNKNOWN"]},
                "power": {"type": "string", "enum": sorted(set(self.rules.allowed_powers))},
                "evidence_source_ids": {"type": "array", "items": {"type": "integer"}},
                "evidence_phrase": {"type": "string"},
                "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                "reason_short": {"type": "string"},
            },
            "required": ["decision", "product_type_raw", "product_role", "domain_hint", "power", "evidence_source_ids", "evidence_phrase", "confidence", "reason_short"],
            "additionalProperties": False,
        }
        system = SEMANTIC_DEFINITION_SYSTEM_PROMPT
        user = json.dumps({
            "brand_from_import": brand,
            "model": model,
            "sources": prompt_sources,
        }, ensure_ascii=False)
        data, hit, usage = self._structured_json_call(
            cache_scope=SEMANTIC_PROMPT_VERSION,
            key=key,
            raw_hash=raw_hash,
            schema_name="semantic_product_definition",
            schema=schema,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        data, refs_ok = self._normalize_evidence_refs(data, prompt_to_source_id)
        usage = dict(usage or {})
        usage["evidence_refs_valid"] = refs_ok
        usage["semantic_prompt_sources"] = prompt_to_source_id
        return data, hit, usage

    def semantic_map(
        self,
        key: str,
        raw_hash: str,
        raw_type: str,
        product_role: str,
        domain_hint: str,
        candidates: Sequence[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        allowed = [safe(x.get("type")) for x in candidates if safe(x.get("type")) in self.rules.types]
        if not allowed:
            return {"decision": "NONE", "canonical_type": "NONE", "reason_short": "No taxonomy candidates"}, True, {}
        candidate_rows = []
        for row in candidates:
            typ = safe(row.get("type"))
            if typ not in allowed:
                continue
            spec = self.rules.types.get(typ, {}) or {}
            candidate_rows.append({
                "canonical_type": typ,
                "matched_label": safe(row.get("matched_label")),
                "score": row.get("score"),
                "aliases": list(spec.get("aliases", []) or [])[:6],
            })
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["MATCH", "NONE"]},
                "canonical_type": {"type": "string", "enum": sorted(set(allowed) | {"NONE"})},
                "reason_short": {"type": "string"},
            },
            "required": ["decision", "canonical_type", "reason_short"],
            "additionalProperties": False,
        }
        system = (
            "You map an already-identified physical product type to a controlled taxonomy. "
            "You may choose ONLY one supplied canonical candidate or NONE. Do not force a match. "
            "Match physical meaning, not superficial shared words. product_role and domain_hint are supporting context."
        )
        user = json.dumps({
            "product_type_raw": raw_type,
            "product_role": product_role,
            "domain_hint": domain_hint,
            "candidates": candidate_rows,
        }, ensure_ascii=False)
        map_key = key + "|SEM_MAP|" + hashlib.sha256(
            json.dumps([raw_type, product_role, domain_hint, allowed], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        return self._structured_json_call(
            cache_scope=SEMANTIC_MAP_PROMPT_VERSION,
            key=map_key,
            raw_hash=raw_hash,
            schema_name="semantic_taxonomy_mapping",
            schema=schema,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )

    def classify(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        cached = self.db.get_llm(
            key, raw_hash, self.rules.hash, self.provider, self.model
        )
        if cached is not None:
            data, usage = cached
            return data, True, usage

        if self.max_calls_per_run and self.api_calls >= self.max_calls_per_run:
            raise OperationalPause(
                f"{self.provider.upper()}_RUN_CAP",
                f"Limite local de {self.max_calls_per_run} chamadas {self.provider.upper()}/run atingido.",
            )

        messages, context_types, prompt_to_source_id = self._messages(
            brand, model, s1
        )

        if (
            self.max_total_tokens_per_run
            and self.total_tokens >= self.max_total_tokens_per_run
        ):
            raise OperationalPause(
                f"{self.provider.upper()}_LOCAL_TOKEN_CAP",
                f"Cap real de {self.max_total_tokens_per_run} tokens/run atingido. "
                f"Consumidos={self.total_tokens}.",
            )

        LOG.info(
            "%s CALL %d | ritmo=%.1f req/min | context_types=%d | sources=%d",
            self.provider.upper(),
            self.api_calls + 1,
            self.operational_requests_per_minute,
            len(context_types),
            min(len(s1.sources), self.max_llm_sources),
        )

        # O pacing efetivo é aplicado imediatamente antes de cada POST,
        # incluindo retries de parse, para respeitar RPM + TPM.
        self.api_calls += 1

        if self.provider == "groq":
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "product_classification",
                        "strict": True,
                        "schema": self.schema(s1, context_types),
                    },
                },
                "reasoning_effort": self.reasoning_effort,
                "temperature": 0,
                "max_completion_tokens": self.max_output_tokens,
            }
        elif self.provider == "openrouter":
            # OpenRouter suporta o contrato OpenAI e, para gpt-oss-120b:free,
            # structured outputs via response_format/json_schema.
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "product_classification",
                        "strict": True,
                        "schema": self.schema(s1, context_types),
                    },
                },
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "stream": False,
            }
        else:
            # NVIDIA hosted: JSON mode + validator Python.
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "stream": False,
            }

            # Nemotron 3.5: desliga thinking para usar o orçamento de saída
            # no JSON final, conforme recomendação da documentação NVIDIA.
            if self.model == "nvidia/nemotron-3.5-lightning-30b-a3b":
                payload["chat_template_kwargs"] = {
                    "enable_thinking": False
                }

        parse_max_attempts = 2
        data = None
        usage = None
        last_parse_error = None
        failed_parse_tokens = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for parse_attempt in range(1, parse_max_attempts + 1):
            self._wait_provider_limits(payload)
            resp = self._post(payload)

            try:
                envelope = resp.json()
                raw_usage = envelope.get("usage", {}) or {}
                current_usage = {
                    "prompt_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                }

                content = safe(envelope["choices"][0]["message"]["content"])
                data = self._decode_model_json(content)
                data, evidence_refs_valid = self._normalize_evidence_refs(
                    data, prompt_to_source_id
                )

                usage = {
                    **current_usage,
                    "context_types": context_types,
                    "rate_limit_headers": self._header_snapshot(resp),
                    "json_parse_retries": parse_attempt - 1,
                    "failed_parse_tokens": failed_parse_tokens["total_tokens"],
                    "evidence_refs_valid": evidence_refs_valid,
                    "evidence_prompt_map": {
                        str(k): int(v) for k, v in prompt_to_source_id.items()
                    },
                }
                break

            except Exception as exc:
                last_parse_error = exc

                # Se a API entregou usage, contabiliza também a resposta inválida.
                try:
                    envelope_for_usage = resp.json()
                    raw_failed = envelope_for_usage.get("usage", {}) or {}
                    failed_now = {
                        "prompt_tokens": int(raw_failed.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(raw_failed.get("completion_tokens", 0) or 0),
                        "total_tokens": int(raw_failed.get("total_tokens", 0) or 0),
                    }
                    for k in failed_parse_tokens:
                        failed_parse_tokens[k] += failed_now[k]
                except Exception:
                    pass

                if parse_attempt < parse_max_attempts:
                    LOG.warning(
                        "%s JSON inválido | retry controlado %d/%d | %s: %s",
                        self.provider.upper(),
                        parse_attempt + 1,
                        parse_max_attempts,
                        type(exc).__name__,
                        safe(exc),
                    )

                    # Mantém o mesmo produto/evidência/regras e só reforça o formato.
                    retry_payload = dict(payload)
                    retry_messages = list(payload.get("messages", []))
                    retry_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON. "
                                "Return ONLY one syntactically valid JSON object, "
                                "with exactly these keys: decision, product_type, "
                                "power, evidence_source_ids, reason_short. "
                                "No markdown, no comments, no trailing text."
                            ),
                        }
                    )
                    retry_payload["messages"] = retry_messages
                    payload = retry_payload

                    # Conta a segunda requisição real na métrica.
                    self.api_calls += 1
                    time.sleep(1.0)
                    continue

                raise OperationalPause(
                    f"{self.provider.upper()}_OUTPUT_PARSE",
                    f"Falha ao interpretar JSON de {self.provider.upper()} "
                    f"após {parse_max_attempts} tentativa(s): "
                    f"{type(exc).__name__}: {safe(exc)}",
                ) from exc

        if data is None or usage is None:
            raise OperationalPause(
                f"{self.provider.upper()}_OUTPUT_PARSE",
                f"Resposta sem JSON válido: {safe(last_parse_error)}",
            )

        # Consumo total inclui tentativas de parse que consumiram tokens.
        self.prompt_tokens += usage["prompt_tokens"] + failed_parse_tokens["prompt_tokens"]
        self.completion_tokens += (
            usage["completion_tokens"] + failed_parse_tokens["completion_tokens"]
        )
        self.total_tokens += usage["total_tokens"] + failed_parse_tokens["total_tokens"]

        self.db.put_llm(
            key,
            raw_hash,
            self.rules.hash,
            self.provider,
            self.model,
            data,
            usage,
        )
        return data, False, usage


class ProviderCascade:
    """Cascata configurável; RC4 HF1 suporta failover NVIDIA + provider."""

    EXHAUSTION_CODES = {
        "GROQ_TPD_EXHAUSTED",
        "NVIDIA_QUOTA_EXHAUSTED",
        "OPENROUTER_QUOTA_EXHAUSTED",
    }

    def __init__(
        self,
        providers: List[OpenAICompatibleClassifier],
        switch_only_on_quota_exhaustion: bool = False,
    ):
        if not providers:
            raise ValueError("ProviderCascade requer ao menos um provider.")
        self.providers = providers
        self.active_index = 0
        self.disabled: set = set()
        self.switch_only_on_quota_exhaustion = bool(
            switch_only_on_quota_exhaustion
        )

    @property
    def active(self) -> OpenAICompatibleClassifier:
        return self.providers[self.active_index]

    @property
    def provider(self) -> str:
        return self.active.provider

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def api_calls(self) -> int:
        return sum(x.api_calls for x in self.providers)

    @property
    def prompt_tokens(self) -> int:
        return sum(x.prompt_tokens for x in self.providers)

    @property
    def completion_tokens(self) -> int:
        return sum(x.completion_tokens for x in self.providers)

    @property
    def total_tokens(self) -> int:
        return sum(x.total_tokens for x in self.providers)

    def preflight_all(self) -> List[Dict[str, Any]]:
        """Valida cada provider isoladamente.

        Falha de preflight em um provider NÃO derruba toda a rota: o provider é
        desabilitado somente nesta execução e os demais continuam disponíveis.
        Isso é especialmente importante para providers de fallback opcionais
        (ex.: OpenRouter bloqueado por TLS/proxy corporativo).

        Se TODOS falharem, aí sim ocorre SAFE PAUSE antes da classificação.
        """
        results: List[Dict[str, Any]] = []
        enabled_indexes: List[int] = []

        for idx, provider in enumerate(self.providers):
            try:
                pf = dict(provider.preflight() or {})
                pf["enabled"] = True
                pf["error_code"] = ""
                pf["error_detail"] = ""
                enabled_indexes.append(idx)
                results.append(pf)
            except OperationalPause as exc:
                self.disabled.add(idx)
                results.append({
                    "provider": provider.provider,
                    "model": provider.model,
                    "base_url": provider.base_url,
                    "operational_rpm": provider.operational_requests_per_minute,
                    "enabled": False,
                    "error_code": exc.code,
                    "error_detail": safe(exc),
                    "model_discovery": None,
                })
                LOG.warning(
                    "LLM PRE-FLIGHT SKIP | provider=%s | model=%s | code=%s | %s",
                    provider.provider,
                    provider.model,
                    exc.code,
                    safe(exc),
                )

        if not enabled_indexes:
            details = " | ".join(
                f"{x.get('provider')}={x.get('error_code')}" for x in results
            )
            raise OperationalPause(
                "LLM_NO_PROVIDER_AVAILABLE",
                f"Nenhum provider passou no preflight. {details}",
            )

        self.active_index = enabled_indexes[0]
        return results

    def semantic_live_smoke_all(self) -> List[Dict[str, Any]]:
        """Prova inferência estruturada real e desabilita providers incompatíveis.

        Só é usado no Product Definition Pilot. Se todos falharem, o piloto
        encerra ANTES do primeiro produto em vez de descobrir o problema no lote.
        """
        results: List[Dict[str, Any]] = []
        enabled_indexes: List[int] = []
        for idx, provider in enumerate(self.providers):
            if idx in self.disabled:
                continue
            LOG.info(
                "LLM LIVE SMOKE START | provider=%s | model=%s",
                provider.provider.upper(), provider.model,
            )
            try:
                result = dict(provider.semantic_contract_smoke() or {})
                result["enabled"] = True
                result["error_code"] = ""
                results.append(result)
                enabled_indexes.append(idx)
                LOG.info(
                    "LLM LIVE SMOKE PASS | provider=%s | model=%s | %.1fs",
                    provider.provider.upper(), provider.model, float(result.get("elapsed_seconds", 0.0) or 0.0),
                )
            except OperationalPause as exc:
                self.disabled.add(idx)
                results.append({
                    "provider": provider.provider,
                    "model": provider.model,
                    "enabled": False,
                    "error_code": exc.code,
                    "error_detail": safe(exc),
                })
                LOG.warning(
                    "LLM LIVE SMOKE SKIP | provider=%s | model=%s | code=%s | %s",
                    provider.provider.upper(), provider.model, exc.code, safe(exc),
                )

        if not enabled_indexes:
            details = " | ".join(
                f"{x.get('provider')}={x.get('error_code')}" for x in results
            )
            raise OperationalPause(
                "LLM_NO_STRUCTURED_PROVIDER_AVAILABLE",
                "Nenhum provider concluiu o live structured-output smoke. " + details,
            )

        # Se o primeiro provider foi desabilitado no smoke, reposiciona a rota.
        self.active_index = enabled_indexes[0]
        return results

    def enabled_route(self) -> List[OpenAICompatibleClassifier]:
        return [
            p for idx, p in enumerate(self.providers)
            if idx not in self.disabled
        ]

    def _advance(self) -> bool:
        for idx in range(self.active_index + 1, len(self.providers)):
            if idx not in self.disabled:
                self.active_index = idx
                return True
        return False

    def classify(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        while True:
            current = self.active

            try:
                data, cache_hit, usage = current.classify(
                    key, raw_hash, brand, model, s1
                )
                usage = dict(usage or {})
                usage["provider"] = current.provider
                usage["model"] = current.model
                return data, cache_hit, usage

            except OperationalPause as exc:
                # Primeiro tenta outro modelo NVIDIA quando a falha parece
                # específica do modelo/endpoint e a política permite failover.
                nvidia_model_failover = (
                    current.provider == "nvidia"
                    and (
                        exc.code.endswith("_OUTPUT_PARSE")
                        or exc.code.endswith("_JSON_TOKEN_LIMIT")
                        or exc.code.endswith("_MODEL_EOL")
                        or (
                            not self.switch_only_on_quota_exhaustion
                            and exc.code.endswith("_TRANSIENT")
                        )
                    )
                )
                if nvidia_model_failover and current.advance_nvidia_model(exc.code):
                    continue

                always_rotate = (
                    exc.code in self.EXHAUSTION_CODES
                    or exc.code.endswith("_LOCAL_TOKEN_CAP")
                    or exc.code.endswith("_RUN_CAP")
                    or exc.code.endswith("_OUTPUT_PARSE")
                    or exc.code.endswith("_JSON_TOKEN_LIMIT")
                )
                operational_rotate = (
                    not self.switch_only_on_quota_exhaustion
                    and (
                        exc.code.endswith("_TRANSIENT")
                        or exc.code.endswith("_RATE_LIMIT")
                        or exc.code.endswith("_MODEL_EOL")
                    )
                )
                rotate_provider = always_rotate or operational_rotate
                if not rotate_provider:
                    raise

                old_provider = current.provider.upper()
                old_model = current.model
                self.disabled.add(self.active_index)

                if not self._advance():
                    raise

                LOG.warning(
                    "%s / %s não pôde concluir com segurança (%s). "
                    "FALLBACK AUTOMÁTICO -> %s / %s",
                    old_provider,
                    old_model,
                    exc.code,
                    self.active.provider.upper(),
                    self.active.model,
                )


    def _semantic_invoke(self, method_name: str, *args) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        while True:
            current = self.active
            try:
                method = getattr(current, method_name)
                data, cache_hit, usage = method(*args)
                usage = dict(usage or {})
                usage["provider"] = current.provider
                usage["model"] = current.model
                return data, cache_hit, usage
            except OperationalPause as exc:
                nvidia_model_failover = (
                    current.provider == "nvidia"
                    and (
                        exc.code.endswith("_OUTPUT_PARSE")
                        or exc.code.endswith("_JSON_TOKEN_LIMIT")
                        or exc.code.endswith("_MODEL_EOL")
                        or (
                            not self.switch_only_on_quota_exhaustion
                            and exc.code.endswith("_TRANSIENT")
                        )
                    )
                )
                if nvidia_model_failover and current.advance_nvidia_model(exc.code):
                    continue
                always_rotate = (
                    exc.code in self.EXHAUSTION_CODES
                    or exc.code.endswith("_LOCAL_TOKEN_CAP")
                    or exc.code.endswith("_RUN_CAP")
                    or exc.code.endswith("_OUTPUT_PARSE")
                    or exc.code.endswith("_JSON_TOKEN_LIMIT")
                )
                operational_rotate = (
                    not self.switch_only_on_quota_exhaustion
                    and (
                        exc.code.endswith("_TRANSIENT")
                        or exc.code.endswith("_RATE_LIMIT")
                        or exc.code.endswith("_MODEL_EOL")
                    )
                )
                if not (always_rotate or operational_rotate):
                    raise
                old_provider = current.provider.upper()
                old_model = current.model
                self.disabled.add(self.active_index)
                if not self._advance():
                    raise
                LOG.warning(
                    "%s / %s falhou no piloto semântico (%s). FALLBACK -> %s / %s",
                    old_provider, old_model, exc.code,
                    self.active.provider.upper(), self.active.model,
                )

    def semantic_identify(
        self,
        key: str,
        raw_hash: str,
        brand: str,
        model: str,
        s1: Stage1Result,
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        return self._semantic_invoke(
            "semantic_identify", key, raw_hash, brand, model, s1
        )

    def semantic_map(
        self,
        key: str,
        raw_hash: str,
        raw_type: str,
        product_role: str,
        domain_hint: str,
        candidates: Sequence[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
        return self._semantic_invoke(
            "semantic_map", key, raw_hash, raw_type, product_role, domain_hint, candidates
        )


# -----------------------------------------------------------------------------
# Stage 3/4 e saída
# -----------------------------------------------------------------------------
def validate_llm_output(
    data: Dict[str, Any],
    rules: Rules,
    s1: Stage1Result,
) -> Tuple[bool, str]:
    """Post-gate determinístico: LLM nunca vira fonte de verdade.

    RESOLVED só é aceito se:
    - TYPE já existia nos candidatos Stage 1;
    - POWER já existia nos candidatos Stage 1 (ou UNKNOWN);
    - ao menos uma fonte citada realmente pontuou para o TYPE;
    - há suporte explícito no TITLE ou suporte independente suficiente;
    - identidade/conflitos respeitam a política JSON.
    """
    decision = safe(data.get("decision"))
    typ = safe(data.get("product_type"))
    power = safe(data.get("power"))
    refs = data.get("evidence_source_ids", [])
    allowed_refs = {int(s.source_id) for s in s1.sources}
    gate = rules.classification_policy.get("llm_resolution_gate", {}) or {}

    if decision not in {"RESOLVED", "INSUFFICIENT_EVIDENCE", "CONFLICT"}:
        return False, "DECISION_INVALID"
    if power not in rules.allowed_powers:
        return False, "POWER_OUTSIDE_TAXONOMY"
    if not isinstance(refs, list) or any(
        isinstance(x, bool) or not isinstance(x, int) or x not in allowed_refs
        for x in refs
    ):
        return False, "EVIDENCE_REFS_INVALID"

    if decision != "RESOLVED":
        if typ != "REVISAR" and typ not in rules.types:
            return False, "TYPE_OUTSIDE_TAXONOMY"
        return True, "OK"

    if typ not in rules.types:
        return False, "TYPE_OUTSIDE_TAXONOMY"
    if not refs:
        return False, "RESOLVED_WITHOUT_EVIDENCE"

    funnel = candidate_funnel(rules, s1)
    allowed_types = set(funnel.candidates)
    if bool(gate.get("allow_only_stage1_type_candidates", True)) and typ not in allowed_types:
        return False, "TYPE_NOT_STAGE15_CANDIDATE"

    if bool(gate.get("block_multi_primary_title_conflict", True)):
        candidate_set = allowed_types
        for src in s1.sources:
            explicit_here = {
                t for t in candidate_set
                if "TITLE" in (src.type_evidence_fields.get(t, []) or [])
                and float(src.type_scores.get(t, 0.0) or 0.0) > 0.0
            }
            if len(explicit_here) >= 2:
                return False, "MULTI_PRIMARY_TITLE_CONFLICT"

    allowed_power = set(s1.power_candidates) | {"UNKNOWN"}
    if bool(gate.get("allow_only_stage1_power_candidates", True)) and power not in allowed_power:
        return False, "POWER_NOT_STAGE1_CANDIDATE"

    by_id = {s.source_id: s for s in s1.sources}
    cited = [by_id[x] for x in refs if x in by_id]
    supporting = [s for s in cited if float(s.type_scores.get(typ, 0.0) or 0.0) > 0.0]
    if bool(gate.get("require_cited_source_support", True)) and not supporting:
        return False, "CITED_SOURCE_DOES_NOT_SUPPORT_TYPE"

    title_support = any("TITLE" in (s.type_evidence_fields.get(typ, []) or []) for s in supporting)
    strong_support = int(s1.type_support.get(typ, 0) or 0)
    hit_support = sum(
        1 for s in s1.sources
        if s.source_weight > 0
        and s.relevance not in {"WEAK", "CONFLICT"}
        and float(s.type_scores.get(typ, 0.0) or 0.0) > 0.0
    )
    total_support = max(strong_support, hit_support)
    typ_score = float(s1.type_scores.get(typ, 0.0) or 0.0)
    score_total = sum(max(0.0, float(s1.type_scores.get(t, 0.0) or 0.0)) for t in allowed_types) or 1.0
    candidate_share = typ_score / score_total
    min_sources = int(gate.get("require_title_support_or_min_sources", 2) or 2)
    min_share = float(gate.get("min_candidate_share_without_title", 0.45) or 0.45)
    if not title_support and not (total_support >= min_sources and candidate_share >= min_share):
        return False, "TYPE_EVIDENCE_TOO_WEAK_FOR_LLM_RESOLVED"

    id_rank = DeterministicEngine.IDENTITY_RANK
    req_id = safe(gate.get("require_identity_at_least", "CONTEXTUAL")) or "CONTEXTUAL"
    best_id = max((s.relevance for s in supporting), key=lambda x: id_rank.get(x, 0), default="WEAK")
    if id_rank.get(best_id, 0) < id_rank.get(req_id, 2):
        return False, "IDENTITY_TOO_WEAK_FOR_LLM_RESOLVED"
    return True, "OK"


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


def repair_llm_evidence_refs(data: Dict[str, Any], rules: Rules, s1: Stage1Result) -> Tuple[Dict[str, Any], bool]:
    """Repara somente IDs de evidência quando TYPE já é elegível e suportado.

    Nunca altera TYPE/POWER escolhidos pela LLM. Evita perder uma classificação
    correta apenas porque o modelo citou um id local inválido.
    """
    if safe(data.get("decision")) != "RESOLVED":
        return data, False
    typ = safe(data.get("product_type"))
    funnel = candidate_funnel(rules, s1)
    if typ not in set(funnel.candidates):
        return data, False
    rank = DeterministicEngine.IDENTITY_RANK
    gate = rules.classification_policy.get("llm_resolution_gate", {}) or {}
    req = safe(gate.get("require_identity_at_least", "PROBABLE")) or "PROBABLE"
    supporting = [
        src for src in s1.sources
        if float(src.type_scores.get(typ, 0.0) or 0.0) > 0.0
        and rank.get(src.relevance, 0) >= rank.get(req, 3)
    ]
    if not supporting:
        return data, False
    supporting.sort(
        key=lambda src: (
            1 if "TITLE" in (src.type_evidence_fields.get(typ, []) or []) else 0,
            rank.get(src.relevance, 0),
            float(src.type_scores.get(typ, 0.0) or 0.0),
            src.identity_score,
        ),
        reverse=True,
    )
    out = dict(data)
    out["evidence_source_ids"] = [supporting[0].source_id]
    return out, True


def record_for_raw_state(raw: RawEvidence) -> Dict[str, Any]:
    status_map = {
        "RAW_MISSING": ("PENDING_COLLECTION", "Produto ausente do cache Search"),
        "RAW_EMPTY": ("PENDING_RECOLLECTION", "Consulta Search existe, mas não retornou resultados"),
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
        "LLM Provider": "",
        "LLM Model": "",
        "LLM Cache": "",
        "LLM Prompt Tokens": 0,
        "LLM Completion Tokens": 0,
        "LLM Total Tokens": 0,
    }


def llm_preflight_rejection_reason(rules: Rules, s1: Stage1Result) -> str:
    """Stage 0 + Stage 1.5 pre-flight; nenhuma chamada LLM inútil."""
    gate = rules.classification_policy.get("llm_resolution_gate", {}) or {}
    rank = DeterministicEngine.IDENTITY_RANK
    target_sources = [s for s in s1.sources if rank.get(s.relevance, 0) >= rank.get("PROBABLE", 3)]
    if not target_sources:
        return "LOW_TARGET_EVIDENCE"

    if bool(gate.get("block_multi_primary_title_conflict", True)) and multi_product_target_detected(rules, s1):
        return "MULTI_PRODUCT_TARGET"

    if bool(gate.get("no_candidate_means_no_llm", True)) and not s1.type_candidates:
        return "TAXONOMY_GAP_NO_ALIAS"

    funnel = candidate_funnel(rules, s1)
    if funnel.blocked:
        if funnel.reason.startswith("TOO_MANY_PLAUSIBLE_CANDIDATES"):
            return "NOISY_TOO_MANY_CANDIDATES"
        return "NO_LLM_ELIGIBLE_TARGET_EVIDENCE"
    if not funnel.candidates:
        return "NO_LLM_ELIGIBLE_TARGET_EVIDENCE"
    return ""


def finalize_product(
    brand: str,
    model: str,
    raw: RawEvidence,
    s1: Stage1Result,
    rules: Rules,
    cfg: Dict[str, Any],
    llm: Optional[OpenAICompatibleClassifier],
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
    consensus_cg, consensus_fam, consensus_reason = taxonomy_consensus_for_review(rules, s1, brand, model)
    if s1.state == "RESOLVED_RULE":
        next_action = "CONCLUIDO"
    elif not s1.type_candidates and ref_identity in {"STRONG", "PROBABLE"}:
        next_action = "JSON_GAP_OU_LLM_GAP"
    elif not s1.type_candidates and raw_result_count:
        next_action = "RECOLETAR_EVIDENCIA_TARGET"
    elif not s1.type_candidates:
        next_action = "NOVA_COLETA"
    elif consensus_cg and consensus_fam:
        next_action = "TYPE_REVIEW_FAMILY_JA_DEFINIDA"
    else:
        next_action = "LLM_COM_CANDIDATOS_OU_REVISAO"

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
            "Reason Code": "POWER_TOOL_COMBO_KIT",
            "Motivo": f"Combo kit explícito com múltiplas ferramentas principais: {', '.join(combo_components)} | {tax_reason}",
            "LLM Provider": "",
            "LLM Model": "",
            "LLM Cache": "",
            "LLM Prompt Tokens": 0,
            "LLM Completion Tokens": 0,
            "LLM Total Tokens": 0,
        }

    if s1.state == "RESOLVED_RULE":
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
            "Reason Code": "RULE_UNIQUE",
            "Motivo": " | ".join(s1.reasons + [tax_reason]),
            "LLM Provider": "",
            "LLM Model": "",
            "LLM Cache": "",
            "LLM Prompt Tokens": 0,
            "LLM Completion Tokens": 0,
            "LLM Total Tokens": 0,
        }

    # Stage 1.5 pode encerrar deterministicamente quando restou um único TYPE
    # bem sustentado e a alimentação necessária já está resolvida/dispensável.
    if len(funnel.candidates) == 1 and not multi_product_target_detected(rules, s1):
        typ = funnel.candidates[0]
        if _single_funnel_candidate_can_resolve(rules, s1, typ, brand, model):
            power = s1.power or "UNKNOWN"
            tax_evidence = taxonomy_evidence_text(s1.sources, typ)
            cg, fam, tax_reason = rules.map_taxonomy(typ, power, tax_evidence, brand, model)
            title, url = choose_title_for_type(s1.sources, typ, min_identity="STRONG")
            return {
                **common,
                "Próxima Ação": "CONCLUIDO",
                "Resolution State": "RESOLVED",
                "Stage": "FUNNEL_RULE",
                "O que é (Tipo)": typ,
                "Alimentação": power,
                "Category General": cg,
                "FAMILY": fam,
                "Título Real": title,
                "Fonte URL": url,
                "Review Required": "NÃO",
                "Reason Code": "FUNNEL_SINGLE_CANDIDATE",
                "Motivo": f"Stage 1.5 reduziu para um TYPE target-aware; {tax_reason}",
                "LLM Provider": "",
                "LLM Model": "",
                "LLM Cache": "",
                "LLM Prompt Tokens": 0,
                "LLM Completion Tokens": 0,
                "LLM Total Tokens": 0,
            }

    preflight_reason = llm_preflight_rejection_reason(rules, s1)
    if preflight_reason:
        # Mantém título apenas de fonte target; para taxonomy gap não inventa tipo.
        title, url = _output_title(*choose_title(s1.sources, []))
        reason_text = {
            "LOW_TARGET_EVIDENCE":
                "Stage 0 não encontrou fonte PROBABLE/STRONG do SKU alvo; candidato a recoleta seletiva.",
            "TAXONOMY_GAP_NO_ALIAS":
                "Há evidência target do produto, mas nenhum alias do JSON gerou TYPE; revisar cobertura da taxonomia/regras.",
            "MULTI_PRODUCT_TARGET":
                "Fonte target descreve múltiplos tipos/produtos (kit/combo); escolha automática bloqueada.",
            "NOISY_TOO_MANY_CANDIDATES":
                "Stage 1.5 encontrou candidatos plausíveis demais; LLM bloqueada para não arbitrar ruído.",
            "NO_LLM_ELIGIBLE_TARGET_EVIDENCE":
                "Há candidato bruto, mas nenhum sobreviveu ao funil mínimo de score/identidade/suporte.",
        }.get(preflight_reason, "LLM bloqueada pelo pre-flight determinístico.")
        return {
            **common,
            "Resolution State": "REVIEW",
            "Stage": "STAGE0" if preflight_reason == "LOW_TARGET_EVIDENCE" else "FUNNEL",
            "O que é (Tipo)": s1.product_type or "REVISAR",
            "Alimentação": s1.power or "UNKNOWN",
            "Category General": consensus_cg,
            "FAMILY": consensus_fam,
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "SIM",
            "Reason Code": preflight_reason,
            "Motivo": (reason_text + (f" | {consensus_reason}" if consensus_reason else "")),
            "LLM Provider": "",
            "LLM Model": "",
            "LLM Cache": "",
            "LLM Prompt Tokens": 0,
            "LLM Completion Tokens": 0,
            "LLM Total Tokens": 0,
        }

    if llm is None:
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
            "Reason Code": f"STAGE15_{s1.state}",
            "Motivo": " | ".join(s1.reasons + ([consensus_reason] if consensus_reason else [])),
            "LLM Provider": "",
            "LLM Model": "",
            "LLM Cache": "",
            "LLM Prompt Tokens": 0,
            "LLM Completion Tokens": 0,
            "LLM Total Tokens": 0,
        }

    data, cache_hit, usage = llm.classify(key, raw.raw_hash, brand, model, s1)

    evidence_repaired = False
    if usage.get("evidence_refs_valid") is False:
        data, evidence_repaired = repair_llm_evidence_refs(data, rules, s1)
        if evidence_repaired:
            usage = dict(usage or {})
            usage["evidence_refs_valid"] = True
            usage["evidence_refs_repaired"] = True

    if usage.get("evidence_refs_valid") is False:
        ok, why = False, "EVIDENCE_REFS_INVALID"
    else:
        ok, why = validate_llm_output(data, rules, s1)

    actual_provider = safe(usage.get("provider")) or llm.provider
    actual_model = safe(usage.get("model")) or llm.model
    provider_upper = actual_provider.upper()

    base_llm = {
        **common,
        "LLM Provider": provider_upper,
        "LLM Model": actual_model,
        "LLM Cache": "SIM" if cache_hit else "NÃO",
        "LLM Prompt Tokens": int(usage.get("prompt_tokens", 0) or 0),
        "LLM Completion Tokens": int(usage.get("completion_tokens", 0) or 0),
        "LLM Total Tokens": int(usage.get("total_tokens", 0) or 0),
        "LLM JSON Parse Retries": int(usage.get("json_parse_retries", 0) or 0),
        "LLM Failed Parse Tokens": int(usage.get("failed_parse_tokens", 0) or 0),
        "LLM Evidence Repaired": "SIM" if evidence_repaired else "NÃO",
        "LLM Rule Context": " | ".join(usage.get("context_types", []) or []),
        "LLM Rate Limit Headers": json.dumps(usage.get("rate_limit_headers", {}) or {}, ensure_ascii=False),
    }

    if not ok:
        db.event(run_id, key, f"{provider_upper}_OUTPUT_REJECTED", why)
        rejected_refs = data.get("evidence_source_ids", []) or []
        suggested_type = safe(data.get("product_type"))
        if suggested_type in rules.types:
            title, url = choose_title_for_type(s1.sources, suggested_type, rejected_refs)
        else:
            title, url = choose_title(s1.sources, rejected_refs)
        suggested_power = safe(data.get("power"))
        suggestion = ""
        if suggested_type or suggested_power:
            suggestion = f" | LLM_SUGGESTION={suggested_type or 'REVISAR'}/{suggested_power or 'UNKNOWN'}"
        return {
            **base_llm,
            "Resolution State": "REVIEW",
            "Stage": "LLM",
            "O que é (Tipo)": "REVISAR",
            "Alimentação": "UNKNOWN",
            "Category General": consensus_cg,
            "FAMILY": consensus_fam,
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "SIM",
            "Reason Code": why,
            "Motivo": (f"{safe(data.get('reason_short'))}{suggestion} | POST_GATE={why}" + (f" | {consensus_reason}" if consensus_reason else "")).strip(" |"),
            "Evidence Source IDs": ",".join(map(str, rejected_refs)),
        }

    decision = safe(data.get("decision"))
    refs = data.get("evidence_source_ids", []) or []
    typ = safe(data.get("product_type"))
    title, url = choose_title_for_type(s1.sources, typ, refs) if typ in rules.types else choose_title(s1.sources, refs)

    if decision != "RESOLVED":
        return {
            **base_llm,
            "Resolution State": "REVIEW",
            "Stage": "LLM",
            "O que é (Tipo)": typ,
            "Alimentação": safe(data.get("power")),
            "Category General": consensus_cg,
            "FAMILY": consensus_fam,
            "Título Real": title,
            "Fonte URL": url,
            "Review Required": "SIM",
            "Reason Code": f"{provider_upper}_{decision}",
            "Motivo": (safe(data.get("reason_short")) + (f" | {consensus_reason}" if consensus_reason else "")),
            "Evidence Source IDs": ",".join(map(str, refs)),
        }

    power = safe(data.get("power"))
    title, url = choose_title_for_type(s1.sources, typ, refs, min_identity="STRONG")
    tax_evidence = taxonomy_evidence_text(s1.sources, typ, refs)
    cg, fam, tax_reason = rules.map_taxonomy(typ, power, tax_evidence, brand, model)
    review = (cg, fam) not in rules.valid_pairs or (cg, fam) == ("OTHERS", "OTHERS")
    out_cg, out_fam = ("", "") if review else (cg, fam)

    return {
        **base_llm,
        "Raw State": "RAW_OK",
        "Próxima Ação": "CONCLUIDO" if not review else common.get("Próxima Ação", "LLM_COM_CANDIDATOS_OU_REVISAO"),
        "Resolution State": "REVIEW" if review else "RESOLVED",
        "Stage": "LLM",
        "O que é (Tipo)": typ,
        "Alimentação": power,
        "Category General": out_cg,
        "FAMILY": out_fam,
        "Título Real": title,
        "Fonte URL": url,
        "Review Required": "SIM" if review else "NÃO",
        "Reason Code": "TAXONOMY_UNMAPPED" if review else f"{provider_upper}_RESOLVED",
        "Motivo": f"{safe(data.get('reason_short'))} | {tax_reason}",
        "Evidence Source IDs": ",".join(map(str, refs)),
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
]

BASELINE_MASTER_FIELDS = [
    "O que é (Tipo)",
    "Alimentação",
    "Category General",
    "FAMILY",
    "Título Real",
    "Título Referência",
    "Próxima Ação",
    "Fonte URL",
    "Fonte Referência URL",
    "Collection State",
    "Raw Search Results",
    "Curated Sources",
    "Title Rescue Sources",
    "Status",
    "Status Processamento",
    "Revisão",
    "Revisão B.I",
    "Resolution State",
    "Review Required",
    "Reason Code",
    "Motivo",
    "Stage",
    "Rules Version",
    "Rules Hash",
    "Pipeline Version",
    "Processed At",
    "LLM Provider",
    "LLM Model",
]

_REVIEW_REJECT_STATES = {
    "REVISAR",
    "REVIEW",
    "PENDENTE_COLETA",
    "PENDING_COLLECTION",
    "PENDING_RECOLLECTION",
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
        "LLM Provider": ["LLM Provider"],
        "LLM Model": ["LLM Model"],
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
            by_key[business_key(brand, model)] = row
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
        snapshot[business_key(brand, model)] = row.get(rb) if rb else None
    return snapshot, True


def _apply_review_bi_snapshot(
    df: Any,
    brand_col: str,
    model_col: str,
    snapshot: Dict[str, Any],
    source_exists: bool,
) -> Any:
    """Base Final é a fonte de verdade humana de Revisão B.I."""
    import pandas as pd
    if "Revisão B.I" not in df.columns:
        df["Revisão B.I"] = pd.NA
    if not source_exists:
        return df
    # Limpa inclusive valores antigos do Consolidado e reaplica exatamente o snapshot da Base Final.
    df["Revisão B.I"] = pd.NA
    for idx, row in df.iterrows():
        brand, model = safe(row.get(brand_col)), safe(row.get(model_col))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        if key in snapshot:
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
    """Gera CSV de conflitos objetivos sem modificar Excel e sem usar LLM."""
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

            # BI_OK é absoluto: a Base Final vence o Consolidado inclusive nos vazios.
            if src_bi_ok:
                _copy_bi_locked_row_exact(src, final_df, master_df, idx)
                stats["final_bi_lock_promoted"] += 1
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
    if resolution in {
        "PENDING_COLLECTION",
        "PENDING_RECOLLECTION",
        "CACHE_ERROR",
        "CACHE_COLLISION",
    }:
        return resolution, "PENDENTE_COLETA"

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
    if resolution in {"PENDING_COLLECTION", "PENDING_RECOLLECTION", "CACHE_ERROR", "CACHE_COLLISION"}:
        return "PENDENTE_COLETA"

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


def _export_base_final(
    master_df: Any,
    final_path: Path,
    review_bi_snapshot: Optional[Dict[str, Any]] = None,
    review_bi_source_exists: bool = False,
    bi_lock_snapshot: Optional[Dict[str, Dict[str, str]]] = None,
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

    # HF3.1p2: schema-forward baseline export.
    # Diagnostic columns introduced after older baseline files were created may
    # legitimately be absent from ``work``. Copy them when available and
    # create any remaining final-schema column as NA before the strict slice.
    # This keeps old baselines forward-compatible and prevents KeyError when
    # BASELINE_FINAL_COLUMNS evolves.
    for col in ["Título Referência", "Próxima Ação"]:
        src = find_col(work, [col], required=False)
        out[col] = work[src] if src else pd.NA

    for col in BASELINE_FINAL_COLUMNS:
        if col not in out.columns:
            src = find_col(work, [col], required=False)
            out[col] = work[src] if src else pd.NA

    out = out[BASELINE_FINAL_COLUMNS].reset_index(drop=True)

    _assert_bi_lock_snapshot(out, bi_lock_snapshot or {}, "Base Final gerada")

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

def persist_baseline_excels(
    rows: List[Dict[str, Any]],
    master_path: Optional[Path],
    final_path: Optional[Path],
    input_path: Optional[Path],
    rules: Rules,
    reprocess: bool = False,
    force_reprocess_keys: Optional[set] = None,
) -> Dict[str, int]:
    """Grava Consolidado -> Base Final sem jamais alterar linhas Revisão B.I=OK."""
    import pandas as pd

    if not master_path and not final_path:
        return {}

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

    if source_path:
        master_df, master_sheet = _read_business_sheet(source_path)
    else:
        master_df = pd.DataFrame(columns=["Marca", "Modelo"])

    master_df, brand_col, model_col = _ensure_master_columns(master_df)
    master_df, sync_stats = _sync_final_into_master(master_df, final_path, brand_col, model_col)
    master_df, brand_col, model_col = _ensure_master_columns(master_df)
    master_df, merge_stats = _upsert_run_rows_into_master(
        master_df, rows, brand_col, model_col, rules, reprocess, force_reprocess_keys
    )

    master_df = _apply_review_bi_snapshot(
        master_df, brand_col, model_col, review_bi_snapshot, review_bi_source_exists
    )
    master_df, legacy_taxonomy_cleaned = _sanitize_legacy_unresolved_taxonomy(master_df)

    # Antes de tocar nos arquivos, prova que o HARD LOCK continua idêntico.
    _assert_bi_lock_snapshot(master_df, bi_lock_snapshot, "Consolidado em memória")

    stats = {
        **sync_stats,
        **merge_stats,
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
            master_df, final_path, review_bi_snapshot, review_bi_source_exists, bi_lock_snapshot
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
            "Classificação não iniciada para evitar gastar LLM com baseline inconsistente."
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
) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
    """Seleciona pendências + AUTO_APROVADO de regra antiga, sem Tavily."""
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
    target_all: List[Tuple[str, str, str]] = []
    seen = set()
    locked = good = stale_auto = stale_unknown_meta = 0
    collection_counts: Dict[str, int] = defaultdict(int)

    for _, row in df.iterrows():
        brand, model = safe(row.get(b)), safe(row.get(m))
        if not brand or not model:
            continue
        key = business_key(brand, model)
        if key in seen:
            continue
        seen.add(key)
        if _row_bi_locked(row, df):
            locked += 1
            continue

        recovery_reason = _cache_recovery_reason(
            row, df, key, master_meta, rules
        )
        if not recovery_reason:
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
            if recovery_reason == "AUTO_APROVADO_REGRA_OBSOLETA":
                rec["Ação"] = "REPROCESSAR_REGRA_OBSOLETA"
            elif curated_count:
                rec["Ação"] = "REPROCESSAR_CACHE_CURADO"
            elif raw_count:
                rec["Ação"] = "TITLE_RESCUE_OU_RECOLETA"
            else:
                rec["Ação"] = "RECOLETA_NECESSARIA"
            with_cache.append(rec)
            target_all.append((brand, model, key))
        else:
            rec["Ação"] = "PENDENTE_COLETA"
            without_cache.append(rec)

    _export_recovery_report(with_cache, with_cache_path, "Pendentes_Com_Cache")
    _export_recovery_report(without_cache, without_cache_path, "Pendentes_Sem_Cache")

    targets = target_all[:max_items] if max_items else target_all
    stats = {
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

    store = TavilyReadOnlyStore(cache_path)
    try:
        targets, plan_stats = build_cache_recovery_plan(
            final_path, master_path, store, with_path, without_path, rules, cfg, args.max_items
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
    if args.mode in {"classify", "full", "recover", "semantic", "cachefirst"}:
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
        if explicit_targets is not None
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

        groq_model = (
            os.getenv("GROQ_MODEL", "").strip()
            or safe(cfg.get("groq", {}).get("model"))
            or DEFAULT_MODEL
        )
        nvidia_model = (
            os.getenv("NVIDIA_MODEL", "").strip()
            or safe(cfg.get("nvidia", {}).get("model"))
            or "openai/gpt-oss-20b"
        )
        openrouter_model = (
            os.getenv("OPENROUTER_MODEL", "").strip()
            or safe(cfg.get("openrouter", {}).get("model"))
            or "openai/gpt-oss-120b:free"
        )

        route_names = [safe(x).casefold() for x in (cfg.get("llm", {}).get("route", []) or []) if safe(x)]
        if not route_names:
            route_names = ["nvidia", "groq"]
        model_by_provider = {"groq": groq_model, "nvidia": nvidia_model, "openrouter": openrouter_model}
        llm_identity = "cascade:" + "->".join(
            f"{name}:{model_by_provider.get(name, '')}" for name in route_names if name in model_by_provider
        )
        db = ClassificationDB(db_path)
        run_id = db.start_run(args.mode, rules.hash, llm_identity)
        engine = DeterministicEngine(rules, cfg)

        use_llm = not args.no_llm
        llm: Optional[ProviderCascade] = None

        rows: List[Dict[str, Any]] = []
        stats = defaultdict(int)

        if explicit_targets is None:
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

        if use_llm and targets:
            try:
                groq_llm = OpenAICompatibleClassifier(
                    rules=rules,
                    cfg=cfg,
                    db=db,
                    provider="groq",
                    model=groq_model,
                )
                nvidia_llm = OpenAICompatibleClassifier(
                    rules=rules,
                    cfg=cfg,
                    db=db,
                    provider="nvidia",
                    model=nvidia_model,
                )
                openrouter_llm = OpenAICompatibleClassifier(
                    rules=rules,
                    cfg=cfg,
                    db=db,
                    provider="openrouter",
                    model=openrouter_model,
                )

                provider_map = {
                    "groq": groq_llm,
                    "nvidia": nvidia_llm,
                    "openrouter": openrouter_llm,
                }
                ordered_providers = [provider_map[x] for x in route_names if x in provider_map]
                if not ordered_providers:
                    ordered_providers = [nvidia_llm, groq_llm, openrouter_llm]
                llm = ProviderCascade(
                    ordered_providers,
                    switch_only_on_quota_exhaustion=bool(
                        cfg.get("llm", {}).get(
                            "switch_only_on_quota_exhaustion", False
                        )
                    ),
                )
                preflights = llm.preflight_all()

                # Registra a cadeia de modelos validada no preflight para
                # impedir mistura de checkpoints entre políticas de fallback.
                llm_identity = "cascade:" + "->".join(
                    obj.route_identity_fragment() for obj in llm.providers
                )
                db.update_run_model(run_id, llm_identity)

                for pf, pobj in zip(preflights, llm.providers):
                    if not pf.get("enabled", True):
                        # Já foi registrado em WARNING por ProviderCascade.preflight_all().
                        continue

                    LOG.info(
                        "LLM PRE-FLIGHT OK | provider=%s | model=%s | "
                        "endpoint=%s | ritmo=%.1f req/min | "
                        "output=%d | retry_output_max=%d",
                        pf["provider"],
                        pobj.model,
                        pf["base_url"],
                        pf["operational_rpm"],
                        pobj.max_output_tokens,
                        pobj.json_retry_max_completion_tokens,
                    )
                    if pf.get("model_discovery"):
                        md = pf["model_discovery"]
                        LOG.info(
                            "%s MODELS | disponíveis=%s | solicitado=%s | selecionado=%s | auto=%s",
                            pf["provider"].upper(),
                            md["available_count"],
                            md["requested_model"],
                            md["selected_model"],
                            md["auto_selected"],
                        )
                        if pf["provider"] == "nvidia" and md.get("candidate_models"):
                            LOG.info(
                                "NVIDIA MODEL ROUTE: %s",
                                " -> ".join(md["candidate_models"]),
                            )
                        if pf["provider"] == "openrouter" and md.get("key_info"):
                            ki = md["key_info"]
                            LOG.info(
                                "OPENROUTER KEY | free_tier=%s | usage_daily=%s | key_limit_remaining=%s",
                                ki.get("is_free_tier"),
                                ki.get("usage_daily"),
                                ki.get("limit_remaining"),
                            )

                enabled_names = [p.provider.upper() for p in llm.enabled_route()]
                disabled_names = [
                    p.provider.upper() for i, p in enumerate(llm.providers)
                    if i in llm.disabled
                ]
                LOG.info(
                    "ROTA LLM ATIVA: %s | fallback em cota/cap, formato e falha "
                    "transitória após retries; evidência insuficiente nunca é forçada.%s",
                    " -> ".join(enabled_names),
                    (f" | indisponíveis nesta execução: {', '.join(disabled_names)}"
                     if disabled_names else ""),
                )

            except OperationalPause as exc:
                db.event(run_id, "", exc.code, safe(exc))
                db.finish_run(run_id, "FAILED", f"{exc.code}: {exc}")
                db.close()
                raise RuntimeError(
                    f"{exc.code}: {exc}\nClassificação NÃO iniciada."
                ) from exc
        elif use_llm:
            LOG.info(
                "LLM PRE-FLIGHT DISPENSADO | cohort sem itens pendentes; "
                "nenhuma chamada de provider necessária."
            )

        paused_reason = ""

        try:
            for idx, (brand, model_, key) in enumerate(targets, 1):
                raw = store.get(brand, model_)

                if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
                    rec = record_for_raw_state(raw)
                    stats[raw.state.lower()] += 1
                    db.put_checkpoint(
                        key, brand, model_, raw.raw_hash, rules.hash, rec, llm_identity
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
                        key, brand, model_, raw.raw_hash, rules.hash, rec, llm_identity
                    )
                    rows.append(rec)
                    continue

                cp = None
                if not args.no_checkpoint:
                    cp = db.get_checkpoint(
                        key,
                        raw.raw_hash,
                        rules.hash,
                        llm_identity,
                    )

                if cp is not None:
                    rows.append(cp)
                    stats["checkpoint_hit"] += 1
                    continue

                s1 = engine.classify(brand, model_, raw.payload or {})
                stats[f"stage1_{s1.state.lower()}"] += 1

                try:
                    rec = finalize_product(
                        brand, model_, raw, s1, rules, cfg, llm, db, run_id
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
                            "Stage": "LLM_FORMAT_REVIEW", "O que é (Tipo)": s1.product_type or "REVISAR",
                            "Alimentação": s1.power or "UNKNOWN", "Category General": "", "FAMILY": "",
                            "Título Real": title, "Fonte URL": url, "Review Required": "SIM",
                            "Reason Code": exc.code,
                            "Motivo": f"Todos os providers disponíveis falharam no formato para este item; execução continuou. {safe(exc)}",
                            "LLM Provider": "", "LLM Model": "", "LLM Cache": "",
                            "LLM Prompt Tokens": 0, "LLM Completion Tokens": 0, "LLM Total Tokens": 0,
                        }
                        stats["llm_format_review"] += 1
                        LOG.warning("%s | item enviado a REVIEW; execução continuará.", exc.code)
                    else:
                        stats["api_paused"] += 1
                        paused_reason = f"{exc.code}: {exc}"
                        LOG.error("%s", paused_reason)
                        break

                if should_checkpoint_record(rec):
                    db.put_checkpoint(
                        key, brand, model_, raw.raw_hash, rules.hash, rec, llm_identity
                    )

                rows.append(rec)
                stats[f"final_{safe(rec.get('Resolution State')).lower()}"] += 1
                stats[f"stage_{safe(rec.get('Stage')).lower()}"] += 1

                if safe(rec.get("LLM Cache")).upper() == "SIM":
                    stats["llm_cache_hit"] += 1
                elif safe(rec.get("Stage")).upper() == "LLM":
                    stats["llm_result"] += 1

                if idx % 10 == 0 or idx == len(targets):
                    LOG.info(
                        "PROGRESSO %d/%d | rule=%d ambiguous=%d unresolved=%d | "
                        "llm_ok=%d llm_api=%d llm_cache=%d | review=%d checkpoint=%d | tokens=%d",
                        idx,
                        len(targets),
                        stats["stage1_resolved_rule"],
                        stats["stage1_ambiguous"],
                        stats["stage1_unresolved"],
                        stats["stage_llm"],
                        llm.api_calls if llm is not None else 0,
                        stats["llm_cache_hit"],
                        stats["final_review"],
                        stats["checkpoint_hit"],
                        llm.total_tokens if llm is not None else 0,
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
                "LLM Provider": (
                    "->".join(p.provider for p in llm.providers)
                    if llm is not None
                    else ("NOT_NEEDED" if use_llm else "DISABLED")
                ),
                "LLM Model": (
                    "|".join(p.route_identity_fragment() for p in llm.providers)
                    if llm is not None
                    else ("NOT_NEEDED" if use_llm else "DISABLED")
                ),
                "Produtos no cache": len(store.catalog()),
                "Elegíveis": len(targets),
                "Pilot Cohort Hash": cohort_hash,
                "LLM API Calls": llm.api_calls if llm is not None else 0,
                "LLM Prompt Tokens": llm.prompt_tokens if llm is not None else 0,
                "LLM Completion Tokens": llm.completion_tokens if llm is not None else 0,
                "LLM Total Tokens": llm.total_tokens if llm is not None else 0,
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
                    "Rode novamente depois; checkpoints e cache LLM evitam repetir itens concluídos."
                )
            else:
                LOG.info("CONCLUÍDO. Resultado: %s", output_path)

            LOG.info(
                "Resumo: %s",
                " | ".join(f"{k}={v}" for k, v in stats.items()),
            )

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



def _semantic_build_llm_cascade(
    rules: Rules,
    cfg: Dict[str, Any],
    db: ClassificationDB,
) -> ProviderCascade:
    pilot_cfg = cfg.get("semantic_pilot", {}) or {}
    # p8.5: o piloto pode usar um modelo/rota próprios sem alterar a classificação
    # normal. O preflight valida credencial/modelo antes da primeira inferência.
    groq_model = safe(pilot_cfg.get("groq_model")) or os.getenv("GROQ_MODEL", "").strip() or safe(cfg.get("groq", {}).get("model")) or DEFAULT_MODEL
    nvidia_model = safe(pilot_cfg.get("nvidia_model")) or os.getenv("NVIDIA_MODEL", "").strip() or safe(cfg.get("nvidia", {}).get("model")) or "openai/gpt-oss-20b"
    openrouter_model = safe(pilot_cfg.get("openrouter_model")) or os.getenv("OPENROUTER_MODEL", "").strip() or safe(cfg.get("openrouter", {}).get("model")) or "openai/gpt-oss-120b:free"
    route_source = pilot_cfg.get("llm_route", cfg.get("llm", {}).get("route", [])) or []
    route_names = [safe(x).casefold() for x in route_source if safe(x)] or ["nvidia", "groq"]
    provider_map = {
        "groq": OpenAICompatibleClassifier(rules, cfg, db, "groq", groq_model),
        "nvidia": OpenAICompatibleClassifier(rules, cfg, db, "nvidia", nvidia_model),
        "openrouter": OpenAICompatibleClassifier(rules, cfg, db, "openrouter", openrouter_model),
    }
    ordered = [provider_map[x] for x in route_names if x in provider_map]
    if not ordered:
        ordered = [provider_map["nvidia"], provider_map["groq"]]
    cascade = ProviderCascade(
        ordered,
        switch_only_on_quota_exhaustion=bool(cfg.get("llm", {}).get("switch_only_on_quota_exhaustion", False)),
    )
    preflights = cascade.preflight_all()
    disabled_preflight = [
        f"{safe(x.get('provider')).upper()}={safe(x.get('error_code'))}"
        for x in preflights if not x.get("enabled")
    ]
    if disabled_preflight:
        LOG.warning("PRODUCT DEFINITION PILOT PROVIDERS DESABILITADOS NO PREFLIGHT: %s", " | ".join(disabled_preflight))

    # p8.5: GET /models não basta. Antes de tocar em qualquer produto, executa
    # uma inferência estruturada real com o MESMO parser/schema usado pelo piloto.
    smoke_results = cascade.semantic_live_smoke_all()
    enabled = [
        f"{p.provider.upper()}[{p.model}]"
        for idx, p in enumerate(cascade.providers) if idx not in cascade.disabled
    ]
    disabled_smoke = [
        f"{safe(x.get('provider')).upper()}={safe(x.get('error_code'))}"
        for x in smoke_results if not x.get("enabled")
    ]
    LOG.info("PRODUCT DEFINITION PILOT LLM ROUTE VALIDADA AO VIVO: %s", " -> ".join(enabled))
    if disabled_smoke:
        LOG.warning("PRODUCT DEFINITION PILOT PROVIDERS DESABILITADOS NO LIVE SMOKE: %s", " | ".join(disabled_smoke))
    return cascade


def run_semantic_pilot(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Piloto de definição de produto via LLM sem alterar Base Final/Consolidado/checkpoint de classificação.

    R9.6p8.5: mantém coorte elegível e exige live structured-output smoke antes do primeiro produto; falhas encerram de forma segura.
    """
    pilot_cfg = cfg.get("semantic_pilot", {}) or {}
    if not bool(pilot_cfg.get("enabled", True)):
        raise RuntimeError("SEMANTIC_PILOT_DISABLED no pipeline_config.json")
    if args.no_llm:
        raise RuntimeError("SEMANTIC_PILOT_REQUIRES_LLM: remova --no-llm")

    input_path = Path(args.input or cfg.get("files", {}).get("input", "Base de Produtos.xlsx"))
    cache_path = Path(args.cache or cfg["files"]["cache"])
    db_path = Path(args.db or cfg["files"]["classification_db"])
    output_path = Path(args.output or cfg.get("files", {}).get("semantic_pilot_output", "Piloto_Definicao_LLM_R9_6p8_4.xlsx"))
    source_rows = read_input_rows(input_path)
    default_limit = int(pilot_cfg.get("default_max_items", 10) or 10)
    limit = int(args.max_items or default_limit)
    checkpoint_every = max(1, int(pilot_cfg.get("checkpoint_every", 5) or 5))
    target_states = {
        safe(x).upper() for x in (pilot_cfg.get("target_stage1_states", ["UNRESOLVED", "AMBIGUOUS"]) or [])
        if safe(x)
    } or {"UNRESOLVED", "AMBIGUOUS"}

    store = TavilyReadOnlyStore(cache_path)
    engine = DeterministicEngine(rules, cfg)
    rank = DeterministicEngine.IDENTITY_RANK
    min_identity = safe(rules.semantic_policy.get("min_identity", "PROBABLE")).upper() or "PROBABLE"

    # p8.5 preserva: o limite do piloto significa N CASOS ELEGÍVEIS, não as N
    # primeiras linhas da base. Assim SEM_CACHE_UTIL/COLLECTION_GAP não consomem
    # a amostra destinada a validar a LLM Product Definition.
    seen = set()
    cohort: List[InputProductRow] = []
    prepared: Dict[str, Tuple[RawEvidence, Stage1Result]] = {}
    selection_stats = defaultdict(int)
    LOG.info(
        "PILOTO COHORT SCAN START | alvo=%s | states=%s | min_identity=%s",
        limit if limit > 0 else "ALL", ",".join(sorted(target_states)), min_identity,
    )
    for scan_idx, row in enumerate(source_rows, 1):
        selection_stats["rows_scanned"] += 1
        if scan_idx % 500 == 0:
            LOG.info(
                "PILOTO COHORT SCAN | varridas=%d | elegíveis=%d | selecionados=%d",
                selection_stats["rows_scanned"], selection_stats["eligible"], len(cohort),
            )
        if not row.valid:
            selection_stats["invalid_input"] += 1
            continue
        if row.key in seen:
            selection_stats["duplicate"] += 1
            continue
        seen.add(row.key)

        raw = store.get(row.brand_original, row.model_original)
        if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
            selection_stats["no_usable_cache"] += 1
            continue

        s1 = engine.classify(row.brand_original, row.model_original, raw.payload or {})
        target_sources = [
            src for src in s1.sources
            if rank.get(src.relevance, 0) >= rank.get(min_identity, 3)
        ]
        if not target_sources:
            selection_stats["no_target_evidence"] += 1
            continue
        if safe(s1.state).upper() not in target_states:
            selection_stats[f"skip_state_{safe(s1.state).lower() or 'unknown'}"] += 1
            continue

        selection_stats["eligible"] += 1
        cohort.append(row)
        prepared[row.key] = (raw, s1)
        LOG.info(
            "PILOTO COHORT +%03d | %s | %s | stage1=%s | target_sources=%d",
            len(cohort), row.brand_original, row.model_original, s1.state, len(target_sources),
        )
        if limit > 0 and len(cohort) >= limit:
            break

    LOG.info(
        "PILOTO COHORT READY | varridas=%d | selecionados=%d | sem_cache=%d | sem_target=%d | states=%s",
        selection_stats["rows_scanned"], len(cohort), selection_stats["no_usable_cache"],
        selection_stats["no_target_evidence"], ",".join(sorted(target_states)),
    )
    if not cohort:
        store.close()
        raise RuntimeError(
            "SEMANTIC_PILOT_NO_ELIGIBLE_ITEMS: nenhum item com cache utilizável + evidência target "
            f">={min_identity} + Stage1 em {sorted(target_states)}."
        )

    db = ClassificationDB(db_path)
    try:
        llm = _semantic_build_llm_cascade(rules, cfg, db)
    except OperationalPause as exc:
        LOG.error(
            "PILOTO NÃO INICIADO | code=%s | %s",
            exc.code, safe(exc),
        )
        LOG.error(
            "Nenhum produto foi enviado à LLM. Base Final/Consolidado permanecem intactos. "
            "Corrija somente a credencial/provider indicado antes de tentar novamente."
        )
        db.close()
        store.close()
        return
    except Exception as exc:
        LOG.error(
            "PILOTO NÃO INICIADO | erro de integração LLM=%s | %s",
            type(exc).__name__, safe(exc),
        )
        LOG.error("Nenhum produto foi processado; Base Final/Consolidado permanecem intactos.")
        db.close()
        store.close()
        return
    rows: List[Dict[str, Any]] = []
    stats = defaultdict(int)
    pilot_started = time.monotonic()

    def _summary(status: str) -> Dict[str, Any]:
        return {
            "Pipeline Version": PIPELINE_VERSION,
            "Rules Version": rules.version,
            "Modo": "PRODUCT_DEFINITION_PILOT_NO_BASELINE_WRITE",
            "Pilot Status": status,
            "Produtos solicitados": len(cohort),
            "Produtos registrados": len(rows),
            "LLM API Calls": llm.api_calls,
            "LLM Total Tokens": llm.total_tokens,
            "Elapsed Seconds": round(time.monotonic() - pilot_started, 1),
            **{f"Selection::{k}": v for k, v in dict(selection_stats).items()},
            **dict(stats),
        }

    def _snapshot(status: str) -> None:
        if not rows:
            return
        export_excel(rows, output_path, _summary(status))
        LOG.info("PILOTO SNAPSHOT | status=%s | linhas=%d | arquivo=%s", status, len(rows), output_path)

    def _finish_item(idx: int, started: float, calls_before: int) -> None:
        row = rows[-1] if rows else {}
        elapsed = time.monotonic() - started
        calls = llm.api_calls - calls_before
        LOG.info(
            "PILOTO %03d/%03d | END | decision=%s | definido=%s | type=%s | taxonomy=%s|%s | reason=%s | live_llm_calls=%d | %.1fs",
            idx, len(cohort), safe(row.get("Pilot Decision")) or "-",
            safe(row.get("Produto Definido LLM")) or "-", safe(row.get("O que é (Tipo)")) or "-",
            safe(row.get("Category General")) or "-", safe(row.get("FAMILY")) or "-",
            safe(row.get("Reason Code")) or "-", calls, elapsed,
        )
        if idx % checkpoint_every == 0 or idx == len(cohort):
            _snapshot("RUNNING" if idx < len(cohort) else "COMPLETED")

    try:
        for idx, item in enumerate(cohort, 1):
            item_started = time.monotonic()
            calls_before = llm.api_calls
            brand, model, key = item.brand_original, item.model_original, item.key
            LOG.info("PILOTO %03d/%03d | START | %s | %s", idx, len(cohort), brand, model)
            prepared_item = prepared.get(key)
            if prepared_item is not None:
                raw, s1 = prepared_item
            else:
                raw, s1 = store.get(brand, model), None
            base = {
                "Marca": brand,
                "Modelo": model,
                "Raw State": raw.state,
                "Pipeline Version": PIPELINE_VERSION,
                "Processed At": now_iso(),
            }
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
                rows.append({
                    **base,
                    "Produto Definido LLM": "",
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": "",
                    "Pilot Decision": "SEM_CACHE_UTIL",
                    "Review Required": "SIM",
                    "Reason Code": raw.state,
                    "Motivo": raw.reason,
                })
                stats["sem_cache_util"] += 1
                _finish_item(idx, item_started, calls_before)
                continue

            if s1 is None:
                s1 = engine.classify(brand, model, raw.payload or {})
            ref_title, ref_url, ref_identity, _ = choose_reference_title(raw, brand, model, cfg)
            target_sources = [src for src in s1.sources if rank.get(src.relevance, 0) >= rank.get(min_identity, 3)]
            if not target_sources:
                rows.append({
                    **base,
                    "Produto Definido LLM": "",
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": ref_title,
                    "Fonte URL": ref_url,
                    "Pilot Decision": "COLLECTION_GAP",
                    "Review Required": "SIM",
                    "Reason Code": "NO_PROBABLE_TARGET_EVIDENCE",
                    "Motivo": "A coleta não entregou fonte target PROBABLE/STRONG suficiente para chamar a LLM semântica.",
                })
                stats["collection_gap"] += 1
                _finish_item(idx, item_started, calls_before)
                continue

            define_started = time.monotonic()
            LOG.info(
                "PILOTO %03d/%03d | LLM DEFINE START | provider=%s | model=%s | target_sources=%d",
                idx, len(cohort), llm.provider.upper(), llm.model, len(target_sources),
            )
            try:
                data, identify_cache, usage = llm.semantic_identify(key, raw.raw_hash, brand, model, s1)
            except OperationalPause as exc:
                LOG.error(
                    "PILOTO %03d/%03d | LLM DEFINE STOP | code=%s | %s",
                    idx, len(cohort), exc.code, safe(exc),
                )
                rows.append({
                    **base,
                    "Produto Definido LLM": "",
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": ref_title,
                    "Fonte URL": ref_url,
                    "Pilot Decision": "LLM_PROVIDER_ERROR",
                    "Review Required": "SIM",
                    "Reason Code": exc.code,
                    "Motivo": safe(exc),
                    "LLM Provider": llm.provider.upper() if llm.enabled_route() else "",
                    "LLM Model": llm.model if llm.enabled_route() else "",
                })
                stats["llm_provider_error"] += 1
                _finish_item(idx, item_started, calls_before)
                _snapshot("PARTIAL_PROVIDER_ERROR")
                LOG.error(
                    "PILOTO INTERROMPIDO COM SEGURANÇA APÓS FALHA DE PROVIDER. "
                    "O Excel parcial foi salvo; Base Final/Consolidado permanecem intactos."
                )
                break
            except Exception as exc:
                LOG.exception(
                    "PILOTO %03d/%03d | LLM DEFINE RUNTIME ERROR | %s",
                    idx, len(cohort), safe(exc),
                )
                rows.append({
                    **base,
                    "Produto Definido LLM": "",
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": ref_title,
                    "Fonte URL": ref_url,
                    "Pilot Decision": "LLM_RUNTIME_ERROR",
                    "Review Required": "SIM",
                    "Reason Code": type(exc).__name__,
                    "Motivo": safe(exc),
                })
                stats["llm_runtime_error"] += 1
                _finish_item(idx, item_started, calls_before)
                _snapshot("PARTIAL_RUNTIME_ERROR")
                break
            LOG.info(
                "PILOTO %03d/%03d | LLM DEFINE END | provider=%s | model=%s | cache=%s | decision=%s | raw=%s | %.1fs",
                idx, len(cohort), safe(usage.get("provider")).upper() or llm.provider.upper(),
                safe(usage.get("model")) or llm.model, "SIM" if identify_cache else "NÃO",
                safe(data.get("decision")).upper() or "-", safe(data.get("product_type_raw")).upper() or "-",
                time.monotonic() - define_started,
            )
            if usage.get("evidence_refs_valid") is False:
                grounded, grounding_reason = False, "SEMANTIC_EVIDENCE_REFS_INVALID"
            else:
                grounded, grounding_reason = semantic_grounding_ok(rules, s1, data)

            decision = safe(data.get("decision")).upper()
            refs = [x for x in (data.get("evidence_source_ids", []) or []) if isinstance(x, int)]
            title, url = choose_title(s1.sources, refs)
            raw_type = safe(data.get("product_type_raw")).upper()
            role = safe(data.get("product_role")).upper()
            domain = safe(data.get("domain_hint")).upper()
            power = safe(data.get("power")).upper() or "UNKNOWN"

            if not grounded:
                rows.append({
                    **base,
                    "Produto Definido LLM": raw_type,
                    "Papel do Produto": role,
                    "Contexto": domain,
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": "UNKNOWN",
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": title or ref_title,
                    "Fonte URL": url or ref_url,
                    "Pilot Decision": "LLM_REJECTED",
                    "Review Required": "SIM",
                    "Reason Code": grounding_reason,
                    "Motivo": safe(data.get("reason_short")),
                    "LLM Provider": safe(usage.get("provider")).upper(),
                    "LLM Model": safe(usage.get("model")),
                })
                stats["llm_rejected"] += 1
                _finish_item(idx, item_started, calls_before)
                continue

            if decision != "IDENTIFIED":
                rows.append({
                    **base,
                    "Produto Definido LLM": raw_type,
                    "Papel do Produto": role,
                    "Contexto": domain,
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": power,
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": title or ref_title,
                    "Fonte URL": url or ref_url,
                    "Pilot Decision": decision or "INSUFFICIENT_EVIDENCE",
                    "Review Required": "SIM",
                    "Reason Code": f"SEMANTIC_{decision or 'INSUFFICIENT_EVIDENCE'}",
                    "Motivo": safe(data.get("reason_short")),
                    "LLM Provider": safe(usage.get("provider")).upper(),
                    "LLM Model": safe(usage.get("model")),
                })
                stats["semantic_not_identified"] += 1
                _finish_item(idx, item_started, calls_before)
                continue

            canonical, candidates, map_reason = semantic_resolve_canonical(rules, raw_type, role)
            map_cache = False
            map_usage: Dict[str, Any] = {}
            if not canonical and candidates:
                map_started = time.monotonic()
                LOG.info(
                    "PILOTO %03d/%03d | LLM MAP START | provider=%s | model=%s | raw=%s | candidates=%d",
                    idx, len(cohort), llm.provider.upper(), llm.model, raw_type or "-", len(candidates),
                )
                try:
                    map_data, map_cache, map_usage = llm.semantic_map(
                        key, raw.raw_hash, raw_type, role, domain, candidates
                    )
                except OperationalPause as exc:
                    LOG.error(
                        "PILOTO %03d/%03d | LLM MAP STOP | code=%s | %s",
                        idx, len(cohort), exc.code, safe(exc),
                    )
                    rows.append({
                        **base,
                        "Produto Definido LLM": raw_type,
                        "Papel do Produto": role,
                        "Contexto": domain,
                        "O que é (Tipo)": "REVISAR",
                        "Alimentação": power,
                        "Category General": "",
                        "FAMILY": "",
                        "Título Real": title or ref_title,
                        "Fonte URL": url or ref_url,
                        "Pilot Decision": "LLM_MAP_PROVIDER_ERROR",
                        "Review Required": "SIM",
                        "Reason Code": exc.code,
                        "Motivo": safe(exc),
                    })
                    stats["llm_map_provider_error"] += 1
                    _finish_item(idx, item_started, calls_before)
                    _snapshot("PARTIAL_PROVIDER_ERROR")
                    LOG.error(
                        "PILOTO INTERROMPIDO COM SEGURANÇA DURANTE TAXONOMY MAP. "
                        "O Excel parcial foi salvo; Base Final/Consolidado permanecem intactos."
                    )
                    break
                LOG.info(
                    "PILOTO %03d/%03d | LLM MAP END | provider=%s | model=%s | cache=%s | decision=%s | canonical=%s | %.1fs",
                    idx, len(cohort), safe(map_usage.get("provider")).upper() or llm.provider.upper(),
                    safe(map_usage.get("model")) or llm.model, "SIM" if map_cache else "NÃO",
                    safe(map_data.get("decision")).upper() or "-", safe(map_data.get("canonical_type")) or "-",
                    time.monotonic() - map_started,
                )
                if safe(map_data.get("decision")).upper() == "MATCH":
                    proposed = safe(map_data.get("canonical_type"))
                    if proposed in {safe(x.get("type")) for x in candidates} and proposed in rules.types:
                        canonical = proposed
                        map_reason = "LLM_TAXONOMY_VALIDATED"
                    else:
                        map_reason = "LLM_TAXONOMY_INVALID_CHOICE"
                else:
                    map_reason = "LLM_TAXONOMY_NONE"

            candidate_text = " | ".join(
                f"{safe(x.get('type'))}:{float(x.get('score',0) or 0):.2f}"
                for x in candidates
            )
            if not canonical:
                rows.append({
                    **base,
                    "Produto Definido LLM": raw_type,
                    "Papel do Produto": role,
                    "Contexto": domain,
                    "O que é (Tipo)": "REVISAR",
                    "Alimentação": power,
                    "Category General": "",
                    "FAMILY": "",
                    "Título Real": title or ref_title,
                    "Fonte URL": url or ref_url,
                    "Taxonomy Candidates": candidate_text,
                    "Pilot Decision": "TAXONOMY_GAP",
                    "Review Required": "SIM",
                    "Reason Code": map_reason,
                    "Motivo": safe(data.get("reason_short")),
                    "LLM Provider": safe(usage.get("provider")).upper(),
                    "LLM Model": safe(usage.get("model")),
                })
                stats["taxonomy_gap"] += 1
                _finish_item(idx, item_started, calls_before)
                continue

            cg, fam, tax_reason = semantic_taxonomy_result(
                rules, s1, canonical, raw_type, power, refs, brand, model
            )
            valid_pair = (cg, fam) in rules.valid_pairs
            explicit_oo = (
                (cg, fam) == ("OTHERS", "OTHERS")
                and canonical in set(rules.type_policy.get("allow_explicit_others_others_types", []) or [])
                and tax_reason.startswith("TAXONOMY_EVIDENCE:")
            )
            review = (not valid_pair) or ((cg, fam) == ("OTHERS", "OTHERS") and not explicit_oo)
            rows.append({
                **base,
                "Produto Definido LLM": raw_type,
                "Papel do Produto": role,
                "Contexto": domain,
                "O que é (Tipo)": canonical,
                "Alimentação": power,
                "Category General": cg if valid_pair else "",
                "FAMILY": fam if valid_pair else "",
                "Título Real": title or ref_title,
                "Fonte URL": url or ref_url,
                "Evidence Phrase": safe(data.get("evidence_phrase")),
                "Taxonomy Candidates": candidate_text,
                "Pilot Decision": "MATCHED" if not review else "MATCHED_REVIEW",
                "Review Required": "SIM" if review else "NÃO",
                "Reason Code": map_reason,
                "Motivo": f"{safe(data.get('reason_short'))} | {tax_reason}",
                "LLM Provider": safe(usage.get("provider")).upper(),
                "LLM Model": safe(usage.get("model")),
                "LLM Identify Cache": "SIM" if identify_cache else "NÃO",
                "LLM Map Cache": "SIM" if map_cache else "NÃO",
                "LLM Total Tokens": int(usage.get("total_tokens",0) or 0) + int(map_usage.get("total_tokens",0) or 0),
            })
            stats["matched_review" if review else "matched"] += 1
            _finish_item(idx, item_started, calls_before)

        _snapshot("COMPLETED")
        LOG.info(
            "PILOTO DEFINIÇÃO LLM CONCLUÍDO | itens=%d | llm_calls=%d | %.1fs | arquivo=%s",
            len(rows), llm.api_calls, time.monotonic() - pilot_started, output_path,
        )
        LOG.info("BASE FINAL/CONSOLIDADO NÃO FORAM ALTERADOS.")
    except KeyboardInterrupt:
        _snapshot("INTERRUPTED")
        LOG.warning(
            "PILOTO INTERROMPIDO PELO USUÁRIO | linhas preservadas=%d | cache LLM persistido | arquivo=%s",
            len(rows), output_path,
        )
        raise
    except BaseException:
        _snapshot("FAILED_PARTIAL")
        raise
    finally:
        db.close()
        store.close()

# -----------------------------------------------------------------------------
# R9.7 - Cache-First Free-Tier / Local Taxonomy Retrieval
# -----------------------------------------------------------------------------
@dataclass
class CacheEvidenceItem:
    source_id: int
    title: str
    url: str
    snippet: str
    raw_excerpt: str
    identity: str
    identity_score: int
    model_match: str
    model_match_field: str
    generic_title: bool
    cross_brand_conflicts: List[str]
    tavily_score: float
    trusted: bool
    reason: str

    @property
    def evidence_text(self) -> str:
        parts = [self.title, self.snippet, self.raw_excerpt]
        return "\n".join(x for x in parts if safe(x)).strip()


@dataclass
class CacheEvidencePack:
    brand: str
    model: str
    business_key: str
    raw_state: str
    raw_hash: str
    items: List[CacheEvidenceItem]
    query_text: str
    best_identity: str
    trusted_count: int
    context_count: int
    cross_brand_count: int

    @property
    def trusted_items(self) -> List[CacheEvidenceItem]:
        return [x for x in self.items if x.trusted]

    @property
    def best_item(self) -> Optional[CacheEvidenceItem]:
        rank = {"WEAK": 0, "CONTEXTUAL": 1, "PROBABLE": 2, "STRONG": 3}
        candidates = [x for x in self.items if x.trusted and not x.generic_title]
        if not candidates:
            candidates = [x for x in self.items if x.trusted]
        if not candidates:
            candidates = [x for x in self.items if not x.generic_title]
        if not candidates:
            candidates = list(self.items)
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda x: (rank.get(x.identity, 0), int(not x.generic_title), x.tavily_score),
            reverse=True,
        )[0]


class KnownBrandIndex:
    """Índice local de marcas conhecidas no próprio cache.

    Serve apenas para detectar conflito explícito no título/URL (ex.: alvo HYUNDAI,
    fonte intitulada TCL ...). Não cria identidade positiva e não usa fuzzy.
    """
    def __init__(self, brands: Sequence[str]):
        self.variant_to_brands: Dict[str, set] = defaultdict(set)
        self.max_tokens = 1
        for brand in brands:
            b = safe(brand)
            if not b:
                continue
            for variant in _brand_identity_variants(b):
                v = compact(variant)
                if len(v) < 3:
                    continue
                self.variant_to_brands[v].add(canonical_display(b))
            toks = [t for t in norm_text(b).split() if t]
            self.max_tokens = max(self.max_tokens, min(5, len(toks)))

    def conflicts(self, target_brand: str, title: str, url: str) -> List[str]:
        primary = norm_text(f"{title} {url}")
        if not primary:
            return []
        target_variants = {compact(x) for x in _brand_identity_variants(target_brand)}
        tokens = [x for x in re.split(r"[^a-z0-9]+", primary) if x]
        found: set = set()
        nmax = min(self.max_tokens, 5)
        for n in range(1, nmax + 1):
            for i in range(0, max(0, len(tokens) - n + 1)):
                key = compact(" ".join(tokens[i:i+n]))
                if key in target_variants:
                    continue
                for brand in self.variant_to_brands.get(key, set()):
                    if canonical_display(brand) != canonical_display(target_brand):
                        found.add(brand)
        return sorted(found)


def _looks_generic_search_title(title: str, url: str = "") -> bool:
    raw = safe(title).strip()
    if not raw:
        return True
    folded = norm_text(raw)
    if not folded:
        return True
    generic_words = {
        "home", "homepage", "official", "official website", "website", "products",
        "product", "catalog", "catalogue", "support", "downloads", "download",
        "welcome", "shop", "store", "online store", "company", "manufacturer",
    }
    if folded in generic_words:
        return True
    # Título que é essencialmente um domínio (www.XYtronic-USA.com).
    domainish = re.sub(r"^https?://", "", raw, flags=re.I).strip().strip("/")
    if re.fullmatch(r"(?:www\.)?[a-z0-9][a-z0-9._-]*\.[a-z]{2,}(?:/.*)?", domainish, flags=re.I):
        return True
    host = safe(urlparse(url).hostname).casefold().removeprefix("www.")
    if host:
        label = host.split(".")[0]
        if compact(raw) in {compact(host), compact(label), compact("www." + host)}:
            return True
    if len(folded.split()) <= 3 and any(x in folded for x in ["official site", "official website"]):
        return True
    return False


def _model_windows_for_cache(text: str, model: str, cfg: Dict[str, Any], before: int, after: int, max_windows: int = 2) -> str:
    raw = safe(text)
    if not raw:
        return ""
    spans = _target_spans(raw, model, cfg)
    if not spans:
        return raw[: max(500, min(len(raw), before + after))]
    chunks: List[str] = []
    used: List[Tuple[int, int]] = []
    for start, end in spans[:max_windows]:
        lo = max(0, start - before)
        hi = min(len(raw), end + after)
        if any(lo >= a and hi <= b for a, b in used):
            continue
        used.append((lo, hi))
        chunks.append(raw[lo:hi].strip())
    return "\n...\n".join(x for x in chunks if x)


def build_cache_evidence_pack(
    raw: RawEvidence,
    cfg: Dict[str, Any],
    brand_index: KnownBrandIndex,
) -> CacheEvidencePack:
    ccfg = cfg.get("cache_first_free_tier", {}) or {}
    max_sources = max(1, int(ccfg.get("max_sources", 5) or 5))
    before = max(100, int(ccfg.get("model_window_before", 450) or 450))
    after = max(300, int(ccfg.get("model_window_after", 1200) or 1200))
    max_query_chars = max(1000, int(ccfg.get("max_query_chars", 6000) or 6000))
    include_cross = bool(ccfg.get("include_cross_brand_as_context", True))
    rank = {"WEAK": 0, "CONTEXTUAL": 1, "PROBABLE": 2, "STRONG": 3}

    payload = raw.payload or {}
    rows = [x for x in (payload.get("results", []) or []) if isinstance(x, dict)]
    items: List[CacheEvidenceItem] = []
    identity_model = derive_search_model(raw.brand, raw.model, cfg)
    for i, res in enumerate(rows, 1):
        title = safe(res.get("title"))
        url = safe(res.get("url"))
        snippet = safe(res.get("content"))
        raw_content = res.get("raw_content")
        if not isinstance(raw_content, str):
            raw_content = ""
        body_for_identity = "\n".join(x for x in [snippet, raw_content[:5000]] if x)
        meta = _source_identity_for_result(raw.brand, identity_model, {
            "title": title, "url": url, "content": body_for_identity,
        }, cfg)
        conflicts = brand_index.conflicts(raw.brand, title, url)
        generic = _looks_generic_search_title(title, url)
        excerpt_source = raw_content or snippet
        excerpt = _model_windows_for_cache(excerpt_source, identity_model, cfg, before, after)
        level = safe(meta.get("level")) or "WEAK"
        # Fonte trusted exige identidade >= PROBABLE e nenhuma marca concorrente
        # explícita no campo primário. PROBABLE com marca concorrente ainda pode
        # ser mantida apenas como contexto manual, nunca como base de auto-aceite.
        trusted = rank.get(level, 0) >= rank["PROBABLE"] and not conflicts
        if generic and not excerpt:
            trusted = False
        try:
            tavily_score = float(res.get("score", 0) or 0)
        except Exception:
            tavily_score = 0.0
        reason_parts = [f"IDENTITY={level}"]
        if conflicts:
            reason_parts.append("CROSS_BRAND=" + ",".join(conflicts[:4]))
        if generic:
            reason_parts.append("GENERIC_TITLE")
        if trusted:
            reason_parts.append("TRUSTED")
        elif include_cross and rank.get(level, 0) >= rank["PROBABLE"]:
            reason_parts.append("CONTEXT_ONLY")
        items.append(CacheEvidenceItem(
            source_id=i,
            title=title,
            url=url,
            snippet=snippet,
            raw_excerpt=excerpt,
            identity=level,
            identity_score=int(meta.get("score", 0) or 0),
            model_match=safe(meta.get("model_match")),
            model_match_field=safe(meta.get("model_match_field")),
            generic_title=generic,
            cross_brand_conflicts=conflicts,
            tavily_score=tavily_score,
            trusted=trusted,
            reason=" | ".join(reason_parts),
        ))

    def order_key(x: CacheEvidenceItem) -> Tuple[int, int, int, float]:
        return (
            int(x.trusted),
            rank.get(x.identity, 0),
            int(not x.generic_title),
            x.tavily_score,
        )

    items.sort(key=order_key, reverse=True)
    items = items[:max_sources]
    chunks: List[str] = []
    for item in items:
        if not item.trusted and not include_cross:
            continue
        label = "TRUSTED" if item.trusted else "CONTEXT_ONLY"
        text = item.evidence_text
        if not text:
            continue
        chunks.append(f"[{label} SOURCE {item.source_id}]\n{text}")
    query_text = "\n---\n".join(chunks)[:max_query_chars]
    trusted = [x for x in items if x.trusted]
    best_identity = max((x.identity for x in trusted), key=lambda v: rank.get(v, 0), default="WEAK")
    return CacheEvidencePack(
        brand=raw.brand,
        model=raw.model,
        business_key=business_key(raw.brand, raw.model),
        raw_state=raw.state,
        raw_hash=raw.raw_hash,
        items=items,
        query_text=query_text,
        best_identity=best_identity,
        trusted_count=len(trusted),
        context_count=len(items) - len(trusted),
        cross_brand_count=sum(1 for x in items if x.cross_brand_conflicts),
    )


class LocalTaxonomyRetriever:
    """Recuperador local de TYPEs da taxonomia sem API paga.

    Ordem de backend:
      1) sentence-transformers local (se pacote + modelo já estiverem disponíveis)
      2) scikit-learn TF-IDF char n-gram
      3) similaridade lexical pura (stdlib)

    O backend local nunca escreve Category/FAMILY por conta própria. Ele apenas
    propõe PRODUCT_TYPE canônico; o mapeamento final continua em regras_negocio.json.
    """
    def __init__(self, rules: Rules, cfg: Dict[str, Any]):
        self.rules = rules
        self.cfg = cfg
        self.ccfg = cfg.get("cache_first_free_tier", {}) or {}
        self.backend_requested = safe(self.ccfg.get("backend", "auto")).casefold() or "auto"
        self.model_id = safe(self.ccfg.get(
            "sentence_transformers_model",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ))
        self.entries: List[Tuple[str, str, str]] = []
        self._build_entries()
        self.backend = "lexical"
        self.signature = "lexical-v1"
        self._st_model = None
        self._st_matrix = None
        self._tfidf = None
        self._tfidf_matrix = None
        self._init_backend()

    def _build_entries(self) -> None:
        seen = set()
        def add(typ: str, phrase: str, origin: str) -> None:
            t, p = safe(typ), safe(phrase)
            if t not in self.rules.types or not p:
                return
            key = (t, norm_text(p))
            if not key[1] or key in seen:
                return
            seen.add(key)
            self.entries.append((t, p, origin))

        for typ, data in self.rules.types.items():
            add(typ, typ.replace("_", " "), "TYPE")
            for alias in (data.get("aliases", []) or []):
                add(typ, alias, "ALIAS")
            fp = data.get("fallback_pair")
            if isinstance(fp, list) and len(fp) == 2:
                add(typ, safe(fp[1]), "FAMILY")
        for row in self.rules.taxonomy_rules:
            typ = safe(row.get("primary_type"))
            add(typ, safe(row.get("family")), "TAXONOMY_FAMILY")
            add(typ, safe(row.get("category_general")), "TAXONOMY_CATEGORY")
        for row in self.rules.taxonomy_evidence_rules:
            typ = safe(row.get("primary_type"))
            add(typ, safe(row.get("family")), "EVIDENCE_FAMILY")
            for alias in (row.get("aliases", []) or []):
                add(typ, alias, "EVIDENCE_ALIAS")
        for phrase, typ in (self.rules.semantic_policy.get("raw_type_alias_to_canonical", {}) or {}).items():
            add(safe(typ), safe(phrase), "SEMANTIC_BRIDGE")

    def _init_backend(self) -> None:
        wanted = self.backend_requested
        allow_st = wanted in {"auto", "sentence_transformers", "sentence-transformers", "st"}
        if allow_st:
            try:
                from sentence_transformers import SentenceTransformer
                local_only = bool(self.ccfg.get("sentence_transformers_local_only", True))
                kwargs: Dict[str, Any] = {}
                if local_only:
                    kwargs["local_files_only"] = True
                self._st_model = SentenceTransformer(self.model_id, **kwargs)
                phrases = [p for _, p, _ in self.entries]
                self._st_matrix = self._st_model.encode(
                    phrases,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                self.backend = "sentence_transformers"
                self.signature = f"sentence_transformers:{self.model_id}"
                LOG.info("CACHE-FIRST LOCAL BACKEND | sentence-transformers | model=%s | entries=%d", self.model_id, len(self.entries))
                return
            except Exception as exc:
                LOG.warning(
                    "CACHE-FIRST sentence-transformers indisponível (%s: %s). Fallback local -> TF-IDF.",
                    type(exc).__name__, safe(exc)[:300],
                )
                self._st_model = None
                self._st_matrix = None
                if wanted not in {"auto", "sentence_transformers", "sentence-transformers", "st"}:
                    raise

        if wanted in {"auto", "tfidf", "sklearn", "sentence_transformers", "sentence-transformers", "st"}:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer
                self._tfidf = TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    lowercase=True,
                    strip_accents="unicode",
                    sublinear_tf=True,
                )
                self._tfidf_matrix = self._tfidf.fit_transform([p for _, p, _ in self.entries])
                self.backend = "sklearn_tfidf"
                self.signature = "sklearn_tfidf:char_wb_3_5:v1"
                LOG.info("CACHE-FIRST LOCAL BACKEND | scikit-learn TF-IDF | entries=%d", len(self.entries))
                return
            except Exception as exc:
                LOG.warning("CACHE-FIRST TF-IDF indisponível (%s: %s). Fallback -> lexical stdlib.", type(exc).__name__, safe(exc)[:300])

        self.backend = "lexical"
        self.signature = "lexical:jaccard-sequencematcher:v1"
        LOG.info("CACHE-FIRST LOCAL BACKEND | lexical stdlib | entries=%d", len(self.entries))

    def _aggregate(self, scores: Sequence[float], top_k: int) -> List[Dict[str, Any]]:
        best: Dict[str, Dict[str, Any]] = {}
        for idx, raw_score in enumerate(scores):
            try:
                score = float(raw_score)
            except Exception:
                continue
            typ, phrase, origin = self.entries[idx]
            prev = best.get(typ)
            if prev is None or score > float(prev["score"]):
                best[typ] = {"type": typ, "score": score, "matched_phrase": phrase, "origin": origin}
        rows = sorted(best.values(), key=lambda x: (-float(x["score"]), safe(x["type"])))
        return rows[:max(1, top_k)]

    @staticmethod
    def _meaningful_tokens(value: str) -> List[str]:
        stop = {
            "a", "an", "the", "of", "for", "with", "and", "or", "to", "de", "da", "do", "das", "dos",
            "el", "la", "los", "las", "y", "con", "para", "product", "products", "tool", "tools", "machine",
            "electric", "electrical", "battery", "corded", "cordless", "petrol", "gas", "part", "parts", "spare",
        }
        return [t for t in norm_text(value).split() if len(t) >= 3 and t not in stop]

    def _rerank_local_lexical_support(self, rows: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Conservador para TF-IDF/lexical: sem âncora lexical, não força match.

        Char n-gram sozinho pode confundir 'air conditioner' com 'mower conditioner'.
        A recuperação local só sobe candidatos que compartilhem palavras/bigramas
        informativos com a evidência. Sentence-transformers não usa este redutor.
        """
        if self.backend == "sentence_transformers":
            return rows
        qtokens = self._meaningful_tokens(query)
        qset = set(qtokens)
        qbigrams = set(zip(qtokens, qtokens[1:]))
        out: List[Dict[str, Any]] = []
        qfold = norm_text(query)
        for row in rows:
            typ = safe(row.get("type"))
            phrase = safe(row.get("matched_phrase"))
            ptokens = self._meaningful_tokens(phrase)
            pset = set(ptokens)
            if not pset:
                continue
            overlap = len(pset & qset) / max(1, len(pset))
            pbigrams = set(zip(ptokens, ptokens[1:]))
            bigram_overlap = len(pbigrams & qbigrams) / max(1, len(pbigrams)) if pbigrams else 0.0
            exact = 1.0 if norm_text(phrase) and norm_text(phrase) in qfold else 0.0
            # Bloqueia candidatos cuja própria taxonomia declara contexto negativo.
            negatives = (self.rules.types.get(typ, {}) or {}).get("negative_aliases", []) or []
            negative_hit = next((x for x in negatives if safe(x) and contains_alias_prepared(qfold, x)), "")
            if negative_hit:
                continue
            base = float(row.get("score", 0.0) or 0.0)
            support_multiplier = 0.30 + (0.40 * overlap) + (0.20 * bigram_overlap) + (0.10 * exact)
            adjusted = base * support_multiplier
            # Sem qualquer palavra informativa compartilhada, TF-IDF char-ngram é
            # tratado como ruído, salvo match quase-exato excepcional.
            if overlap <= 0.0 and exact <= 0.0 and base < 0.80:
                continue
            row = dict(row)
            row["base_score"] = base
            row["lexical_overlap"] = overlap
            row["bigram_overlap"] = bigram_overlap
            row["score"] = adjusted
            out.append(row)
        out.sort(key=lambda x: (-float(x.get("score", 0.0) or 0.0), safe(x.get("type"))))
        return out

    def retrieve(self, text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        query = safe(text)
        if not query:
            return []
        # Embedding models têm contexto limitado; o início privilegia título e
        # primeiras janelas target, que o EvidencePack ordena por qualidade.
        query = query[:8000]
        if self.backend == "sentence_transformers" and self._st_model is not None and self._st_matrix is not None:
            q = self._st_model.encode([query], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)[0]
            scores = self._st_matrix @ q
            return self._aggregate(scores, top_k)
        if self.backend == "sklearn_tfidf" and self._tfidf is not None and self._tfidf_matrix is not None:
            from sklearn.metrics.pairwise import cosine_similarity
            q = self._tfidf.transform([query])
            scores = cosine_similarity(q, self._tfidf_matrix).ravel()
            raw_rows = self._aggregate(scores, max(top_k * 6, 30))
            return self._rerank_local_lexical_support(raw_rows, query)[:max(1, top_k)]

        qnorm = norm_text(query)
        qtokens = set(qnorm.split())
        vals: List[float] = []
        for _, phrase, _ in self.entries:
            pnorm = norm_text(phrase)
            ptokens = set(pnorm.split())
            inter = len(qtokens & ptokens)
            union = len(qtokens | ptokens) or 1
            jaccard = inter / union
            seq = SequenceMatcher(None, pnorm, qnorm[: max(len(pnorm) * 4, 120)]).ratio()
            contains = 1.0 if pnorm and pnorm in qnorm else 0.0
            vals.append(max(contains, 0.65 * jaccard + 0.35 * seq))
        raw_rows = self._aggregate(vals, max(top_k * 6, 30))
        return self._rerank_local_lexical_support(raw_rows, query)[:max(1, top_k)]


class CacheFirstResultStore:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS cache_first_results (
                result_id TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                rules_hash TEXT NOT NULL,
                retriever_signature TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    @staticmethod
    def make_id(key: str, raw_hash: str, rules_hash: str, signature: str) -> str:
        return hashlib.sha256(f"{key}|{raw_hash}|{rules_hash}|{signature}".encode("utf-8")).hexdigest()

    def get(self, key: str, raw_hash: str, rules_hash: str, signature: str) -> Optional[Dict[str, Any]]:
        rid = self.make_id(key, raw_hash, rules_hash, signature)
        row = self.conn.execute("SELECT result_json FROM cache_first_results WHERE result_id=?", (rid,)).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["result_json"])
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def put(self, key: str, raw_hash: str, rules_hash: str, signature: str, result: Dict[str, Any]) -> None:
        rid = self.make_id(key, raw_hash, rules_hash, signature)
        self.conn.execute(
            """INSERT INTO cache_first_results(result_id,business_key,raw_hash,rules_hash,retriever_signature,result_json,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(result_id) DO UPDATE SET result_json=excluded.result_json, created_at=excluded.created_at""",
            (rid, key, raw_hash, rules_hash, signature, json.dumps(result, ensure_ascii=False, default=str), now_iso()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def _cache_first_stage1_payload(pack: CacheEvidencePack) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for item in pack.trusted_items:
        rows.append({
            "title": item.title,
            "content": "\n".join(x for x in [item.snippet, item.raw_excerpt] if x),
            "url": item.url,
            "score": item.tavily_score,
        })
    return {"results": rows}


def _cache_first_candidate_taxonomy(rules: Rules, candidate: Dict[str, Any], power: str, pack: CacheEvidencePack) -> Tuple[str, str, str]:
    typ = safe(candidate.get("type"))
    if typ not in rules.types:
        return "", "", "TYPE_OUTSIDE_TAXONOMY"
    return rules.map_taxonomy(typ, power or "UNKNOWN", pack.query_text, pack.brand, pack.model)


def _cache_first_product_title(item: Optional[CacheEvidenceItem], model: str) -> str:
    """Nunca devolve domínio/homepage como Título Real.

    Quando o SERP title é genérico, usa um trecho curto da evidência local que
    contenha o modelo; isso atende o requisito de levar informação de produto à
    base de revisão sem fingir que o domínio é título do anúncio.
    """
    if item is None:
        return ""
    if item.title and not item.generic_title:
        return item.title[:300]
    candidates = [item.raw_excerpt, item.snippet]
    model_compact = compact(model)
    for text in candidates:
        raw = safe(text).replace("\r", " ").replace("\n", " ")
        if not raw:
            continue
        pieces = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\s{2,}", raw) if x.strip()]
        for piece in pieces:
            if model_compact and model_compact in compact(piece):
                return piece[:300]
        return raw[:300]
    return ""


def _cache_first_snapshot(rows: List[Dict[str, Any]], output_path: Path, status: str) -> None:
    import pandas as pd
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cache First")
        summary = pd.DataFrame([
            {"Status": status, "Linhas": len(rows), "Gerado em": now_iso()},
        ])
        summary.to_excel(writer, index=False, sheet_name="Resumo")


def run_cache_first_free_tier(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Piloto escalável, 0 Search e 0 LLM/API paga.

    Reaproveita todo o cache Tavily, reconstrói evidência por Marca+Modelo,
    bloqueia conflito explícito de marca e usa recuperação taxonômica local.
    Nunca altera Base Final/Consolidado.
    """
    ccfg = cfg.get("cache_first_free_tier", {}) or {}
    cache_path = Path(args.cache or cfg["files"]["cache"])
    output_path = Path(args.output or cfg["files"].get("cache_first_output", "Piloto_Cache_First_Free_Tier_R9_7.xlsx"))
    evidence_path = Path(cfg["files"].get("cache_first_evidence_jsonl", "Cache_Evidence_Packs_R9_7.jsonl"))
    db_path = Path(args.db or cfg["files"].get("classification_db", "pipeline_v3.sqlite"))
    target_states = {safe(x) for x in (ccfg.get("target_stage1_states", ["UNRESOLVED", "AMBIGUOUS"]) or []) if safe(x)}
    max_items = int(args.max_items or ccfg.get("default_max_items", 100) or 100)
    checkpoint_every = max(1, int(ccfg.get("checkpoint_every", 25) or 25))
    top_k = max(1, int(ccfg.get("top_k", 5) or 5))
    auto_accept = bool(ccfg.get("auto_accept", False))
    auto_margin = float(ccfg.get("auto_accept_margin", 0.10) or 0.10)

    store = TavilyReadOnlyStore(cache_path)
    result_store = CacheFirstResultStore(db_path)
    try:
        catalog = store.catalog()
        brands = sorted({b for b, _, _ in catalog if safe(b)})
        brand_index = KnownBrandIndex(brands)
        retriever = LocalTaxonomyRetriever(rules, cfg)
        engine = DeterministicEngine(rules, cfg)
        LOG.info(
            "CACHE-FIRST START | cache=%d produtos | brands=%d | backend=%s | alvo=%d | 0 Search | 0 LLM",
            len(catalog), len(brands), retriever.backend, max_items,
        )

        # Primeiro monta coorte local inteira e faz sampling estável por hash.
        cohort: List[Tuple[str, RawEvidence, CacheEvidencePack, Stage1Result]] = []
        scan_stats = defaultdict(int)
        for brand, model, key in catalog:
            raw = store.get(brand, model)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                scan_stats["raw_unusable"] += 1
                continue
            pack = build_cache_evidence_pack(raw, cfg, brand_index)
            s1 = engine.classify(brand, model, _cache_first_stage1_payload(pack))
            scan_stats[f"stage1_{s1.state}"] += 1
            if target_states and s1.state not in target_states:
                continue
            if not pack.query_text:
                scan_stats["no_query"] += 1
            cohort.append((key, raw, pack, s1))

        cohort.sort(key=lambda x: hashlib.sha256(x[0].encode("utf-8")).hexdigest())
        if max_items > 0:
            cohort = cohort[:max_items]
        LOG.info(
            "CACHE-FIRST COHORT READY | selecionados=%d | scanned=%d | unresolved=%d | ambiguous=%d | resolved_rule=%d",
            len(cohort), len(catalog), scan_stats.get("stage1_UNRESOLVED", 0), scan_stats.get("stage1_AMBIGUOUS", 0), scan_stats.get("stage1_RESOLVED_RULE", 0),
        )

        rows: List[Dict[str, Any]] = []
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with evidence_path.open("w", encoding="utf-8") as evf:
            for idx, (key, raw, pack, s1) in enumerate(cohort, 1):
                cached = result_store.get(key, raw.raw_hash, rules.hash, retriever.signature)
                if cached is not None:
                    result = dict(cached)
                    result["Checkpoint Cache"] = "SIM"
                    LOG.info("CACHE-FIRST %03d/%03d | CACHE HIT | %s | %s | decision=%s", idx, len(cohort), pack.brand, pack.model, safe(result.get("Decision")))
                else:
                    LOG.info(
                        "CACHE-FIRST %03d/%03d | START | %s | %s | trusted=%d | context=%d | identity=%s",
                        idx, len(cohort), pack.brand, pack.model, pack.trusted_count, pack.context_count, pack.best_identity,
                    )
                    candidates = retriever.retrieve(pack.query_text, top_k=top_k) if pack.query_text else []
                    top = candidates[0] if candidates else {}
                    second = candidates[1] if len(candidates) > 1 else {}
                    top_score = float(top.get("score", 0.0) or 0.0)
                    second_score = float(second.get("score", 0.0) or 0.0)
                    margin = top_score - second_score
                    power = safe(s1.power) or "UNKNOWN"
                    best_item = pack.best_item
                    top_cg = top_fam = top_tax_reason = ""
                    if top:
                        top_cg, top_fam, top_tax_reason = _cache_first_candidate_taxonomy(rules, top, power, pack)

                    thresholds = ccfg.get("proposal_thresholds", {}) or {}
                    proposal_threshold = float(thresholds.get(retriever.backend, thresholds.get("default", 0.20)) or 0.20)
                    accept_thresholds = ccfg.get("auto_accept_thresholds", {}) or {}
                    accept_threshold = float(accept_thresholds.get(retriever.backend, accept_thresholds.get("default", 0.80)) or 0.80)
                    no_conflict = pack.cross_brand_count == 0
                    strong = pack.best_identity == "STRONG"

                    exact_alias_auto = bool(ccfg.get("exact_alias_auto_accept", True))
                    matched_phrase = safe(top.get("matched_phrase"))
                    exact_phrase = bool(matched_phrase and contains_alias(pack.query_text, matched_phrase))
                    exact_phrase_specific = len(LocalTaxonomyRetriever._meaningful_tokens(matched_phrase)) >= 2

                    if pack.trusted_count == 0:
                        decision = "CACHE_IDENTITY_GAP"
                        review = "SIM"
                    elif not pack.query_text:
                        decision = "CACHE_INSUFFICIENT"
                        review = "SIM"
                    elif not top or top_score < proposal_threshold:
                        decision = "LOCAL_LOW_SIGNAL"
                        review = "SIM"
                    elif exact_alias_auto and strong and no_conflict and exact_phrase and exact_phrase_specific and (top_cg, top_fam) in rules.valid_pairs and (top_cg, top_fam) != ("OTHERS", "OTHERS"):
                        decision = "LOCAL_EXACT_ALIAS_ACCEPTED"
                        review = "NÃO"
                    elif auto_accept and strong and no_conflict and top_score >= accept_threshold and margin >= auto_margin and (top_cg, top_fam) in rules.valid_pairs and (top_cg, top_fam) != ("OTHERS", "OTHERS"):
                        decision = "LOCAL_SEMANTIC_ACCEPTED"
                        review = "NÃO"
                    else:
                        decision = "LOCAL_SEMANTIC_PROPOSAL"
                        review = "SIM"

                    cand_text_parts = []
                    for cand in candidates:
                        cg, fam, _ = _cache_first_candidate_taxonomy(rules, cand, power, pack)
                        cand_text_parts.append(f"{safe(cand.get('type'))}:{float(cand.get('score',0) or 0):.3f}->{cg}|{fam} [{safe(cand.get('matched_phrase'))}]")

                    result = {
                        "Marca": pack.brand,
                        "Modelo": pack.model,
                        "Raw State": pack.raw_state,
                        "Pipeline Version": PIPELINE_VERSION,
                        "Build": BUILD_LABEL,
                        "Processed At": now_iso(),
                        "Stage1 State": s1.state,
                        "Stage1 Type": safe(s1.product_type),
                        "Stage1 Power": power,
                        "Best Identity": pack.best_identity,
                        "Trusted Sources": pack.trusted_count,
                        "Context Sources": pack.context_count,
                        "Cross Brand Sources": pack.cross_brand_count,
                        "Local Backend": retriever.backend,
                        "Local Model": retriever.model_id if retriever.backend == "sentence_transformers" else retriever.signature,
                        "Suggested Type": safe(top.get("type")),
                        "Matched Taxonomy Phrase": safe(top.get("matched_phrase")),
                        "Similarity Score": round(top_score, 6),
                        "Similarity Margin": round(margin, 6),
                        "Suggested Category General": top_cg,
                        "Suggested FAMILY": top_fam,
                        "Taxonomy Reason": top_tax_reason,
                        "Top Candidates": " ; ".join(cand_text_parts),
                        "Título Real": _cache_first_product_title(best_item, pack.model),
                        "Fonte URL": best_item.url if best_item else "",
                        "Evidence Extract": (best_item.evidence_text[:2200] if best_item else ""),
                        "Evidence Reason": best_item.reason if best_item else "",
                        "Decision": decision,
                        "Review Required": review,
                        "Checkpoint Cache": "NÃO",
                    }
                    result_store.put(key, raw.raw_hash, rules.hash, retriever.signature, result)
                    LOG.info(
                        "CACHE-FIRST %03d/%03d | END | decision=%s | suggested=%s | score=%.3f | margin=%.3f | taxonomy=%s|%s",
                        idx, len(cohort), decision, safe(top.get("type")) or "-", top_score, margin, top_cg or "-", top_fam or "-",
                    )

                rows.append(result)
                evidence_payload = {
                    "brand": pack.brand,
                    "model": pack.model,
                    "business_key": pack.business_key,
                    "raw_hash": pack.raw_hash,
                    "best_identity": pack.best_identity,
                    "query_text": pack.query_text,
                    "sources": [
                        {
                            "source_id": x.source_id,
                            "title": x.title,
                            "url": x.url,
                            "identity": x.identity,
                            "generic_title": x.generic_title,
                            "cross_brand_conflicts": x.cross_brand_conflicts,
                            "trusted": x.trusted,
                            "reason": x.reason,
                            "evidence_text": x.evidence_text,
                        }
                        for x in pack.items
                    ],
                }
                evf.write(json.dumps(evidence_payload, ensure_ascii=False, default=str) + "\n")

                if idx % checkpoint_every == 0:
                    _cache_first_snapshot(rows, output_path, "RUNNING")
                    LOG.info("CACHE-FIRST SNAPSHOT | linhas=%d | arquivo=%s", len(rows), output_path)

        _cache_first_snapshot(rows, output_path, "COMPLETED")
        counts: Dict[str, int] = defaultdict(int)
        for row in rows:
            counts[safe(row.get("Decision"))] += 1
        LOG.info(
            "CACHE-FIRST CONCLUÍDO | itens=%d | backend=%s | decisions=%s | arquivo=%s | evidence_jsonl=%s",
            len(rows), retriever.backend, dict(counts), output_path, evidence_path,
        )
        LOG.info("BASE FINAL/CONSOLIDADO NÃO FORAM ALTERADOS | Search=0 | LLM/API=0")
    finally:
        result_store.close()
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

    semantic_cases = qg.get("SEMANTIC_RECOVERY_CASES", []) or []
    semantic_passed = 0
    for i, case in enumerate(semantic_cases, 1):
        cid = safe(case.get("id")) or f"semantic_{i}"
        raw_type = safe(case.get("semantic_raw"))
        role = safe(case.get("role"))
        power = safe(case.get("power")) or "UNKNOWN"
        canonical, _, _ = semantic_resolve_canonical(rules, raw_type, role)
        expected_type = safe(case.get("expect_type"))
        if canonical != expected_type:
            errors.append(f"{cid}: semantic canonical={canonical!r}, esperado={expected_type!r}")
            continue
        title = safe(case.get("title"))
        probe = engine.classify(
            safe(case.get("brand")) or "TEST",
            safe(case.get("model")) or f"SEM{i}",
            {"results": [{"title": title, "content": title, "url": "https://example.com/semantic-case"}]},
        )
        refs = [probe.sources[0].source_id] if probe.sources else []
        cg, fam, _ = semantic_taxonomy_result(
            rules, probe, canonical, raw_type, power, refs,
            safe(case.get("brand")), safe(case.get("model")),
        )
        if cg != safe(case.get("expect_category")) or fam != safe(case.get("expect_family")):
            errors.append(
                f"{cid}: semantic taxonomy={cg}|{fam}, esperado={safe(case.get('expect_category'))}|{safe(case.get('expect_family'))}"
            )
            continue
        semantic_passed += 1

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
        "semantic_cases": len(semantic_cases),
        "semantic_passed": semantic_passed,
    }


def validate_all(rules: Rules, cfg: Dict[str, Any], cache_override: str = "") -> None:
    errors = rules.validate()
    if errors:
        for e in errors:
            LOG.error(e)
        raise RuntimeError(f"Validação falhou: {len(errors)} erro(s)")

    qg = run_quality_gates(rules, cfg)
    LOG.info(
        "QUALITY GATES OK | synthetic=%d | identity=%d | real_explicit=%d/%d | reference_only=%d | semantic=%d/%d",
        qg["synthetic_cases"], qg["identity_passed"], qg["real_passed"], qg["real_active"], qg["real_reference_only"], qg.get("semantic_passed",0), qg.get("semantic_cases",0),
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
    p = argparse.ArgumentParser(description="Pipeline de Classificação de Produtos v3")
    p.add_argument("--mode", choices=["validate", "inventory", "collect", "curate", "classify", "full", "recover", "repair", "semantic", "cachefirst"], default="validate")
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
    p.add_argument("--llm-provider", default="")
    p.add_argument("--llm-model", default="")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-checkpoint", action="store_true")
    p.add_argument("--reprocess", action="store_true")
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--max-tavily-new", "--max-search-new", dest="max_tavily_new", type=int, default=0)
    p.add_argument("--search-provider", choices=["tavily", "serper", "brightdata"], default="")
    p.add_argument("--recollect-empty", action="store_true")
    p.add_argument("--recollect-low-evidence", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def _menu_limit(prompt: str) -> int:
    """Permite piloto sem expor argumentos de linha de comando ao operador."""
    raw = input(prompt).strip()
    if not raw:
        return 0
    if not raw.isdigit() or int(raw) <= 0:
        print("Valor inválido. Será considerado TODOS.")
        return 0
    return int(raw)


def _menu_search_provider() -> str:
    raw = input("Provider de busca [TAVILY / SERPER / BRIGHTDATA] (ENTER = TAVILY): ").strip().casefold()
    if not raw:
        return "tavily"
    aliases = {"bright data": "brightdata", "bright_data": "brightdata", "bright-data": "brightdata"}
    raw = aliases.get(raw, raw)
    if raw not in ALLOWED_SEARCH_PROVIDERS:
        print("Provider inválido. Será usado TAVILY.")
        return "tavily"
    return raw


def interactive_menu(base: Path) -> argparse.Namespace:
    parser = build_parser()
    print("=" * 86)
    print(f" PIPELINE ENRIQUECEDOR DE PRODUTOS {PIPELINE_VERSION} | {BUILD_LABEL}")
    print(" Search/Cache -> Identity -> Evidence Pack -> Stage 1 -> Local Taxonomy Retrieval -> optional LLM")
    print(" COLLECTION HARD RULE: 1 produto = no máximo 1 request | sem retry/fallback/extract/advanced")
    print(" Revisão B.I=OK = HARD LOCK ABSOLUTO | novos itens mantêm Revisão B.I vazia")
    print("=" * 86)
    print("1 - COLETA / EXTRAÇÃO R9.6 (1 REQUEST/PRODUTO)")
    print("2 - CLASSIFICAÇÃO")
    print("3 - PROCESSO COMPLETO")
    print("4 - RECUPERAR PENDÊNCIAS DO CACHE (SEM BUSCA)")
    print("5 - AUDITORIA / REPARO BASE (SEM LLM)")
    print("6 - CURAR CACHE LEGADO (SEM BUSCA / 0 CRÉDITOS)")
    print("7 - PILOTO DEFINIÇÃO LLM LEGADO/EXPERIMENTAL (NÃO ALTERA BASE)")
    print("8 - CACHE-FIRST FREE-TIER LOCAL (0 BUSCA / 0 LLM / NÃO ALTERA BASE)")
    print("9 - SAIR")
    op = input("Opção: ").strip()
    if op == "1":
        provider = _menu_search_provider()
        limite = _menu_limit("Limite de produtos para coleta (ENTER = todos): ")
        argv = ["--mode", "collect", "--search-provider", provider]
        low = input("Recoletar também RAW existente com evidência target fraca? (S/N): ").strip().upper()
        if low == "S":
            argv += ["--recollect-low-evidence"]
        if limite:
            argv += ["--max-search-new", str(limite)]
        return parser.parse_args(argv)
    if op == "2":
        limite = _menu_limit("Limite de produtos para piloto (ENTER = todos): ")
        argv = ["--mode", "classify"]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "3":
        provider = _menu_search_provider()
        limite_search = _menu_limit("Limite de produtos na coleta (ENTER = todos): ")
        limite_class = _menu_limit("Limite de produtos na classificação (ENTER = todos): ")
        argv = ["--mode", "full", "--search-provider", provider]
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
        argv = ["--mode", "recover"]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "5":
        return parser.parse_args(["--mode", "repair"])
    if op == "6":
        limite = _menu_limit("Limite de produtos do cache para piloto (ENTER = todos): ")
        argv = ["--mode", "curate"]
        if limite:
            argv += ["--max-items", str(limite)]
        return parser.parse_args(argv)
    if op == "7":
        raw = input("Limite do piloto LLM legado (ENTER = 5): ").strip()
        limite = 5
        if raw:
            if raw.isdigit() and int(raw) > 0:
                limite = int(raw)
            else:
                print("Valor inválido. Será usado 5.")
        return parser.parse_args(["--mode", "semantic", "--max-items", str(limite)])
    if op == "8":
        raw = input("Limite do piloto Cache-First local (ENTER = 100): ").strip()
        limite = 100
        if raw:
            if raw.isdigit() and int(raw) > 0:
                limite = int(raw)
            else:
                print("Valor inválido. Será usado 100.")
        return parser.parse_args(["--mode", "cachefirst", "--max-items", str(limite)])
    raise SystemExit(0)


def run_baseline_repair(args: argparse.Namespace, cfg: Dict[str, Any], rules: Rules) -> None:
    """Audita e saneia baseline local sem Tavily e sem LLM."""
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
    )
    LOG.info(
        "REPARO BASE CONCLUÍDO SEM LLM | %s",
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

    if env_info["exists"]:
        LOG.info(
            ".env carregado: %s | GROQ=%s | NVIDIA=%s | OPENROUTER=%s | TAVILY=%s | SERPER=%s | BRIGHTDATA=%s | BRIGHTDATA_ZONE=%s",
            env_info["path"],
            "CONFIGURADA" if bool(os.getenv("GROQ_API_KEY", "").strip()) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("NVIDIA_API_KEY", "").strip()) else "AUSENTE",
            "CONFIGURADA" if bool(os.getenv("OPENROUTER_API_KEY", "").strip()) else "AUSENTE",
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
    if args.mode in {"classify", "full", "recover", "semantic", "cachefirst"}:
        qg = run_quality_gates(rules, cfg)
        LOG.info(
            "QUALITY GATES OK | synthetic=%d | identity=%d | real_explicit=%d/%d | semantic=%d/%d | classificação liberada",
            qg["synthetic_cases"], qg["identity_passed"], qg["real_passed"], qg["real_active"], qg.get("semantic_passed",0), qg.get("semantic_cases",0),
        )
    if args.mode == "semantic":
        run_semantic_pilot(args, cfg, rules)
        return 0
    if args.mode == "cachefirst":
        run_cache_first_free_tier(args, cfg, rules)
        return 0
    if args.mode == "inventory":
        run_pipeline(args, cfg, rules)
        return 0
    if args.mode == "collect":
        provider = _resolve_search_provider(args, cfg)
        _provider_credentials(provider, cfg)
        collect_products(args, cfg, rules)
        return 0
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
