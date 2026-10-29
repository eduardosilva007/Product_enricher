#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4.2 classification orchestration contracts.

This module owns orchestration only. It does not search the web and does not
hard-code brands/SKUs. Concrete local-semantic and LLM providers are adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

import pipeline_produtos as pp


IDENTITY_CONFIRMED = "TARGET_CONFIRMED"
IDENTITY_LIKELY = "TARGET_LIKELY"
IDENTITY_MODEL_ONLY = "MODEL_ONLY"
IDENTITY_CONFLICT = "CONFLICT"

ROLE_PRIMARY = "PRIMARY_PRODUCT"
ROLE_BUNDLE = "BUNDLE"
ROLE_COMPONENT = "COMPONENT"
ROLE_ACCESSORY = "ACCESSORY"
ROLE_UNKNOWN = "UNKNOWN"

CLASSIFIED_RULE = "CLASSIFIED_RULE"
CLASSIFIED_LOCAL = "CLASSIFIED_LOCAL"
CLASSIFIED_LLM = "CLASSIFIED_LLM"
NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
OPERATIONAL_ERROR = "OPERATIONAL_ERROR"


@dataclass
class SemanticDecision:
    product_type: str = ""
    power: str = "UNKNOWN"
    confidence: float = 0.0
    margin: float = 0.0
    calibrated_close: bool = False
    candidates: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderDecision:
    status: str
    product_type: str = ""
    power: str = "UNKNOWN"
    category_general: str = ""
    family: str = ""
    reason: str = ""
    evidence_source_ids: List[int] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedEntities:
    """Raw entity spans only. No canonical taxonomy decisions live here."""
    product_type: List[str] = field(default_factory=list)
    power_source: List[str] = field(default_factory=list)
    voltage: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class EntityExtractor(Protocol):
    name: str
    def extract(self, text: str) -> ExtractedEntities: ...


class SemanticProvider(Protocol):
    name: str
    def resolve(self, ctx: "ClassificationContext") -> SemanticDecision: ...


class DecisionProvider(Protocol):
    name: str
    def decide(
        self,
        ctx: "ClassificationContext",
        semantic: Optional[SemanticDecision],
    ) -> ProviderDecision: ...


@dataclass
class ClassificationContext:
    brand: str
    model: str
    key: str
    raw_hash: str
    s1: Any
    identity: str
    identity_reasons: List[str]
    role: str
    role_reasons: List[str]
    evidence_text: str
    semantic_text: str
    target_source_ids: List[int]
    rule_target_supported: bool
    combo_components: List[str] = field(default_factory=list)
    extracted_entities: Optional[ExtractedEntities] = None


@dataclass
class ClassificationResult:
    brand: str
    model: str
    state: str
    product_type: str = ""
    power: str = "UNKNOWN"
    category_general: str = ""
    family: str = ""
    identity: str = ""
    role: str = ""
    decision_source: str = ""
    reason_code: str = ""
    next_action: str = ""
    recollection_queries: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "Marca": self.brand,
            "Modelo": self.model,
            "Resolution State": self.state,
            "O que é (Tipo)": self.product_type,
            "Alimentação": self.power,
            "Category General": self.category_general,
            "FAMILY": self.family,
            "Identity": self.identity,
            "Product Role": self.role,
            "Decision Source": self.decision_source,
            "Reason Code": self.reason_code,
            "Próxima Ação": self.next_action,
            "Recollection Queries": " || ".join(self.recollection_queries),
            **self.metadata,
        }


def _brand_tokens(value: str) -> List[str]:
    return pp.norm_text(value).split()


def _phrase_tokens_occurs(needle: str, haystack: str) -> bool:
    n, h = _brand_tokens(needle), _brand_tokens(haystack)
    if not n or len(n) > len(h):
        return False
    return any(h[i:i + len(n)] == n for i in range(len(h) - len(n) + 1))


class IdentityResolver:
    """Target identity independent from TYPE evidence."""

    def __init__(self, cfg: Dict[str, Any], known_brands: Sequence[str] = ()):
        self.cfg = cfg
        self.known_brands = list(known_brands)
        self.suffixes = (cfg.get("identity", {}) or {}).get("regional_suffixes", []) or []

    def _longer_brand_conflict(self, brand: str, text: str) -> bool:
        target = _brand_tokens(brand)
        for other in self.known_brands:
            if pp.compact(other) == pp.compact(brand):
                continue
            ot = _brand_tokens(other)
            if len(ot) > len(target) and ot[:len(target)] == target:
                if _phrase_tokens_occurs(other, text):
                    return True
        return False

    def _brand_primary(self, brand: str, title: str, url: str) -> bool:
        host = pp.safe(url)
        if self._longer_brand_conflict(brand, title) or self._longer_brand_conflict(brand, host):
            return False
        return _phrase_tokens_occurs(brand, title) or (
            bool(pp.compact(brand)) and pp.compact(brand) in pp.compact(host)
        )

    def resolve(self, brand: str, model: str, s1: Any) -> Tuple[str, List[str], List[int]]:
        reasons: List[str] = []
        confirmed_ids: List[int] = []
        model_only = False
        conflict = False

        for src in (getattr(s1, "sources", None) or []):
            if bool(getattr(src, "identity_conflict", False)):
                conflict = True
                reasons.append(f"S{src.source_id}:CONFLICT")
                continue
            title = pp.safe(getattr(src, "title", ""))
            url = pp.safe(getattr(src, "url", ""))
            title_model = bool(pp._model_match_in_field(model, title, self.suffixes)[0])
            url_model = bool(pp._model_match_in_field(model, url, self.suffixes)[0])
            model_primary = title_model or url_model
            brand_primary = self._brand_primary(brand, title, url)
            if brand_primary and model_primary:
                confirmed_ids.append(int(src.source_id))
                reasons.append(f"S{src.source_id}:BRAND+MODEL_PRIMARY")
            elif model_primary:
                model_only = True
                reasons.append(f"S{src.source_id}:MODEL_ONLY")

        if confirmed_ids:
            return IDENTITY_CONFIRMED, reasons, sorted(set(confirmed_ids))
        if conflict:
            return IDENTITY_CONFLICT, reasons, []
        if model_only:
            return IDENTITY_MODEL_ONLY, reasons, []
        if getattr(s1, "identity_best", "") == "PROBABLE":
            return IDENTITY_LIKELY, reasons or ["LEGACY_PROBABLE"], []
        return IDENTITY_MODEL_ONLY, reasons or ["NO_PRIMARY_BRAND_MODEL_ANCHOR"], []


class RoleResolver:
    """Conservative product-role resolver. No SKU/brand exceptions."""

    def __init__(self, rules: Any):
        self.rules = rules

    def _text_type_hits(self, text: str, eligible: Sequence[str]) -> List[str]:
        folded = pp.strip_accents(text).casefold()
        hits: List[str] = []
        for typ in eligible:
            meta = self.rules.types.get(typ, {}) or {}
            positives = [typ] + list(meta.get("aliases", []) or [])
            negatives = list(meta.get("negative_aliases", []) or [])
            if negatives and any(pp.contains_alias_prepared(folded, x) for x in negatives):
                continue
            if any(pp.contains_alias_prepared(folded, x) for x in positives):
                hits.append(typ)
        return sorted(set(hits))

    def resolve(
        self,
        model: str,
        s1: Any,
        target_source_ids: Sequence[int],
    ) -> Tuple[str, List[str], List[str]]:
        policy = self.rules.raw.get("COMBO_KIT_POLICY", {}) or {}
        eligible = [pp.safe(x) for x in policy.get("eligible_primary_types", []) or [] if pp.safe(x)]
        combo_terms = [pp.safe(x) for x in policy.get("combo_terms", []) or [] if pp.safe(x)]
        minimum = max(2, int(policy.get("min_distinct_primary_types", 2) or 2))
        target_ids = {int(x) for x in target_source_ids}
        composite = len(_composite_model_parts(model)) >= 2

        for src in (getattr(s1, "sources", None) or []):
            if int(src.source_id) not in target_ids or getattr(src, "quality_flags", None):
                continue
            text = f"{pp.safe(src.title)}\n{pp.safe(src.content)[:1600]}"
            folded = pp.strip_accents(text).casefold()
            combo_term = any(pp.contains_alias_prepared(folded, x) for x in combo_terms)
            types = self._text_type_hits(text, eligible)
            if (combo_term and len(types) >= minimum) or (composite and combo_term):
                return ROLE_BUNDLE, ["TARGET_COMBO_EVIDENCE"], types

        if target_ids:
            return ROLE_PRIMARY, ["TARGET_CONFIRMED_PRIMARY"], []
        return ROLE_UNKNOWN, ["ROLE_NOT_ESTABLISHED"], []


def _composite_model_parts(model: str) -> List[str]:
    import re
    raw = pp.safe(model)
    groups = [x.strip() for x in re.split(r"[_|;]+", raw) if x.strip()]
    explicit = [g for g in groups if len(pp.compact(g)) >= 5 and re.search(r"[A-Za-z]", g) and re.search(r"\d", g)]
    if len(explicit) >= 2:
        return explicit
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", raw)
    mixed = [t for t in tokens if len(pp.compact(t)) >= 5 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]
    return mixed if len(mixed) >= 2 else []


def build_context_from_stage1(
    brand: str,
    model: str,
    key: str,
    raw_hash: str,
    s1: Any,
    cfg: Dict[str, Any],
    rules: Any,
    known_brands: Sequence[str] = (),
) -> ClassificationContext:
    """Single adapter from legacy Stage1 into the V4.2 contract."""
    identity, identity_reasons, target_ids = IdentityResolver(
        cfg, known_brands
    ).resolve(brand, model, s1)

    role, role_reasons, combo_components = RoleResolver(rules).resolve(
        model, s1, target_ids
    )

    target_set = set(target_ids)
    evidence_chunks: List[str] = []
    semantic_chunks: List[str] = []
    rule_target_supported = False
    stage1_type = pp.safe(getattr(s1, "product_type", ""))

    for src in (getattr(s1, "sources", None) or []):
        if int(src.source_id) not in target_set:
            continue
        title = pp.safe(getattr(src, "title", ""))
        content = pp.safe(getattr(src, "content", ""))
        chunk = f"TITLE: {title}\nCONTENT: {content[:1800]}".strip()
        if chunk:
            evidence_chunks.append(chunk)

        quality_flags = list(getattr(src, "quality_flags", None) or [])
        if not quality_flags:
            # Semantic engine receives only clean target-confirmed evidence.
            if title:
                semantic_chunks.append(f"PRODUCT TITLE: {title}")
            if content:
                semantic_chunks.append(f"CONTENT: {content[:700]}")

        supports_stage1 = bool(
            stage1_type
            and (
                stage1_type in (getattr(src, "type_hits", {}) or {})
                or float((getattr(src, "type_scores", {}) or {}).get(stage1_type, 0.0) or 0.0) > 0.0
            )
        )
        if supports_stage1 and not quality_flags:
            rule_target_supported = True

    return ClassificationContext(
        brand=brand,
        model=model,
        key=key,
        raw_hash=raw_hash,
        s1=s1,
        identity=identity,
        identity_reasons=identity_reasons,
        role=role,
        role_reasons=role_reasons,
        evidence_text="\n---TARGET SOURCE---\n".join(evidence_chunks),
        semantic_text="\n\n".join(semantic_chunks),
        target_source_ids=target_ids,
        rule_target_supported=rule_target_supported,
        combo_components=combo_components,
    )


class QueryPlanner:
    def plan(self, brand: str, model: str, reason: str, type_hint: str = "") -> List[str]:
        base = f'"{brand}" "{model}"'
        queries = [
            f"{base} product",
            f"{base} manual datasheet",
            f"{base} specifications",
        ]
        if type_hint:
            queries.insert(1, f'{base} "{type_hint}"')
        return list(dict.fromkeys(queries))[:4]


class V42ClassificationEngine:
    def __init__(
        self,
        rules: Any,
        semantic_provider: Optional[SemanticProvider] = None,
        decision_provider: Optional[DecisionProvider] = None,
        entity_extractor: Optional[EntityExtractor] = None,
        query_planner: Optional[QueryPlanner] = None,
    ):
        self.rules = rules
        self.entity_extractor = entity_extractor
        self.semantic_provider = semantic_provider
        self.decision_provider = decision_provider
        self.query_planner = query_planner or QueryPlanner()

    def _map(self, typ: str, power: str, ctx: ClassificationContext) -> Tuple[str, str, str]:
        return self.rules.map_taxonomy(
            typ,
            power or "UNKNOWN",
            ctx.evidence_text,
            ctx.brand,
            ctx.model,
        )

    def _needs_evidence(self, ctx: ClassificationContext, code: str, type_hint: str = "") -> ClassificationResult:
        return ClassificationResult(
            brand=ctx.brand,
            model=ctx.model,
            state=NEEDS_EVIDENCE,
            identity=ctx.identity,
            role=ctx.role,
            decision_source="EVIDENCE_GATE",
            reason_code=code,
            next_action="RECOLETA_DIRIGIDA",
            recollection_queries=self.query_planner.plan(ctx.brand, ctx.model, code, type_hint),
        )

    def classify(self, ctx: ClassificationContext) -> ClassificationResult:
        if ctx.identity != IDENTITY_CONFIRMED:
            return self._needs_evidence(ctx, f"IDENTITY_{ctx.identity}")

        if ctx.role == ROLE_BUNDLE:
            typ = "POWER TOOL COMBO KIT"
            if typ in self.rules.types:
                cg, fam, reason = self._map(typ, ctx.s1.power or "UNKNOWN", ctx)
                if (cg, fam) in self.rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS"):
                    return ClassificationResult(
                        ctx.brand, ctx.model, CLASSIFIED_RULE, typ,
                        ctx.s1.power or "UNKNOWN", cg, fam, ctx.identity, ctx.role,
                        "ROLE_COMBO_RULE", reason, "CONCLUIDO",
                        metadata={"Combo Components": " | ".join(ctx.combo_components)},
                    )

        stage1_type = pp.safe(getattr(ctx.s1, "product_type", ""))
        if (
            getattr(ctx.s1, "state", "") == "RESOLVED_RULE"
            and ctx.rule_target_supported
            and stage1_type in self.rules.types
        ):
            power = pp.safe(getattr(ctx.s1, "power", "")) or "UNKNOWN"
            cg, fam, reason = self._map(stage1_type, power, ctx)
            if (cg, fam) in self.rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS"):
                return ClassificationResult(
                    ctx.brand, ctx.model, CLASSIFIED_RULE, stage1_type, power,
                    cg, fam, ctx.identity, ctx.role, "STAGE1_RULE", reason,
                    "CONCLUIDO",
                )

        # Entity extraction is deliberately non-decisional. Extracted spans may
        # enrich the semantic evidence, but cannot close TYPE/ROLE/TAXONOMY.
        if self.entity_extractor is not None and ctx.extracted_entities is None:
            try:
                ctx.extracted_entities = self.entity_extractor.extract(ctx.evidence_text)
            except Exception as exc:
                return ClassificationResult(
                    ctx.brand, ctx.model, OPERATIONAL_ERROR,
                    identity=ctx.identity, role=ctx.role,
                    decision_source=getattr(self.entity_extractor, "name", "ENTITY_EXTRACTOR"),
                    reason_code=f"ENTITY_EXTRACTOR_ERROR:{type(exc).__name__}",
                    next_action="CORRIGIR_OPERACAO",
                    metadata={"Operational Error": str(exc)},
                )

        semantic: Optional[SemanticDecision] = None
        if self.semantic_provider is not None:
            try:
                semantic = self.semantic_provider.resolve(ctx)
            except Exception as exc:
                return ClassificationResult(
                    ctx.brand, ctx.model, OPERATIONAL_ERROR,
                    identity=ctx.identity, role=ctx.role,
                    decision_source=getattr(self.semantic_provider, "name", "LOCAL"),
                    reason_code=f"SEMANTIC_PROVIDER_ERROR:{type(exc).__name__}",
                    next_action="CORRIGIR_OPERACAO",
                    metadata={"Operational Error": str(exc)},
                )
            if semantic.calibrated_close and semantic.product_type in self.rules.types:
                cg, fam, reason = self._map(semantic.product_type, semantic.power, ctx)
                if (cg, fam) in self.rules.valid_pairs and (cg, fam) != ("OTHERS", "OTHERS"):
                    return ClassificationResult(
                        ctx.brand, ctx.model, CLASSIFIED_LOCAL,
                        semantic.product_type, semantic.power, cg, fam,
                        ctx.identity, ctx.role,
                        getattr(self.semantic_provider, "name", "LOCAL"),
                        reason, "CONCLUIDO",
                        metadata={
                            "Local Confidence": semantic.confidence,
                            "Local Margin": semantic.margin,
                            "Local Candidates": " | ".join(semantic.candidates),
                        },
                    )

        if self.decision_provider is None:
            return ClassificationResult(
                ctx.brand, ctx.model, OPERATIONAL_ERROR,
                identity=ctx.identity, role=ctx.role,
                decision_source="ROUTER",
                reason_code="DECISION_PROVIDER_NOT_CONFIGURED",
                next_action="CONFIGURAR_PROVIDER",
                metadata={
                    "Local Candidates": " | ".join(semantic.candidates) if semantic else "",
                },
            )

        try:
            decision = self.decision_provider.decide(ctx, semantic)
        except Exception as exc:
            return ClassificationResult(
                ctx.brand, ctx.model, OPERATIONAL_ERROR,
                identity=ctx.identity, role=ctx.role,
                decision_source=getattr(self.decision_provider, "name", "LLM"),
                reason_code=f"DECISION_PROVIDER_ERROR:{type(exc).__name__}",
                next_action="CORRIGIR_OPERACAO",
                metadata={"Operational Error": str(exc)},
            )

        if decision.status == "INSUFFICIENT":
            return self._needs_evidence(ctx, decision.reason or "LLM_INSUFFICIENT", semantic.product_type if semantic else "")
        if decision.status != "RESOLVED":
            return ClassificationResult(
                ctx.brand, ctx.model, OPERATIONAL_ERROR,
                identity=ctx.identity, role=ctx.role,
                decision_source=getattr(self.decision_provider, "name", "LLM"),
                reason_code=decision.reason or "INVALID_PROVIDER_DECISION",
                next_action="CORRIGIR_OPERACAO",
            )

        pair = (decision.category_general, decision.family)
        if decision.product_type not in self.rules.types or pair not in self.rules.valid_pairs or pair == ("OTHERS", "OTHERS"):
            return ClassificationResult(
                ctx.brand, ctx.model, OPERATIONAL_ERROR,
                identity=ctx.identity, role=ctx.role,
                decision_source=getattr(self.decision_provider, "name", "LLM"),
                reason_code="PROVIDER_OUTSIDE_CLOSED_SCHEMA",
                next_action="CORRIGIR_OPERACAO",
            )

        return ClassificationResult(
            ctx.brand, ctx.model, CLASSIFIED_LLM,
            decision.product_type, decision.power, decision.category_general,
            decision.family, ctx.identity, ctx.role,
            getattr(self.decision_provider, "name", "LLM"),
            decision.reason or "LLM_RESOLVED", "CONCLUIDO",
            metadata=decision.metadata,
        )
