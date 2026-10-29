import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import classification_engine_v42 as v42
import classify_v42
from semantic_bge_v42 import BGEHybridSemanticProvider
from v42_adapters import GLiNEREntityExtractor, LegacyClefDecisionProvider


class DummyRules:
    def __init__(self):
        self.types = {
            "ANGLE GRINDER": {},
            "HAMMER DRILL": {},
            "POWER TOOL COMBO KIT": {},
        }
        self.valid_pairs = {
            ("OTHERS", "ANGLE GRINDER"),
            ("OTHERS", "IMPACT DRILL"),
            ("OTHERS", "COMBO KIT"),
        }
        self.raw = {
            "COMBO_KIT_POLICY": {
                "enabled": True,
                "min_distinct_primary_types": 2,
                "combo_terms": ["combo kit", "kit herramientas"],
                "eligible_primary_types": ["ANGLE GRINDER", "HAMMER DRILL"],
            }
        }

    def map_taxonomy(self, typ, power, evidence_text="", brand="", model=""):
        mapping = {
            "ANGLE GRINDER": ("OTHERS", "ANGLE GRINDER"),
            "HAMMER DRILL": ("OTHERS", "IMPACT DRILL"),
            "POWER TOOL COMBO KIT": ("OTHERS", "COMBO KIT"),
        }
        cg, fam = mapping.get(typ, ("OTHERS", "OTHERS"))
        return cg, fam, "TEST_MAP"


def ctx(**overrides):
    s1 = SimpleNamespace(
        state="AMBIGUOUS",
        product_type="",
        power="UNKNOWN",
        sources=[],
    )
    data = dict(
        brand="BRAND",
        model="MODEL123",
        key="brand|model123",
        raw_hash="raw",
        s1=s1,
        identity=v42.IDENTITY_CONFIRMED,
        identity_reasons=["S1:BRAND+MODEL_PRIMARY"],
        role=v42.ROLE_PRIMARY,
        role_reasons=["TARGET_CONFIRMED_PRIMARY"],
        evidence_text="angle grinder evidence",
        semantic_text="angle grinder",
        target_source_ids=[1],
        rule_target_supported=False,
        combo_components=[],
    )
    data.update(overrides)
    return v42.ClassificationContext(**data)


class FakeSemantic:
    name = "LOCAL_BGE"
    def __init__(self, close=False):
        self.close = close
        self.calls = 0

    def resolve(self, context):
        self.calls += 1
        return v42.SemanticDecision(
            product_type="ANGLE GRINDER",
            power="CORDED",
            confidence=0.95,
            margin=0.4,
            calibrated_close=self.close,
            candidates=["ANGLE GRINDER", "HAMMER DRILL"],
        )


class FakeDecision:
    name = "FAKE_LLM"
    def __init__(self, status="RESOLVED"):
        self.status = status
        self.calls = 0

    def decide(self, context, semantic):
        self.calls += 1
        if self.status == "INSUFFICIENT":
            return v42.ProviderDecision(status="INSUFFICIENT", reason="MORE_EVIDENCE")
        return v42.ProviderDecision(
            status="RESOLVED",
            product_type="ANGLE GRINDER",
            power="CORDED",
            category_general="OTHERS",
            family="ANGLE GRINDER",
            reason="LLM_RESOLVED",
        )


class V42EngineContracts(unittest.TestCase):
    def setUp(self):
        self.rules = DummyRules()

    def test_identity_gate_prevents_classification(self):
        sem, llm = FakeSemantic(True), FakeDecision()
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        out = engine.classify(ctx(identity=v42.IDENTITY_MODEL_ONLY))
        self.assertEqual(v42.NEEDS_EVIDENCE, out.state)
        self.assertEqual("RECOLETA_DIRIGIDA", out.next_action)
        self.assertEqual(0, sem.calls)
        self.assertEqual(0, llm.calls)
        self.assertTrue(out.recollection_queries)

    def test_rule_closes_before_semantic_and_llm(self):
        sem, llm = FakeSemantic(True), FakeDecision()
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        s1 = SimpleNamespace(
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
            sources=[],
        )
        out = engine.classify(ctx(s1=s1, rule_target_supported=True))
        self.assertEqual(v42.CLASSIFIED_RULE, out.state)
        self.assertEqual("ANGLE GRINDER", out.family)
        self.assertEqual(0, sem.calls)
        self.assertEqual(0, llm.calls)

    def test_bundle_closes_before_semantic(self):
        sem, llm = FakeSemantic(True), FakeDecision()
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        out = engine.classify(ctx(
            role=v42.ROLE_BUNDLE,
            combo_components=["ANGLE GRINDER", "HAMMER DRILL"],
        ))
        self.assertEqual(v42.CLASSIFIED_RULE, out.state)
        self.assertEqual("COMBO KIT", out.family)
        self.assertEqual(0, sem.calls)
        self.assertEqual(0, llm.calls)

    def test_calibrated_local_closes_without_llm(self):
        sem, llm = FakeSemantic(True), FakeDecision()
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        out = engine.classify(ctx())
        self.assertEqual(v42.CLASSIFIED_LOCAL, out.state)
        self.assertEqual(1, sem.calls)
        self.assertEqual(0, llm.calls)

    def test_llm_result_preserves_semantic_diagnostics(self):
        class DiagnosticSemantic(FakeSemantic):
            def resolve(self, context):
                self.calls += 1
                return v42.SemanticDecision(
                    product_type="ANGLE GRINDER",
                    power="CORDED",
                    confidence=0.91,
                    margin=0.31,
                    calibrated_close=False,
                    candidates=["ANGLE GRINDER", "HAMMER DRILL"],
                    metadata={"BGE Raw Winner": "ANGLE GRINDER"},
                )

        out = v42.V42ClassificationEngine(
            self.rules, DiagnosticSemantic(False), FakeDecision()
        ).classify(ctx())
        self.assertEqual(v42.CLASSIFIED_LLM, out.state)
        self.assertEqual("ANGLE GRINDER", out.metadata["BGE Raw Winner"])
        self.assertEqual(0.91, out.metadata["Local Confidence"])
    def test_uncalibrated_local_routes_to_llm(self):
        sem, llm = FakeSemantic(False), FakeDecision()
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        out = engine.classify(ctx())
        self.assertEqual(v42.CLASSIFIED_LLM, out.state)
        self.assertEqual(1, sem.calls)
        self.assertEqual(1, llm.calls)

    def test_llm_insufficient_routes_to_recollection(self):
        sem, llm = FakeSemantic(False), FakeDecision("INSUFFICIENT")
        engine = v42.V42ClassificationEngine(self.rules, sem, llm)
        out = engine.classify(ctx())
        self.assertEqual(v42.NEEDS_EVIDENCE, out.state)
        self.assertEqual("RECOLETA_DIRIGIDA", out.next_action)

    def test_missing_llm_provider_is_operational_not_evidence_error(self):
        sem = FakeSemantic(False)
        engine = v42.V42ClassificationEngine(self.rules, sem, None)
        out = engine.classify(ctx())
        self.assertEqual(v42.OPERATIONAL_ERROR, out.state)
        self.assertEqual("DECISION_PROVIDER_NOT_CONFIGURED", out.reason_code)

    def test_provider_outside_closed_schema_is_operational_error(self):
        class BadProvider:
            name = "BAD"
            def decide(self, context, semantic):
                return v42.ProviderDecision(
                    status="RESOLVED",
                    product_type="NOT_A_TYPE",
                    category_general="X",
                    family="Y",
                )
        engine = v42.V42ClassificationEngine(self.rules, FakeSemantic(False), BadProvider())
        out = engine.classify(ctx())
        self.assertEqual(v42.OPERATIONAL_ERROR, out.state)
        self.assertEqual("PROVIDER_OUTSIDE_CLOSED_SCHEMA", out.reason_code)

    def test_context_builder_uses_only_confirmed_target_source(self):
        good = SimpleNamespace(
            source_id=1,
            title="BRAND MODEL123 angle grinder",
            content="angle grinder 220V",
            url="https://brand.example/model123",
            identity_conflict=False,
            quality_flags=[],
            type_hits={"ANGLE GRINDER": ["angle grinder"]},
            type_scores={"ANGLE GRINDER": 3.0},
        )
        noise = SimpleNamespace(
            source_id=2,
            title="OTHER MODEL999 pressure washer",
            content="pressure washer",
            url="https://other.example/model999",
            identity_conflict=False,
            quality_flags=[],
            type_hits={"PRESSURE WASHER": ["pressure washer"]},
            type_scores={"PRESSURE WASHER": 3.0},
        )
        s1 = SimpleNamespace(
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
            sources=[good, noise],
            identity_best="STRONG",
        )
        cfg = {"identity": {"regional_suffixes": []}}
        built = v42.build_context_from_stage1(
            "BRAND", "MODEL123", "brand|model123", "raw", s1,
            cfg, self.rules, ["BRAND", "OTHER"],
        )
        self.assertEqual(v42.IDENTITY_CONFIRMED, built.identity)
        self.assertEqual([1], built.target_source_ids)
        self.assertIn("angle grinder", built.semantic_text)
        self.assertNotIn("pressure washer", built.semantic_text)
        self.assertTrue(built.rule_target_supported)

    def test_context_builder_marks_unresolved_composite_role(self):
        src = SimpleNamespace(
            source_id=1,
            title="BRAND IE6200_AA615 inverter welder angle grinder",
            content="inverter welder and angle grinder",
            url="https://brand.example/ie6200-aa615",
            identity_conflict=False,
            quality_flags=[],
            type_hits={"ANGLE GRINDER": ["angle grinder"]},
            type_scores={"ANGLE GRINDER": 2.0},
        )
        s1 = SimpleNamespace(
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
            sources=[src],
            identity_best="STRONG",
        )
        built = v42.build_context_from_stage1(
            "BRAND", "IE6200_AA615", "brand|combo", "raw", s1,
            {"identity": {"regional_suffixes": []}}, self.rules, ["BRAND"],
        )
        self.assertEqual(v42.IDENTITY_CONFIRMED, built.identity)
        self.assertEqual(v42.ROLE_UNKNOWN, built.role)
        self.assertIn("COMPOSITE_TARGET_ROLE_UNRESOLVED", built.role_reasons)
    def test_context_builder_brand_prefix_conflict_does_not_confirm_shorter_brand(self):
        src = SimpleNamespace(
            source_id=1,
            title="BRAND PRO MODEL123 angle grinder",
            content="angle grinder",
            url="https://example.com/model123",
            identity_conflict=False,
            quality_flags=[],
            type_hits={"ANGLE GRINDER": ["angle grinder"]},
            type_scores={"ANGLE GRINDER": 2.0},
        )
        s1 = SimpleNamespace(
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
            sources=[src],
            identity_best="STRONG",
        )
        cfg = {"identity": {"regional_suffixes": []}}
        built = v42.build_context_from_stage1(
            "BRAND", "MODEL123", "brand|model123", "raw", s1,
            cfg, self.rules, ["BRAND", "BRAND PRO"],
        )
        self.assertNotEqual(v42.IDENTITY_CONFIRMED, built.identity)
        self.assertEqual([], built.target_source_ids)

    def test_bge_normalizer_maps_raw_entity_span_outside_gliner(self):
        class Rules:
            types = {
                "ANGLE GRINDER": {
                    "aliases": ["angle grinder", "amoladora angular"],
                    "negative_aliases": [],
                },
                "HAMMER DRILL": {
                    "aliases": ["hammer drill", "taladro percutor"],
                    "negative_aliases": [],
                },
            }

        provider = BGEHybridSemanticProvider(Rules(), {"semantic_funnel": {}})
        hints = provider._normalize_entity_spans(["amoladora angular", "18 V"])
        self.assertEqual(["ANGLE GRINDER"], hints)
    def test_composite_target_does_not_close_single_product_stage1_rule(self):
        sem, llm = FakeSemantic(False), FakeDecision()
        s1 = SimpleNamespace(
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
            sources=[],
        )
        out = v42.V42ClassificationEngine(self.rules, sem, llm).classify(
            ctx(
                model="IE6200_AA615",
                s1=s1,
                role=v42.ROLE_UNKNOWN,
                role_reasons=["COMPOSITE_TARGET_ROLE_UNRESOLVED"],
                rule_target_supported=True,
            )
        )
        self.assertEqual(v42.CLASSIFIED_LLM, out.state)
        self.assertEqual(1, sem.calls)
        self.assertEqual(1, llm.calls)

    def test_provider_error_preserves_semantic_diagnostics(self):
        class DiagnosticSemantic(FakeSemantic):
            def resolve(self, context):
                self.calls += 1
                return v42.SemanticDecision(
                    product_type="ANGLE GRINDER",
                    power="CORDED",
                    confidence=0.88,
                    margin=0.22,
                    calibrated_close=False,
                    candidates=["ANGLE GRINDER", "HAMMER DRILL"],
                    metadata={"BGE Raw Winner": "ANGLE GRINDER"},
                )

        class BrokenDecision:
            name = "BROKEN_PROVIDER"
            def decide(self, context, semantic):
                raise RuntimeError("quota")

        out = v42.V42ClassificationEngine(
            self.rules, DiagnosticSemantic(False), BrokenDecision()
        ).classify(ctx())
        self.assertEqual(v42.OPERATIONAL_ERROR, out.state)
        self.assertEqual("ANGLE GRINDER", out.metadata.get("BGE Raw Winner"))
        self.assertEqual(0.88, out.metadata.get("Local Confidence"))
        self.assertIn("ANGLE GRINDER", out.metadata.get("Local Candidates", ""))
        self.assertIn("quota", out.metadata.get("Operational Error", ""))
    def test_gliner_failure_falls_back_to_semantic_and_decision_provider(self):
        class BrokenExtractor:
            name = "BROKEN_GLINER"
            def extract(self, text):
                raise RuntimeError("boom")

        semantic = FakeSemantic(False)
        decision = FakeDecision()
        out = v42.V42ClassificationEngine(
            self.rules,
            semantic_provider=semantic,
            decision_provider=decision,
            entity_extractor=BrokenExtractor(),
        ).classify(ctx())

        self.assertEqual(v42.CLASSIFIED_LLM, out.state)
        self.assertEqual(1, semantic.calls)
        self.assertEqual(1, decision.calls)
        self.assertEqual("ERROR_FALLBACK", out.metadata.get("Entity Extractor Status"))
        self.assertIn("RuntimeError", out.metadata.get("Entity Extractor Error", ""))
    def test_gliner_adapter_returns_raw_entities_only(self):
        class FakeModel:
            def extract_entities(self, text, schema):
                return {
                    "entities": {
                        "product_type": ["amoladora angular"],
                        "power_source": ["batería"],
                        "voltage": ["18 V"],
                    }
                }

        extractor = GLiNEREntityExtractor("dummy")
        extractor.model = FakeModel()
        out = extractor.extract("Amoladora angular a batería 18 V")
        self.assertEqual(["amoladora angular"], out.product_type)
        self.assertEqual(["batería"], out.power_source)
        self.assertEqual(["18 V"], out.voltage)
        self.assertFalse(hasattr(out, "canonical_type"))
        self.assertFalse(hasattr(out, "category_general"))
        self.assertFalse(hasattr(out, "family"))

    def test_entity_extractor_cannot_close_classification(self):
        class FakeExtractor:
            name = "GLINER_ENTITY_EXTRACTOR"
            def __init__(self):
                self.calls = 0
            def extract(self, text):
                self.calls += 1
                return v42.ExtractedEntities(
                    product_type=["amoladora angular"],
                    power_source=["batería"],
                    voltage=["18 V"],
                )

        extractor = FakeExtractor()
        llm = FakeDecision()
        c = ctx()
        engine = v42.V42ClassificationEngine(
            self.rules,
            semantic_provider=None,
            decision_provider=llm,
            entity_extractor=extractor,
        )
        out = engine.classify(c)
        self.assertEqual(1, extractor.calls)
        self.assertEqual(1, llm.calls)
        self.assertEqual(v42.CLASSIFIED_LLM, out.state)
        self.assertEqual(["amoladora angular"], c.extracted_entities.product_type)
    def test_legacy_clef_adapter_maps_resolved_decision(self):
        class FakeClef:
            model = "clef"
            def classify(self, key, raw_hash, brand, model, s1):
                return (
                    {
                        "decision": "RESOLVED",
                        "product_type": "ANGLE GRINDER",
                        "power": "CORDED",
                        "category_general": "OTHERS",
                        "family": "ANGLE GRINDER",
                        "evidence_source_ids": [1],
                    },
                    False,
                    {
                        "provider": "clef",
                        "model": "clef",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                )

        adapter = LegacyClefDecisionProvider(FakeClef())
        out = adapter.decide(ctx(), None)
        self.assertEqual("RESOLVED", out.status)
        self.assertEqual("ANGLE GRINDER", out.product_type)
        self.assertEqual("ANGLE GRINDER", out.family)

    def test_legacy_clef_adapter_maps_insufficient(self):
        class FakeClef:
            model = "clef"
            def classify(self, key, raw_hash, brand, model, s1):
                return (
                    {
                        "decision": "INSUFFICIENT",
                        "product_type": "INSUFFICIENT",
                    },
                    True,
                    {"provider": "clef", "model": "clef"},
                )

        adapter = LegacyClefDecisionProvider(FakeClef())
        out = adapter.decide(ctx(), None)
        self.assertEqual("INSUFFICIENT", out.status)

    def test_v42_runtime_env_loads_before_provider_preflight(self):
        old_account = os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)
        old_token = os.environ.pop("CLOUDFLARE_API_TOKEN", None)
        old_gliner = os.environ.pop("GLINER_MODEL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                base = Path(td)
                (base / ".env").write_text(
                    "CLOUDFLARE_ACCOUNT_ID=test-account\n"
                    "CLOUDFLARE_API_TOKEN=test-token\n"
                    "GLINER_MODEL_PATH=C:\\\\models\\\\gliner\n",
                    encoding="utf-8",
                )
                info = classify_v42._load_runtime_env(base)
                self.assertTrue(info["exists"])
                self.assertEqual("test-account", os.getenv("CLOUDFLARE_ACCOUNT_ID"))
                self.assertEqual("test-token", os.getenv("CLOUDFLARE_API_TOKEN"))
                self.assertTrue(os.getenv("GLINER_MODEL_PATH", "").endswith("gliner"))
        finally:
            for key, value in {
                "CLOUDFLARE_ACCOUNT_ID": old_account,
                "CLOUDFLARE_API_TOKEN": old_token,
                "GLINER_MODEL_PATH": old_gliner,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    def test_query_planner_is_targeted_and_deduped(self):
        qs = v42.QueryPlanner().plan("BRAND", "MODEL123", "IDENTITY_MODEL_ONLY", "ANGLE GRINDER")
        self.assertGreaterEqual(len(qs), 3)
        self.assertEqual(len(qs), len(set(qs)))
        self.assertTrue(all("BRAND" in q and "MODEL123" in q for q in qs))


if __name__ == "__main__":
    unittest.main()
