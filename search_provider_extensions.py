"""Adaptadores sem efeitos colaterais para resultados ScraperAPI e OpenSERP.

Não promovem evidências nem alteram o cache. A validação de identidade permanece
exclusivamente no Source Sieve do pipeline.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse


def _safe(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _rank(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


def _direct_url(value: Any) -> str:
    """Links Google /goto não comprovam a URL de destino: descartar sem follow-up."""
    value = _safe(value)
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.lower()
        if parsed.username or parsed.password:
            return ""
        try:
            address = ipaddress.ip_address(hostname)
            if not address.is_global:
                return ""
        except ValueError:
            if hostname.endswith(".local") or hostname.endswith(".internal"):
                return ""
        if hostname == "google.com" or hostname.endswith(".google.com"):
            if parsed.path.startswith("/goto"):
                return ""
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            return ""
        return value
    except ValueError:
        return ""


def _records(rows: Any, *, url_field: str = "link") -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("PROVIDER_SCHEMA_INVALID: organic deve ser uma lista")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            continue
        if _safe(item.get("type", "organic")).lower() not in {"organic", ""}:
            continue
        url = _direct_url(item.get(url_field) or item.get("url"))
        title = _safe(item.get("title"))
        if not url or not title or url.split("#", 1)[0].lower() in seen:
            continue
        seen.add(url.split("#", 1)[0].lower())
        pos = _rank(item.get("position", item.get("rank")), idx)
        output.append({
            "title": title,
            "url": url,
            "content": _safe(item.get("snippet") or item.get("description")),
            "score": round(1.0 / pos, 6),
            "raw_content": "",
            "_provider_position": pos,
            "_provider_meta": {"source_rank": pos},
        })
    return output


def normalize_scraperapi(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("SCRAPERAPI_SCHEMA_INVALID: objeto esperado")
    if "organic_results" not in payload:
        raise ValueError("SCRAPERAPI_SCHEMA_INVALID: organic_results ausente")
    return _records(payload["organic_results"])


def normalize_openserp(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("OPENSERP_SCHEMA_INVALID: objeto esperado")
    if payload.get("status") not in (None, "CAPTURED"):
        raise ValueError("OPENSERP_SCHEMA_INVALID: coleta nao concluida")
    if "organic" not in payload:
        raise ValueError("OPENSERP_SCHEMA_INVALID: organic ausente")
    return _records(payload["organic"], url_field="url")


def iter_openserp_cohorts(document: dict[str, Any]):
    """Suporta pilot_summary.json e arquivos individuais de Google Results."""
    if not isinstance(document, dict):
        raise ValueError("OPENSERP_DOCUMENT_INVALID")
    if "results" in document and isinstance(document["results"], list):
        for record in document["results"]:
            if not isinstance(record, dict) or record.get("provider") != "openserp":
                continue
            yield _safe(record.get("query")), normalize_openserp(record)
    elif isinstance(document.get("query"), dict) and "results" in document:
        raise ValueError("OPENSERP_DOCUMENT_UNSUPPORTED")
    elif "query" in document and "organic" in document:
        yield _safe(document.get("query")), normalize_openserp(document)
    else:
        raise ValueError("OPENSERP_DOCUMENT_UNKNOWN_SCHEMA")


def _query_identity(value: str) -> str:
    """Separator-insensitive only; does not permit token edits or fuzzy SKU matches."""
    plain = unicodedata.normalize("NFKD", _safe(value))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", plain.casefold())


def load_openserp_staging(path: str, brand: str, model: str, query: str):
    """Consume um Search já capturado, sem rede, para o fluxo normal de coleta.

    Exige correspondência exata de SKU e query, não confia nas aprovações do
    staging: o Source Sieve será executado novamente pelo pipeline principal.
    """
    import hashlib
    import json
    from pathlib import Path

    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise ValueError(f"OPENSERP_ARTIFACT_MISSING: {file_path}")
    raw = file_path.read_bytes()
    if len(raw) > 20_000_000:
        raise ValueError("OPENSERP_ARTIFACT_TOO_LARGE")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OPENSERP_ARTIFACT_INVALID_JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != "openserp-staging-v1":
        raise ValueError("OPENSERP_ARTIFACT_SCHEMA_INVALID")
    if document.get("promotion_allowed") is not False:
        raise ValueError("OPENSERP_ARTIFACT_NOT_REVIEW_STAGING")
    cohorts = document.get("cohorts")
    if not isinstance(cohorts, list) or not cohorts:
        raise ValueError("OPENSERP_ARTIFACT_COHORTS_INVALID")
    matches = []
    queries = set()
    sku_keys = set()
    for cohort in cohorts:
        if not isinstance(cohort, dict) or cohort.get("provider") != "openserp":
            raise ValueError("OPENSERP_COHORT_PROVIDER_INVALID")
        if cohort.get("status") != "STAGED_REVIEW_REQUIRED":
            raise ValueError("OPENSERP_COHORT_NOT_STAGED")
        b, m, q = (_safe(cohort.get(k)) for k in ("brand", "model", "query"))
        sk = (b.casefold(), m.casefold())
        qk = q.casefold()
        if not all((b, m, q)) or sk in sku_keys or qk in queries:
            raise ValueError("OPENSERP_COHORT_DUPLICATE_OR_BLANK")
        sku_keys.add(sk)
        queries.add(qk)
        if sk == (brand.casefold().strip(), model.casefold().strip()):
            matches.append(cohort)
    if len(matches) != 1:
        raise ValueError(f"OPENSERP_SKU_NOT_IN_ARTIFACT: {brand} {model}")
    match = matches[0]
    # The provider may have searched the literal model, while the pipeline
    # generates a separator-normalized search query (e.g. F 2/4 B -> F 2 4 B).
    # Accept ONLY byte-equivalent alphanumeric identity; no fuzzy matching,
    # no token replacement and no change to the canonical business key.
    staged_query = _safe(match.get("query"))
    original_query = _safe(match.get("original_provider_query"))
    raw_sku_query = f"{brand} {model}"
    requested = _query_identity(query)
    staged = _query_identity(staged_query)
    if not requested or staged != requested:
        # Provider's original query may be the literal model (not a trimmed
        # search anchor). This path remains deliberately fail-closed if its
        # identity differs from the pipeline's current query anchor.
        raise ValueError(
            f"OPENSERP_QUERY_MISMATCH: staged={staged_query!r} expected={query!r}"
        )
    if original_query and _query_identity(original_query) not in {
        _query_identity(raw_sku_query), requested
    }:
        raise ValueError("OPENSERP_ORIGINAL_QUERY_IDENTITY_MISMATCH")
    rows = match.get("normalized_results")
    if not isinstance(rows, list):
        raise ValueError("OPENSERP_RAW_RESULTS_INVALID")
    # Nunca importa approved_candidates, source_audit ou score calculados fora.
    normalized = []
    seen = set()
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError("OPENSERP_RAW_ROW_INVALID")
        url = _direct_url(row.get("url"))
        title = _safe(row.get("title"))
        if not url or not title:
            raise ValueError("OPENSERP_RAW_UNSAFE_URL_OR_TITLE")
        key = url.split("#", 1)[0].casefold()
        if key in seen:
            continue
        seen.add(key)
        pos = _rank(row.get("_provider_position"), idx)
        normalized.append({
            "title": title,
            "url": url,
            "content": _safe(row.get("content")),
            "score": round(1.0 / pos, 6),
            "raw_content": "",
            "_provider_position": pos,
            "_provider_meta": {
                "source_rank": pos,
                "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            },
        })
    return normalized, {
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "artifact_path": str(file_path),
        "submitted_query": query,
        "network_requests": 0,
        "artifact_searches_reused": 1,
    }
