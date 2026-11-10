"""Integration tests for the production collection loop; no HTTP, no Excel writes."""
import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import os
import importlib.util
if os.getenv("ARGENTINA_PILOT_SCRIPT"):
    spec = importlib.util.spec_from_file_location("argentina_pilot_pipeline", os.environ["ARGENTINA_PILOT_SCRIPT"])
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
else:
    import pipeline_produtos as p
from nightly_provider_router import Slot

class CollectionAutoProviderTests(unittest.TestCase):
    def test_http_429_advances_provider_on_next_sku_no_retry(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.sqlite"
            slots = [
                Slot("tavily","tavily:1","TAVILY_API_KEY","",1,"t"),
                Slot("serper","serper:1","SERPER_API_KEY","",2,"s"),
            ]
            plan = SimpleNamespace(
                candidates=[
                    SimpleNamespace(brand_original="ABC",model_original="111",raw_state="RAW_MISSING"),
                    SimpleNamespace(brand_original="ABC",model_original="222",raw_state="RAW_MISSING"),
                ],
                summary={},
            )
            args = argparse.Namespace(
                search_provider="tavily", auto_providers=True,
                input="input.xlsx",cache=str(Path(d)/"cache.sqlite"),
                db=str(db),master="",final="",precollection_output="",
                recollect_empty=False,recollect_low_evidence=False,
                max_tavily_new=20,yes=True,
            )
            cfg = {"files":{"input":"input.xlsx","cache":str(Path(d)/"cache.sqlite"),
                            "classification_db":str(db),"collection_report":str(Path(d)/"report.xlsx")}}
            calls = []
            def mock_search(brand,model,cfg,limiter,provider):
                calls.append((model,provider))
                if model == "111":
                    raise p.OperationalPause("TAVILY_LIMIT","mock 429")
                return {"results":[{"title":"test"}],
                        "_collection":{"state":"COLLECTED","curated_results":[],"source_audit":[],
                                       "query_audit":[],"usage":{"requests":1}}}
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",return_value=plan), \
                 patch.object(p,"configured_slots",return_value=slots), \
                 patch.object(p,"search_product_once",side_effect=mock_search), \
                 patch.object(p,"TavilyWritableStore") as writer, \
                 patch.object(p,"_export_collection_quality_report"):
                self.assertTrue(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
                self.assertEqual(calls,[("111","tavily"),("222","serper")])
                self.assertTrue(writer.return_value.put.called)
            # No repeat on restart, even when the first attempt returned HTTP 429.
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",return_value=plan), \
                 patch.object(p,"configured_slots",return_value=slots), \
                 patch.object(p,"search_product_once",side_effect=AssertionError("No HTTP allowed")), \
                 patch.object(p,"TavilyWritableStore"), \
                 patch.object(p,"_export_collection_quality_report"):
                self.assertTrue(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
            self.assertNotIn("_active_search_slot",cfg)

    def test_no_explicit_budget_does_not_start_collection(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                search_provider="tavily",auto_providers=True,
                input="test.xlsx",cache=str(Path(d)/"cache.sqlite"),
                db=str(Path(d)/"db.sqlite"),master="",final="",precollection_output="",
                recollect_empty=False,recollect_low_evidence=False,
                max_tavily_new=20,yes=True)
            plan = SimpleNamespace(
                candidates=[SimpleNamespace(brand_original="ABC",model_original="111",raw_state="RAW_MISSING")],
                summary={})
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",return_value=plan), \
                 patch.object(p,"configured_slots",return_value=[]):
                with self.assertRaisesRegex(RuntimeError,"AUTO_PROVIDERS"):
                    p.collect_products(args,{"files":{"cache":args.cache,"classification_db":args.db}},SimpleNamespace(hash="test"))

if __name__=="__main__":
    unittest.main()
