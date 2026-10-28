import unittest

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


if __name__ == "__main__":
    unittest.main()
