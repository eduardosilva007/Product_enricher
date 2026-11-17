"""Octoparse task-data adapter (offline staging, not a SERP/search provider).

Actual Octoparse Data Hub / MCP tasks have task-specific input and output schemas.
No arbitrary SKU search endpoint is assumed. Nothing is written to canonical
cache or Excel until the user approves an evidence promotion contract.
"""
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OctoparseMapping:
    """Field names for one verified Octoparse task/template."""
    task_id: str
    title_field: str
    url_field: str
    description_field: str = ""
    model_field: str = ""
    brand_field: str = ""


def normalize_task_rows(rows: Any, mapping: OctoparseMapping) -> list[dict]:
    if not mapping.task_id.strip():
        raise ValueError("OCTOPARSE_TASK_ID_REQUIRED")
    if not mapping.title_field.strip() or not mapping.url_field.strip():
        raise ValueError("OCTOPARSE_EXPLICIT_FIELD_MAPPING_REQUIRED")
    if not isinstance(rows, list):
        raise ValueError("OCTOPARSE_ROWS_MUST_BE_LIST")
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = str(row.get(mapping.title_field) or "").strip()
        link = str(row.get(mapping.url_field) or "").strip()
        url = urlsplit(link)
        if not title or url.scheme not in ("http", "https") or not url.netloc:
            continue
        normalized.append({
            "provider": "octoparse",
            "task_id": mapping.task_id,
            "source_row": index,
            "title": title,
            "url": link,
            "content": str(row.get(mapping.description_field) or "") if mapping.description_field else "",
            "brand_hint": str(row.get(mapping.brand_field) or "") if mapping.brand_field else "",
            "model_hint": str(row.get(mapping.model_field) or "") if mapping.model_field else "",
            "identity": "UNVERIFIED",
            "approval": "BLOCKED_PENDING_IDENTITY",
        })
    return normalized


def estimate_allowed_rows(requested: int, remaining: int, max_rows_per_run: int) -> int:
    """Rows, NOT Search requests or products. Account allowance is supplied by the caller."""
    if min(requested, remaining, max_rows_per_run) < 0:
        raise ValueError("OCTOPARSE_NEGATIVE_ALLOWANCE")
    return min(requested, remaining, max_rows_per_run)
