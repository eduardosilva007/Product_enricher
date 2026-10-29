#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adapters between V4.2 provider-neutral contracts and existing implementations."""

from __future__ import annotations

from typing import Any, Optional

import classification_engine_v42 as v42
import pipeline_produtos as pp


class GLiNEREntityExtractor:
    """GLiNER2 adapter restricted to entity extraction only.

    It never maps spans to canonical TYPEs, never decides ROLE/COMBO, and never
    selects Category General/FAMILY. Those responsibilities remain downstream.
    """

    name = "GLINER_ENTITY_EXTRACTOR"

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None

    def load(self) -> None:
        if self.model is not None:
            return
        if not self.model_name:
            raise RuntimeError("GLINER_MODEL_PATH_NOT_CONFIGURED")
        try:
            from gliner2 import AutoExtractor
        except ImportError as exc:
            raise RuntimeError("GLINER2_NOT_INSTALLED") from exc
        self.model = AutoExtractor.from_pretrained(self.model_name)

    @staticmethod
    def _values(result: Any, label: str) -> list[str]:
        if not isinstance(result, dict):
            return []
        entities = result.get("entities", {})
        if not isinstance(entities, dict):
            return []
        raw = entities.get(label, [])
        out: list[str] = []
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                text = pp.safe(item.get("text"))
                if text:
                    out.append(text)
        return list(dict.fromkeys(out))

    def extract(self, text: str) -> v42.ExtractedEntities:
        self.load()
        result = self.model.extract_entities(
            pp.safe(text)[:4000],
            {
                "product_type": "literal phrase naming the physical product type",
                "power_source": "literal phrase naming the product power source",
                "voltage": "literal voltage expression associated with the product",
            },
        )
        return v42.ExtractedEntities(
            product_type=self._values(result, "product_type"),
            power_source=self._values(result, "power_source"),
            voltage=self._values(result, "voltage"),
            metadata={"raw_entity_labels": ["product_type", "power_source", "voltage"]},
        )


class LegacyClefDecisionProvider:
    """Migration adapter.

    Business routing lives in V4.2. This adapter reuses the current Clef funnel
    only as a decision backend while the remote-provider layer is extracted.
    """

    name = "CLEF_LEGACY_FUNNEL"

    def __init__(self, classifier: Any):
        self.classifier = classifier

    def decide(
        self,
        ctx: v42.ClassificationContext,
        semantic: Optional[v42.SemanticDecision],
    ) -> v42.ProviderDecision:
        try:
            data, cache_hit, usage = self.classifier.classify(
                ctx.key,
                ctx.raw_hash,
                ctx.brand,
                ctx.model,
                ctx.s1,
            )
        except pp.OperationalPause as exc:
            if exc.code == "CLEF_SEMANTIC_FUNNEL_EMPTY":
                return v42.ProviderDecision(
                    status="INSUFFICIENT",
                    reason=exc.code,
                    metadata={"message": str(exc)},
                )
            raise

        status = pp.safe(data.get("decision")).upper()
        typ = pp.safe(data.get("product_type"))
        power = pp.safe(data.get("power")) or "UNKNOWN"
        cg = pp.safe(data.get("category_general"))
        fam = pp.safe(data.get("family"))

        insufficient_tokens = {"INSUFFICIENT", "UNKNOWN", ""}
        if (
            status in {"INSUFFICIENT", "NEEDS_EVIDENCE", "UNRESOLVED"}
            or typ.upper() == "INSUFFICIENT"
            or cg.upper() == "INSUFFICIENT"
            or fam.upper() == "INSUFFICIENT"
        ):
            return v42.ProviderDecision(
                status="INSUFFICIENT",
                reason=pp.safe(data.get("reason_code")) or "CLEF_INSUFFICIENT",
                metadata={
                    "cache_hit": bool(cache_hit),
                    "provider": pp.safe(usage.get("provider")) or "clef",
                    "model": pp.safe(usage.get("model")) or pp.safe(getattr(self.classifier, "model", "")),
                },
            )

        if status != "RESOLVED":
            return v42.ProviderDecision(
                status="ERROR",
                reason=pp.safe(data.get("reason_code")) or f"CLEF_DECISION_{status or 'EMPTY'}",
                metadata={"raw_decision": status},
            )

        return v42.ProviderDecision(
            status="RESOLVED",
            product_type=typ,
            power=power,
            category_general=cg,
            family=fam,
            reason="CLEF_RESOLVED",
            evidence_source_ids=[
                int(x) for x in (data.get("evidence_source_ids", []) or [])
                if str(x).isdigit()
            ],
            metadata={
                "Clef Cache": "SIM" if cache_hit else "NÃO",
                "Clef Provider": pp.safe(usage.get("provider")) or "clef",
                "Clef Model": pp.safe(usage.get("model")) or pp.safe(getattr(self.classifier, "model", "")),
                "Clef Prompt Tokens": int(usage.get("prompt_tokens", 0) or 0),
                "Clef Completion Tokens": int(usage.get("completion_tokens", 0) or 0),
                "Clef Total Tokens": int(usage.get("total_tokens", 0) or 0),
                "Semantic Type Candidates": " | ".join(usage.get("semantic_type_candidates", []) or []),
                "Semantic Taxonomy Candidates": " || ".join(usage.get("semantic_taxonomy_candidates", []) or []),
                "Clef Type Probability": usage.get("clef_type_probability", ""),
                "Clef Type Margin": usage.get("clef_type_margin", ""),
                "Clef Taxonomy Probability": usage.get("clef_taxonomy_probability", ""),
                "Clef Taxonomy Margin": usage.get("clef_taxonomy_margin", ""),
            },
        )
