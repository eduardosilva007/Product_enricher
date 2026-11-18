import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline_produtos as pp
from openserp_recover_artifact import recover


class OpenSERPRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache.sqlite"
        self.db = self.root / "classification.sqlite"
        self.config = self.root / "pipeline_config.json"
        self.policy = self.root / "collection_quality_policy.json"
        self.artifact = self.root / "openserp_staging.json"
        self.brand = "KSB"
        self.model = "MOVITEC VS F 2/4 B"
        self.key = pp.business_key(self.brand, self.model)
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            cx.execute("CREATE TABLE tavily_cache(cache_key TEXT PRIMARY KEY, brand TEXT, model TEXT, response_json TEXT, created_at TEXT)")
        with closing(sqlite3.connect(self.db)) as cx, cx:
            cx.execute("CREATE TABLE collection_attempt_reservations(business_key TEXT PRIMARY KEY, account TEXT, provider TEXT, reserved_at TEXT)")
            cx.execute("INSERT INTO collection_attempt_reservations VALUES (?,?,?,?)",
                       (self.key, "single:tavily", "tavily", "2026-11-17 14:01:57"))
        (self.root / "master.xlsx").write_bytes(b"placeholder")
        (self.root / "final.xlsx").write_bytes(b"placeholder")
        self.config.write_text(json.dumps({
            "files": {"master": str(self.root / "master.xlsx"),
                      "final": str(self.root / "final.xlsx")},
            "collection": {"query_template": "{brand} {search_model}",
                           "hard_limits": {"max_requests_per_product": 1,
                                           "allow_automatic_retry": False,
                                           "allow_secondary_search": False,
                                           "allow_provider_failover": False,
                                           "allow_extract": False},
                           "local_enrichment": {"enabled": True}}
        }), encoding="utf-8")
        self.policy.write_text(json.dumps({}), encoding="utf-8")
        self.artifact.write_text(json.dumps({
            "schema": "openserp-staging-v1", "promotion_allowed": False,
            "cohorts": [{
                "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
                "brand": self.brand, "model": self.model,
                "query": "KSB MOVITEC VS F 2/4 B",
                "original_provider_query": "KSB MOVITEC VS F 2/4 B",
                "normalized_results": [{
                    "title": "KSB MOVITEC VS F 2/4 B",
                    "url": "https://example.org/pump",
                    "content": "pump", "_provider_position": 1
                }]
            }]
        }), encoding="utf-8")

    def invoke(self, commit=False):
        return recover(artifact=self.artifact, brand=self.brand, model=self.model,
                       commit=commit, config=self.config, policy=self.policy,
                       cache=self.cache, db=self.db)

    def test_dry_run_checks_exact_staging_and_existing_reservation_without_writing(self):
        with patch("pipeline_produtos.load_baseline_inventory", return_value={"processed": set()}):
            report = self.invoke()
        self.assertEqual(report["staged_raw_results"], 1)
        self.assertEqual(report["network_search_calls"], 0)
        self.assertFalse(report["committed"])
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            self.assertEqual(cx.execute("SELECT COUNT(*) FROM tavily_cache").fetchone()[0], 0)

    def test_missing_reservation_still_recovers_captured_artifact_without_search(self):
        with closing(sqlite3.connect(self.db)) as cx, cx:
            cx.execute("DELETE FROM collection_attempt_reservations")
        with patch("pipeline_produtos.load_baseline_inventory", return_value={"processed": set()}), \
             patch("pipeline_produtos.search_product_once") as search:
            report = self.invoke()
        self.assertEqual(report["status"], "PREFLIGHT_OK")
        self.assertEqual(report["previous_attempts"], [])
        self.assertEqual(report["network_search_calls"], 0)
        search.assert_not_called()
        with closing(sqlite3.connect(self.db)) as cx, cx:
            self.assertEqual(cx.execute(
                "SELECT COUNT(*) FROM collection_attempt_reservations"
            ).fetchone()[0], 0)

    def test_commit_without_prior_reservation_keeps_ledger_unchanged(self):
        with closing(sqlite3.connect(self.db)) as cx, cx:
            cx.execute("DELETE FROM collection_attempt_reservations")
        from hashlib import sha256
        payload = {
            "results": [{"title": "KSB pump", "url": "https://example.org/pump"}],
            "_search_meta": {
                "network_requests": 0, "artifact_searches_reused": 1,
                "artifact_sha256": sha256(self.artifact.read_bytes()).hexdigest(),
            },
            "_collection": {
                "state": "NO_TARGET_EVIDENCE", "curated_results": [],
                "usage": {"requests": 1},
            },
        }
        with patch("pipeline_produtos.load_baseline_inventory", return_value={"processed": set()}), \
             patch("pipeline_produtos.search_product_once", return_value=payload):
            report = self.invoke(commit=True)
        self.assertTrue(report["committed"])
        self.assertEqual(report["network_search_calls"], 0)
        with closing(sqlite3.connect(self.db)) as cx, cx:
            self.assertEqual(cx.execute(
                "SELECT COUNT(*) FROM collection_attempt_reservations"
            ).fetchone()[0], 0)

    def test_cannot_replay_other_model_or_override_prior_cache(self):
        with self.assertRaisesRegex(ValueError, "REPLAY_COHORT_IDENTITY_MISMATCH"):
            recover(artifact=self.artifact, brand=self.brand,
                    model="MOVITEC VS F 2/5 B", commit=False,
                    config=self.config, policy=self.policy,
                    cache=self.cache, db=self.db)
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            cx.execute("INSERT INTO tavily_cache VALUES (?,?,?,?,?)",
                       ("v3|" + self.key, self.brand, self.model, '{"results":[]}', "now"))
        with self.assertRaisesRegex(RuntimeError, "REPLAY_CACHE_ALREADY_EXISTS"):
            self.invoke()

    def test_commit_persists_raw_only_and_preserves_attempt(self):
        fake_payload = {
            "results": [{"title": "KSB pump", "url": "https://example.org/pump"}],
            "_search_meta": {"network_requests": 0, "artifact_searches_reused": 1},
            "_collection": {"state": "NO_TARGET_EVIDENCE", "curated_results": [],
                            "usage": {"requests": 1}}
        }
        def fake_search(brand, model, cfg, limiter, provider):
            self.assertEqual(provider, "openserp")
            self.assertFalse(cfg["collection"]["local_enrichment"]["enabled"])
            from search_provider_extensions import load_openserp_staging
            from hashlib import sha256
            fake_payload["_search_meta"]["artifact_sha256"] = sha256(self.artifact.read_bytes()).hexdigest()
            return fake_payload

        with patch("pipeline_produtos.load_baseline_inventory", return_value={"processed": set()}), \
             patch("pipeline_produtos.search_product_once", side_effect=fake_search):
            report = self.invoke(commit=True)
        self.assertTrue(report["committed"])
        self.assertEqual(report["curated_sources"], 0)
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            row = cx.execute("SELECT response_json FROM tavily_cache WHERE cache_key=?",
                             ("v3|" + self.key,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(len(json.loads(row[0])["results"]), 1)
        with closing(sqlite3.connect(self.db)) as cx, cx:
            self.assertEqual(cx.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
