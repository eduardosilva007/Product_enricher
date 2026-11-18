"""Regressão offline de adaptadores de Search; zero chamadas HTTP/LLM."""
import ast
import json
import tempfile
import unittest
from pathlib import Path

from search_provider_extensions import (
    normalize_scraperapi, normalize_openserp, iter_openserp_cohorts, load_openserp_staging,
)

ROOT = Path(__file__).resolve().parents[1]


class ProviderExtensionTests(unittest.TestCase):
    def test_scraperapi_raw_shape_and_goto_block(self):
        sample = {
            "organic_results": [
                {"position": 1, "title": "STIHL FS250 Brush Cutter",
                 "link": "https://example.com/stihl-fs250", "snippet": "40.2 cc brush cutter"},
                {"position": 2, "title": "Part for STIHL FS250",
                 "link": "https://www.google.com/goto?url=opaque", "snippet": "Spare parts"},
            ]
        }
        rows = normalize_scraperapi(sample)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 1.0)
        self.assertEqual(rows[0]["raw_content"], "")

    def test_scraperapi_fail_closed_for_wrong_schema(self):
        with self.assertRaises(ValueError):
            normalize_scraperapi({"organic": []})

    def test_openserp_batch_and_unverified(self):
        sample = {
            "results": [{
                "provider": "openserp", "status": "CAPTURED",
                "query": "STIHL MS382", "organic": [
                    {"rank": 1, "title": "STIHL MS 382 Chainsaw",
                     "url": "https://example.com/stihl-ms382", "snippet": "Petrol chainsaw"},
                    {"rank": 2, "title": "STIHL MS382 spare part",
                     "url": "https://www.google.com/goto?url=opaque", "snippet": "Parts"}
                ],
            }]
        }
        groups = list(iter_openserp_cohorts(sample))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "STIHL MS382")
        self.assertEqual(len(groups[0][1]), 1)

    def test_openserp_not_captured_rejected(self):
        with self.assertRaises(ValueError):
            normalize_openserp({"status": "FAILED", "organic": []})

    def test_no_new_automatic_router_or_extra_search(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        ast.parse(source)
        self.assertIn('ALLOWED_SEARCH_PROVIDERS = {"tavily", "serper", "brightdata", "scraperapi", "openserp"}', source)
        self.assertIn('if provider == "scraperapi":', source)
        self.assertIn('"4": "scraperapi"', source)
        self.assertIn('"5": "openserp"', source)
        self.assertIn('if provider == "openserp":', source)
        self.assertIn('for provider, prefix in (("tavily","TAVILY"),("brightdata","BRIGHTDATA"),("serper","SERPER")):', source)
        self.assertIn('EXPERIMENTAL_PROVIDER_MANUAL_ONLY', source)
        self.assertIn('_experimental_collected_targets', source)
        self.assertIn('run_pipeline(args, cfg, rules, explicit_targets=targets)', source)

    def test_menu_full_auto_routes_to_existing_account_router(self):
        from unittest.mock import patch
        from pipeline_produtos import interactive_menu
        responses = iter(("3", "6", "3", "3", "1", "N"))
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            args = interactive_menu(ROOT)
        self.assertEqual(args.mode, "full")
        self.assertTrue(args.auto_providers)
        self.assertEqual(args.max_tavily_new, 3)
        self.assertEqual(args.max_items, 3)
        self.assertEqual(args.llm_provider, "jev")
        self.assertEqual(args.max_api_calls, 15)
        self.assertEqual(args.max_tokens, 30000)

    def test_menu_openserp_is_explicitly_staged_no_live_search(self):
        from unittest.mock import patch
        from pipeline_produtos import interactive_menu
        responses = iter(("3", "5"))
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            with self.assertRaisesRegex(RuntimeError, "OPENSERP_GITHUB_ACTIONS_ONLY"):
                interactive_menu(ROOT)

    def test_simplified_menu_routes_maintenance(self):
        from unittest.mock import patch
        from pipeline_produtos import interactive_menu
        with patch("builtins.input", side_effect=["4", "11"]):
            args = interactive_menu(ROOT)
        self.assertEqual(args.mode, "audit-central-cache")

    def test_staging_script_compiles(self):
        ast.parse((ROOT / "import_openserp_staging.py").read_text(encoding="utf-8"))


    def test_openserp_staging_exact_source_and_query(self):
        item = {
            "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
            "query": "STIHL FS250", "brand": "STIHL", "model": "FS250",
            "normalized_results": [{
                "title": "STIHL FS250 Brushcutter", "url": "https://stihl.example.com/fs250",
                "content": "STIHL FS250 brushcutter", "_provider_position": 2,
                "raw_content": "SHOULD NEVER BE USED"
            }],
            "approved_candidates": [{"title": "UNTRUSTED"}]
        }
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "staging.json"
            file.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [item],
            }), encoding="utf-8")
            results, provenance = load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS250")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["raw_content"], "")
            self.assertEqual(results[0]["score"], 0.5)
            self.assertEqual(provenance["network_requests"], 0)
            self.assertEqual(provenance["artifact_searches_reused"], 1)
            self.assertEqual(len(provenance["artifact_sha256"]), 64)
            with self.assertRaisesRegex(ValueError, "QUERY_MISMATCH"):
                load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS251")
            with self.assertRaisesRegex(ValueError, "SKU_NOT_IN_ARTIFACT"):
                load_openserp_staging(str(file), "STIHL", "FS251", "STIHL FS250")
            item["normalized_results"][0]["url"] = "https://www.google.com/goto?url=opaque"
            file.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [item],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "RAW_UNSAFE_URL"):
                load_openserp_staging(str(file), "STIHL", "FS250", "STIHL FS250")

    def test_openserp_to_sqlite_roundtrip_offline(self):
        """Exercita request adapter -> Source Sieve -> RAW -> SQLite -> releitura."""
        from contextlib import closing
        from pipeline_produtos import (
            RateLimiter, TavilyWritableStore, TavilyReadOnlyStore,
            read_json, search_product_once,
        )
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            artifact = base / "staging.json"
            artifact.write_text(json.dumps({
                "schema": "openserp-staging-v1", "promotion_allowed": False,
                "cohorts": [{
                    "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
                    "query": "STIHL FS250", "brand": "STIHL", "model": "FS250",
                    "normalized_results": [{
                        "title": "STIHL FS250 Brushcutter",
                        "url": "https://example.com/stihl-fs250",
                        "content": "STIHL FS250 petrol brushcutter",
                        "_provider_position": 1
                    }],
                }],
            }), encoding="utf-8")
            cfg = read_json(ROOT / "pipeline_config.json")
            cfg["_collection_policy"] = read_json(ROOT / "collection_quality_policy.json")
            cfg["_openserp_artifact_path"] = str(artifact)
            cfg["_openserp_target"] = ("STIHL", "FS250")
            cfg["collection"]["local_enrichment"]["enabled"] = False
            payload = search_product_once(
                "STIHL", "FS250", cfg, RateLimiter(10000), "openserp"
            )
            self.assertEqual(payload["_collection"]["provider"], "openserp")
            self.assertEqual(payload["_collection"]["usage"]["requests"], 1)
            self.assertEqual(payload["_collection"]["usage"]["network_requests"], 0)
            self.assertEqual(len(payload["results"]), 1)
            sqlite_path = base / "cache.sqlite"
            with closing(TavilyWritableStore(sqlite_path)) as writer:
                writer.put("STIHL", "FS250", payload)
            with closing(TavilyReadOnlyStore(sqlite_path)) as reader:
                raw = reader.get("STIHL", "FS250")
            self.assertEqual(raw.state, "RAW_OK")
            self.assertEqual(raw.payload["_collection"]["provider"], "openserp")
            self.assertEqual(raw.payload["results"][0]["url"], "https://example.com/stihl-fs250")

    def test_experimental_providers_require_canonical_paths_and_hard_caps(self):
        from pipeline_produtos import (
            _enforce_central_search_paths, build_parser, read_json,
        )
        cfg = read_json(ROOT / "pipeline_config.json")
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "staging.json"
            marker.write_text("{}", encoding="utf-8")
            cfg["_openserp_artifact_path"] = str(marker)
            for provider in ("openserp", "scraperapi"):
                args = build_parser().parse_args([
                    "--mode", "full", "--search-provider", provider,
                    "--max-search-new", "3", "--max-items", "3",
                    "--max-api-calls", "15", "--max-tokens", "30000"
                ])
                _enforce_central_search_paths(args, cfg, provider)
                args.cache = str(Path(folder) / "pilot_cache.sqlite")
                with self.assertRaisesRegex(ValueError, "CENTRAL_PATH_REQUIRED"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.cache = ""
                args.db = str(Path(folder) / "pilot_classification.sqlite")
                with self.assertRaisesRegex(ValueError, "CENTRAL_PATH_REQUIRED"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.db = ""
                args.auto_providers = True
                with self.assertRaisesRegex(ValueError, "MANUAL_ONLY"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.auto_providers = False
                args.recollect_low_evidence = True
                with self.assertRaisesRegex(ValueError, "NO_RECOLLECT_OR_REPROCESS"):
                    _enforce_central_search_paths(args, cfg, provider)
                args.recollect_low_evidence = False
                args.reprocess = True
                with self.assertRaisesRegex(ValueError, "NO_RECOLLECT_OR_REPROCESS"):
                    _enforce_central_search_paths(args, cfg, provider)

    def test_experimental_provider_blocks_unbounded_batches(self):
        from pipeline_produtos import _enforce_central_search_paths, build_parser, read_json
        cfg = read_json(ROOT / "pipeline_config.json")
        args = build_parser().parse_args(["--mode", "collect", "--search-provider", "scraperapi"])
        with self.assertRaisesRegex(ValueError, "PILOT_CAP_REQUIRED"):
            _enforce_central_search_paths(args, cfg, "scraperapi")
        args.max_tavily_new = 11
        with self.assertRaisesRegex(ValueError, "PILOT_CAP_REQUIRED"):
            _enforce_central_search_paths(args, cfg, "scraperapi")

    def test_brightdata_two_suffixed_accounts_diagnostic(self):
        from unittest.mock import patch
        from pipeline_produtos import _brightdata_slot_diagnostic
        environment = {
            "BRIGHTDATA_API_KEY_1": "private_1",
            "BRIGHTDATA_API_KEY_2": "private_2",
            "BRIGHTDATA_ZONE_1": "zone_1",
            "BRIGHTDATA_ZONE_2": "zone_2",
        }
        with patch.dict("os.environ", environment, clear=True):
            self.assertEqual(
                _brightdata_slot_diagnostic(),
                ("CONFIGURADA(2_CONTAS)", "CONFIGURADA(2_CONTAS)")
            )
        with patch.dict("os.environ", {
            "BRIGHTDATA_API_KEY": "legacy_key", "BRIGHTDATA_ZONE": "legacy_zone"
        }, clear=True):
            self.assertEqual(
                _brightdata_slot_diagnostic(),
                ("CONFIGURADA(1_CONTAS)", "CONFIGURADA(1_CONTAS)")
            )

    def test_accepted_cache_contract_matches_mirror_skip(self):
        import pandas as pd
        from pipeline_produtos import _cache_contract_human_or_accepted
        df = pd.DataFrame([
            {"Marca": "GRUNDFOS", "Modelo": "CM 102 ARAEAVBE FAAN",
             "O que é (Tipo)": "PUMP", "Category General": "OTHERS",
             "FAMILY": "PUMP", "Status": "Script", "Revisão": "AUTO_APROVADO",
             "Resolution State": "RESOLVED", "Classification Identity": "STRONG",
             "Status Processamento": "PROCESSADO", "Status Coleta": ""},
            {"Marca": "STIHL", "Modelo": "FS250", "Status": "Script",
             "Revisão": "PENDENTE_COLETA", "Resolution State": "PENDING_COLLECTION",
             "Status Coleta": ""},
            {"Marca": "STIHL", "Modelo": "MS382", "Status": "Manual",
             "Revisão B.I": "OK", "Status Coleta": ""}
        ]).fillna("")
        self.assertTrue(_cache_contract_human_or_accepted(df.loc[0], df))
        self.assertFalse(_cache_contract_human_or_accepted(df.loc[1], df))
        self.assertTrue(_cache_contract_human_or_accepted(df.loc[2], df))

    def test_raw_missing_reserved_is_audited_not_silently_dropped(self):
        import pandas as pd
        from types import SimpleNamespace
        from unittest.mock import patch
        from pipeline_produtos import build_precollection_plan, business_key
        brand = "TEST"
        rows = [
            SimpleNamespace(row_number=i + 2, brand_original=brand, model_original=model,
                            brand_canonical=brand, model_canonical=model,
                            key=business_key(brand, model), valid=True, reason="",
                            duplicate_rank=1, duplicate_count=1, representative=True,
                            action="", raw_state="")
            for i, model in enumerate(("SKU100", "SKU200"))
        ]
        with tempfile.TemporaryDirectory() as folder:
            from pathlib import Path
            from pipeline_produtos import _raw_missing_without_cache
            class MissingStore:
                def get(self, b, m):
                    return _raw_missing_without_cache(b, m)
                def close(self):
                    pass
            with patch("pipeline_produtos.read_input_rows", return_value=rows), \
                 patch("pipeline_produtos.TavilyReadOnlyStore", return_value=MissingStore()), \
                 patch("pipeline_produtos.export_precollection_report"):
                path = Path(folder) / "fake.sqlite"
                path.touch()
                plan = build_precollection_plan(
                    Path(folder) / "fake.xlsx", path,
                    Path(folder) / "report.xlsx",
                    exclude_attempted_keys={business_key(brand, "SKU200")}
                )
            self.assertEqual(plan.summary["RAW_MISSING"], 2)
            self.assertEqual(plan.summary["RAW_MISSING aptos à coleta"], 1)
            self.assertEqual(plan.summary["RAW_MISSING bloqueados por reserva anterior"], 1)
            self.assertEqual(len(plan.candidates), 1)
            self.assertEqual(plan.candidates[0].model_original, "SKU100")

    def test_auto_route_full_cohort_is_input_scoped(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn("AUTO_ROUTE_PILOT_SEARCH_CAP_REQUIRED",source)
        self.assertIn("AUTO_ROUTE_PILOT_CLASSIFICATION_CAP_REQUIRED",source)
        self.assertIn('if (auto_route or provider in {"openserp", "scraperapi"}) and curated:', source)
        self.assertIn('if args.auto_providers or provider in {"openserp", "scraperapi"}:',source)
        self.assertIn('run_pipeline(args, cfg, rules, explicit_targets=targets)',source)

    def test_serper_quota_recovery_preserves_unknown_and_audit(self):
        import sqlite3
        from contextlib import closing
        from types import SimpleNamespace
        from pipeline_produtos import (
            recover_confirmed_serper_quota_reservations, PreCollectionPlan,
        )
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "central_test.sqlite"
            with closing(sqlite3.connect(db)) as con, con:
                con.executescript("""
                    CREATE TABLE collection_attempt_reservations (
                        business_key TEXT PRIMARY KEY, account TEXT, provider TEXT);
                    CREATE TABLE collection_disabled_accounts (
                        account TEXT PRIMARY KEY, reason TEXT,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT, business_key TEXT, created_at TEXT,
                        event_type TEXT, detail TEXT);
                """)
                for key, provider in (("quota","serper"), ("timeout","serper"),
                                      ("other","tavily"), ("valid","serper"), ("legacy","serper")):
                    con.execute("INSERT INTO collection_attempt_reservations VALUES(?,?,?)",
                                (key,provider+":1",provider))
                for key, typ in (("quota","SERPER_LIMIT"),
                                 ("timeout","SEARCH_R9.6p7.2.3_ERROR_NO_RETRY"),
                                 ("other","SERPER_LIMIT"), ("valid","SERPER_LIMIT"),
                                 ("legacy","SEARCH_R9.6p7.2.3_ERROR_NO_RETRY")):
                    con.execute("INSERT INTO events(run_id,business_key,created_at,event_type,detail) VALUES(?,?,?,?,?)",
                                ("run",key,"2026-11-17",typ,
                                 "SERPER HTTP 400: not enough credits" if key=="legacy" else "test"))
            plan = PreCollectionPlan(
                input_path=Path("unused"), report_path=Path("unused"),
                rows=[], representatives=[
                    SimpleNamespace(key=key,raw_state=state,
                                    action="TENTATIVA_ANTERIOR_PRESERVADA")
                    for key,state in (("quota","RAW_MISSING"),("timeout","RAW_MISSING"),
                                      ("other","RAW_MISSING"),("valid","RAW_OK"),
                                      ("legacy","RAW_MISSING"))
                ], candidates=[], summary={}
            )
            self.assertEqual(recover_confirmed_serper_quota_reservations(db, plan),2)
            self.assertEqual(recover_confirmed_serper_quota_reservations(db, plan),0)
            with closing(sqlite3.connect(db)) as con, con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0],3)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='SERPER_LIMIT'").fetchone()[0],3)
                self.assertEqual(con.execute("SELECT reason FROM collection_disabled_accounts WHERE account='serper:1'").fetchone()[0],"SERPER_LIMIT_HISTORICAL")

    def test_no_parallel_openserp_runner(self):
        self.assertFalse((ROOT / "run_openserp_full_pilot.py").exists())

    def test_openserp_reuses_without_network_or_provider_secret(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn('load_openserp_staging(', source)
        self.assertIn('"network_requests": 0', source)
        self.assertIn('budget.reserve()', source)
        self.assertIn('"openserp"', source)


if __name__ == "__main__":
    unittest.main()
