"""Offline SQLite router tests. No external API calls."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nightly_provider_router import Slot, DurableRouter, configured_slots

class RouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "run.sqlite"
        self.slots = [
            Slot("tavily","tavily:1","TAVILY_API_KEY","",1,"a"),
            Slot("brightdata","brightdata:1","BRIGHTDATA_API_KEY","BRIGHTDATA_ZONE",1,"b"),
            Slot("brightdata","brightdata:2","BRIGHTDATA_API_KEY_2","BRIGHTDATA_ZONE",1,"c"),
            Slot("serper","serper:1","SERPER_API_KEY","",1,"d"),
        ]
    def tearDown(self):
        self.tmp.cleanup()
    def test_reservations_survive_restart(self):
        r=DurableRouter(self.path,self.slots)
        self.assertEqual(r.reserve("brand|sku1").account,"tavily:1")
        r.close()
        r=DurableRouter(self.path,self.slots)
        self.assertIsNone(r.reserve("brand|sku1"))
        self.assertEqual(r.reserve("brand|sku2").account,"brightdata:1")
        r.close()
    def test_quota_rotation_and_exhaustion(self):
        r=DurableRouter(self.path,self.slots)
        accounts=[r.reserve(f"k{i}").account for i in range(4)]
        self.assertEqual(accounts, [x.account for x in self.slots])
        self.assertIsNone(r.reserve("k5"))
        r.close()
    def test_disable_account_after_http_limit(self):
        r=DurableRouter(self.path,self.slots)
        r.reserve("k1")
        r.disable("brightdata:1","BRIGHTDATA_LIMIT")
        self.assertEqual(r.reserve("k2").account,"brightdata:2")
        r.close()
    def test_shared_group_cap(self):
        slots=[
            Slot("brightdata","brightdata:1","BRIGHTDATA_API_KEY","BRIGHTDATA_ZONE",2,"same"),
            Slot("brightdata","brightdata:2","BRIGHTDATA_API_KEY_2","BRIGHTDATA_ZONE",2,"same"),
        ]
        r=DurableRouter(self.path,slots)
        self.assertIsNotNone(r.reserve("a"))
        self.assertIsNotNone(r.reserve("b"))
        self.assertIsNone(r.reserve("c"))
        r.close()
    def test_shared_group_with_conflicting_caps_fails_before_sqlite(self):
        slots = [
            Slot("brightdata", "brightdata:1", "BRIGHTDATA_API_KEY", "BRIGHTDATA_ZONE", 2, "shared"),
            Slot("brightdata", "brightdata:2", "BRIGHTDATA_API_KEY_2", "BRIGHTDATA_ZONE", 3, "shared"),
        ]
        with self.assertRaisesRegex(ValueError, "Conflicting limits"):
            DurableRouter(self.path, slots)
        self.assertFalse(self.path.exists())

    def test_duplicate_accounts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate provider account"):
            DurableRouter(self.path, [self.slots[0], self.slots[0]])

    def test_requires_explicit_budget(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY":"test-key", "TAVILY_LIMIT_1":""},clear=True):
            self.assertEqual(configured_slots(),[])
    def test_multi_key_config(self):
        env={"BRIGHTDATA_API_KEY_1":"a","BRIGHTDATA_API_KEY_2":"b",
             "BRIGHTDATA_ZONE_1":"serp_api1", "BRIGHTDATA_ZONE_2":"serp_api2",
             "BRIGHTDATA_LIMIT_1":"2","BRIGHTDATA_LIMIT_2":"3"}
        with patch.dict(os.environ,env,clear=True):
            self.assertEqual([s.account for s in configured_slots()], ["brightdata:1","brightdata:2"])
if __name__ == "__main__":
    unittest.main()
