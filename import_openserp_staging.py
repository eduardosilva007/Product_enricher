#!/usr/bin/env python3
"""Importa resultados OpenSERP validados para STAGING, nunca para a base oficial.

Uso:
 python import_openserp_staging.py --input pilot_summary.json --manifest manifest.csv --output openserp_staging.json

CSV manifesto: query,brand,model (chaves exatas; sem inferência a partir da query).
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from search_provider_extensions import iter_openserp_cohorts


def build_staging(document, manifest, policy_config):
    from pipeline_produtos import (
        _source_sieve, _clean_semantic_text, derive_search_model,
        _collection_policy, validate_collection_policy,
    )
    cfg = dict(policy_config)
    errors = validate_collection_policy(_collection_policy(cfg))
    if errors:
        raise ValueError("COLLECTION_POLICY_INVALID: " + "; ".join(errors))
    output = []
    seen_queries = set()
    seen_canonical_queries = set()
    seen_skus = set()
    # AI Overview stays outside normalized_results/approved_candidates.
    # Map only exact provider-query keys, never fuzzy or model-only matches.
    overview_by_query = {}
    if isinstance(document, dict):
        for record in document.get("results", []) if isinstance(document.get("results"), list) else []:
            if isinstance(record, dict) and record.get("provider") == "openserp":
                overview = record.get("ai_overview")
                if isinstance(overview, dict) and record.get("query"):
                    overview_by_query[str(record["query"]).strip()] = overview
    for query, results in iter_openserp_cohorts(document):
        if query not in manifest:
            raise ValueError(f"MANIFEST_MISSING_QUERY: {query}")
        if query in seen_queries:
            raise ValueError(f"DUPLICATE_QUERY: {query}")
        seen_queries.add(query)
        brand, model = manifest[query]
        sku = (brand.casefold().strip(), model.casefold().strip())
        if sku in seen_skus:
            raise ValueError(f"DUPLICATE_SKU_IN_IMPORT: {brand} {model}")
        seen_skus.add(sku)
        search_model = derive_search_model(brand, model, cfg)
        expected = (brand + " " + search_model).strip()
        raw_expected = (brand + " " + model).strip()
        from pipeline_produtos import compact
        # The isolated workflow may collect the literal SKU; the central pipeline
        # derives a normalized search anchor. Both must remain attributable to
        # the exact manifest SKU, without fuzzy matching or silent reassignment.
        if compact(query) not in {compact(expected), compact(raw_expected)}:
            raise ValueError(f"MANIFEST_QUERY_MISMATCH: {query} != {expected} or {raw_expected}")
        canonical_query_key = compact(expected)
        if canonical_query_key in seen_canonical_queries:
            raise ValueError(f"DUPLICATE_CANONICAL_QUERY: {expected}")
        seen_canonical_queries.add(canonical_query_key)
        approved, audits = _source_sieve(brand, search_model, results, cfg)
        # Revisão humana antes de promover: o importador não executa fetch nem classificação.
        output.append({
            "provider": "openserp",
            "query": expected,
            "original_provider_query": query,
            "brand": brand,
            "model": model,
            "status": "STAGED_REVIEW_REQUIRED",
            "organic_usable_urls": len(results),
            "source_sieve_accepted": len(approved),
            "normalized_results": results,
            "source_audit": audits,
            "approved_candidates": approved,
            "ai_overview": overview_by_query.get(query),
        })
    if seen_queries != set(manifest):
        raise ValueError(f"MANIFEST_UNUSED_QUERIES: {sorted(set(manifest)-seen_queries)}")
    return {"schema": "openserp-staging-v1", "promotion_allowed": False, "cohorts": output}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--config", default="pipeline_config.json")
    ap.add_argument("--policy", default="collection_quality_policy.json")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    input_path = Path(args.input).resolve()
    manifest_path = Path(args.manifest).resolve()
    config_path = Path(args.config).resolve()
    policy_path = Path(args.policy).resolve()
    output_path = Path(args.output).resolve()
    if output_path in {input_path, manifest_path, config_path, policy_path}:
        ap.error("OUTPUT_MUST_NOT_OVERWRITE_SOURCE")
    if output_path.exists():
        ap.error("OUTPUT_ALREADY_EXISTS: escolha novo nome")
    document = json.loads(input_path.read_text(encoding="utf-8"))
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["_collection_policy"] = json.loads(policy_path.read_text(encoding="utf-8"))
    with manifest_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not {"query", "brand", "model"}.issubset(reader.fieldnames or []):
            ap.error("MANIFEST_COLUMNS_REQUIRED: query,brand,model")
        rows = list(reader)
    manifest = {}
    for row in rows:
        q, b, m = (str(row.get(k) or "").strip() for k in ("query", "brand", "model"))
        if not q or not b or not m or q in manifest:
            ap.error("MANIFEST_BLANK_OR_DUPLICATE")
        manifest[q] = (b, m)
    report = build_staging(document, manifest, cfg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    print(f"STAGED={len(report['cohorts'])} OUTPUT={output_path} CACHE_WRITES=0 API_CALLS=0")


if __name__ == "__main__":
    main()
