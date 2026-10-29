#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adapters between V4.2 provider-neutral contracts and existing implementations."""

from __future__ import annotations

from typing import Any, Optional

import classification_engine_v42 as v42
import pipeline_produtos as pp


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
