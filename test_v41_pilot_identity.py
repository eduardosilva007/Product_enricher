import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import pilot_v41_ab as pilot


def _bundle(**overrides):
    data = dict(
        brand="GLADIATOR",
        model="AA518/220/PLUS",
        identity="TARGET_CONFIRMED",
        identity_reasons=["S1:BRAND+MODEL_PRIMARY"],
        evidence="target evidence",
        semantic_evidence="amoladora angular",
        target_evidence="amoladora angular",
        source_count=1,
        semantic_source_count=1,
        structured_count=0,
        target_source_ids=[1],
        stage1_state="RESOLVED_RULE",
        stage1_type="ANGLE GRINDER",
        stage1_power="CORDED",
        stage1_rule_target_supported=True,
    )
    data.update(overrides)
    return pilot.EvidenceBundle(**data)


class PilotV41IdentityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "identity": {
                "regional_suffixes": ["AR", "BR", "B1", "B2", "B3", "B4"],
                "structural_sku_core": {
                    "enabled": True,
                    "edge_decorator_regexes": list(pilot.pp.DEFAULT_STRUCTURAL_EDGE_DECORATOR_REGEXES),
                    "max_edge_decorators": 2,
                    "min_core_compact_chars": 6,
                    "require_model_like_core": True,
                    "max_identity_window_chars": 80,
                    "fuzzy_matching": False,
                },
            }
        }

    def test_short_brand_is_not_substring_match(self):
        self.assertFalse(
            pilot._brand_primary_occurs(
                "ADV3",
                "American Airlines AA7000 flight information",
                "https://example.com/aa7000",
            )
        )

    def test_known_longer_brand_blocks_prefix_collision(self):
        self.assertFalse(
            pilot._brand_primary_occurs(
                "GLADIATOR",
                "GLADIATOR PRO AA 815/18 C1 Esmeril Angular",
                "",
                known_brands=["GLADIATOR", "GLADIATOR PRO"],
            )
        )
        self.assertTrue(
            pilot._brand_primary_occurs(
                "GLADIATOR PRO",
                "GLADIATOR PRO AA 815/18 C1 Esmeril Angular",
                "",
                known_brands=["GLADIATOR", "GLADIATOR PRO"],
            )
        )

    def test_brand_and_model_primary_confirm_target(self):
        src = SimpleNamespace(
            title="Barovo BIM-TPA13B Taladro Percutor",
            url="https://barovo.com/bim-tpa13b",
            identity_conflict=False,
        )
        self.assertTrue(
            pilot._source_primary_target_anchor(
                src, "BAROVO", "BIM-TPA13B", self.cfg, ["BAROVO"]
            )
        )

    def test_model_collision_does_not_confirm_wrong_brand(self):
        src = SimpleNamespace(
            title="Akai AA-8500 Stereo Amplifier",
            url="https://akai.example/aa-8500",
            identity_conflict=False,
        )
        self.assertFalse(
            pilot._source_primary_target_anchor(
                src, "ADV3", "AA8500", self.cfg, ["ADV3", "AKAI"]
            )
        )

    def test_jsonld_brand_plus_sku_confirms(self):
        products = [{"brand": "ADV3", "sku": "AA7000", "name": "Produto ADV3"}]
        self.assertTrue(
            pilot._structured_identity_confirmed("ADV3", "AA7000", products, self.cfg)
        )

    def test_jsonld_prefix_brand_is_not_same_brand(self):
        products = [{"brand": "GLADIATOR PRO", "sku": "AA518/220/PLUS"}]
        self.assertFalse(
            pilot._structured_identity_confirmed(
                "GLADIATOR", "AA518/220/PLUS", products, self.cfg
            )
        )

    def test_jsonld_wrong_brand_rejects_same_model(self):
        products = [{"brand": "American Airlines", "sku": "AA7000", "name": "Flight"}]
        self.assertFalse(
            pilot._structured_identity_confirmed("ADV3", "AA7000", products, self.cfg)
        )

    def test_jsonld_parser_extracts_product(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Taladro Percutor",
          "brand": {"@type": "Brand", "name": "BAROVO"},
          "sku": "BIM-TPA13B",
          "gtin13": "7791234567890"
        }
        </script>
        </head></html>
        """
        rows = pilot.parse_jsonld_products(html)
        self.assertEqual(1, len(rows))
        self.assertEqual("BAROVO", rows[0]["brand"])
        self.assertEqual("BIM-TPA13B", rows[0]["sku"])

    def test_semantic_bundle_excludes_unconfirmed_source_noise(self):
        good = SimpleNamespace(
            source_id=1,
            title="GLADIATOR AA518/220/PLUS amoladora angular",
            content="amoladora angular 220V",
            url="https://example.com/gladiator-aa518",
            relevance="STRONG",
            identity_score=100,
            source_weight=1.0,
            tavily_score=0.9,
            identity_conflict=False,
            quality_flags=[],
            type_hits={"ANGLE GRINDER": ["amoladora angular"]},
            type_scores={"ANGLE GRINDER": 3.0},
        )
        noise = SimpleNamespace(
            source_id=2,
            title="Pressure washer unrelated product",
            content="pressure washer pressure cleaner",
            url="https://example.com/washer",
            relevance="WEAK",
            identity_score=0,
            source_weight=0.0,
            tavily_score=0.95,
            identity_conflict=False,
            quality_flags=[],
            type_hits={"PRESSURE WASHER": ["pressure washer"]},
            type_scores={"PRESSURE WASHER": 3.0},
        )
        s1 = SimpleNamespace(
            sources=[good, noise],
            state="RESOLVED_RULE",
            product_type="ANGLE GRINDER",
            power="CORDED",
        )
        bundle = pilot.build_evidence_bundle(
            "GLADIATOR",
            "AA518/220/PLUS",
            s1,
            self.cfg,
            jsonld_cache=SimpleNamespace(get=lambda url: []),
            fetch_jsonld=False,
            known_brands=["GLADIATOR", "GLADIATOR PRO"],
        )
        self.assertEqual("TARGET_CONFIRMED", bundle.identity)
        self.assertEqual(1, bundle.semantic_source_count)
        self.assertIn("amoladora angular", bundle.semantic_evidence)
        self.assertNotIn("pressure washer", bundle.semantic_evidence.casefold())
        self.assertTrue(bundle.stage1_rule_target_supported)

    def test_stage1_resolved_type_is_not_overwritten_by_bge_when_target_supported(self):
        class DummyRules:
            types = {"ANGLE GRINDER": {}, "PRESSURE WASHER": {}}
            raw = {"COMBO_KIT_POLICY": {"enabled": False}}

        typ, source, _ = pilot._select_fused_type(
            _bundle(),
            SimpleNamespace(sources=[]),
            {"winner": "PRESSURE WASHER"},
            DummyRules(),
        )
        self.assertEqual("ANGLE GRINDER", typ)
        self.assertEqual("STAGE1_RULE", source)

    def test_stage1_resolved_type_is_not_trusted_without_target_support(self):
        class DummyRules:
            types = {"ANGLE GRINDER": {}, "PRESSURE WASHER": {}}
            raw = {"COMBO_KIT_POLICY": {"enabled": False}}

        typ, source, _ = pilot._select_fused_type(
            _bundle(stage1_rule_target_supported=False),
            SimpleNamespace(sources=[]),
            {"winner": "PRESSURE WASHER"},
            DummyRules(),
        )
        self.assertEqual("PRESSURE WASHER", typ)
        self.assertEqual("BGE_RERANK", source)

    def test_model_composite_detection(self):
        self.assertTrue(
            pilot._model_looks_composite("IE 6200/8/220_AA615/5/220 PLUS")
        )
        self.assertTrue(
            pilot._model_looks_composite("TP413/6 AA415/2/220K")
        )
        self.assertFalse(pilot._model_looks_composite("AA615/5/220 PLUS"))

    def test_combo_requires_two_types_on_same_target_source(self):
        class DummyRules:
            raw = {
                "COMBO_KIT_POLICY": {
                    "enabled": True,
                    "min_distinct_primary_types": 2,
                    "combo_terms": ["combo kit", "kit herramientas"],
                    "eligible_primary_types": ["ANGLE GRINDER", "HAMMER DRILL"],
                }
            }

        src = SimpleNamespace(
            source_id=1,
            title="Kit Herramientas con amoladora angular y taladro percutor",
            content="",
            quality_flags=[],
            type_hits={
                "ANGLE GRINDER": ["amoladora angular"],
                "HAMMER DRILL": ["taladro percutor"],
            },
            type_scores={"ANGLE GRINDER": 3.0, "HAMMER DRILL": 3.0},
        )
        hits = pilot._safe_combo_kit_components(
            _bundle(model="TP413/7/220K", target_source_ids=[1]),
            SimpleNamespace(sources=[src]),
            DummyRules(),
        )
        self.assertEqual(["ANGLE GRINDER", "HAMMER DRILL"], hits)

    def test_combo_does_not_merge_types_from_different_sources(self):
        class DummyRules:
            raw = {
                "COMBO_KIT_POLICY": {
                    "enabled": True,
                    "min_distinct_primary_types": 2,
                    "combo_terms": ["combo kit"],
                    "eligible_primary_types": ["ANGLE GRINDER", "HAMMER DRILL"],
                }
            }

        s1 = SimpleNamespace(sources=[
            SimpleNamespace(
                source_id=1, title="combo kit amoladora angular", content="",
                quality_flags=[], type_hits={"ANGLE GRINDER": ["amoladora angular"]},
                type_scores={"ANGLE GRINDER": 3.0},
            ),
            SimpleNamespace(
                source_id=2, title="combo kit taladro percutor", content="",
                quality_flags=[], type_hits={"HAMMER DRILL": ["taladro percutor"]},
                type_scores={"HAMMER DRILL": 3.0},
            ),
        ])
        hits = pilot._safe_combo_kit_components(
            _bundle(model="TP413/7/220K", target_source_ids=[1, 2]),
            s1,
            DummyRules(),
        )
        self.assertEqual([], hits)

    def test_combo_family_matches_canonical_gold_taxonomy(self):
        rules = pilot.pp.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
        cg, fam, _ = rules.map_taxonomy(
            "POWER TOOL COMBO KIT", "UNKNOWN", "combo kit drill grinder"
        )
        self.assertEqual("OTHERS", cg)
        self.assertEqual("COMBO KIT", fam)

    def test_generic_waterpump_fallback_matches_gold_taxonomy(self):
        rules = pilot.pp.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
        cg, fam, reason = rules.map_taxonomy(
            "WATERPUMP", "UNKNOWN", "water pump", "ANY", "MODEL"
        )
        self.assertEqual(("WATERPUMPS", "WATERPUMP"), (cg, fam))
        self.assertEqual("TYPE_FALLBACK_PAIR", reason)

    def test_rules_operational_contract_has_no_validation_errors(self):
        rules = pilot.pp.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
        self.assertEqual([], rules.validate())

    def test_angle_grinder_multilingual_aliases_cover_esmeriladora(self):
        rules = pilot.pp.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
        aliases = set(rules.types["ANGLE GRINDER"]["aliases"])
        self.assertIn("esmeriladora", aliases)
        self.assertIn("esmeril angular", aliases)

    def test_type_document_does_not_embed_business_taxonomy(self):
        class DummyRules:
            types = {
                "ANGLE GRINDER": {
                    "aliases": ["angle grinder", "amoladora angular"],
                    "negative_aliases": ["die grinder"],
                }
            }

        clf = pilot.HybridTypeClassifier(DummyRules(), {"semantic_funnel": {}})
        doc = clf.type_docs["ANGLE GRINDER"]
        self.assertIn("amoladora angular", doc)
        self.assertNotIn("Business taxonomy mappings", doc)

    def test_pilot_reranker_does_not_inherit_192_token_truncation(self):
        class DummyRules:
            types = {"ANGLE GRINDER": {"aliases": [], "negative_aliases": []}}

        clf = pilot.HybridTypeClassifier(
            DummyRules(),
            {"semantic_funnel": {"rerank_max_length": 192}},
        )
        self.assertGreaterEqual(clf.rerank_max_length, 512)

    def test_reranker_normalization_is_pairwise_sigmoid_not_topk_softmax(self):
        a = pilot._sigmoid_scores([0.0, 2.0])
        b = pilot._sigmoid_scores([0.0, 2.0, 10.0])
        self.assertAlmostEqual(0.5, float(a[0]), places=7)
        self.assertAlmostEqual(float(a[0]), float(b[0]), places=7)
        self.assertAlmostEqual(float(a[1]), float(b[1]), places=7)

    def test_explicit_gold_columns_are_used_without_status_flag(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gold.xlsx"
            pd.DataFrame([{
                "Marca": "GLADIATOR",
                "Modelo": "AA518/220/PLUS",
                "Gold O que é canônico": "ANGLE GRINDER",
                "Gold Category General": "OTHERS",
                "Gold FAMILY": "ANGLE GRINDER",
                "Status": "Script",
                "Revisão B.I": "",
            }]).to_excel(path, index=False)
            gold = pilot._validated_gold_map(path)
        row = gold[pilot.pp.business_key("GLADIATOR", "AA518/220/PLUS")]
        self.assertEqual("ANGLE GRINDER", row["type"])
        self.assertEqual("OTHERS", row["category"])
        self.assertEqual("ANGLE GRINDER", row["family"])
        self.assertEqual("EXPLICIT_GOLD", row["source"])

    def test_no_identity_means_no_bge_or_gliner_call(self):
        class NeverClassifier:
            def classify(self, query):
                raise AssertionError("BGE must not run without target identity")

        class DummyRules:
            valid_pairs = set()

        bundle = _bundle(
            identity="NOT_CONFIRMED",
            semantic_source_count=0,
            semantic_evidence="",
            target_evidence="",
            target_source_ids=[],
            stage1_rule_target_supported=False,
        )
        rows = pilot._run_arm(
            "SEM_GLINER",
            [(bundle, SimpleNamespace(sources=[]), "k")],
            NeverClassifier(),
            None,
            DummyRules(),
            {},
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("NÃO", rows[0]["Semantic Evaluated"])
        self.assertEqual("NEEDS_EVIDENCE", rows[0]["Decision Source"])
        self.assertEqual("", rows[0]["Pred Type"])
        self.assertEqual("IDENTITY_OR_EVIDENCE_GATE", rows[0]["Local Gate Reason"])


if __name__ == "__main__":
    unittest.main()
