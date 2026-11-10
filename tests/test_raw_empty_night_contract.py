"""Contract tests for capped 1,130 RAW_EMPTY pilot. No network or model inference."""
from __future__ import annotations
import sys,tempfile,types,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import audit_raw_empty_campaign as mod
import run_controlled_night_pilot as pilot

class RawEmptyNightTests(unittest.TestCase):
    def test_audit_separates_error_hardlocks_and_missing(self):
        rows=[SimpleNamespace(valid=True,key=f"k{i}",brand_original="CNH",
                              model_original=str(i)) for i in range(5)]
        states={
            "0":("RAW_EMPTY",{"_collection":{"state":"NO_TARGET_EVIDENCE"}}),
            "1":("RAW_EMPTY",{"_collection":{"state":"REQUEST_ERROR","error":"429"}}),
            "2":("RAW_EMPTY",{}),
            "3":("RAW_MISSING",{}),
            "4":("RAW_EMPTY",{})}
        class Store:
            def __init__(self,path):pass
            def get(self,brand,sku):
                state,payload=states[sku]
                return SimpleNamespace(state=state,payload=payload)
            def close(self):pass
        fake=types.ModuleType("pipeline_produtos")
        fake.read_input_rows=lambda p:rows
        fake.TavilyReadOnlyStore=Store
        fake.load_baseline_inventory=lambda m,f:{"hard_lock":{"k2"},"processed":{"k2","k4"}}
        fake.safe=lambda x:"" if x is None else str(x)
        with patch.dict(sys.modules,{"pipeline_produtos":fake}):
            rep=mod.audit(Path("input"),Path("cache"),Path("master"),Path("final"))
        self.assertEqual(rep["recollectable_without_prior_error"],1)
        self.assertEqual(rep["previous_search_errors_requiring_explicit_review"],1)
        self.assertEqual(rep["summary"]["raw_empty_hard_locked"],1)
        self.assertEqual(rep["summary"]["raw_empty_processed"],1)
        self.assertEqual(rep["summary"]["state_RAW_MISSING"],1)
        self.assertEqual(rep["candidate_rows"][0]["model"],"0")

    def test_live_requires_positive_bounded_budgets(self):
        cfg={"collection":{"hard_limits":{"max_requests_per_product":1}},
             "recovery":{"hard_lock_review_bi_value":"OK"},
             "identity":{"auto_approval_requires_strong_identity":True},
             "semantic_funnel":{"offline_mode":True}}
        for n in (0,-1,21,19000):
            args=SimpleNamespace(execute_live=True,search_budget=n,
                llm_budget=20,token_budget=20000,timeout_minutes=120)
            with self.assertRaises(ValueError):pilot.gate(cfg,args)

    def test_pilot_isolated_resume_and_no_mass_promotion(self):
        text=(ROOT/"run_controlled_night_pilot.py").read_text(encoding="utf-8")
        self.assertIn('"cohort_keys":[r["business_key"] for r in eligible]',text)
        self.assertIn('"production_approved":False',text)
        self.assertIn('prior_attempts.update(history.get("cohort_keys",[]))',text)
        self.assertIn('"--recollect-empty"',text)
        self.assertNotIn('"--recollect-low-evidence"',text)
        self.assertIn('"--max-search-new",str(state["budget"]["search"])',text)
        self.assertIn('state["budget"]!={"search":args.search_budget',text)
        self.assertIn('"--max-api-calls",str(state["budget"]["jev_calls"])',text)

if __name__=="__main__":unittest.main()
