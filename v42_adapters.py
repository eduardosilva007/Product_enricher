#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adapters between V4.2 provider-neutral contracts and existing implementations."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import hashlib
import json
import os
import time

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


class ClefCandidateDecisionProvider:
    """V4.2 Clef backend: consumes candidates, never runs embeddings."""

    name = "CLEF_V42"

    def __init__(
        self,
        rules: Any,
        cfg: Dict[str, Any],
        db: Any,
        model: str = "clef",
    ):
        self.rules = rules
        self.cfg = cfg
        self.db = db
        self.model = pp.safe(model) or "clef"
        ccfg = cfg.get("clef", {}) or {}
        self.account_id_env = pp.safe(ccfg.get("account_id_env")) or "CLOUDFLARE_ACCOUNT_ID"
        self.api_key_env = pp.safe(ccfg.get("api_token_env")) or "CLOUDFLARE_API_TOKEN"
        self.timeout_seconds = int(ccfg.get("timeout_seconds", 90) or 90)
        self.retry_attempts = max(1, int(ccfg.get("retry_attempts", 3) or 3))
        self.retry_backoff_seconds = [
            float(x) for x in (ccfg.get("retry_backoff_seconds", [2, 5, 10]) or [2, 5, 10])
        ]
        self.limiter = pp.RateLimiter(
            float(ccfg.get("operational_requests_per_minute", 10.0) or 10.0)
        )
        self.max_type_candidates = max(
            3, int((cfg.get("classification_v42", {}) or {}).get("max_type_candidates", 8) or 8)
        )

    def preflight(self) -> Dict[str, Any]:
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            raise pp.OperationalPause("CLEF_DEPENDENCY_MISSING", "requests ausente") from exc
        if not os.getenv(self.account_id_env, "").strip():
            raise pp.OperationalPause("CLEF_ACCOUNT_ID_MISSING", self.account_id_env)
        token = (
            os.getenv(self.api_key_env, "").strip()
            or os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
        )
        if not token:
            raise pp.OperationalPause("CLEF_API_TOKEN_MISSING", self.api_key_env)
        return {"provider": self.name, "model": self.model}

    @staticmethod
    def _pair_label(pair: Tuple[str, str]) -> str:
        return f"{pp.safe(pair[0])} | {pp.safe(pair[1])}"

    @staticmethod
    def _split_pair(value: str) -> Tuple[str, str]:
        raw = pp.safe(value)
        if "|" not in raw:
            return "", ""
        a, b = raw.split("|", 1)
        return pp.safe(a), pp.safe(b)

    @staticmethod
    def _unit(value: Any) -> float:
        try:
            v = float(value)
        except Exception:
            return 0.0
        if v > 1.0:
            v /= 100.0
        return max(0.0, min(1.0, v))

    @classmethod
    def _parse_choice(cls, answers: Dict[str, Any], qid: str) -> Dict[str, Any]:
        ans = answers.get(qid) or {}
        probs = {}
        for k, v in (ans.get("probabilities") or {}).items():
            probs[pp.safe(k)] = cls._unit(v)
        ranked = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
        choice = pp.safe(ans.get("choice")) or (ranked[0][0] if ranked else "")
        p1 = probs.get(choice, ranked[0][1] if ranked else 0.0)
        runner, p2 = "", 0.0
        for k, v in ranked:
            if k != choice:
                runner, p2 = k, v
                break
        return {
            "choice": choice,
            "probability": p1,
            "confidence": cls._unit(ans.get("confidence")),
            "top2": runner,
            "margin": max(0.0, p1 - p2),
        }

    def _type_criteria(self, typ: str) -> str:
        meta = self.rules.types.get(typ, {}) or {}
        aliases = [pp.safe(x) for x in (meta.get("aliases", []) or []) if pp.safe(x)][:5]
        text = f"Canonical physical product type: {typ}."
        if aliases:
            text += " Common aliases: " + "; ".join(aliases) + "."
        return text

    def _taxonomy_candidates(self, types: Sequence[str]) -> List[str]:
        out: List[str] = []
        for typ in types:
            for pair in sorted(pp._taxonomy_pairs_for_type(self.rules, typ)):
                label = self._pair_label(pair)
                if label not in out:
                    out.append(label)
        return out

    def _cache_model_id(self, semantic: Optional[v42.SemanticDecision]) -> str:
        material = {
            "model": self.model,
            "types": list((semantic.candidates if semantic else [])[: self.max_type_candidates]),
            "version": "v42-candidates-v1",
        }
        sig = hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.model}|v42:{sig}"

    def decide(
        self,
        ctx: v42.ClassificationContext,
        semantic: Optional[v42.SemanticDecision],
    ) -> v42.ProviderDecision:
        if semantic is None or not semantic.candidates:
            return v42.ProviderDecision(
                status="INSUFFICIENT",
                reason="NO_SEMANTIC_CANDIDATES",
            )

        type_candidates = [
            x for x in semantic.candidates[: self.max_type_candidates]
            if x in self.rules.types
        ]
        tax_candidates = self._taxonomy_candidates(type_candidates)
        if not type_candidates or not tax_candidates:
            return v42.ProviderDecision(
                status="INSUFFICIENT",
                reason="CLOSED_SCHEMA_CANDIDATES_EMPTY",
            )

        cache_model = self._cache_model_id(semantic)
        cached = self.db.get_llm(
            ctx.key, ctx.raw_hash, self.rules.hash, "clef-v42", cache_model
        )
        if cached is not None:
            data, usage = cached
            return v42.ProviderDecision(
                status=pp.safe(data.get("status")) or "ERROR",
                product_type=pp.safe(data.get("product_type")),
                power=pp.safe(data.get("power")) or "UNKNOWN",
                category_general=pp.safe(data.get("category_general")),
                family=pp.safe(data.get("family")),
                reason=pp.safe(data.get("reason")) or "CLEF_V42_CACHE",
                evidence_source_ids=list(data.get("evidence_source_ids", []) or []),
                metadata={**(usage or {}), "Clef Cache": "SIM"},
            )

        power_candidates = list(dict.fromkeys(
            [pp.safe(x) for x in (getattr(ctx.s1, "power_candidates", None) or []) if pp.safe(x)]
            + ([pp.safe(getattr(ctx.s1, "power", ""))] if pp.safe(getattr(ctx.s1, "power", "")) else [])
            + list(self.rules.allowed_powers)
        ))
        if "UNKNOWN" not in power_candidates:
            power_candidates.append("UNKNOWN")

        type_criteria = {x: self._type_criteria(x) for x in type_candidates}
        type_criteria["OTHER"] = "None of the listed canonical product types is correct."
        type_criteria["INSUFFICIENT"] = "Evidence is insufficient to identify the target product type."

        power_criteria = {x: f"Primary power source: {x}." for x in power_candidates}
        tax_criteria = {
            x: f"Valid closed business taxonomy pair: {x}."
            for x in tax_candidates
        }
        tax_criteria["OTHER"] = "None of the listed valid taxonomy pairs fits."
        tax_criteria["INSUFFICIENT"] = "Evidence is insufficient to choose a taxonomy pair."

        payload = {
            "model": self.model,
            "state": {
                "brand": ctx.brand,
                "model": ctx.model,
                "target_evidence": ctx.evidence_text,
                "extracted_entities": {
                    "product_type": (
                        ctx.extracted_entities.product_type
                        if ctx.extracted_entities else []
                    ),
                    "power_source": (
                        ctx.extracted_entities.power_source
                        if ctx.extracted_entities else []
                    ),
                    "voltage": (
                        ctx.extracted_entities.voltage
                        if ctx.extracted_entities else []
                    ),
                },
            },
            "questions": {
                "product_type": {
                    "type": "choice",
                    "instructions": (
                        "Choose the canonical physical product type supported by the target evidence. "
                        "Extracted entities are observations only, not decisions. "
                        "Do not confuse accessories, compatible products, power source, voltage, or secondary items with the target."
                    ),
                    "criteria": type_criteria,
                },
                "power_source": {
                    "type": "choice",
                    "instructions": (
                        "Choose the primary input power source. Voltage alone is not proof of BATTERY or CORDED. "
                        "Use UNKNOWN if not established."
                    ),
                    "criteria": power_criteria,
                },
                "taxonomy_pair": {
                    "type": "choice",
                    "instructions": (
                        "Choose one listed Category General | FAMILY pair. Never invent a pair outside the options."
                    ),
                    "criteria": tax_criteria,
                },
            },
        }

        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
        import requests

        token = (
            os.getenv(self.api_key_env, "").strip()
            or os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
        )
        account_id = os.getenv(self.account_id_env, "").strip()
        slug = "@cf/cloudflare/clef-flash" if self.model == "clef-flash" else "@cf/cloudflare/clef"
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{slug}"
        transient = {429, 500, 502, 503, 504}
        body: Dict[str, Any] = {}
        last_detail = ""

        for attempt in range(self.retry_attempts):
            self.limiter.wait()
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}
                if resp.status_code in transient and attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                if not resp.ok:
                    raise pp.OperationalPause(
                        "CLEF_V42_HTTP_ERROR",
                        f"Cloudflare HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:1200]}",
                    )
                if body.get("success") is False:
                    raise pp.OperationalPause(
                        "CLEF_V42_API_ERROR",
                        json.dumps(body, ensure_ascii=False)[:1200],
                    )
                break
            except requests.RequestException as exc:
                last_detail = pp.safe(exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                raise pp.OperationalPause("CLEF_V42_TRANSIENT", last_detail) from exc
        else:
            raise pp.OperationalPause("CLEF_V42_TRANSIENT", last_detail or "unknown")

        result = body.get("result") or {}
        answers = result.get("answers") or {}
        ta = self._parse_choice(answers, "product_type")
        pa = self._parse_choice(answers, "power_source")
        xa = self._parse_choice(answers, "taxonomy_pair")
        typ = pp.safe(ta["choice"])
        power = pp.safe(pa["choice"]) or "UNKNOWN"
        cg, fam = self._split_pair(xa["choice"])

        insufficient = (
            typ in {"", "INSUFFICIENT"}
            or xa["choice"] in {"", "INSUFFICIENT"}
        )
        resolved = (
            not insufficient
            and typ in type_candidates
            and (cg, fam) in self.rules.valid_pairs
            and (cg, fam) != ("OTHERS", "OTHERS")
        )

        status = "RESOLVED" if resolved else ("INSUFFICIENT" if insufficient else "ERROR")
        data = {
            "status": status,
            "product_type": typ if resolved else "",
            "power": power,
            "category_general": cg if resolved else "",
            "family": fam if resolved else "",
            "reason": "CLEF_V42_RESOLVED" if resolved else (
                "CLEF_V42_INSUFFICIENT" if insufficient else "CLEF_V42_INVALID_CHOICE"
            ),
            "evidence_source_ids": list(ctx.target_source_ids),
        }
        usage_raw = result.get("usage") or {}
        usage = {
            "provider": "clef-v42",
            "model": self.model,
            "prompt_tokens": int(usage_raw.get("input_tokens") or 0),
            "completion_tokens": int(usage_raw.get("output_tokens") or 0),
            "total_tokens": int(usage_raw.get("input_tokens") or 0)
                + int(usage_raw.get("output_tokens") or 0),
            "Clef Cache": "NÃO",
            "Clef Type Probability": ta["probability"],
            "Clef Type Confidence": ta["confidence"],
            "Clef Type Top2": ta["top2"],
            "Clef Type Margin": ta["margin"],
            "Clef Taxonomy Probability": xa["probability"],
            "Clef Taxonomy Confidence": xa["confidence"],
            "Clef Taxonomy Top2": xa["top2"],
            "Clef Taxonomy Margin": xa["margin"],
            "Semantic Type Candidates": " | ".join(type_candidates),
            "Semantic Taxonomy Candidates": " || ".join(tax_candidates),
        }
        self.db.put_llm(
            ctx.key, ctx.raw_hash, self.rules.hash, "clef-v42", cache_model,
            data, usage,
        )

        return v42.ProviderDecision(
            status=status,
            product_type=data["product_type"],
            power=power,
            category_general=data["category_general"],
            family=data["family"],
            reason=data["reason"],
            evidence_source_ids=list(ctx.target_source_ids),
            metadata=usage,
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
