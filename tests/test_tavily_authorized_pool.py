"""Offline authorization, budget and restart regressions for Tavily-only routing.

No network, API credits, Excel writes or LLM calls.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_produtos import DurableRouter, configured_slots, build_parser


class TavilyAuthorizedPoolTests(unittest.TestCase):
    def test_parser_accepts_explicit_pool_mode(self):
        args = build_parser().parse_args([
            "--mode", "collect", "--search-provider", "tavily",
            "--tavily-pool", "--max-search-new", "3",
        ])
        self.assertTrue(args.tavily_pool)
        self.assertFalse(args.auto_providers)
        self.assertEqual(args.max_tavily_new, 3)

    def test_twenty_tavily_slots_and_other_providers_can_be_excluded(self):
        env = {f"TAVILY_API_KEY_{i}": f"offline-key-{i}" for i in range(1, 21)}
        env.update({f"TAVILY_LIMIT_{i}": "2" for i in range(1, 21)})
        env.update({"SERPER_API_KEY_1": "unused", "SERPER_LIMIT_1": "2"})
        with patch.dict(os.environ, env, clear=True):
            authorized = [slot for slot in configured_slots() if slot.provider == "tavily"]
        self.assertEqual(len(authorized), 20)
        self.assertEqual([slot.account for slot in authorized], [f"tavily:{i}" for i in range(1, 21)])

    def test_budget_switches_on_next_sku_not_retry_of_same_sku(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "1",
                "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "1",
            }
            with patch.dict(os.environ, env, clear=True):
                slots = [s for s in configured_slots() if s.provider == "tavily"]
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                self.assertEqual(router.reserve("SKU-A").account, "tavily:1")
                self.assertIsNone(router.reserve("SKU-A"))
                self.assertEqual(router.reserve("SKU-B").account, "tavily:2")
                self.assertIsNone(router.reserve("SKU-C"))
            finally:
                router.close()
            resumed = DurableRouter(path, slots)
            try:
                self.assertTrue(resumed.already_reserved("SKU-A"))
                self.assertTrue(resumed.already_reserved("SKU-B"))
                self.assertIsNone(resumed.reserve("SKU-C"))
            finally:
                resumed.close()
            # sqlite3.Connection.__exit__ commits/rolls back but does NOT close.
            # Windows prevents TemporaryDirectory cleanup while the DB handle is open.
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                count = db.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0]
            self.assertEqual(count, 2)

    def test_billing_group_shared_budget_not_multiplied_by_keys(self):
        env = {
            "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "1",
            "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "1",
            "TAVILY_BILLING_GROUP_1": "same-authorized-billing-group",
            "TAVILY_BILLING_GROUP_2": "same-authorized-billing-group",
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            router = DurableRouter(Path(tmp) / "attempts.sqlite", slots)
            try:
                self.assertEqual(router.reserve("SKU-A").account, "tavily:1")
                self.assertIsNone(router.reserve("SKU-B"))
            finally:
                router.close()

    def test_explicit_new_period_keeps_old_sku_reservations(self):
        env = {
            "TAVILY_API_KEY_1": "offline-key-1",
            "TAVILY_LIMIT_1": "2",
            "TAVILY_BUDGET_SINCE_1": "2099-01-01",
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                router.conn.execute(
                    "INSERT INTO collection_attempt_reservations "
                    "(business_key,account,provider,reserved_at) VALUES (?,?,?,?)",
                    ("SKU-LEGACY", "tavily:1", "tavily", "2026-11-15 06:10:48"),
                )
                router.conn.commit()
                self.assertIsNone(router.reserve("SKU-LEGACY"))
                self.assertEqual(router.reserve("SKU-NEW").account, "tavily:1")
            finally:
                router.close()
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                total = db.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0]
            self.assertEqual(total, 2)

    def test_invalid_budget_period_rejected(self):
        with patch.dict(os.environ, {
            "TAVILY_API_KEY_1": "offline-key",
            "TAVILY_LIMIT_1": "2",
            "TAVILY_BUDGET_SINCE_1": "yesterday",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "Invalid budget date"):
                configured_slots()

    def test_disabled_account_remains_disabled_after_restart(self):
        env = {
            "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "2",
            "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "2",
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                router.disable("tavily:1", "TAVILY_LIMIT")
            finally:
                router.close()
            restarted = DurableRouter(path, slots)
            try:
                self.assertEqual(restarted.reserve("SKU-A").account, "tavily:2")
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
