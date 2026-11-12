"""Contract tests for capped 1,130 RAW_EMPTY pilot. No network or model inference."""
from __future__ import annotations
from contextlib import closing
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

    def test_master_offline_audit_real_entrypoint_function(self):
        import pipeline_produtos as master
        from unittest.mock import patch
        from types import SimpleNamespace
        rows=[
            SimpleNamespace(valid=True,key="brand|sku1",brand_original="Brand",model_original="SKU1"),
            SimpleNamespace(valid=True,key="brand|sku2",brand_original="Brand",model_original="SKU2"),
        ]
        class FakeStore:
            def __init__(self,path): pass
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def close(self): pass
            def get(self,brand,model):
                return SimpleNamespace(state="RAW_EMPTY",payload={})
        with patch.object(master,"read_input_rows",return_value=rows),\
             patch.object(master,"load_baseline_inventory",return_value={
                 "hard_lock":set(),"processed":set()}),\
             patch.object(master,"TavilyReadOnlyStore",FakeStore):
            result=master.overnight_audit(Path("input"),Path("cache"),
                                          Path("master"),Path("final"))
        self.assertEqual(result["recollectable_without_prior_error"],2)
        self.assertEqual(result["summary"]["state_RAW_EMPTY"],2)
        self.assertEqual(len(result["candidate_rows"]),2)
        self.assertEqual(result["search_calls"],0)

    def test_master_forensic_diff_on_real_excel_lock(self):
        import pipeline_produtos as master
        import pandas as pd
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            old=base/"original.xlsx"
            new=base/"isolated.xlsx"
            frame=pd.DataFrame([{"Marca":"CNH","Modelo":"123456",
                                "Status":"Manual","Revisão B.I":"OK",
                                "Category General":"OTHERS","FAMILY":"PART"}])
            frame.to_excel(old,index=False)
            frame.to_excel(new,index=False)
            (base/"original_sources.json").write_text(json.dumps({
                "final":{"path":str(old)}}),encoding="utf-8")
            (base/"isolated_config.json").write_text(json.dumps({
                "files":{"final":str(new)}}),encoding="utf-8")
            changes,counts,old_n,new_n=master.overnight_examine(base)
            self.assertEqual(changes,[])
            self.assertEqual((old_n,new_n),(1,1))
            changed=frame.copy()
            changed.loc[0,"FAMILY"]="FAN"
            changed.to_excel(new,index=False)
            changes,counts,_,_=master.overnight_examine(base)
            self.assertEqual(len(changes),1)
            self.assertEqual(changes[0]["field"],"FAMILY")
            self.assertEqual(changes[0]["severity"],"CRITICAL")

    def test_cumulative_quota_from_prior_isolated_campaigns(self):
        from nightly_provider_router import DurableRouter,Slot
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); old=base/"history"/"run1"
            old.mkdir(parents=True)
            db=old/"prior.sqlite"
            with closing(sqlite3.connect(db)) as cx, cx:
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
            with closing(sqlite3.connect(db)) as cx, cx:
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
            with closing(sqlite3.connect(db)) as cx, cx:
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

    def test_master_promote_complete_campaign_to_canonical_cache_and_final(self):
        import pipeline_produtos as master
        import pandas as pd
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            controlled=root/"controlled_night"
            run=controlled/"run1"
            campaign=controlled/"campaigns"/"c1"
            run.mkdir(parents=True);campaign.mkdir(parents=True)
            official_cache=root/"official_cache.sqlite"
            pilot_cache=run/"cache.sqlite"
            official_db=root/"official_classification.sqlite"
            pilot_db=run/"source_classification.sqlite"
            for database in (official_cache,pilot_cache,official_db):
                with closing(sqlite3.connect(database)) as cx, cx:
                    cx.execute("CREATE TABLE IF NOT EXISTS tavily_cache "
                               "(cache_key TEXT PRIMARY KEY,brand TEXT,model TEXT,"
                               "response_json TEXT,created_at TEXT)")
            with closing(sqlite3.connect(pilot_cache)) as cx, cx:
                cx.execute("INSERT INTO tavily_cache VALUES (?,?,?,?,?)",(
                    "v3|cnh|123456","CNH","123456",
                    json.dumps({"results":[{"title":"CNH Product", "url":"https://example.com/p",
                                           "content":"CNH product"}],
                                "_collection":{"state":"READY_FOR_CLASSIFICATION"}}),
                    "2026-11-11T10:00:00"))
            cls_db=master.ClassificationDB(pilot_db)
            cls_db.conn.execute(
                "INSERT INTO classification_results "
                "(checkpoint_id,business_key,brand,model,raw_hash,rules_hash,"
                "pipeline_version,result_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("check-123456","cnh|123456","CNH","123456","raw","rules",
                 "v4",json.dumps({"Resolution State":"CLASSIFIED_CLEF",
                                  "Category General":"OTHERS","FAMILY":"ENGINE"}),
                 "2026-11-11T10:00:00"))
            cls_db.conn.execute("CREATE TABLE collection_attempt_reservations "
                                "(business_key TEXT PRIMARY KEY,account TEXT,provider TEXT)")
            cls_db.conn.execute("INSERT INTO collection_attempt_reservations VALUES (?,?,?)",
                                ("cnh|123456","tavily:1","tavily"))
            cls_db.close()
            original=pd.DataFrame([{"Marca":"CNH","Modelo":"123456",
                                    "Category General":"","FAMILY":"",
                                    "Revisão B.I":""}])
            locked=pd.DataFrame([{"Marca":"CNH","Modelo":"LOCKED",
                                   "O que é (Tipo)":"TRACTOR",
                                   "Category General":"TRACTORS","FAMILY":"TRACTORS",
                                   "Status":"Manual","Revisão B.I":"OK"}])
            historical=pd.DataFrame([
                {"Marca":"ANNOVI REVERBERI","Modelo":"52180",
                 "O que é (Tipo)":"PUMP","Category General":"OTHERS",
                 "FAMILY":"PUMP","Status":"Script",
                 "Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
                {"Marca":"Annovi-Reverberi","Modelo":"52180",
                 "O que é (Tipo)":"PUMP","Category General":"OTHERS",
                 "FAMILY":"PUMP","Status":"Manual",
                 "Revisão":"MANUAL","Revisão B.I":"OK"},
                {"Marca":"BOSCH","Modelo":"GWS2200180",
                 "O que é (Tipo)":"ANGLE GRINDER","Category General":"OTHERS",
                 "FAMILY":"ANGLE GRINDER","Status":"Script",
                 "Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
                {"Marca":"Bosch","Modelo":"GWS 2200 180",
                 "O que é (Tipo)":"ANGLE GRINDER","Category General":"OTHERS",
                 "FAMILY":"ANGLE GRINDER","Status":"Manual",
                 "Revisão":"VALIDADO_MANUAL","Revisão B.I":"OK"}
            ])
            official=pd.concat([original,locked,historical],ignore_index=True)
            final=root/"Final.xlsx";consolidated=root/"Master.xlsx"
            inp=root/"Input.xlsx";result_file=run/"classification_output.xlsx"
            official.to_excel(final,index=False)
            official.to_excel(consolidated,index=False)
            original[["Marca","Modelo"]].to_excel(inp,index=False)
            pd.DataFrame([{
                "Marca":"CNH","Modelo":"123456",
                "O que é (Tipo)":"ENGINE",
                "Alimentação":"PETROL","Category General":"OTHERS","FAMILY":"ENGINE",
                "Título Real":"CNH Product","Fonte URL":"https://example.com/p",
                "Resolution State":"CLASSIFIED_CLEF",
                "Classification Outcome":"CLEF_CLASSIFIED",
                "Review Required":"SIM", "Auto Eligible":"NÃO",
                "Classification Identity":"WEAK",
            }]).to_excel(result_file,sheet_name="Resultados",index=False)
            (run/"raw_empty_cohort.xlsx").write_bytes(inp.read_bytes())
            config={"files":{"input":str(run/"raw_empty_cohort.xlsx"),
                              "cache":str(pilot_cache),"output":str(result_file),
                              "classification_db":str(pilot_db)}}
            (run/"isolated_config.json").write_text(json.dumps(config),encoding="utf-8")
            (run/"run_report.json").write_text(json.dumps({
                "cohort_keys":["cnh|123456"],"cohort_count":1,
                "stages":{"collection":{"status":"PASS"},
                          "classification":{"status":"PASS"}}}),encoding="utf-8")
            (run/"quality_audit.json").write_text(json.dumps({
                "status":"PASS","protected_changes_total":0,
                "original_source_modified":[]}),encoding="utf-8")
            (campaign/"campaign.json").write_text(json.dumps({
                "status":"COMPLETE","audited_products":1,
                "active_run":"","pending_run_creation":False,
                "batches":[{"run_dir":str(run),"cohort_keys":["cnh|123456"],
                            "cohort_count":1,"raw_ok":1,"status":"AUDIT_PASS"}]
            }),encoding="utf-8")
            cfg={"files":{"input":str(inp),"cache":str(official_cache),
                          "master":str(consolidated),"final":str(final),
                          "classification_db":str(official_db)}}
            rules=SimpleNamespace(version="test",hash="abc")
            # Simulate a prior pre-commit failure: immutable backups already
            # exist and staged files may be half-written. Retrying is safe
            # only when the official originals still match their hashes.
            backup_dir=campaign/"promotion_backup"
            backup_dir.mkdir()
            originals={}
            for name,source in (
                ("cache",official_cache),("master",consolidated),
                ("final",final),("classification_db",official_db)):
                b=backup_dir/(name+source.suffix)
                if source.suffix==".sqlite":
                    master.snapshot(source,b)
                else:
                    import shutil
                    shutil.copy2(source,b)
                originals[name]={"path":str(source),"sha256":master.digest(source),
                                 "backup":str(b)}
                (backup_dir/("staged_"+name+source.suffix)).write_bytes(b"old partial")
            (backup_dir/"source_manifest.json").write_text(
                json.dumps(originals),encoding="utf-8")
            canonical_hashes={name:master.digest(path) for name,path in (
                ("cache",official_cache),("master",consolidated),
                ("final",final),("classification_db",official_db))}
            with patch.object(master,"OUT",controlled),patch.object(master,"ROOT",root):
                dry=master.overnight_promote_campaign(campaign,cfg,rules,dry_run=True)
            self.assertEqual(dry["status"],"DRY_RUN_PASS")
            self.assertTrue(dry["canonical_files_unchanged"])
            self.assertFalse(json.loads((campaign/"campaign.json").read_text(
                encoding="utf-8")).get("promotion"))
            for name,path in (("cache",official_cache),("master",consolidated),
                              ("final",final),("classification_db",official_db)):
                self.assertEqual(master.digest(path),canonical_hashes[name])
            with patch.object(master,"OUT",controlled),patch.object(master,"ROOT",root):
                promoted=master.overnight_promote_campaign(campaign,cfg,rules)
            self.assertEqual(promoted["status"],"COMMITTED")
            self.assertEqual(promoted["cohort_count"],1)
            result=pd.read_excel(final,dtype=object,keep_default_na=False)
            idx=result.set_index("Modelo")
            self.assertEqual(idx.loc["123456","FAMILY"],"ENGINE")
            self.assertEqual(idx.loc["123456","Category General"],"OTHERS")
            self.assertEqual(idx.loc["LOCKED","Revisão B.I"],"OK")
            self.assertEqual(idx.loc["LOCKED","FAMILY"],"TRACTORS")
            self.assertEqual(result.loc[
                result["Marca"]=="ANNOVI REVERBERI","Revisão"].iloc[0],
                "AUTO_APROVADO")
            self.assertEqual(result.loc[
                result["Marca"]=="Annovi-Reverberi","Revisão"].iloc[0],
                "MANUAL")
            self.assertEqual(result.loc[
                result["Marca"]=="Bosch","Revisão"].iloc[0],
                "VALIDADO_MANUAL")
            store=master.TavilyReadOnlyStore(official_cache)
            try:
                self.assertEqual(store.get("CNH","123456").state,"RAW_OK")
            finally:
                store.close()
            with closing(sqlite3.connect(official_db)) as conn, conn:
                migrated=conn.execute(
                    "SELECT result_json FROM classification_results WHERE checkpoint_id=?",
                    ("check-123456",)).fetchone()
                reservation=conn.execute(
                    "SELECT provider FROM collection_attempt_reservations WHERE business_key=?",
                    ("cnh|123456",)).fetchone()
                self.assertIsNotNone(migrated)
                self.assertEqual(reservation[0],"tavily")
            with patch.object(master,"OUT",controlled),patch.object(master,"ROOT",root):
                again=master.overnight_promote_campaign(campaign,cfg,rules)
            self.assertEqual(again["status"],"COMMITTED")

    def test_master_cli_supports_overnight_campaign_and_explicit_total_budget(self):
        import pipeline_produtos as master
        args=master.build_parser().parse_args([
            "--mode","overnight","--execute-live",
            "--campaign-total","250","--search-budget","20",
            "--campaign-jev-budget","750","--campaign-token-budget","1500000",
        ])
        self.assertEqual(args.mode,"overnight")
        self.assertTrue(args.execute_live)
        self.assertEqual(args.campaign_total,250)
        self.assertEqual(args.search_budget,20)
        self.assertEqual(args.campaign_jev_budget,750)
        self.assertEqual(args.campaign_token_budget,1500000)

    def test_weak_resolved_identity_never_enters_accepted_final(self):
        import pandas as pd
        import pipeline_produtos as master
        base=pd.DataFrame([
            {"Marca":"DCA","Modelo":"AMQ02 65",
             "Resolution State":"RESOLVED","Classification Identity":"WEAK",
             "O que é (Tipo)":"DRILL","Category General":"OTHERS",
             "FAMILY":"COMBO KIT","Revisão":"AUTO_APROVADO",
             "Status":"Script","Revisão B.I":""},
            {"Marca":"DCA","Modelo":"AJZ03 16A",
             "Resolution State":"RESOLVED","Classification Identity":"STRONG",
             "O que é (Tipo)":"DRILL","Category General":"OTHERS",
             "FAMILY":"POWER TOOLS","Revisão":"AUTO_APROVADO",
             "Status":"Script","Revisão B.I":""},
        ])
        self.assertFalse(master._baseline_row_accepted(base.iloc[0],base))
        self.assertTrue(master._baseline_row_accepted(base.iloc[1],base))
        self.assertFalse(master._record_is_accepted(base.iloc[0].to_dict()))
        self.assertTrue(master._record_is_accepted(base.iloc[1].to_dict()))
        base.loc[0,"Revisão B.I"]="OK"
        self.assertTrue(master._baseline_row_accepted(base.iloc[0],base))
        base.loc[0,"Revisão B.I"]=""
        base.loc[0,"Status"]="Manual"
        self.assertTrue(master._baseline_row_accepted(base.iloc[0],base))

    def test_failed_classification_salvage_reuses_all_10_results_without_http(self):
        import pandas as pd
        import pipeline_produtos as master
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/"controlled_night"
            run=root/"failed"
            campaign=root/"campaigns"/"c10"
            run.mkdir(parents=True)
            campaign.mkdir(parents=True)
            keys=["dca|sku%d"%i for i in range(10)]
            input_rows=[{"Marca":"DCA","Modelo":"SKU%d"%i} for i in range(10)]
            output=[]
            final_rows=[]
            for i, pair in enumerate(input_rows):
                strong=i!=6
                approved=i<7
                value={"Marca":pair["Marca"],"Modelo":pair["Modelo"],
                       "Resolution State":"RESOLVED" if approved else "REVIEW_NO_RESULTS",
                       "Classification Identity":"STRONG" if strong else "WEAK",
                       "O que é (Tipo)":"DRILL","Category General":"OTHERS",
                       "FAMILY":"POWER TOOLS","Review Required":"NÃO"}
                output.append(value)
                final_rows.append({**value,"Status":"Script",
                                   "Revisão":"AUTO_APROVADO"})
            files={}
            for name in ("input","output","final"):
                files[name]=str(run/(name+".xlsx"))
            pd.DataFrame(input_rows).to_excel(files["input"],index=False)
            pd.DataFrame(output).to_excel(files["output"],
                                         sheet_name="Resultados",index=False)
            pd.DataFrame(final_rows).to_excel(files["final"],index=False)
            prior=run/"prior.xlsx"
            pd.DataFrame(input_rows).to_excel(prior,index=False)
            (run/"original_sources.json").write_text(
                json.dumps({"final":{"path":str(prior)}}),encoding="utf-8")
            (run/"isolated_config.json").write_text(
                json.dumps({"files":files}),encoding="utf-8")
            (run/"classification_01.log").write_text(
                "PROGRESSO 10/10 | BASELINE EXCEL ATUALIZADA | "
                "BASELINE_COHORT_MISMATCH | classification",encoding="utf-8")
            report={"stages":{
                 "collection":{"status":"PASS","attempts":1},
                 "classification":{"status":"FAIL","attempts":1,
                                   "exit_code":1,"log":"classification_01.log"}},
                "originals":str(run/"original_sources.json"),
                "cohort_keys":keys,"cohort_count":10}
            (run/"run_report.json").write_text(
                json.dumps(report),encoding="utf-8")
            manifest={"status":"BLOCKED","batches":[],"audited_products":0,
                      "active_run":str(run),"pending_run_creation":False,
                      "limits":{"target":10},"reason":"failed classification"}
            (campaign/"campaign.json").write_text(
                json.dumps(manifest),encoding="utf-8")
            def fake_audit(run,state,isolated):
                (run/"quality_audit.json").write_text(json.dumps({
                    "status":"PASS","protected_changes_total":0,
                    "useful_raw_count":10,"original_source_modified":[]
                }),encoding="utf-8")
                return True
            with patch.object(master,"OUT",root),\
                 patch.object(master,"protected_originals",return_value=[]),\
                 patch.object(master,"outcome_audit",side_effect=fake_audit),\
                 patch.object(master,"overnight_promote_campaign",
                              return_value={"status":"DRY_RUN_PASS"}) as promote:
                recovered=master.overnight_reconcile_failed_campaign(
                    campaign,{},SimpleNamespace(version="x",hash="h"))
            self.assertEqual(recovered["status"],"RECOVERED_OFFLINE_DRY_RUN_PASS")
            self.assertEqual(recovered["accepted_strict"],6)
            self.assertEqual(recovered["classification_rows"],10)
            promote.assert_called_once()
            self.assertTrue(promote.call_args.kwargs["dry_run"])
            state=json.loads((campaign/"campaign.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"],"COMPLETE")
            self.assertEqual(state["audited_products"],10)
            self.assertEqual(len(state["batches"]),1)
            self.assertEqual(json.loads((run/"run_report.json").read_text(
                encoding="utf-8"))["stages"]["classification"]["recovery"][
                "original_exit_code"],1)

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
                with patch.object(master,"overnight_promote_campaign",return_value={"status":"COMMITTED"}) as promote:
                    rc=master.overnight_campaign(args,{},rules=SimpleNamespace(version="x",hash="h"))
                    promote.assert_called_once()
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

    def test_master_pause_after_first_audited_five_then_resume_five(self):
        import pipeline_produtos as master
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"controlled_night"
            args=SimpleNamespace(
                execute_live=True,campaign_total=10,campaign_jev_budget=30,
                campaign_token_budget=60000,campaign_resume_dir="",
                campaign_pause_after_sublots=1,resume_dir="",
                search_budget=5,llm_budget=15,token_budget=30000,
                timeout_minutes=120,allow_prior_serper_quota_error=True)
            processed=[]
            def fake_one(local_args, cfg, on_run_created=None):
                n=len(processed)
                run=target/("batch%d"%n)
                run.mkdir(parents=True)
                on_run_created(run)
                keys=["test|%d"%i for i in range(n*5,(n+1)*5)]
                (run/"run_report.json").write_text(json.dumps({
                    "budget":{"search":5,"jev_calls":15,"tokens":30000},
                    "cohort_keys":keys,"cohort_count":5}),encoding="utf-8")
                (run/"quality_audit.json").write_text(json.dumps({
                    "status":"PASS","protected_changes_total":0,
                    "original_source_modified":[],"useful_raw_count":5}),encoding="utf-8")
                processed.append((keys,local_args.resume_dir))
                return 0
            with patch.object(master,"OUT",target),patch.object(master,"gate"),\
                 patch.object(master,"overnight_run_one",side_effect=fake_one),\
                 patch.object(master,"overnight_promote_campaign",
                              return_value={"status":"COMMITTED"}) as promote:
                self.assertEqual(master.overnight_campaign(args,{},rules=SimpleNamespace(version="x",hash="h")),0)
                manifest=next((target/"campaigns").glob("*/campaign.json"))
                partial=json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(partial["status"],"PAUSED_FOR_RESUME_TEST")
                self.assertEqual(partial["audited_products"],5)
                self.assertEqual(partial["reserved_jev_calls"],15)
                self.assertEqual(partial["reserved_tokens"],30000)
                self.assertEqual(len(partial["batches"]),1)
                promote.assert_not_called()
                args.campaign_resume_dir=str(manifest.parent)
                args.campaign_pause_after_sublots=0
                self.assertEqual(master.overnight_campaign(args,{},rules=SimpleNamespace(version="x",hash="h")),0)
                final=json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(final["status"],"COMPLETE")
                self.assertEqual(final["audited_products"],10)
                self.assertEqual(final["reserved_jev_calls"],30)
                self.assertEqual(final["reserved_tokens"],60000)
                self.assertEqual(len(final["batches"]),2)
                self.assertEqual(len({k for batch in final["batches"]
                                      for k in batch["cohort_keys"]}),10)
                self.assertTrue(all(not resumed for _,resumed in processed))
                self.assertEqual(len(processed),2)
                promote.assert_called_once()

    def test_master_resumption_after_jev_started_never_retries_http(self):
        import pipeline_produtos as master
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/"controlled_night"
            run=root/"interrupted_jev"
            run.mkdir(parents=True)
            args=SimpleNamespace(
                execute_live=True,resume_dir=str(run),
                search_budget=5,llm_budget=15,token_budget=30000,
                timeout_minutes=120,allow_prior_serper_quota_error=True)
            (run/"run_report.json").write_text(json.dumps({
                "budget":{"search":5,"jev_calls":15,"tokens":30000},
                "cohort_count":5,
                "prior_serper_quota_error_rescue_authorized":True,
                "originals":str(run/"original_sources.json"),
                "stages":{"collection":{"status":"PASS","attempts":1},
                          "classification":{"status":"FAIL","attempts":1}}
            }),encoding="utf-8")
            (run/"isolated_config.json").write_text(
                json.dumps({"files":{
                    "input":str(run/"cohort.xlsx"),
                    "cache":str(run/"cache.sqlite"),
                    "master":str(run/"master.xlsx"),
                    "final":str(run/"final.xlsx"),
                    "classification_db":str(run/"classification.sqlite"),
                    "output":str(run/"out.xlsx"),
                    "inventory_output":str(run/"inventory.xlsx")
                }}),encoding="utf-8")
            (run/"original_sources.json").write_text("{}",encoding="utf-8")
            with patch.object(master,"OUT",root),patch.object(master,"gate"),\
                 patch.object(master,"protected_originals",return_value=[]),\
                 patch.object(master,"run_stage") as run_stage:
                with self.assertRaisesRegex(
                        RuntimeError,"JEV_INCOMPLETE_REQUIRES_AUDIT"):
                    master.overnight_run_one(args,{})
                run_stage.assert_not_called()

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
            # Windows tempfile can emit RUNNER~1 while Path.resolve() uses
            # runneradmin for the SAME directory. Test filesystem identity,
            # not a different textual spelling of an identical location.
            self.assertTrue(
                Path(calls[1]).samefile(target/"interrupted"),
                f"Resumed a different run: {calls[1]} vs {target/'interrupted'}",
            )

    def test_master_resume_blocks_ambiguous_pre_checkpoint_budget(self):
        import pipeline_produtos as master
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/"controlled_night"
            args=SimpleNamespace(
                execute_live=True,campaign_total=20,campaign_jev_budget=40,
                campaign_token_budget=2000,campaign_resume_dir="",resume_dir="",
                search_budget=20,llm_budget=20,token_budget=1000,
                timeout_minutes=120,allow_prior_serper_quota_error=True)
            with patch.object(master,"OUT",root),patch.object(master,"gate"),\
                 patch.object(master,"overnight_run_one",
                              side_effect=RuntimeError("crash before checkpoint")) as call:
                with self.assertRaisesRegex(RuntimeError,"crash before checkpoint"):
                    master.overnight_campaign(args,{})
                path=next((root/"campaigns").glob("*/campaign.json"))
                state=json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(state["pending_run_creation"])
                self.assertEqual(state["reserved_jev_calls"],20)
                self.assertEqual(state["reserved_tokens"],1000)
                args.campaign_resume_dir=str(path.parent)
                with self.assertRaisesRegex(RuntimeError,"UNCERTAIN_SUBLOT_START"):
                    master.overnight_campaign(args,{})
                self.assertEqual(call.call_count,1)

    def test_promotion_scoped_overlay_preserves_unrelated_duplicate_keys(self):
        import pipeline_produtos as master
        import pandas as pd
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=root/"canonical.xlsx"
            generated=root/"staged.xlsx"
            pd.DataFrame([
                {"Marca":"ANNOVI REVERBERI","Modelo":"52180","FAMILY":"OLD1","Status":"Script"},
                {"Marca":"Annovi-Reverberi","Modelo":"52180","FAMILY":"OLD2","Status":"Script"},
                {"Marca":"CNH","Modelo":"123456","FAMILY":"","Status":"Script"}
            ]).to_excel(original,index=False)
            pd.DataFrame([
                {"Marca":"CNH","Modelo":"123456","FAMILY":"ENGINE","Status":"Script"},
                {"Marca":"ANNOVI REVERBERI","Modelo":"52180","FAMILY":"WRONG","Status":"Script"}
            ]).to_excel(generated,index=False)
            stats=master._overnight_scoped_excel_overlay(
                original,generated,{"cnh|123456":("CNH","123456")})
            actual=pd.read_excel(generated,dtype=object,keep_default_na=False)
            self.assertEqual(len(actual),3)
            self.assertEqual(actual["FAMILY"].tolist(),["OLD1","OLD2","ENGINE"])
            self.assertEqual(stats["updated"],1)
            self.assertEqual(stats["original_rows_preserved"],3)

    def test_promotion_human_lock_does_not_confuse_normalized_variants(self):
        import pipeline_produtos as master
        import pandas as pd
        original=pd.DataFrame([
            {"Marca":"ANNOVI REVERBERI","Modelo":"52180",
             "FAMILY":"PUMP","Status":"Script",
             "Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
            {"Marca":"Annovi-Reverberi","Modelo":"52180",
             "FAMILY":"PUMP","Status":"Manual",
             "Revisão":"MANUAL","Revisão B.I":"OK"},
            {"Marca":"BOSCH","Modelo":"GWS2200180",
             "FAMILY":"ANGLE GRINDER","Status":"Script",
             "Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
            {"Marca":"BOSCH","Modelo":"GWS 2200 180",
             "FAMILY":"ANGLE GRINDER","Status":"Manual",
             "Revisão":"VALIDADO_MANUAL","Revisão B.I":"OK"},
            {"Marca":"CNH","Modelo":"123456",
             "FAMILY":"","Status":"Script",
             "Revisão":"","Revisão B.I":""},
        ])
        staged=original.copy()
        staged.loc[4,"FAMILY"]="ENGINE"
        result=master._overnight_assert_exact_human_locks(original,staged)
        self.assertEqual(result["protected_original_rows_verified"],4)
        # Genuine change to a human-reviewed cell still fails.
        staged.loc[1,"Revisão"]="AUTO_APROVADO"
        with self.assertRaisesRegex(RuntimeError,"PROMOTION_PROTECTED_FIELD_CHANGED"):
            master._overnight_assert_exact_human_locks(original,staged)
        staged=original.copy()
        staged.loc[1,"Modelo"]="OTHER"
        with self.assertRaisesRegex(RuntimeError,"PROMOTION_PROTECTED_PAIR_CHANGED"):
            master._overnight_assert_exact_human_locks(original,staged)
        # Repeated exact approved rows are verified independently.
        duplicates=pd.concat([original,original.iloc[[0]]],ignore_index=True)
        verified=master._overnight_assert_exact_human_locks(duplicates,duplicates.copy())
        self.assertEqual(verified["protected_original_rows_verified"],5)

    def test_overlay_uses_audited_exact_comet_variant_not_normalized_rank(self):
        import pipeline_produtos as master
        import pandas as pd
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=root/"canonical.xlsx"
            generated=root/"staged.xlsx"
            frame=pd.DataFrame([
                {"Marca":"COMET","Modelo":"K250TSR13190T",
                 "FAMILY":"OLD_TARGET","Status":"Script"},
                {"Marca":"COMET","Modelo":"K250 TSR 13190T",
                 "FAMILY":"OTHER_VARIANT","Status":"Script"},
                {"Marca":"COMET","Modelo":"K250TSR13190T",
                 "FAMILY":"MANUAL_VARIANT","Status":"Manual"},
            ])
            # A repeated exact pair is genuinely ambiguous and must block.
            frame.iloc[:2].to_excel(original,index=False)
            pd.DataFrame([
                {"Marca":"COMET","Modelo":"K250TSR13190T",
                 "FAMILY":"POWER WASHER","Status":"Script"},
                {"Marca":"COMET","Modelo":"K250 TSR 13190T",
                 "FAMILY":"WRONG","Status":"Script"}
            ]).to_excel(generated,index=False)
            exact={"comet|k250tsr13190t":("COMET","K250TSR13190T")}
            stats=master._overnight_scoped_excel_overlay(original,generated,exact)
            result=pd.read_excel(generated,dtype=object,keep_default_na=False)
            self.assertEqual(result["FAMILY"].tolist(),["POWER WASHER","OTHER_VARIANT"])
            self.assertEqual(stats["updated"],1)
            frame.to_excel(original,index=False)
            with self.assertRaisesRegex(RuntimeError,"PROMOTION_DUPLICATE_EXACT_COHORT_PAIR"):
                master._overnight_scoped_excel_overlay(original,generated,exact)

    def test_legacy_two_twenty_sku_pilots_register_offline_with_verified_manifest(self):
        import pipeline_produtos as master
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/"controlled_night"
            out.mkdir()
            runs=[]
            for n in (1,2):
                run=out/("legacy"+str(n))
                run.mkdir()
                keys=["cnh|%04d"%i for i in range((n-1)*20,n*20)]
                (run/"run_report.json").write_text(json.dumps({
                    "cohort_keys":keys,"cohort_count":20,
                    "stages":{"collection":{"status":"PASS"},
                              "classification":{"status":"PASS"}}}),encoding="utf-8")
                (run/"quality_audit.json").write_text(json.dumps({
                    "status":"PASS","useful_raw_count":20,
                    "protected_changes_total":0,
                    "original_source_modified":[]}),encoding="utf-8")
                files={}
                for name in ("cache","input","output","classification_db"):
                    p=run/(name+".txt")
                    p.write_text("fixture",encoding="utf-8")
                    files[name]=str(p)
                (run/"isolated_config.json").write_text(
                    json.dumps({"files":files}),encoding="utf-8")
                runs.append(str(run))
            with patch.object(master,"OUT",out):
                campaign=master.overnight_register_legacy_runs(runs)
                report=json.loads((campaign/"campaign.json").read_text(encoding="utf-8"))
                self.assertEqual(report["audited_products"],40)
                self.assertEqual(len(report["batches"]),2)
                self.assertEqual(report["status"],"COMPLETE")
                self.assertEqual(master.overnight_register_legacy_runs(runs),campaign)
                bad=out/"legacy2"/"quality_audit.json"
                quality=json.loads(bad.read_text(encoding="utf-8"))
                quality["status"]="BLOCKED"
                bad.write_text(json.dumps(quality),encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError,"LEGACY_RUN_NOT_AUDITABLE"):
                    master.overnight_register_legacy_runs(runs)

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
