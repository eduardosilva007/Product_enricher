"""No-live-search batch orchestration regression tests."""
import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pipeline_produtos as pp


class OpenSERPBatchCampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "pipeline_config.json"
        self.cache = root / "cache.sqlite"
        self.db = root / "db.sqlite"
        self.config.write_text(json.dumps({
            "files": {"classification_db": str(self.db), "cache": str(self.cache)}
        }), encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as cx, cx:
            cx.execute("CREATE TABLE collection_attempt_reservations(business_key TEXT PRIMARY KEY)")
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            cx.execute("CREATE TABLE tavily_cache(cache_key TEXT PRIMARY KEY)")
        self.args = pp.build_parser().parse_args([
            "--mode", "collect", "--search-provider", "openserp",
            "--openserp-auto", "--max-search-new", "25",
            "--config", str(self.config), "--yes"
        ])
        self.issued = 0

    def parse_child(self, argv):
        # argv[0:2] are interpreter + script; parse aliases using the
        # production parser rather than depending on a particular flag spelling.
        parsed = pp.build_parser().parse_args(argv[2:])
        self.assertTrue(parsed.openserp_auto)
        self.assertEqual(parsed.search_provider, "openserp")
        return parsed

    def callback(self, argv, **kwargs):
        limit = self.parse_child(argv).max_tavily_new
        self.issued += limit
        with closing(sqlite3.connect(self.db)) as cx, cx:
            for i in range(self.issued-limit, self.issued):
                cx.execute("INSERT INTO collection_attempt_reservations VALUES (?)", (f"sku{i}",))
        with closing(sqlite3.connect(self.cache)) as cx, cx:
            for i in range(self.issued-limit, self.issued):
                cx.execute("INSERT INTO tavily_cache VALUES (?)", (f"v3|sku{i}",))
        return argparse.Namespace(returncode=0)

    def test_twenty_five_skus_split_10_10_5(self):
        sizes = []
        def fake_run(argv, **kwargs):
            sizes.append(self.parse_child(argv).max_tavily_new)
            return self.callback(argv, **kwargs)
        with patch("subprocess.run", side_effect=fake_run):
            status = pp._run_openserp_batched_campaign(self.args)
        self.assertEqual(status, 0)
        self.assertEqual(sizes, [10, 10, 5])
        with closing(sqlite3.connect(self.cache)) as cx:
            self.assertEqual(cx.execute("SELECT COUNT(*) FROM tavily_cache").fetchone()[0], 25)

    def test_missing_central_persistence_stops_before_next_batch(self):
        def reserve_only(argv, **kwargs):
            with closing(sqlite3.connect(self.db)) as cx, cx:
                cx.execute("INSERT INTO collection_attempt_reservations VALUES ('unwritten')")
            return argparse.Namespace(returncode=0)
        with patch("subprocess.run", side_effect=reserve_only) as proc:
            status = pp._run_openserp_batched_campaign(self.args)
        self.assertEqual(status, 3)
        self.assertEqual(proc.call_count, 1)

    def test_failed_child_stops_without_retry(self):
        with patch("subprocess.run", return_value=argparse.Namespace(returncode=4)) as proc:
            status = pp._run_openserp_batched_campaign(self.args)
        self.assertEqual(status, 4)
        self.assertEqual(proc.call_count, 1)

    def test_full_mode_preserves_classification_cap_per_batch(self):
        self.args.mode = "full"
        self.args.max_items = 25
        caps = []
        def fake_run(argv, **kwargs):
            caps.append(self.parse_child(argv).max_items)
            return self.callback(argv, **kwargs)
        with patch("subprocess.run", side_effect=fake_run):
            status = pp._run_openserp_batched_campaign(self.args)
        self.assertEqual(status, 0)
        self.assertEqual(caps, [10, 10, 5])

    def test_full_mode_rejects_undersized_classification_total(self):
        self.args.mode = "full"
        self.args.max_items = 10
        with self.assertRaisesRegex(ValueError, "CLASS_LIMIT_GE_SEARCH_TOTAL"):
            pp._run_openserp_batched_campaign(self.args)


if __name__ == "__main__":
    unittest.main()
