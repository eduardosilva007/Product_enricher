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
        except ImportError:
            # Older pilots used the original GLiNER package rather than GLiNER2.
            try:
                from gliner import GLiNER
            except ImportError as exc:
                raise RuntimeError("GLINER_OR_GLINER2_NOT_INSTALLED") from exc
            legacy = GLiNER.from_pretrained(self.model_name)
            class _LegacyAdapter:
                def __init__(self, inner):
                    self.inner = inner
                def extract_entities(self, text, labels):
                    spans = self.inner.predict_entities(text, list(labels))
                    return {"entities": {
                        key: [str(row.get("text", "")).strip()
                              for row in spans if row.get("label") == key
                              and str(row.get("text", "")).strip()]
                        for key in labels
                    }}
            self.model = _LegacyAdapter(legacy)
        else:
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


    def extract_context(self, text: str) -> Dict[str, Any]:
        """Extract physical product TYPE only from unverified external context.

        This OpenSERP-specific method is independent of V4.2 extract(), so the
        previously evaluated classifier contracts remain unchanged. All input
        segments are visited; extraction does not imply identity approval.
        """
        self.load()
        content = pp.safe(text)
        if not content:
            return {"entities": {}, "segments_processed": 0}
        labels = {
            "product_type": (
                "literal complete noun phrase describing what the physical "
                "product is, including the product head noun (e.g. pump)"
            ),
        }
        window, overlap = 2400, 160
        fields: Dict[str, List[str]] = {"product_type": []}
        segments = 0
        position = 0
        while position < len(content):
            end = min(len(content), position + window)
            if end < len(content):
                boundary = max(
                    content.rfind("\n", position + window // 2, end),
                    content.rfind(". ", position + window // 2, end),
                )
                if boundary > position:
                    end = boundary + 1
            segment = content[position:end]
            result = self.model.extract_entities(segment, labels)
            for value in self._values(result, "product_type"):
                if value not in fields["product_type"]:
                    fields["product_type"].append(value)
            segments += 1
            if end >= len(content):
                break
            position = max(position + 1, end - overlap)
        return {
            "entities": {key: values for key, values in fields.items() if values},
            "segments_processed": segments,
            "extractor": self.name,
            "model": self.model_name,
        }


class JevDecisionProvider:
    """TypeSafe/JEV V4.2 backend using closed joint choices only."""

    name = "JEV_V42"

    def __init__(
        self,
        rules: Any,
        cfg: Dict[str, Any],
        db: Any,
        model: str = "",
    ):
        self.rules = rules
        self.cfg = cfg
        self.db = db
        jcfg = cfg.get("jev", {}) or {}
        self.base_url = (
            pp.safe(os.getenv("TYPESAFE_BASE_URL", ""))
            or pp.safe(os.getenv("JEV_BASE_URL", ""))
            or pp.safe(jcfg.get("base_url"))
            or "https://api.typesafe.ai"
        ).rstrip("/")
        self.api_key_env = pp.safe(jcfg.get("api_key_env")) or "TYPESAFE_API_KEY"
        self.model = (
            pp.safe(model)
            or pp.safe(os.getenv("TYPESAFE_MODEL", ""))
            or pp.safe(os.getenv("JEV_MODEL", ""))
            or pp.safe(jcfg.get("model"))
            or "jev-latest"
        )
        self.timeout_seconds = int(jcfg.get("timeout_seconds", 90) or 90)
        self.retry_attempts = max(1, int(jcfg.get("retry_attempts", 3) or 3))
        self.retry_backoff_seconds = [
            float(x) for x in (jcfg.get("retry_backoff_seconds", [2, 5, 10]) or [2, 5, 10])
        ]
        self.limiter = pp.RateLimiter(
            float(jcfg.get("operational_requests_per_minute", 60.0) or 60.0)
        )
        self.max_type_candidates = max(
            3, int((cfg.get("classification_v42", {}) or {}).get("max_type_candidates", 8) or 8)
        )

    def _api_key(self) -> str:
        return (
            os.getenv(self.api_key_env, "").strip()
            or os.getenv("JEV_API_KEY", "").strip()
            or os.getenv("TYPESAFE_API_KEY", "").strip()
        )

    def preflight(self, validate_remote: bool = True) -> Dict[str, Any]:
        try:
            import requests
        except ImportError as exc:
            raise pp.OperationalPause("JEV_DEPENDENCY_MISSING", "requests ausente") from exc
        token = self._api_key()
        if not token:
            raise pp.OperationalPause("JEV_API_KEY_MISSING", self.api_key_env)
        if validate_remote:
            try:
                resp = requests.get(
                    f"{self.base_url}/v1/models",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=min(self.timeout_seconds, 30),
                )
            except requests.RequestException as exc:
                raise pp.OperationalPause("JEV_PREFLIGHT_TRANSIENT", pp.safe(exc)) from exc
            if not resp.ok:
                raise pp.OperationalPause(
                    "JEV_PREFLIGHT_HTTP_ERROR",
                    f"TypeSafe HTTP {resp.status_code}: {pp.safe(resp.text)[:600]}",
                )
            body = resp.json()
            models = [pp.safe(x.get("name")) for x in (body.get("models") or []) if isinstance(x, dict)]
            if self.model not in models:
                raise pp.OperationalPause(
                    "JEV_MODEL_UNAVAILABLE",
                    f"{self.model}; available={models[:20]}",
                )
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}

    @staticmethod
    def _pair_label(pair: Tuple[str, str]) -> str:
        return f"{pp.safe(pair[0])} | {pp.safe(pair[1])}"

    @staticmethod
    def _parse_joint(value: str) -> Tuple[str, str, str]:
        raw = pp.safe(value)
        if "||" not in raw:
            return "", "", ""
        typ, pair = [pp.safe(x) for x in raw.split("||", 1)]
        if "|" not in pair:
            return "", "", ""
        cg, fam = [pp.safe(x) for x in pair.split("|", 1)]
        return typ, cg, fam

    @staticmethod
    def _choice(answer: Dict[str, Any]) -> Dict[str, Any]:
        probs = {
            pp.safe(k): float(v)
            for k, v in (answer.get("probabilities") or {}).items()
            if pp.safe(k)
        }
        choice = pp.safe(answer.get("choice"))
        ranked = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
        if not choice and ranked:
            choice = ranked[0][0]
        top = probs.get(choice, ranked[0][1] if ranked else 0.0)
        runner, p2 = "", 0.0
        for key, value in ranked:
            if key != choice:
                runner, p2 = key, value
                break
        return {
            "choice": choice,
            "confidence": float(answer.get("confidence") or 0.0),
            "probability": float(top or 0.0),
            "top2": runner,
            "margin": max(0.0, float(top or 0.0) - float(p2 or 0.0)),
        }

    def _type_candidates(
        self,
        ctx: v42.ClassificationContext,
        semantic: Optional[v42.SemanticDecision],
    ) -> List[str]:
        out: List[str] = []
        for typ in list(semantic.candidates if semantic else []):
            if typ in self.rules.types and typ not in out:
                out.append(typ)
        if (
            "COMPOSITE_TARGET_ROLE_UNRESOLVED" in (ctx.role_reasons or [])
            and "POWER TOOL COMBO KIT" in self.rules.types
            and "POWER TOOL COMBO KIT" not in out
        ):
            out.insert(0, "POWER TOOL COMBO KIT")
        return out[: self.max_type_candidates]

    def _joint_criteria(self, type_candidates: Sequence[str]) -> Dict[str, str]:
        criteria: Dict[str, str] = {}
        for typ in type_candidates:
            meta = self.rules.types.get(typ, {}) or {}
            aliases = [pp.safe(x) for x in (meta.get("aliases", []) or []) if pp.safe(x)][:5]
            type_desc = f"Canonical physical product type: {typ}."
            if aliases:
                type_desc += " Known aliases: " + "; ".join(aliases) + "."
            for pair in sorted(pp._taxonomy_pairs_for_type(self.rules, typ)):
                label = f"{typ} || {self._pair_label(pair)}"
                criteria[label] = (
                    type_desc
                    + f" Valid taxonomy: Category General={pair[0]}; FAMILY={pair[1]}."
                )
        criteria["INSUFFICIENT"] = (
            "The target evidence is not sufficient to choose one coherent physical "
            "product type and valid business taxonomy pair reliably."
        )
        return criteria

    def _cache_model_id(self, type_candidates: Sequence[str]) -> str:
        sig = hashlib.sha256(
            json.dumps(
                {"model": self.model, "types": list(type_candidates), "version": "jev-v42-joint-v1"},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.model}|jev-v42:{sig}"

    def decide(
        self,
        ctx: v42.ClassificationContext,
        semantic: Optional[v42.SemanticDecision],
    ) -> v42.ProviderDecision:
        type_candidates = self._type_candidates(ctx, semantic)
        if not type_candidates:
            return v42.ProviderDecision(status="INSUFFICIENT", reason="NO_SEMANTIC_CANDIDATES")

        joint_criteria = self._joint_criteria(type_candidates)
        if len(joint_criteria) <= 1:
            return v42.ProviderDecision(status="INSUFFICIENT", reason="CLOSED_SCHEMA_CANDIDATES_EMPTY")

        cache_model = self._cache_model_id(type_candidates)
        cached = self.db.get_llm(
            ctx.key, ctx.raw_hash, self.rules.hash, "jev-v42", cache_model
        )
        if cached is not None:
            data, usage = cached
            return v42.ProviderDecision(
                status=pp.safe(data.get("status")) or "ERROR",
                product_type=pp.safe(data.get("product_type")),
                power=pp.safe(data.get("power")) or "UNKNOWN",
                category_general=pp.safe(data.get("category_general")),
                family=pp.safe(data.get("family")),
                reason=pp.safe(data.get("reason")) or "JEV_V42_CACHE",
                evidence_source_ids=list(data.get("evidence_source_ids", []) or []),
                metadata={**(usage or {}), "JEV Cache": "SIM"},
            )

        power_candidates = list(dict.fromkeys(
            [pp.safe(x) for x in (getattr(ctx.s1, "power_candidates", None) or []) if pp.safe(x)]
            + ([pp.safe(getattr(ctx.s1, "power", ""))] if pp.safe(getattr(ctx.s1, "power", "")) else [])
            + list(self.rules.allowed_powers)
        ))
        if "UNKNOWN" not in power_candidates:
            power_candidates.append("UNKNOWN")

        state = {
            "brand": ctx.brand,
            "model": ctx.model,
            "product_role": ctx.role,
            "role_reasons": ctx.role_reasons,
            "target_evidence": ctx.evidence_text,
            "semantic_type_candidates": type_candidates,
            "extracted_entities": {
                "product_type": ctx.extracted_entities.product_type if ctx.extracted_entities else [],
                "power_source": ctx.extracted_entities.power_source if ctx.extracted_entities else [],
                "voltage": ctx.extracted_entities.voltage if ctx.extracted_entities else [],
            },
        }
        payload = {
            "model": self.model,
            "state": state,
            "questions": {
                "joint_classification": {
                    "type": "choice",
                    "instructions": (
                        "Choose exactly one coherent canonical physical PRODUCT_TYPE and "
                        "valid Category General | FAMILY pair for the requested target. "
                        "For composite model identifiers, distinguish a true bundle/combo "
                        "from a single product. Extracted entities are observations only. "
                        "Do not classify compatible accessories, replacement parts, or "
                        "secondary products as the target. Use INSUFFICIENT when evidence "
                        "cannot support a reliable choice."
                    ),
                    "criteria": joint_criteria,
                },
                "power_source": {
                    "type": "choice",
                    "instructions": (
                        "Choose the target product's primary input power source. "
                        "Voltage alone is not proof of BATTERY or CORDED. Use UNKNOWN "
                        "when the target evidence does not establish the input power source."
                    ),
                    "criteria": {
                        x: f"Primary input power source is {x}."
                        for x in power_candidates
                    },
                },
            },
        }

        import requests
        token = self._api_key()
        transient = {429, 500, 502, 503, 504}
        body: Dict[str, Any] = {}
        last_detail = ""
        for attempt in range(self.retry_attempts):
            self.limiter.wait()
            try:
                resp = requests.post(
                    f"{self.base_url}/v1/systemone",
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
                    code = (
                        "JEV_RATE_LIMIT" if resp.status_code == 429
                        else "JEV_HTTP_ERROR"
                    )
                    raise pp.OperationalPause(
                        code,
                        f"TypeSafe HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:1200]}",
                    )
                break
            except requests.RequestException as exc:
                last_detail = pp.safe(exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)])
                    continue
                raise pp.OperationalPause("JEV_TRANSIENT", last_detail) from exc
        else:
            raise pp.OperationalPause("JEV_TRANSIENT", last_detail or "unknown")

        answers = body.get("answers") or {}
        joint = self._choice(answers.get("joint_classification") or {})
        power_ans = self._choice(answers.get("power_source") or {})
        joint_choice = joint["choice"]
        power = pp.safe(power_ans["choice"]) or "UNKNOWN"

        if joint_choice == "INSUFFICIENT" or not joint_choice:
            status = "INSUFFICIENT"
            typ = cg = fam = ""
            reason = "JEV_V42_INSUFFICIENT"
        else:
            typ, cg, fam = self._parse_joint(joint_choice)
            compatible = (cg, fam) in pp._taxonomy_pairs_for_type(self.rules, typ)
            resolved = (
                typ in type_candidates
                and (cg, fam) in self.rules.valid_pairs
                and (cg, fam) != ("OTHERS", "OTHERS")
                and compatible
            )
            status = "RESOLVED" if resolved else "ERROR"
            reason = "JEV_V42_RESOLVED" if resolved else "JEV_V42_INVALID_CHOICE"

        usage_raw = body.get("usage") or {}
        data = {
            "status": status,
            "product_type": typ if status == "RESOLVED" else "",
            "power": power,
            "category_general": cg if status == "RESOLVED" else "",
            "family": fam if status == "RESOLVED" else "",
            "reason": reason,
            "evidence_source_ids": list(ctx.target_source_ids),
        }
        usage = {
            "provider": "jev-v42",
            "model": pp.safe(body.get("model")) or self.model,
            "prompt_tokens": int(usage_raw.get("input_tokens") or 0),
            "completion_tokens": int(usage_raw.get("output_tokens") or 0),
            "total_tokens": int(usage_raw.get("input_tokens") or 0)
                + int(usage_raw.get("output_tokens") or 0),
            "JEV Cache": "NÃO",
            "JEV Joint Probability": joint["probability"],
            "JEV Joint Confidence": joint["confidence"],
            "JEV Joint Top2": joint["top2"],
            "JEV Joint Margin": joint["margin"],
            "JEV Power Probability": power_ans["probability"],
            "JEV Power Confidence": power_ans["confidence"],
            "Semantic Type Candidates": " | ".join(type_candidates),
        }
        self.db.put_llm(
            ctx.key, ctx.raw_hash, self.rules.hash, "jev-v42", cache_model, data, usage
        )

        return v42.ProviderDecision(
            status=status,
            product_type=data["product_type"],
            power=power,
            category_general=data["category_general"],
            family=data["family"],
            reason=reason,
            evidence_source_ids=list(ctx.target_source_ids),
            metadata=usage,
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
