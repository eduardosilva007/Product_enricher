"""Integration tests for the production collection loop; no HTTP, no Excel writes."""
import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import os
import sys
import importlib.util
if os.getenv("ARGENTINA_PILOT_SCRIPT"):
    spec = importlib.util.spec_from_file_location("argentina_pilot_pipeline", os.environ["ARGENTINA_PILOT_SCRIPT"])
    p = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = p
    spec.loader.exec_module(p)
else:
    import pipeline_produtos as p
from nightly_provider_router import Slot, DurableRouter

class ProviderResponseContractTests(unittest.TestCase):
    def test_precollection_skips_attempted_before_limit(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "input.csv"
            src.write_text("Marca,Modelo\nCNH,111\nCNH,222\nCNH,333\n", encoding="utf-8")
            plan = p.build_precollection_plan(
                input_path=src, cache_path=Path(d)/"none.sqlite",
                report_path=Path(d)/"report.xlsx",max_tavily_new=2,
                exclude_attempted_keys={p.business_key("CNH","111")},
            )
            self.assertEqual([x.model_original for x in plan.candidates],["222","333"])
            self.assertEqual(plan.summary["Tentativas Search anteriores preservadas"],1)

    def test_bright_direct_light_organic(self):
        payload = {"organic":[{"title":"CNH original","link":"https://example.com/part","description":"Part",
                               "global_rank":1}]}
        self.assertEqual(p._normalize_brightdata_results(payload)[0]["title"],"CNH original")

    def test_bright_json_wrapper_string_body(self):
        payload = {"body":'{"organic":[{"title":"CNH engine","link":"https://example.com/engine","description":"Motor"}]}'}
        result = p._normalize_brightdata_results(payload)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["url"],"https://example.com/engine")

    def test_bright_schema_malformed_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError,"SCHEMA_INVALID"):
            p._normalize_brightdata_results({"body":{"unrelated":{"organic":[]}}})

    def test_serper_http400_not_enough_credits_is_operational_pause(self):
        from types import SimpleNamespace
        response = SimpleNamespace(status_code=400,text='{"message":"Not enough credits","statusCode":400}')
        mock_limiter = SimpleNamespace(wait=lambda:None)
        mock_budget = SimpleNamespace(reserve=lambda:None)
        with patch("requests.post",return_value=response) as post:
            with self.assertRaises(p.OperationalPause) as error:
                p._provider_post_once("serper","https://google.serper.dev/search",{},{"q":"test"},
                                      3,mock_limiter,mock_budget)
            self.assertEqual(error.exception.code,"SERPER_LIMIT")
            self.assertEqual(post.call_count,1)

    def test_bright_request_contract_uses_official_serp_json(self):
        cfg = {"collection":{"providers":{"brightdata":{"base_url":"https://api.brightdata.com/request",
             "max_results":10}}}}
        limiter = SimpleNamespace(wait=lambda:None)
        budget = SimpleNamespace(reserve=lambda:None,used=1)
        creds = {"api_key":"test","zone":"serp"}
        response = {"organic":[{"title":"Part","link":"https://example.com/part","description":"Part"}]}
        with patch.object(p,"_provider_credentials",return_value=creds), \
             patch.object(p,"_provider_post_once",return_value=response) as request:
            p._provider_request_once("brightdata","CNH 123",cfg,limiter,budget)
            body = request.call_args.args[3]
            self.assertEqual(body["format"],"json")
            self.assertNotIn("data_format",body)
            self.assertIn("brd_json=1",body["url"])

class BrightDataManualEntrypointContractTests(unittest.TestCase):
    def test_collect_and_full_skip_legacy_brightdata_preflight(self):
        # Both menu entrypoints must defer Bright Data credentials to the
        # durable per-account router; the unsuffixed key is not configured.
        import ast
        import inspect
        import textwrap
        main_tree = ast.parse(textwrap.dedent(inspect.getsource(p.main)))
        expected_modes = {"collect", "full"}
        found = set()
        for node in ast.walk(main_tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Attribute)
                    and test.left.attr == "mode"
                    and test.ops and isinstance(test.ops[0], ast.Eq)
                    and test.comparators and isinstance(test.comparators[0], ast.Constant)):
                continue
            mode = test.comparators[0].value
            if mode not in expected_modes:
                continue
            calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_provider_credentials"
            ]
            self.assertTrue(calls, mode)
            for call in calls:
                parent_guard = [
                    branch for branch in ast.walk(node)
                    if isinstance(branch, ast.If) and call in list(ast.walk(branch))
                    and isinstance(branch.test, ast.BoolOp)
                    and any(isinstance(n, ast.Compare)
                            and isinstance(n.left, ast.Name) and n.left.id == "provider"
                            and any(isinstance(op, ast.NotEq) for op in n.ops)
                            and any(isinstance(v, ast.Constant) and v.value == "brightdata"
                                    for v in n.comparators)
                            for n in ast.walk(branch.test))
                ]
                self.assertTrue(parent_guard, f"{mode}: legacy Bright Data preflight reachable")
            found.add(mode)
        self.assertEqual(found, expected_modes)


class RuntimeModuleContractTests(unittest.TestCase):
    def test_runtime_module_contract(self):
        self.assertTrue(callable(getattr(DurableRouter, "already_reserved", None)),
                        "nightly_provider_router.py local desatualizado: recarregue o arquivo da branch Argentina")
        self.assertTrue(callable(getattr(DurableRouter, "reserve", None)))
        self.assertTrue(callable(getattr(DurableRouter, "disable", None)))
        self.assertIn("--auto-providers", p.build_parser().format_help())

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

    def test_manual_brightdata_uses_only_durable_brightdata_slots(self):
        # Real collection orchestration, mocked Search: no legacy unsuffixed
        # credential lookup; no Tavily/Serper fallback; slot quota enforced.
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.sqlite"
            slots = [
                Slot("tavily", "tavily:1", "TAVILY_API_KEY", "", 9, "t"),
                Slot("brightdata", "brightdata:1", "BRIGHTDATA_API_KEY_1", "BRIGHTDATA_ZONE_1", 1, "b1"),
                Slot("brightdata", "brightdata:2", "BRIGHTDATA_API_KEY_2", "BRIGHTDATA_ZONE_2", 1, "b2"),
            ]
            plan = SimpleNamespace(
                candidates=[
                    SimpleNamespace(brand_original="ABC", model_original=str(n), raw_state="RAW_MISSING")
                    for n in (111, 222, 333)
                ], summary={})
            args = argparse.Namespace(
                search_provider="brightdata", auto_providers=False,
                input="input.xlsx", cache=str(Path(d)/"cache.sqlite"),
                db=str(db), master="", final="", precollection_output="",
                recollect_empty=False, recollect_low_evidence=False,
                max_tavily_new=3, yes=True,
            )
            cfg = {"files":{"input":"input.xlsx", "cache":args.cache,
                   "classification_db":args.db, "collection_report":str(Path(d)/"report.xlsx")}}
            calls = []
            def mock_search(brand, model, cfg, limiter, provider):
                calls.append((model, provider, cfg["_active_search_slot"]["key_env"]))
                return {"results":[{"title":"test"}], "_collection":{
                    "state":"NO_TARGET_EVIDENCE", "curated_results":[], "source_audit":[],
                    "query_audit":[], "usage":{"requests":1}}}
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",return_value=plan), \
                 patch.object(p,"configured_slots",return_value=slots), \
                 patch.object(p,"_provider_credentials",side_effect=AssertionError("Unexpected legacy credential check")), \
                 patch.object(p,"search_product_once",side_effect=mock_search), \
                 patch.object(p,"TavilyWritableStore"), \
                 patch.object(p,"_export_collection_quality_report"):
                self.assertFalse(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
            self.assertEqual(calls, [
                ("111","brightdata","BRIGHTDATA_API_KEY_1"),
                ("222","brightdata","BRIGHTDATA_API_KEY_2"),
            ])
            self.assertNotIn("_active_search_slot",cfg)

    def test_raw_empty_no_keeps_queue_zero(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                search_provider="tavily", auto_providers=True, input="test.xlsx",
                cache=str(Path(d)/"cache.sqlite"),db=str(Path(d)/"db.sqlite"),
                master="",final="",precollection_output=str(Path(d)/"pre.xlsx"),
                recollect_empty=False,recollect_low_evidence=False,max_tavily_new=20,yes=True)
            plan = SimpleNamespace(candidates=[],summary={"RAW_EMPTY":1030})
            cfg = {"files":{"cache":args.cache,"classification_db":args.db}}
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",return_value=plan) as build, \
                 patch("builtins.input",return_value="N"):
                self.assertTrue(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
                self.assertEqual(build.call_count,1)

    def test_raw_empty_yes_replans_with_20_limit(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                search_provider="tavily", auto_providers=True, input="test.xlsx",
                cache=str(Path(d)/"cache.sqlite"),db=str(Path(d)/"db.sqlite"),
                master="",final="",precollection_output=str(Path(d)/"pre.xlsx"),
                recollect_empty=False,recollect_low_evidence=False,max_tavily_new=20,yes=False)
            plan_empty = SimpleNamespace(candidates=[],summary={"RAW_EMPTY":1030})
            plan_selected = SimpleNamespace(
                candidates=[SimpleNamespace(brand_original="CNH",model_original="47408215",raw_state="RAW_EMPTY")],
                summary={"RAW_EMPTY":1030})
            cfg = {"files":{"cache":args.cache,"classification_db":args.db}}
            slot = Slot("tavily","tavily:1","TAVILY_API_KEY","",20,"t")
            payload = {"results":[{"title":"test"}], "_collection":{
                "state":"COLLECTED","curated_results":[],"source_audit":[],
                "query_audit":[],"usage":{"requests":1}}}
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",side_effect=[plan_empty,plan_selected]) as build, \
                 patch.object(p,"configured_slots",return_value=[slot]), \
                 patch.object(p,"search_product_once",return_value=payload) as search, \
                 patch.object(p,"TavilyWritableStore"), \
                 patch.object(p,"_export_collection_quality_report"), \
                 patch("builtins.input",side_effect=["S", "CONFIRMAR"]) as confirm:
                self.assertTrue(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
                self.assertEqual(build.call_count,2)
                self.assertTrue(build.call_args.kwargs["recollect_empty"])
                self.assertEqual(build.call_args.kwargs["max_tavily_new"],20)
                self.assertEqual(search.call_count,1)
                self.assertEqual(confirm.call_count,2)
                self.assertIn("CONFIRMAR", confirm.call_args_list[1].args[0])

    def test_raw_empty_authorized_but_payment_not_confirmed_makes_zero_requests(self):
        # "S" merely unlocks replanning; a second explicit CONFIRMAR
        # is mandatory before any paid Search request/reservation.
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                search_provider="tavily", auto_providers=True, input="test.xlsx",
                cache=str(Path(d)/"cache.sqlite"), db=str(Path(d)/"db.sqlite"),
                master="", final="", precollection_output=str(Path(d)/"pre.xlsx"),
                recollect_empty=False, recollect_low_evidence=False,
                max_tavily_new=20, yes=False)
            empty = SimpleNamespace(candidates=[],summary={"RAW_EMPTY":1})
            selected = SimpleNamespace(
                candidates=[SimpleNamespace(
                    brand_original="CNH", model_original="47408215",
                    raw_state="RAW_EMPTY")],
                summary={"RAW_EMPTY":1})
            cfg = {"files":{"cache":args.cache,"classification_db":args.db}}
            slot = Slot("tavily","tavily:1","TAVILY_API_KEY","",20,"t")
            with patch.object(p,"validate_collection_cost_policy",return_value=[]), \
                 patch.object(p,"load_baseline_inventory",return_value={"processed":set()}), \
                 patch.object(p,"build_precollection_plan",side_effect=[empty,selected]), \
                 patch.object(p,"configured_slots",return_value=[slot]), \
                 patch.object(p,"search_product_once") as search, \
                 patch("builtins.input",side_effect=["S","N"]) as prompts:
                self.assertFalse(p.collect_products(args,cfg,SimpleNamespace(hash="test")))
                self.assertEqual(prompts.call_count,2)
                search.assert_not_called()

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
