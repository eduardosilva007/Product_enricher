"""Regressão offline de adaptadores de Search; zero chamadas HTTP/LLM."""
import ast
import json
import tempfile
import unittest
from pathlib import Path

from search_provider_extensions import (
    normalize_scraperapi, normalize_openserp, iter_openserp_cohorts, load_openserp_staging,
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
        self.assertIn('ALLOWED_SEARCH_PROVIDERS = {"tavily", "serper", "brightdata", "scraperapi", "openserp"}', source)
        self.assertIn('if provider == "scraperapi":', source)
        self.assertIn('"4": "scraperapi"', source)
        self.assertIn('"5": "openserp"', source)
        self.assertIn('if provider == "openserp":', source)
        self.assertNotIn('"openserp", "scraperapi"', source)

    def test_staging_script_compiles(self):
        ast.parse((ROOT / "import_openserp_staging.py").read_text(encoding="utf-8"))


    def test_openserp_staging_exact_source_and_query(self):
        item = {
            "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
            "query": "STIHL FS250", "brand": "STIHL", "model": "FS250",
            "normalized_results": [{
                "title": "STIHL FS250 Brushcutter", "url": "https://stihl.example.com/fs250",
                "content": "STIHL FS250 brushcutter", "_provider_position": 2,
                "raw_content": "SHOULD NEVER BE USED"
            }],
            "approved_candidates": [{"title": "UNTRUSTED"}]
        }
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "staging.json"
            file.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [item],
            }), encoding="utf-8")
            results, provenance = load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS250")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["raw_content"], "")
            self.assertEqual(results[0]["score"], 0.5)
            self.assertEqual(provenance["network_requests"], 0)
            self.assertEqual(provenance["artifact_searches_reused"], 1)
            self.assertEqual(len(provenance["artifact_sha256"]), 64)
            with self.assertRaisesRegex(ValueError, "QUERY_MISMATCH"):
                load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS251")
            with self.assertRaisesRegex(ValueError, "SKU_NOT_IN_ARTIFACT"):
                load_openserp_staging(str(file), "STIHL", "FS251", "STIHL FS250")
            item["normalized_results"][0]["url"] = "https://www.google.com/goto?url=opaque"
            file.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [item],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "RAW_UNSAFE_URL"):
                load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS250")

    def test_openserp_to_sqlite_roundtrip_offline(self):
        """Exercita request adapter -> Source Sieve -> RAW -> SQLite -> releitura."""
        from contextlib import closing
        from pipeline_produtos import (
            RateLimiter, TavilyWritableStore, TavilyReadOnlyStore,
            read_json, search_product_once,
        )
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            artifact = base / "staging.json"
            artifact.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [{
                    "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
                    "query": "STIHL FS250", "brand": "STIHL", "model": "FS250",
                    "normalized_results": [{
                        "title": "STIHL FS250 Brushcutter",
                        "url": "https://example.com/stihl-fs250",
                        "content": "STIHL FS250 petrol brushcutter",
                        "_provider_position": 1
                    }],
                }],
            }), encoding="utf-8")
            cfg = read_json(ROOT / "pipeline_config.json")
            cfg["_collection_policy"] = read_json(ROOT / "collection_quality_policy.json")
            cfg["_openserp_artifact_path"] = str(artifact)
            cfg["_openserp_target"] = ("STIHL", "FS250")
            cfg["collection"]["local_enrichment"]["enabled"] = False
            payload = search_product_once(
                "STIHL", "FS250", cfg, RateLimiter(10000), "openserp"
            )
            self.assertEqual(payload["_collection"]["provider"], "openserp")
            self.assertEqual(payload["_collection"]["usage"]["requests"], 1)
            self.assertEqual(payload["_collection"]["usage"]["network_requests"], 0)
            self.assertEqual(len(payload["results"]), 1)
            sqlite_path = base / "cache.sqlite"
            with closing(TavilyWritableStore(sqlite_path)) as writer:
                writer.put("STIHL", "FS250", payload)
            with closing(TavilyReadOnlyStore(sqlite_path)) as reader:
                raw = reader.get("STIHL", "FS250")
            self.assertEqual(raw.state, "RAW_OK")
            self.assertEqual(raw.payload["_collection"]["provider"], "openserp")
            self.assertEqual(raw.payload["results"][0]["url"], "https://example.com/stihl-fs250")

    def test_experimental_providers_require_canonical_paths_and_hard_caps(self):
        from pipeline_produtos import (
            _enforce_central_search_paths, build_parser, read_json,
        )
        cfg = read_json(ROOT / "pipeline_config.json")
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "staging.json"
            marker.write_text("{}", encoding="utf-8")
            cfg["_openserp_artifact_path"] = str(marker)
            for provider in ("openserp", "scraperapi"):
                args = build_parser().parse_args([
                    "--mode", "full", "--search-provider", provider,
                    "--max-search-new", "3", "--max-items", "3",
                    "--max-api-calls", "15", "--max-tokens", "30000"
                ])
                _enforce_central_search_paths(args, cfg, provider)
                args.cache = str(Path(folder) / "pilot_cache.sqlite")
                with self.assertRaisesRegex(ValueError, "CENTRAL_PATH_REQUIRED"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.cache = ""
                args.db = str(Path(folder) / "pilot_classification.sqlite")
                with self.assertRaisesRegex(ValueError, "CENTRAL_PATH_REQUIRED"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.db = ""
                args.auto_providers = True
                with self.assertRaisesRegex(ValueError, "MANUAL_ONLY"):
                    _enforce_central_search_paths(args, cfg, provider)

    def test_experimental_provider_blocks_unbounded_batches(self):
        from pipeline_produtos import _enforce_central_search_paths, build_parser, read_json
        cfg = read_json(ROOT / "pipeline_config.json")
        args = build_parser().parse_args(["--mode", "collect", "--search-provider", "scraperapi"])
        with self.assertRaisesRegex(ValueError, "PILOT_CAP_REQUIRED"):
            _enforce_central_search_paths(args, cfg, "scraperapi")
        args.max_tavily_new = 11
        with self.assertRaisesRegex(ValueError, "PILOT_CAP_REQUIRED"):
            _enforce_central_search_paths(args, cfg, "scraperapi")

    def test_no_parallel_openserp_runner(self):
        self.assertFalse((ROOT / "run_openserp_full_pilot.py").exists())

    def test_openserp_reuses_without_network_or_provider_secret(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn('load_openserp_staging(', source)
        self.assertIn('"network_requests": 0', source)
        self.assertIn('budget.reserve()', source)
        self.assertIn('"openserp"', source)


if __name__ == "__main__":
    unittest.main()
