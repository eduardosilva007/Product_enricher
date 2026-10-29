#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Executable V4.2 classifier over the existing local RAW cache.

Does not search. Does not write Consolidado/Base Final. Produces a standalone
classification workbook with explicit routing states.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

import classification_engine_v42 as v42
import pipeline_produtos as pp
from semantic_bge_v42 import BGEHybridSemanticProvider
from v42_adapters import ClefCandidateDecisionProvider, GLiNEREntityExtractor


def _load_input(path: Path, max_items: int) -> List[Tuple[str, str, str]]:
    df = pd.read_excel(path, dtype=object)
    b = pp.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = pp.find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    out: List[Tuple[str, str, str]] = []
    seen = set()
    for _, row in df.iterrows():
        brand, model = pp.safe(row.get(b)), pp.safe(row.get(m))
        if not brand or not model:
            continue
        key = pp.business_key(brand, model)
        if key in seen:
            continue
        seen.add(key)
        out.append((brand, model, key))
        if max_items > 0 and len(out) >= max_items:
            break
    return out


def _known_brands(path: Path) -> List[str]:
    df = pd.read_excel(path, dtype=object)
    b = pp.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    return sorted({pp.safe(x) for x in df[b].tolist() if pp.safe(x)})


def _raw_missing_result(brand: str, model: str, raw_state: str) -> Dict[str, Any]:
    planner = v42.QueryPlanner()
    return v42.ClassificationResult(
        brand=brand,
        model=model,
        state=v42.NEEDS_EVIDENCE,
        identity=v42.IDENTITY_MODEL_ONLY,
        role=v42.ROLE_UNKNOWN,
        decision_source="RAW_CACHE_GATE",
        reason_code=raw_state,
        next_action="RECOLETA_DIRIGIDA",
        recollection_queries=planner.plan(brand, model, raw_state),
    ).as_dict()


def main() -> None:
    p = argparse.ArgumentParser(description="V4.2 classification engine")
    p.add_argument("--input", default="Base de Produtos.xlsx")
    p.add_argument("--config", default="pipeline_config.json")
    p.add_argument("--rules", default="regras_negocio.json")
    p.add_argument("--cache", default="")
    p.add_argument("--output", default="Classificacao_V42.xlsx")
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--provider", choices=["clef", "none"], default="clef")
    p.add_argument("--clef-model", default="")
    p.add_argument("--gliner-model", default=os.getenv("GLINER_MODEL_PATH", ""))
    p.add_argument("--no-gliner", action="store_true")
    args = p.parse_args()

    base = Path(__file__).resolve().parent
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = base / input_path
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = base / cfg_path
    rules_path = Path(args.rules)
    if not rules_path.is_absolute():
        rules_path = base / rules_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = base / output_path

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rules = pp.Rules(rules_path)
    errors = rules.validate()
    if errors:
        raise RuntimeError("RULES_INVALID: " + " | ".join(errors[:20]))

    cache_path = Path(args.cache) if args.cache else base / pp.safe(
        (cfg.get("files", {}) or {}).get("cache") or "cache_tavily.sqlite"
    )
    db_path = base / pp.safe(
        (cfg.get("files", {}) or {}).get("classification_db") or "pipeline_v3.sqlite"
    )

    cohort = _load_input(input_path, args.max_items)
    known_brands = _known_brands(input_path)
    store = pp.TavilyReadOnlyStore(cache_path)
    stage1 = pp.DeterministicEngine(rules, cfg)
    db = pp.ClassificationDB(db_path)
    semantic_provider = BGEHybridSemanticProvider(rules, cfg)
    entity_extractor = None
    provider = None

    try:
        if not args.no_gliner and pp.safe(args.gliner_model):
            entity_extractor = GLiNEREntityExtractor(pp.safe(args.gliner_model))

        if args.provider == "clef":
            model = (
                pp.safe(args.clef_model)
                or os.getenv("CLOUDFLARE_CLEF_MODEL", "").strip()
                or pp.safe((cfg.get("clef", {}) or {}).get("model"))
                or pp.DEFAULT_CLEF_MODEL
            )
            provider = ClefCandidateDecisionProvider(rules, cfg, db, model=model)
            provider.preflight()

        engine = v42.V42ClassificationEngine(
            rules=rules,
            semantic_provider=semantic_provider,
            decision_provider=provider,
            entity_extractor=entity_extractor,
        )

        rows: List[Dict[str, Any]] = []
        stats = {
            v42.CLASSIFIED_RULE: 0,
            v42.CLASSIFIED_LOCAL: 0,
            v42.CLASSIFIED_LLM: 0,
            v42.NEEDS_EVIDENCE: 0,
            v42.OPERATIONAL_ERROR: 0,
        }

        for idx, (brand, model, key) in enumerate(cohort, 1):
            raw = store.get(brand, model)
            if raw.state not in {"RAW_OK", "RAW_OK_MERGED"} or not raw.payload:
                row = _raw_missing_result(brand, model, raw.state)
                rows.append(row)
                stats[v42.NEEDS_EVIDENCE] += 1
                continue

            s1 = stage1.classify(brand, model, raw.payload)
            ctx = v42.build_context_from_stage1(
                brand=brand,
                model=model,
                key=key,
                raw_hash=raw.raw_hash,
                s1=s1,
                cfg=cfg,
                rules=rules,
                known_brands=known_brands,
            )
            result = engine.classify(ctx)
            row = result.as_dict()
            row.update({
                "Stage1 State": pp.safe(s1.state),
                "Stage1 Type": pp.safe(s1.product_type),
                "Stage1 Power": pp.safe(s1.power) or "UNKNOWN",
                "Identity Reasons": " | ".join(ctx.identity_reasons),
                "Role Reasons": " | ".join(ctx.role_reasons),
                "Target Source IDs": " | ".join(str(x) for x in ctx.target_source_ids),
                "Rule Target Supported": "SIM" if ctx.rule_target_supported else "NÃO",
                "Raw State": raw.state,
                "GLiNER Product Type Entities": " | ".join(
                    ctx.extracted_entities.product_type if ctx.extracted_entities else []
                ),
                "GLiNER Power Entities": " | ".join(
                    ctx.extracted_entities.power_source if ctx.extracted_entities else []
                ),
                "GLiNER Voltage Entities": " | ".join(
                    ctx.extracted_entities.voltage if ctx.extracted_entities else []
                ),
            })
            rows.append(row)
            stats[result.state] = stats.get(result.state, 0) + 1

            if idx % 10 == 0 or idx == len(cohort):
                print(
                    f"V42 {idx}/{len(cohort)} | "
                    f"rule={stats[v42.CLASSIFIED_RULE]} "
                    f"local={stats[v42.CLASSIFIED_LOCAL]} "
                    f"llm={stats[v42.CLASSIFIED_LLM]} "
                    f"needs_evidence={stats[v42.NEEDS_EVIDENCE]} "
                    f"operational_error={stats[v42.OPERATIONAL_ERROR]}"
                )

        df = pd.DataFrame(rows)
        summary = pd.DataFrame([{
            "Produtos": len(cohort),
            "CLASSIFIED_RULE": stats[v42.CLASSIFIED_RULE],
            "CLASSIFIED_LOCAL": stats[v42.CLASSIFIED_LOCAL],
            "CLASSIFIED_LLM": stats[v42.CLASSIFIED_LLM],
            "NEEDS_EVIDENCE": stats[v42.NEEDS_EVIDENCE],
            "OPERATIONAL_ERROR": stats[v42.OPERATIONAL_ERROR],
            "Resolved %": round(
                100.0 * (
                    stats[v42.CLASSIFIED_RULE]
                    + stats[v42.CLASSIFIED_LOCAL]
                    + stats[v42.CLASSIFIED_LLM]
                ) / max(1, len(cohort)),
                2,
            ),
            "Provider": args.provider.upper(),
            "Semantic Provider": semantic_provider.name,
            "Entity Extractor": (
                entity_extractor.name if entity_extractor is not None else "DISABLED"
            ),
        }])

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            summary.to_excel(writer, sheet_name="Resumo", index=False)
            df.to_excel(writer, sheet_name="Resultados", index=False)

        print(f"RESULTADO: {output_path}")
        print(summary.to_dict(orient="records")[0])
    finally:
        store.close()
        db.close()


if __name__ == "__main__":
    main()
