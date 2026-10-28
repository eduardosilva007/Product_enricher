import unittest
from pathlib import Path

import pilot_v41_ab as pilot


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

    def test_brand_and_model_primary_confirm_target(self):
        brand_ok = pilot._brand_primary_occurs(
            "BAROVO",
            "Barovo BIM-TPA13B Taladro Percutor",
            "https://barovo.com/bim-tpa13b",
        )
        model_ok = pilot._model_primary_occurs(
            "BIM-TPA13B",
            "Barovo BIM-TPA13B Taladro Percutor",
            "https://barovo.com/bim-tpa13b",
            self.cfg,
        )
        self.assertTrue(brand_ok and model_ok)

    def test_model_collision_does_not_confirm_wrong_brand(self):
        brand_ok = pilot._brand_primary_occurs(
            "ADV3",
            "Akai AA-8500 Stereo Amplifier",
            "https://akai.example/aa-8500",
        )
        model_ok = pilot._model_primary_occurs(
            "AA8500",
            "Akai AA-8500 Stereo Amplifier",
            "https://akai.example/aa-8500",
            self.cfg,
        )
        self.assertTrue(model_ok)
        self.assertFalse(brand_ok)

    def test_jsonld_brand_plus_sku_confirms(self):
        products = [{"brand": "ADV3", "sku": "AA7000", "name": "Produto ADV3"}]
        self.assertTrue(
            pilot._structured_identity_confirmed("ADV3", "AA7000", products, self.cfg)
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


    def test_stage1_resolved_type_is_not_overwritten_by_bge(self):
        bundle = pilot.EvidenceBundle(
            brand="GLADIATOR",
            model="AA518/220/PLUS",
            identity="TARGET_CONFIRMED",
            identity_reasons=[],
            evidence="",
            semantic_evidence="amoladora angular",
            source_count=1,
            structured_count=0,
            stage1_state="RESOLVED_RULE",
            stage1_type="ANGLE GRINDER",
            stage1_power="CORDED",
        )
        class DummyRules:
            types = {"ANGLE GRINDER": {}, "PRESSURE WASHER": {}}
            raw = {"COMBO_KIT_POLICY": {"enabled": False}}
        typ, source, _ = pilot._select_fused_type(
            bundle, {"winner": "PRESSURE WASHER"}, DummyRules(), "amoladora angular"
        )
        self.assertEqual("ANGLE GRINDER", typ)
        self.assertEqual("STAGE1_RULE", source)

    def test_combo_policy_requires_two_distinct_tool_types(self):
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
        ok, hits = pilot._combo_kit_candidate(
            DummyRules(),
            "Combo kit con amoladora angular y taladro percutor",
        )
        self.assertTrue(ok)
        self.assertEqual(["ANGLE GRINDER", "HAMMER DRILL"], hits)

    def test_combo_family_matches_canonical_gold_taxonomy(self):
        rules = pilot.pp.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
        cg, fam, _ = rules.map_taxonomy(
            "POWER TOOL COMBO KIT", "UNKNOWN", "combo kit drill grinder"
        )
        self.assertEqual("OTHERS", cg)
        self.assertEqual("COMBO KIT", fam)

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



if __name__ == "__main__":
    unittest.main()
