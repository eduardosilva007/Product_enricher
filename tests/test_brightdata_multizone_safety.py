"""Offline Bright Data multizone regression checks (no API/DB)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline_produtos import load_env_file,configured_slots,_provider_credentials,validate_collection_cost_policy

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
    def test_json_policy(self):
        cfg={"collection":{"hard_limits":{"max_requests_per_product":1},
                           "providers":{"brightdata":{"format":"raw","data_format":"parsed_light"}}}}
        self.assertTrue(any("format deve ser json" in x for x in validate_collection_cost_policy(cfg)))
if __name__=="__main__": unittest.main()
