"""Offline Bright Data multizone regression checks (no API/DB)."""
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline_produtos import load_env_file,configured_slots,_provider_credentials,validate_collection_cost_policy,DurableRouter,_normalize_brightdata_results

class TestBrightDataMultiZone(unittest.TestCase):
    def test_two_independent_zones(self):
        env={"BRIGHTDATA_API_KEY_1":"unique-a","BRIGHTDATA_ZONE_1":"serp_api1","BRIGHTDATA_LIMIT_1":"4990",
             "BRIGHTDATA_API_KEY_2":"unique-b","BRIGHTDATA_ZONE_2":"serp_api2","BRIGHTDATA_LIMIT_2":"5000"}
        with patch.dict(os.environ,env,clear=True):
            slots=[s for s in configured_slots() if s.provider=="brightdata"]
            self.assertEqual(len(slots),2)
            zones=[]
            for s in slots:
                cfg={"collection":{"providers":{"brightdata":{}}},"_active_search_slot":{"provider":"brightdata","key_env":s.key_env,"zone_env":s.zone_env}}
                zones.append(_provider_credentials("brightdata",cfg)["zone"])
            self.assertEqual(zones,["serp_api1","serp_api2"])
    def test_missing_second_zone_fails_closed(self):
        env={"BRIGHTDATA_API_KEY_1":"a","BRIGHTDATA_API_KEY_2":"b","BRIGHTDATA_ZONE_1":"one",
             "BRIGHTDATA_ZONE":"shared","BRIGHTDATA_LIMIT_1":"1","BRIGHTDATA_LIMIT_2":"1"}
        with patch.dict(os.environ,env,clear=True):
            with self.assertRaisesRegex(ValueError,"BRIGHTDATA_ZONE_2"): configured_slots()
    def test_duplicate_credential_fails_closed(self):
        env={"BRIGHTDATA_API_KEY_1":"same","BRIGHTDATA_API_KEY_2":"same",
             "BRIGHTDATA_ZONE_1":"one","BRIGHTDATA_ZONE_2":"two",
             "BRIGHTDATA_LIMIT_1":"1","BRIGHTDATA_LIMIT_2":"1"}
        with patch.dict(os.environ,env,clear=True):
            with self.assertRaisesRegex(ValueError,"BRIGHTDATA_DUPLICATE_CREDENTIAL"): configured_slots()
    def test_duplicate_env_line_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/".env"
            path.write_text("BRIGHTDATA_ZONE=serp_api1\nBRIGHTDATA_ZONE=serp_api2\n")
            with patch.dict(os.environ,{},clear=True):
                with self.assertRaisesRegex(ValueError,"DUPLICATE_PROVIDER_ENV_KEY"): load_env_file(path)
    def test_remaining_budget_excludes_historical_reservations(self):
        env={"BRIGHTDATA_API_KEY_1":"a","BRIGHTDATA_ZONE_1":"serp_api1","BRIGHTDATA_LIMIT_1":"2",
             "BRIGHTDATA_API_KEY_2":"b","BRIGHTDATA_ZONE_2":"serp_api2","BRIGHTDATA_LIMIT_2":"3"}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,env,clear=True):
            db=Path(d)/"ledger.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE collection_attempt_reservations (business_key TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)")
                con.executemany("INSERT INTO collection_attempt_reservations(business_key,account,provider) VALUES(?,?,?)",
                                [(f"old-{n}","brightdata:1","brightdata") for n in range(5000)])
            router=DurableRouter(db,configured_slots())
            try:
                self.assertEqual(router.reserve("new-1").account,"brightdata:1")
                self.assertEqual(router.reserve("new-2").account,"brightdata:1")
                self.assertEqual(router.reserve("new-3").account,"brightdata:2")
                self.assertIsNone(router.reserve("old-1"))
                baseline=router.conn.execute("SELECT baseline_attempts FROM collection_budget_epochs WHERE account='brightdata:1'").fetchone()[0]
                self.assertEqual(baseline,5000)
            finally: router.close()
            router=DurableRouter(db,configured_slots())
            try:
                self.assertEqual(router.conn.execute("SELECT baseline_attempts FROM collection_budget_epochs WHERE account='brightdata:1'").fetchone()[0],5000)
                self.assertEqual(router.reserve("new-4").account,"brightdata:2")
            finally: router.close()

    def test_schema_block_not_silently_cleared(self):
        env={"BRIGHTDATA_API_KEY_1":"a","BRIGHTDATA_ZONE_1":"serp_api1","BRIGHTDATA_LIMIT_1":"2"}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,env,clear=True):
            db=Path(d)/"ledger.sqlite"
            router=DurableRouter(db,configured_slots())
            router.disable("brightdata:1","BRIGHTDATA_SCHEMA")
            router.close()
            router=DurableRouter(db,configured_slots())
            try:
                self.assertIsNone(router.reserve("new"))
                self.assertEqual(router.conn.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0],0)
            finally: router.close()

    def test_brightdata_empty_serp_is_not_schema_error(self):
        self.assertEqual(_normalize_brightdata_results({"general":{"query":"rare sku"}}),[])
        self.assertEqual(_normalize_brightdata_results({"body":{"organic":[]}}),[])
        with self.assertRaisesRegex(RuntimeError,"organic"):
            _normalize_brightdata_results({"status":"ok"})
        with self.assertRaisesRegex(RuntimeError,"organic"):
            _normalize_brightdata_results({"general":{},"error":"denied"})

    def test_json_policy(self):
        cfg={"collection":{"hard_limits":{"max_requests_per_product":1},
                           "providers":{"brightdata":{"format":"raw","data_format":"parsed_light"}}}}
        self.assertTrue(any("format deve ser json" in x for x in validate_collection_cost_policy(cfg)))
if __name__=="__main__": unittest.main()
