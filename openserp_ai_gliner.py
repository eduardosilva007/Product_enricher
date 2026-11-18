"""Conditional GLiNER enrichment for OpenSERP AI Overview metadata.

This adapter does NOT validate source identity, alter accepted sources, or
produce classification decisions. It runs only with a configured local model.
"""
from __future__ import annotations

from typing import Any, Callable


def extract_unverified_context(
    overview: dict[str, Any] | None,
    *,
    model_name: str,
    extractor_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(overview, dict):
        return None
    audit = dict(overview)
    audit["may_approve_identity"] = False
    audit["may_approve_classification"] = False
    audit["status"] = "UNVERIFIED_CONTEXT_ONLY"
    content = audit.get("text")
    if not isinstance(content, str) or not content.strip():
        audit["entity_extraction"] = {"status": "SKIPPED_NO_TEXT"}
        return audit
    if not model_name.strip():
        audit["entity_extraction"] = {"status": "SKIPPED_MODEL_NOT_CONFIGURED"}
        return audit
    try:
        if extractor_factory is None:
            # Lazy import: v42_adapters imports pipeline_produtos and should not
            # be loaded during normal search startup unless it is necessary.
            from v42_adapters import GLiNEREntityExtractor
            extractor_factory = GLiNEREntityExtractor
        extractor = extractor_factory(model_name)
        result = extractor.extract_context(content)
        audit["entity_extraction"] = {
            "status": "EXTRACTED" if result.get("entities") else "NO_ENTITIES",
            "model": model_name,
            "segments_processed": result.get("segments_processed", 0),
            "entities": result.get("entities", {}),
            "source": "GLINER2_ENTITY_EXTRACTION",
            "verified": False,
            "may_approve_identity": False,
            "may_approve_classification": False,
        }
    except Exception as exc:
        # An optional model may fail to load. Never block Search cache writes.
        audit["entity_extraction"] = {
            "status": "UNAVAILABLE",
            "error_type": type(exc).__name__,
            "verified": False,
            "may_approve_identity": False,
            "may_approve_classification": False,
        }
    return audit
