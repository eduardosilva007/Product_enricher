import json
import unittest
from pathlib import Path

import pipeline_produtos as p


BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "pipeline_config.json").read_text(encoding="utf-8"))


class StructuralSkuCoreV4073Tests(unittest.TestCase):
    def test_milwaukee_core_variant_is_generated_without_brand_patch(self):
        suffixes = CFG["identity"].get("regional_suffixes", [])
        variants = dict(p._model_identity_variants("2446 21XC M12", suffixes))
        self.assertIn("244621xcm12", variants)
        self.assertIn("244621xc", variants)
        self.assertEqual(variants["244621xc"], "STRUCTURAL_EDGE_DECORATOR")

    def test_milwaukee_published_title_matches_strong(self):
        meta = p._source_identity_for_result(
            "MILWAUKEE",
            "2446 21XC M12",
            {
                "title": "Milwaukee M12 2446-21XC Grease Gun Kit",
                "url": "https://example.com/2446-21xc",
                "content": "",
            },
            CFG,
        )
        self.assertEqual(meta["level"], "STRONG")
        self.assertIn(meta["model_match_field"], {"TITLE", "URL"})

    def test_milwaukee_official_style_url_matches_strong_even_without_model_in_title(self):
        meta = p._source_identity_for_result(
            "MILWAUKEE",
            "2446 21XC M12",
            {
                "title": "M12 Cordless Lithium-ion Grease Gun Kit | Milwaukee Tool",
                "url": "https://www.milwaukeetool.com/products/details/m12-cordless-lithium-ion-grease-gun-kit/2446-21xc",
                "content": "",
            },
            CFG,
        )
        self.assertEqual(meta["level"], "STRONG")
        self.assertEqual(meta["model_match_field"], "URL")

    def test_milwaukee_wrong_variant_stays_non_target(self):
        meta = p._source_identity_for_result(
            "MILWAUKEE",
            "2446 21XC M12",
            {
                "title": "Milwaukee 2446-20 M12 Grease Gun",
                "url": "https://example.com/2446-20",
                "content": "",
            },
            CFG,
        )
        self.assertNotIn(meta["level"], {"STRONG", "PROBABLE"})

    def test_generic_short_core_does_not_drop_platform_token(self):
        suffixes = CFG["identity"].get("regional_suffixes", [])
        variants = {value for value, _ in p._model_identity_variants("ABC 123 M12", suffixes)}
        self.assertIn("abc123m12", variants)
        self.assertNotIn("abc123", variants)

    def test_anthbot_eu_core_is_generic_not_brand_specific(self):
        suffixes = CFG["identity"].get("regional_suffixes", [])
        variants = dict(p._model_identity_variants("GENIE 1000 EU", suffixes))
        self.assertIn("genie1000", variants)
        self.assertEqual(variants["genie1000"], "STRUCTURAL_EDGE_DECORATOR")

    def test_original_model_is_always_preserved(self):
        suffixes = CFG["identity"].get("regional_suffixes", [])
        variants = p._model_identity_variants("GENIE 1000 EU", suffixes)
        self.assertEqual(variants[0], ("genie1000eu", "EXACT"))

    def test_runtime_contract_passes_with_structural_core(self):
        self.assertEqual(p.validate_identity_runtime_contract(CFG), [])

    def test_target_spans_find_core_inside_reordered_platform_title(self):
        spans = p._target_spans(
            "Milwaukee M12 2446-21XC Grease Gun Kit",
            "2446 21XC M12",
            CFG,
        )
        self.assertTrue(spans)

    def test_same_target_sku_recognizes_core_form(self):
        self.assertTrue(
            p._same_target_sku_token(
                "MILWAUKEE",
                "2446 21XC M12",
                "2446-21XC",
                CFG,
            )
        )


if __name__ == "__main__":
    unittest.main()
