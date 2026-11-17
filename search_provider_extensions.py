"""Adaptadores sem efeitos colaterais para resultados ScraperAPI e OpenSERP.

Não promovem evidências nem alteram o cache. A validação de identidade permanece
exclusivamente no Source Sieve do pipeline.
"""
from __future__ import annotations

import json
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
