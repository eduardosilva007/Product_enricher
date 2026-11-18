#!/usr/bin/env python3
"""Offline GLiNER extraction audit from a captured OpenSERP raw JSON.

Never performs Search, touches production SQLite/Excel, or classifies a SKU.
Requires a locally available GLiNER2 model configured via GLINER_MODEL_PATH.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from search_provider_extensions import _query_identity, _unverified_ai_overview
from openserp_ai_gliner import extract_unverified_context


def audit(raw_path: Path, brand: str, model: str, model_path: str) -> dict:
    doc = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("RAW_ARTIFACT_NOT_OBJECT")
    found = doc.get("query", {})
    submitted = found.get("text", "") if isinstance(found, dict) else ""
    expected = f"{brand} {model}"
    if not submitted or _query_identity(submitted) != _query_identity(expected):
        raise ValueError("RAW_ARTIFACT_SKU_IDENTITY_MISMATCH")
    overview = _unverified_ai_overview(
        {}, raw_path.parent / "openserp_staging.json", brand, model, submitted
    )
    # Fallback to the caller's exact raw path (irrespective of filename).
    if overview is None:
        features = doc.get("serp_features", [])
        if isinstance(features, list):
            for item in features:
                if isinstance(item, dict) and item.get("type") == "ai_summary":
                    overview = _unverified_ai_overview(
                        {"ai_overview": {
                            "source": "google_ai_overview",
                            "text": item.get("text", ""),
                        }}, raw_path, brand, model, submitted
                    )
                    break
    if overview is None:
        return {
            "brand": brand, "model": model, "status": "NO_AI_OVERVIEW",
            "api_calls": 0, "official_writes": 0
        }
    # Reuse one loaded model for both comparisons (avoid duplicated inference setup).
    shared_factory = None
    if model_path.strip():
        from v42_adapters import GLiNEREntityExtractor
        extractor = GLiNEREntityExtractor(model_path)
        shared_factory = lambda _: extractor
    enriched = extract_unverified_context(
        overview, model_name=model_path, extractor_factory=shared_factory
    )
    extraction = enriched.get("entity_extraction", {})
    # Offline-only A/B: compare full noisy Google summary against its opening
    # descriptive passage. Never use this passage alone for source validation.
    complete = overview.get("text", "")
    start = complete.find("AI Mode reply for ")
    if start >= 0:
        lead = complete[start:]
        newline = lead.find("\n")
        if newline >= 0:
            lead = lead[newline + 1:]
    else:
        lead = complete
    # Split at the generic explanation heading, not a product-specific label.
    import re
    boundary = re.search(
        r"(?i)\b(?:Breakdown of the Model Designation|Key Specifications for)\b",
        lead,
    )
    opening = lead[:boundary.start()].strip() if boundary else lead.strip()
    opening = opening or lead
    opening_result = extract_unverified_context(
        {**overview, "text": opening}, model_name=model_path,
        extractor_factory=shared_factory,
    )
    opening_extraction = opening_result.get("entity_extraction", {})
    comparison = {
        "full_context": {
            "status": extraction.get("status", "UNKNOWN"),
            "characters": len(complete),
            "entities": extraction.get("entities", {}),
        },
        "opening_description": {
            "status": opening_extraction.get("status", "UNKNOWN"),
            "characters": len(opening),
            "entities": opening_extraction.get("entities", {}),
        },
    }
    return {
        "brand": brand,
        "model": model,
        "submitted_query": submitted,
        "status": extraction.get("status", "UNKNOWN"),
        "type_hypothesis": (extraction.get("entities") or {}).get("product_type", []),
        "entities": extraction.get("entities", {}),
        "context_comparison": comparison,
        "segments_processed": extraction.get("segments_processed", 0),
        "ai_overview_characters": len(enriched.get("text", "")),
        "source": "google_ai_overview",
        "confidence": "UNVERIFIED_CONTEXT_ONLY",
        "identity_approved": False,
        "classification_approved": False,
        "api_calls": 0,
        "official_writes": 0,
        "error_type": extraction.get("error_type", ""),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Offline GLiNER audit of existing OpenSERP raw JSON")
    p.add_argument("--raw", type=Path, required=True)
    p.add_argument("--brand", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--gliner-model", default="")
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()
    if not args.raw.is_file():
        p.error(f"RAW_ARTIFACT_NOT_FOUND: {args.raw}")
    # Reuse project .env configuration without echoing any credentials.
    import pipeline_produtos as pp
    pp.load_env_file(Path(__file__).resolve().parent / ".env")
    model_name = args.gliner_model or os.getenv("GLINER_MODEL_PATH", "")
    result = audit(args.raw, args.brand, args.model, model_name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        if args.output.resolve() == args.raw.resolve():
            p.error("OUTPUT_MUST_NOT_OVERWRITE_RAW")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result["status"] == "SKIPPED_MODEL_NOT_CONFIGURED":
        raise SystemExit("Configure GLINER_MODEL_PATH or --gliner-model for a real GLiNER evaluation")
    if result["status"] == "UNAVAILABLE":
        raise SystemExit("GLiNER unavailable: see error_type above; Search/cache unchanged")


if __name__ == "__main__":
    main()
