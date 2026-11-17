"""Regressão offline de adaptadores de Search; zero chamadas HTTP/LLM."""
import ast
import json
import unittest
from pathlib import Path

from search_provider_extensions import (
    normalize_scraperapi, normalize_openserp, iter_openserp_cohorts,
)

ROOT = Path(__file__).resolve().parents[1]


class ProviderExtensionTests(unittest.TestCase):
    def test_scraperapi_raw_shape_and_goto_block(self):
        sample = {
            "organic_results": [
                {"position": 1, "title": "STIHL FS250 Brush Cutter",
                 "link": "https://example.com/stihl-fs250", "snippet": "40.2 cc brush cutter"},
                {"position": 2, "title": "Part for STIHL FS250",
                 "link": "https://www.google.com/goto?url=opaque", "snippet": "Spare parts"},
            ]
        }
        rows = normalize_scraperapi(sample)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 1.0)
        self.assertEqual(rows[0]["raw_content"], "")

    def test_scraperapi_fail_closed_for_wrong_schema(self):
        with self.assertRaises(ValueError):
            normalize_scraperapi({"organic": []})

    def test_openserp_batch_and_unverified(self):
        sample = {
            "results": [{
                "provider": "openserp", "status": "CAPTURED",
                "query": "STIHL MS382", "organic": [
                    {"rank": 1, "title": "STIHL MS 382 Chainsaw",
                     "url": "https://example.com/stihl-ms382", "snippet": "Petrol chainsaw"},
                    {"rank": 2, "title": "STIHL MS382 spare part",
                     "url": "https://www.google.com/goto?url=opaque", "snippet": "Parts"}
                ],
            }]
        }
        groups = list(iter_openserp_cohorts(sample))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "STIHL MS382")
        self.assertEqual(len(groups[0][1]), 1)

    def test_openserp_not_captured_rejected(self):
        with self.assertRaises(ValueError):
            normalize_openserp({"status": "FAILED", "organic": []})

    def test_no_new_automatic_router_or_extra_search(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        ast.parse(source)
        self.assertIn('ALLOWED_SEARCH_PROVIDERS = {"tavily", "serper", "brightdata", "scraperapi"}', source)
        self.assertIn('if provider == "scraperapi":', source)
        self.assertIn('"4": "scraperapi"', source)
        self.assertNotIn('"openserp", "scraperapi"', source)

    def test_staging_script_compiles(self):
        ast.parse((ROOT / "import_openserp_staging.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
