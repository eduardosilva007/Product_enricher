import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_structured_semantic_evidence_keeps_only_target_product_node(self):
        products = [
            {
                "brand": "GLADIATOR",
                "sku": "AA518/220/PLUS",
                "name": "Amoladora Angular",
                "category": "Herramientas",
            },
            {
                "brand": "OTHER",
                "sku": "PW999",
                "name": "Pressure Washer",
                "category": "Washers",
            },
        ]
        target = pilot._structured_target_products(
            "GLADIATOR", "AA518/220/PLUS", products, self.cfg
        )
        self.assertEqual(1, len(target))
        text = pilot._structured_text(target)
        self.assertIn("Amoladora Angular", text)
        self.assertNotIn("Pressure Washer", text)

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
        self.assertNotIn("TARGET BRAND", bundle.semantic_evidence)
        self.assertNotIn("TARGET MODEL", bundle.semantic_evidence)
        self.assertTrue(bundle.stage1_rule_target_supported)

    def test_stage1_resolved_type_is_not_overwritten_by_bge_when_target_supported(self):
        class DummyRules:
            types = {"ANGLE GRINDER": {}, "PRESSURE WASHER": {}}
            raw = {"COMBO_KIT_POLICY": {"enabled": False}}

        typ, source, _, _ = pilot._select_fused_type(
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

        typ, source, _, _ = pilot._select_fused_type(
            _bundle(stage1_rule_target_supported=False),
            SimpleNamespace(sources=[]),
            {"winner": "PRESSURE WASHER"},
            DummyRules(),
        )
        self.assertEqual("PRESSURE WASHER", typ)
        self.assertEqual("BGE_RERANK", source)

    def test_composite_model_parts_are_individually_anchored_in_title(self):
        title = (
            "GLADIATOR Kit IE 6200/8/220 + AA615/5/220 PLUS "
            "soldadora y amoladora"
        )
        src = SimpleNamespace(
            title=title,
            url="https://example.com/combo",
            identity_conflict=False,
        )
        self.assertTrue(
            pilot._source_primary_target_anchor(
                src,
                "GLADIATOR",
                "IE 6200/8/220_AA615/5/220 PLUS",
                self.cfg,
                ["GLADIATOR", "GLADIATOR PRO"],
            )
        )

    def test_space_separated_composite_model_is_anchored_in_title(self):
        src = SimpleNamespace(
            title="GLADIATOR TP413/6 + AA415/2/220K Kit Herramientas",
            url="https://example.com/combo2",
            identity_conflict=False,
        )
        self.assertTrue(
            pilot._source_primary_target_anchor(
                src,
                "GLADIATOR",
                "TP413/6 AA415/2/220K",
                self.cfg,
                ["GLADIATOR", "GLADIATOR PRO"],
            )
        )

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

        src = SimpleNamespace(
            source_id=1,
            title="Kit Herramientas con amoladora angular y taladro percutor",
            content="",
            quality_flags=[],
            type_hits={},
            type_scores={},
        )
        evidence = pilot._safe_combo_kit_evidence(
            _bundle(model="TP413/7/220K", target_source_ids=[1]),
            SimpleNamespace(sources=[src]),
            DummyRules(),
        )
        self.assertTrue(evidence.is_combo)
        self.assertEqual(["ANGLE GRINDER", "HAMMER DRILL"], evidence.types)
        self.assertEqual("COMBO_TERM+MULTI_TYPE", evidence.reason)

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

        s1 = SimpleNamespace(sources=[
            SimpleNamespace(
                source_id=1, title="combo kit amoladora angular", content="",
                quality_flags=[], type_hits={}, type_scores={},
            ),
            SimpleNamespace(
                source_id=2, title="combo kit taladro percutor", content="",
                quality_flags=[], type_hits={}, type_scores={},
            ),
        ])
        evidence = pilot._safe_combo_kit_evidence(
            _bundle(model="TP413/7/220K", target_source_ids=[1, 2]),
            s1,
            DummyRules(),
        )
        self.assertFalse(evidence.is_combo)

    def test_composite_model_plus_kit_closes_combo_without_type_hits(self):
        class DummyRules:
            raw = {
                "COMBO_KIT_POLICY": {
                    "enabled": True,
                    "min_distinct_primary_types": 2,
                    "combo_terms": ["kit herramientas", "kit"],
                    "eligible_primary_types": ["ANGLE GRINDER", "INVERTER WELDER"],
                }
            }
            types = {
                "ANGLE GRINDER": {"aliases": ["amoladora angular"], "negative_aliases": []},
                "INVERTER WELDER": {"aliases": ["soldadora inverter"], "negative_aliases": []},
            }

        src = SimpleNamespace(
            source_id=1,
            title="GLADIATOR Kit Herramientas IE 6200/8/220 + AA615/5/220 PLUS",
            content="",
            quality_flags=[],
            type_hits={},
            type_scores={},
        )
        evidence = pilot._safe_combo_kit_evidence(
            _bundle(
                model="IE 6200/8/220_AA615/5/220 PLUS",
                target_source_ids=[1],
            ),
            SimpleNamespace(sources=[src]),
            DummyRules(),
        )
        self.assertTrue(evidence.is_combo)
        self.assertEqual("COMPOSITE_MODEL+COMBO_TERM", evidence.reason)

    def test_gliner_two_canonical_types_plus_combo_term_can_close_combo(self):
        class DummyRules:
            raw = {
                "COMBO_KIT_POLICY": {
                    "enabled": True,
                    "min_distinct_primary_types": 2,
                    "combo_terms": ["kit herramientas"],
                    "eligible_primary_types": ["ANGLE GRINDER", "HAMMER DRILL"],
                }
            }
            types = {
                "ANGLE GRINDER": {"aliases": ["amoladora angular"], "negative_aliases": []},
                "HAMMER DRILL": {"aliases": ["taladro percutor"], "negative_aliases": []},
            }

        evidence = pilot._safe_combo_kit_evidence(
            _bundle(
                model="TP413/7/220K",
                target_source_ids=[1],
                target_evidence="Kit Herramientas",
            ),
            SimpleNamespace(sources=[]),
            DummyRules(),
            ["ANGLE GRINDER", "HAMMER DRILL"],
        )
        self.assertTrue(evidence.is_combo)
        self.assertEqual("GLINER_TYPES+COMBO_TERM", evidence.reason)

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
        self.assertNotIn("die grinder", doc)

    def test_gliner_parser_preserves_entity_labels(self):
        result = {
            "entities": {
                "product_type": ["amoladora angular", "taladro percutor"],
                "power_source": ["batería"],
                "voltage": ["18 V"],
            }
        }
        self.assertEqual(
            ["amoladora angular", "taladro percutor"],
            pilot.OptionalGliner._label_values(result, "product_type", "product type"),
        )
        self.assertEqual(
            ["batería"],
            pilot.OptionalGliner._label_values(result, "power_source", "power source"),
        )

    def test_gliner_canonical_type_hints_ignore_power_and_voltage(self):
        class DummyRules:
            types = {
                "ANGLE GRINDER": {
                    "aliases": ["amoladora angular", "esmeriladora"],
                    "negative_aliases": [],
                },
                "HAMMER DRILL": {
                    "aliases": ["taladro percutor"],
                    "negative_aliases": [],
                },
            }
        hints = pilot.OptionalGliner._canonical_type_hints(
            DummyRules(),
            ["amoladora angular", "18 V", "batería"],
        )
        self.assertEqual(["ANGLE GRINDER"], hints)

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

    def test_jsonld_transient_failure_is_not_cached_as_permanent_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pilot.JsonLdCache(Path(td) / "jsonld.json")
            with patch.object(pilot.requests, "get", side_effect=RuntimeError("network down")):
                self.assertEqual([], cache.get("https://example.com/product"))
            self.assertNotIn("https://example.com/product", cache.data)

    def test_bge_local_auto_is_disabled_until_gold_calibration(self):
        class DummyRules:
            valid_pairs = {("OTHERS", "ANGLE GRINDER")}

        ok, reason = pilot._auto_candidate(
            _bundle(),
            {
                "rerank_probability": 0.99,
                "rerank_margin": 0.90,
                "hybrid_margin": 0.50,
            },
            ("OTHERS", "ANGLE GRINDER"),
            DummyRules(),
            "BGE_RERANK",
        )
        self.assertFalse(ok)
        self.assertEqual("LOCAL_GATE_UNCALIBRATED", reason)

    def test_known_brand_catalog_uses_full_input_not_limited_cohort(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "brands.xlsx"
            pd.DataFrame([
                {"Marca": "GLADIATOR", "Modelo": "A1"},
                {"Marca": "X", "Modelo": "X1"},
                {"Marca": "GLADIATOR PRO", "Modelo": "P1"},
            ]).to_excel(path, index=False)
            brands = pilot._known_brands_from_input(path)
        self.assertIn("GLADIATOR", brands)
        self.assertIn("GLADIATOR PRO", brands)

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

    def test_manual_gold_fallback_still_works_when_gold_columns_exist_but_are_blank(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gold_fallback.xlsx"
            pd.DataFrame([
                {
                    "Marca": "A", "Modelo": "1",
                    "Gold O que é canônico": "ANGLE GRINDER",
                    "Gold FAMILY": "ANGLE GRINDER",
                    "O que é (Tipo)": "", "FAMILY": "",
                    "Status": "Script", "Revisão B.I": "",
                },
                {
                    "Marca": "B", "Modelo": "2",
                    "Gold O que é canônico": "",
                    "Gold FAMILY": "",
                    "O que é (Tipo)": "HAMMER DRILL",
                    "Category General": "OTHERS",
                    "FAMILY": "IMPACT DRILL",
                    "Status": "Manual", "Revisão B.I": "",
                },
            ]).to_excel(path, index=False)
            gold = pilot._validated_gold_map(path)
        self.assertEqual("EXPLICIT_GOLD", gold[pilot.pp.business_key("A", "1")]["source"])
        self.assertEqual("VALIDATED_OUTPUT", gold[pilot.pp.business_key("B", "2")]["source"])
        self.assertEqual("HAMMER DRILL", gold[pilot.pp.business_key("B", "2")]["type"])

    def test_summary_measures_family_even_without_gold_type(self):
        df = pd.DataFrame([{
            "Arm": "SEM_GLINER",
            "Identity": "TARGET_CONFIRMED",
            "Semantic Evaluated": "SIM",
            "BGE Evaluated": "SIM",
            "GLiNER Evaluated": "NÃO",
            "Local Auto Candidate": "NÃO",
            "Gold Type": "",
            "Gold Category": "",
            "Gold FAMILY": "ANGLE GRINDER",
            "Type Correct": "",
            "Category Correct": "",
            "Family Correct": "SIM",
            "Taxonomy Correct": "",
            "BGE Seconds": 1.0,
            "Rerank Seconds": 2.0,
            "GLiNER Seconds": 0.0,
            "Inference Total Seconds": 3.0,
        }])
        summary = pilot._summary(df, "SEM_GLINER")
        self.assertEqual(1, summary["Gold FAMILY"])
        self.assertEqual(100.0, summary["Family Accuracy %"])
        self.assertEqual(0, summary["Gold Type"])

    def test_stage1_rule_short_circuits_bge(self):
        class NeverClassifier:
            def classify(self, query):
                raise AssertionError("BGE must not run after resolved rule")

        class DummyRules:
            types = {"ANGLE GRINDER": {}}
            raw = {"COMBO_KIT_POLICY": {"enabled": False}}
            valid_pairs = {("OTHERS", "ANGLE GRINDER")}
            def map_taxonomy(self, *args):
                return "OTHERS", "ANGLE GRINDER", "TEST"

        rows = pilot._run_arm(
            "SEM_GLINER",
            [(_bundle(), SimpleNamespace(sources=[]), "k")],
            NeverClassifier(),
            None,
            DummyRules(),
            {},
        )
        self.assertEqual("STAGE1_RULE", rows[0]["Decision Source"])
        self.assertEqual("NÃO", rows[0]["BGE Evaluated"])
        self.assertEqual(0.0, rows[0]["BGE Seconds"])

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
        self.assertEqual("NÃO", rows[0]["BGE Evaluated"])
        self.assertEqual("NÃO", rows[0]["GLiNER Evaluated"])
        self.assertEqual("NEEDS_EVIDENCE", rows[0]["Decision Source"])
        self.assertEqual("", rows[0]["Pred Type"])
        self.assertEqual("IDENTITY_OR_EVIDENCE_GATE", rows[0]["Local Gate Reason"])


if __name__ == "__main__":
    unittest.main()
