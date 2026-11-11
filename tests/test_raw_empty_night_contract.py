"""Contract tests for capped 1,130 RAW_EMPTY pilot. No network or model inference."""
from __future__ import annotations
import json,sqlite3,sys,tempfile,types,unittest
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

    def test_cumulative_quota_from_prior_isolated_campaigns(self):
        from nightly_provider_router import DurableRouter,Slot
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); old=base/"history"/"run1"
            old.mkdir(parents=True)
            db=old/"prior.sqlite"
            with sqlite3.connect(db) as cx:
                cx.execute("CREATE TABLE collection_attempt_reservations "
                           "(business_key TEXT PRIMARY KEY,account TEXT,provider TEXT)")
                cx.executemany("INSERT INTO collection_attempt_reservations VALUES (?,?,?)",
                    [("cnh|one","tavily:1","tavily"),("cnh|two","tavily:1","tavily")])
            (old/"isolated_config.json").write_text(
                json.dumps({"files":{"classification_db":str(db)}}),encoding="utf-8")
            (old/"run_report.json").write_text(
                json.dumps({"cohort_keys":["cnh|one","cnh|two"],
                            "stages":{"collection":{"attempts":1,"status":"PASS"}}}),
                encoding="utf-8")
            next_db=base/"next.sqlite"
            result=pilot.import_prior_reservations(next_db,base/"history")
            self.assertEqual(result["historical_cohort_keys"],2)
            self.assertEqual(result["historical_reservation_keys"],2)
            router=DurableRouter(next_db,[
                Slot("tavily","tavily:1","TAVILY_API_KEY","",2,"tavily:1")])
            try:
                self.assertIsNone(router.reserve("cnh|three"))
                self.assertIsNone(router.reserve("cnh|one"))
            finally:router.close()

    def test_disabled_provider_account_survives_sublot_rotation(self):
        from nightly_provider_router import DurableRouter,Slot
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            old=base/"history"/"run1"
            old.mkdir(parents=True)
            db=old/"prior.sqlite"
            with sqlite3.connect(db) as cx:
                cx.execute("CREATE TABLE collection_attempt_reservations "
                           "(business_key TEXT PRIMARY KEY,account TEXT,provider TEXT)")
                cx.execute("INSERT INTO collection_attempt_reservations VALUES (?,?,?)",
                           ("cnh|old","tavily:1","tavily"))
                cx.execute("CREATE TABLE collection_disabled_accounts "
                           "(account TEXT PRIMARY KEY, reason TEXT)")
                cx.execute("INSERT INTO collection_disabled_accounts VALUES (?,?)",
                           ("tavily:1","PROVIDER_EXHAUSTED"))
            (old/"isolated_config.json").write_text(
                json.dumps({"files":{"classification_db":str(db)}}),encoding="utf-8")
            (old/"run_report.json").write_text(
                json.dumps({"cohort_keys":["cnh|old"],
                            "stages":{"collection":{"attempts":1}}}),encoding="utf-8")
            next_db=base/"next.sqlite"
            info=pilot.import_prior_reservations(next_db,base/"history")
            self.assertEqual(info["historical_disabled_accounts"],1)
            router=DurableRouter(next_db,[
                Slot("tavily","tavily:1","TAVILY_API_KEY","",5,"tavily:1"),
                Slot("brightdata","brightdata:1","BRIGHTDATA_API_KEY","BRIGHTDATA_ZONE",5,"brightdata:1"),
            ])
            try:
                self.assertEqual(router.reserve("cnh|new").account,"brightdata:1")
            finally:router.close()

    def test_missing_prior_attempt_blocks_campaign(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); old=base/"history"/"run1"
            old.mkdir(parents=True)
            db=old/"prior.sqlite"
            with sqlite3.connect(db) as cx:
                cx.execute("CREATE TABLE collection_attempt_reservations "
                           "(business_key TEXT PRIMARY KEY,account TEXT,provider TEXT)")
                cx.execute("INSERT INTO collection_attempt_reservations VALUES (?,?,?)",
                           ("cnh|one","tavily:1","tavily"))
            (old/"isolated_config.json").write_text(
                json.dumps({"files":{"classification_db":str(db)}}),encoding="utf-8")
            (old/"run_report.json").write_text(
                json.dumps({"cohort_keys":["cnh|one","cnh|two"],
                            "stages":{"collection":{"attempts":1}}}),encoding="utf-8")
            target=base/"new.sqlite"
            with self.assertRaisesRegex(RuntimeError,"Unaccounted previous"):
                pilot.import_prior_reservations(target,base/"history")
            self.assertFalse(target.exists())

    def test_master_campaign_50_products_batches_and_cumulative_reservations(self):
        import pipeline_produtos as master
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"controlled_night"
            args=SimpleNamespace(
                execute_live=True, campaign_total=50,
                campaign_jev_budget=60, campaign_token_budget=3000,
                campaign_resume_dir="",resume_dir="",search_budget=20,
                llm_budget=20,token_budget=1000,timeout_minutes=120,
                allow_prior_serper_quota_error=True)
            seen=[]
            def fake_one(local_args, cfg, on_run_created=None):
                n=len(seen)
                run=target/f"batch{n}"
                run.mkdir(parents=True)
                on_run_created(run)
                keys=[f"test|{i}" for i in range(n*20,n*20+local_args.search_budget)]
                (run/"run_report.json").write_text(
                    json.dumps({"budget":{"search":local_args.search_budget,
                                          "jev_calls":local_args.llm_budget,
                                          "tokens":local_args.token_budget},
                                "cohort_keys":keys,"cohort_count":len(keys)}), encoding="utf-8")
                (run/"quality_audit.json").write_text(
                    json.dumps({"status":"PASS","protected_changes_total":0,
                                "original_source_modified":[],
                                "useful_raw_count":len(keys)}),encoding="utf-8")
                seen.append((local_args.search_budget,local_args.llm_budget,
                             local_args.token_budget))
                return 0
            with patch.object(master,"OUT",target),patch.object(master,"gate"),\
                 patch.object(master,"overnight_run_one",side_effect=fake_one):
                rc=master.overnight_campaign(args,{})
            self.assertEqual(rc,0)
            self.assertEqual(seen,[(20,20,1000),(20,20,1000),(10,20,1000)])
            manifests=list((target/"campaigns").glob("*/campaign.json"))
            self.assertEqual(len(manifests),1)
            state=json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(state["status"],"COMPLETE")
            self.assertEqual(state["audited_products"],50)
            self.assertEqual(state["reserved_jev_calls"],60)
            self.assertEqual(state["reserved_tokens"],3000)
            self.assertEqual(len(state["batches"]),3)

    def test_master_campaign_resume_reuses_reserved_budget(self):
        import pipeline_produtos as master
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"controlled_night"
            args=SimpleNamespace(
                execute_live=True,campaign_total=20,campaign_jev_budget=40,
                campaign_token_budget=2000,campaign_resume_dir="",resume_dir="",
                search_budget=20,llm_budget=20,token_budget=1000,
                timeout_minutes=120,allow_prior_serper_quota_error=True)
            calls=[]
            def fake_one(local, cfg, on_run_created=None):
                calls.append(local.resume_dir)
                if not local.resume_dir:
                    run=target/"interrupted"
                    run.mkdir(parents=True)
                    on_run_created(run)
                    (run/"run_report.json").write_text(json.dumps({
                        "budget":{"search":20,"jev_calls":20,"tokens":1000},
                        "cohort_keys":[f"test|{i}" for i in range(20)],
                        "cohort_count":20}),encoding="utf-8")
                    return 2
                run=Path(local.resume_dir)
                (run/"quality_audit.json").write_text(json.dumps({
                    "status":"PASS","protected_changes_total":0,
                    "original_source_modified":[],"useful_raw_count":20
                }),encoding="utf-8")
                return 0
            with patch.object(master,"OUT",target),patch.object(master,"gate"),\
                 patch.object(master,"overnight_run_one",side_effect=fake_one):
                self.assertEqual(master.overnight_campaign(args,{}),2)
                path=next((target/"campaigns").glob("*/campaign.json"))
                before=json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(before["status"],"BLOCKED")
                self.assertEqual(before["reserved_jev_calls"],20)
                self.assertEqual(before["reserved_tokens"],1000)
                args.campaign_resume_dir=str(path.parent)
                self.assertEqual(master.overnight_campaign(args,{}),0)
            after=json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(after["status"],"COMPLETE")
            self.assertEqual(after["audited_products"],20)
            self.assertEqual(after["reserved_jev_calls"],20)
            self.assertEqual(after["reserved_tokens"],1000)
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[1],str(target/"interrupted"))

    def test_master_campaign_blocks_without_total_llm_budget(self):
        import pipeline_produtos as master
        from types import SimpleNamespace
        args=SimpleNamespace(
            execute_live=True,campaign_total=50,campaign_jev_budget=0,
            campaign_token_budget=0,campaign_resume_dir="",resume_dir="",
            search_budget=20,llm_budget=20,token_budget=1000,
            allow_prior_serper_quota_error=True)
        with self.assertRaisesRegex(ValueError,"cumulative"):
            master.overnight_campaign(args,{})

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
        import inspect
        text=inspect.getsource(pilot.create_run) + inspect.getsource(pilot.overnight_run_one)
        self.assertIn('"cohort_keys":[r["business_key"] for r in eligible]',text)
        self.assertIn('"production_approved":False',text)
        self.assertIn('prior_attempts.update(history.get("cohort_keys",[]))',text)
        self.assertIn('"--recollect-empty"',text)
        self.assertNotIn('"--recollect-low-evidence"',text)
        self.assertIn('"--max-search-new",str(state["budget"]["search"])',text)
        self.assertIn('state["budget"]!={"search":args.search_budget',text)
        self.assertIn('"--max-api-calls",str(state["budget"]["jev_calls"])',text)

if __name__=="__main__":unittest.main()
