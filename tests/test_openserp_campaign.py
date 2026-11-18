"""Offline-only acceptance tests for OpenSERP campaign safety."""
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import openserp_campaign as campaign


def artifact(skus):
    summary = {"results": [
        {"provider": "openserp", "query": f"{s['brand']} {s['model']}",
         "status": "CAPTURED", "organic": [
             {"type": "organic", "title": "example",
              "url": "https://example.com", "snippet": ""}]}
        for s in skus]}
    manifest = io.StringIO()
    writer = csv.DictWriter(manifest, fieldnames=["query", "brand", "model"])
    writer.writeheader()
    for sku in skus:
        writer.writerow({"query": f"{sku['brand']} {sku['model']}", **sku})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("pilot_summary.json", json.dumps(summary))
        z.writestr("openserp_manifest.csv", manifest.getvalue())
        z.writestr("manifest.json", '{"engine":"google"}')
    return data.getvalue()


class CampaignSafetyTests(unittest.TestCase):
    def test_multiword_brand_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.csv"
            p.write_text("brand,model\nBRIGGS & STRATTON,40U7770010\n", encoding="utf-8")
            self.assertEqual(campaign.read_skus(p)[0]["brand"], "BRIGGS & STRATTON")
            p.write_text("brand,model\nSTIHL,FS250\nSTIHL,FS-250\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DUPLICATE"):
                campaign.read_skus(p)

    def test_zip_accepts_exact_skus(self):
        skus = [{"brand": "STIHL", "model": "FS250"},
                {"brand": "BRIGGS & STRATTON", "model": "40U7770010"}]
        with tempfile.TemporaryDirectory() as d:
            digest = campaign.unpack_checked(artifact(skus), Path(d), skus)
            self.assertEqual(len(digest), 64)
            self.assertTrue((Path(d) / "pilot_summary.json").is_file())

    def test_zip_rejects_wrong_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "WRONG_SKU"):
                campaign.unpack_checked(
                    artifact([{"brand": "STIHL", "model": "MS382"}]), Path(d),
                    [{"brand": "STIHL", "model": "FS250"}])

    def test_zip_rejects_traversal(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../pilot_summary.json", "{}")
            z.writestr("openserp_manifest.csv", "query,brand,model")
            z.writestr("manifest.json", "{}")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "UNSAFE_ARTIFACT"):
                campaign.unpack_checked(buf.getvalue(), Path(d), [])

    def test_persist_cache_uses_official_collect_mode(self):
        skus = [{"brand": "STIHL", "model": "FS250"}]
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "pilot_summary.json").write_text("{}", encoding="utf-8")
            (base / "openserp_manifest.csv").write_text(
                "query,brand,model\\nSTIHL FS250,STIHL,FS250\\n", encoding="utf-8")
            with patch.object(campaign.subprocess, "run") as run:
                campaign.execute_pipeline(base, len(skus), 15, 30000, False, True)
                self.assertEqual(run.call_count, 2)
                args = run.call_args_list[1].args[0]
                self.assertEqual(args[args.index("--mode") + 1], "collect")
                self.assertEqual(args[args.index("--search-provider") + 1], "openserp")
                self.assertNotIn("--cache", args)
                self.assertNotIn("--db", args)

    def test_without_persist_cache_only_staging(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(campaign.subprocess, "run") as run:
                campaign.execute_pipeline(Path(d), 1, 15, 30000, False, False)
                self.assertEqual(run.call_count, 1)

    def test_pipeline_native_openserp_is_wired_to_existing_collector(self):
        import ast
        source = Path(__file__).resolve().parents[1] / "pipeline_produtos.py"
        text = source.read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        functions = {node.name: node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("_prepare_openserp_automatic_batch", functions)
        helper = ast.get_source_segment(text, functions["_prepare_openserp_automatic_batch"])
        self.assertIn("build_precollection_plan(", helper)
        self.assertIn("exclude_attempted_keys=existing", helper)
        self.assertIn("load_baseline_inventory(", helper)
        self.assertNotIn("TavilyWritableStore(", helper)
        self.assertIn("def collect_products(", text)
        self.assertIn('cfg["_openserp_artifact_path"]', helper)
        self.assertIn('"--openserp-auto"', text)
        self.assertNotIn("OPENSERP_GITHUB_ACTIONS_ONLY", text)

    def test_signed_artifact_redirect_never_exposes_pat(self):
        from email.message import Message
        from unittest.mock import Mock
        import urllib.error
        import urllib.request
        headers = Message()
        headers["Location"] = "https://store.blob.core.windows.net/artifact/file.zip?sig=masked"
        redirect = urllib.error.HTTPError(
            "https://api.github.com/artifacts/12/zip", 302, "Found", headers, None
        )
        seen = []
        class Storage:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, n):
                return b"PK" + bytes((3, 4)) + b"sample"
        class FakeOpener:
            def open(self, req, timeout):
                seen.append(req)
                raise redirect
        def get_storage(req, timeout):
            seen.append(req)
            return Storage()
        with patch.object(campaign.urllib.request, "build_opener", return_value=FakeOpener()):
            with patch.object(campaign.urllib.request, "urlopen", side_effect=get_storage):
                result = campaign._download_artifact_zip("very_secret", 12)
        self.assertTrue(result.startswith(b"PK"))
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer very_secret")
        self.assertIsNone(seen[1].get_header("Authorization"))

    def test_pipeline_skips_pre_sync_only_for_openserp_auto_collect(self):
        source = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn("openserp_collect_auto = (", source)
        self.assertIn("and not openserp_collect_auto:", source)
        self.assertIn('if completed and not args.auto_providers:', source)

    def test_staging_query_separator_normalization_kSB(self):
        from search_provider_extensions import _query_identity, load_openserp_staging
        self.assertEqual(_query_identity("KSB MOVITEC VS F 2/4 B"),
                         _query_identity("KSB MOVITEC VS F 2 4 B"))
        self.assertNotEqual(_query_identity("KSB MOVITEC VS F 2 5 B"),
                            _query_identity("KSB MOVITEC VS F 2 4 B"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "staging.json"
            p.write_text(json.dumps({
                "schema": "openserp-staging-v1",
                "promotion_allowed": False,
                "cohorts": [{
                    "provider": "openserp", "status": "STAGED_REVIEW_REQUIRED",
                    "brand": "KSB", "model": "MOVITEC VS F 2/4 B",
                    "query": "KSB MOVITEC VS F 2/4 B",
                    "original_provider_query": "KSB MOVITEC VS F 2/4 B",
                    "normalized_results": [{
                        "title": "KSB MOVITEC VS F 2/4 B",
                        "url": "https://example.com/pump",
                        "content": "", "_provider_position": 1
                    }]
                }]
            }), encoding="utf-8")
            rows, metadata = load_openserp_staging(
                str(p), "KSB", "MOVITEC VS F 2/4 B",
                "KSB MOVITEC VS F 2 4 B")
            self.assertEqual(len(rows), 1)
            with self.assertRaisesRegex(ValueError, "OPENSERP_QUERY_MISMATCH"):
                load_openserp_staging(
                    str(p), "KSB", "MOVITEC VS F 2/4 B",
                    "KSB MOVITEC VS F 2 5 B")

    def test_ai_overview_is_only_an_unverified_type_hypothesis(self):
        from search_provider_extensions import _unverified_ai_overview
        with tempfile.TemporaryDirectory() as folder:
            doc = {"ai_overview": {
                "source": "google_ai_overview",
                "status": "UNVERIFIED_CONTEXT_ONLY",
                "text": "KSB Movitec VSF 2/4 B is a vertical multistage centrifugal pump.",
                "may_approve_identity": False,
                "may_approve_classification": False,
            }}
            hint = _unverified_ai_overview(
                doc, Path(folder) / "openserp_staging.json",
                "KSB", "MOVITEC VS F 2/4 B", "KSB MOVITEC VS F 2 4 B")
            self.assertEqual(hint["type_hypothesis"], "")
            self.assertIn("centrifugal pump", hint["text"])
            self.assertEqual(hint["status"], "UNVERIFIED_CONTEXT_ONLY")
            self.assertFalse(hint["may_approve_identity"])
            self.assertFalse(hint["may_approve_classification"])

    def test_old_artifact_ai_overview_is_reused_without_new_search(self):
        from search_provider_extensions import _unverified_ai_overview
        import hashlib
        brand, model = "KSB", "MOVITEC VS F 2/4 B"
        key = hashlib.sha256((brand.casefold() + "|" + model.casefold()).encode()).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / ("openserp_google_" + key + ".json")
            p.write_text(json.dumps({
                "query": {"text": "KSB MOVITEC VS F 2/4 B"},
                "serp_features": [{
                    "type": "ai_summary",
                    "text": "The product is a multistage centrifugal pump."
                }]
            }), encoding="utf-8")
            overview = _unverified_ai_overview(
                {}, Path(folder) / "openserp_staging.json",
                brand, model, "KSB MOVITEC VS F 2 4 B")
            self.assertEqual(overview["type_hypothesis"], "")
            self.assertIn("centrifugal pump", overview["text"])

    def test_ai_overview_mismatched_raw_sku_not_reused(self):
        from search_provider_extensions import _unverified_ai_overview
        import hashlib
        brand, model = "KSB", "MOVITEC VS F 2/4 B"
        key = hashlib.sha256((brand.casefold() + "|" + model.casefold()).encode()).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / ("openserp_google_" + key + ".json")
            p.write_text(json.dumps({
                "query": {"text": "KSB MOVITEC VS F 2/5 B"},
                "serp_features": [{
                    "type": "ai_summary",
                    "text": "The product is a centrifugal pump."
                }]
            }), encoding="utf-8")
            self.assertIsNone(_unverified_ai_overview(
                {}, Path(folder) / "openserp_staging.json",
                brand, model, "KSB MOVITEC VS F 2 4 B"))

    def test_overview_not_in_curated_or_classification_inputs(self):
        source = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn('"ai_overview_unverified": usage.get(', source)
        self.assertIn('"curated_results": curated,', source)
        self.assertIn('"ai_overview_unverified": artifact_meta.get(', source)

    def test_gliner_segmented_context_does_not_truncate(self):
        from v42_adapters import GLiNEREntityExtractor
        class FakeModel:
            def __init__(self):
                self.windows = []
            def extract_entities(self, text, labels):
                self.windows.append(text)
                return {"entities": {"product_type": ["vertical multistage centrifugal pump"],
                                     "manufacturer": ["KSB"],
                                     "model": ["VSF 2/4 B"]}}
        extractor = GLiNEREntityExtractor("fake")
        fake = FakeModel()
        extractor.model = fake
        content = ("KSB Movitec is a vertical multistage centrifugal pump. " * 105)
        result = extractor.extract_context(content)
        self.assertGreater(result["segments_processed"], 1)
        self.assertEqual(set(result["entities"]), {"product_type"})
        self.assertEqual(result["entities"]["product_type"], ["vertical multistage centrifugal pump"])
        self.assertIn(content[-100:], fake.windows[-1])

    def test_gliner_quarantine_and_failure_fallback(self):
        from openserp_ai_gliner import extract_unverified_context
        overview = {"source": "google_ai_overview", "text": "KSB centrifugal pump",
                    "status": "UNVERIFIED_CONTEXT_ONLY"}
        class FakeExtractor:
            def __init__(self, model_name):
                pass
            def extract_context(self, text):
                return {"entities": {"product_type": ["centrifugal pump"]},
                        "segments_processed": 1}
        result = extract_unverified_context(
            overview, model_name="mock-model", extractor_factory=FakeExtractor)
        self.assertEqual(result["entity_extraction"]["status"], "EXTRACTED")
        self.assertFalse(result["may_approve_identity"])
        self.assertFalse(result["may_approve_classification"])
        self.assertNotIn("entity_extraction", overview)
        self.assertEqual(extract_unverified_context(
            overview, model_name="")["entity_extraction"]["status"],
            "SKIPPED_MODEL_NOT_CONFIGURED")
        class BrokenExtractor:
            def __init__(self, model_name):
                raise RuntimeError("offline model missing")
        failed = extract_unverified_context(
            overview, model_name="bad-model", extractor_factory=BrokenExtractor)
        self.assertEqual(failed["entity_extraction"]["status"], "UNAVAILABLE")
        self.assertNotIn("offline model missing", json.dumps(failed))

    def test_gliner_only_if_source_sieve_lacks_curated_results(self):
        source = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn("if query_ok and not curated:", source)
        self.assertIn("from openserp_ai_gliner import extract_unverified_context", source)
        self.assertIn('"ai_overview_unverified": usage.get(', source)

    def test_offline_gliner_audit_checks_sku_before_extracting(self):
        from openserp_gliner_audit import audit
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw.json"
            raw.write_text(json.dumps({
                "query": {"text": "KSB MOVITEC VS F 2/4 B"},
                "serp_features": [{
                    "type": "ai_summary",
                    "text": "KSB Movitec VSF 2/4 B is a multistage centrifugal pump."
                }]
            }), encoding="utf-8")
            with patch("openserp_gliner_audit.extract_unverified_context",
                       return_value={"text": "KSB", "entity_extraction": {
                           "status": "EXTRACTED", "segments_processed": 1,
                           "entities": {"product_type": ["centrifugal pump"]}}}) as extracted:
                result = audit(raw, "KSB", "MOVITEC VS F 2/4 B", "mock-model")
            self.assertEqual(result["type_hypothesis"], ["centrifugal pump"])
            self.assertFalse(result["identity_approved"])
            self.assertEqual(result["api_calls"], 0)
            self.assertEqual(result["official_writes"], 0)
            self.assertEqual(extracted.call_count, 2)
            self.assertIn('opening_description', result['context_comparison'])
            with self.assertRaisesRegex(ValueError, "RAW_ARTIFACT_SKU_IDENTITY_MISMATCH"):
                audit(raw, "KSB", "MOVITEC VS F 2/5 B", "mock-model")

    def test_wait_returns_failed_run_for_durable_audit(self):
        failure = {"id": 1234, "status": "completed",
                   "conclusion": "failure",
                   "html_url": "https://github.com/example/actions/runs/1234"}
        with patch.object(campaign, "find_run", return_value=failure):
            self.assertEqual(campaign.wait_run("dummy", "unique", 20), failure)

    def test_wait_returns_success_run(self):
        success = {"id": 5678, "status": "completed", "conclusion": "success"}
        with patch.object(campaign, "find_run", return_value=success):
            self.assertEqual(campaign.wait_run("dummy", "unique", 20), success)

    def test_failure_diagnostics_preserves_run_and_no_search(self):
        with tempfile.TemporaryDirectory() as d:
            batch = {"run_id": 4321, "run_url": "https://github.com/example/runs/4321",
                     "run_conclusion": "failure",
                     "skus": [{"brand": "STIHL", "model": "FS250"}]}
            with patch.object(campaign, "run_failure_diagnostics",
                              return_value={"jobs": [{"name": "batch", "failed_steps": [
                                  {"name": "Start isolated service", "conclusion": "failure"}]}]}):
                with patch.object(campaign, "download_artifact",
                                  side_effect=RuntimeError("ARTIFACT_MISSING")):
                    with patch.object(campaign, "request") as search:
                        report = campaign.preserve_failed_run("dummy", batch, Path(d))
                        search.assert_not_called()
            self.assertEqual(report["artifact"], "INCOMPLETE_OR_UNAVAILABLE")
            self.assertEqual(report["run_id"], 4321)
            self.assertTrue((Path(d) / "failed_run_diagnostics.json").is_file())
            self.assertFalse((Path(d) / "remote_artifact_quarantine.zip").exists())

    def test_failure_preserves_raw_zip_when_contract_fails(self):
        with tempfile.TemporaryDirectory() as d:
            batch = {"run_id": 4322, "run_conclusion": "failure",
                     "skus": [{"brand": "STIHL", "model": "FS250"}]}
            raw = b"not-a-complete-cohort"
            with patch.object(campaign, "run_failure_diagnostics",
                              return_value={"jobs": []}), patch.object(
                                  campaign, "download_artifact", return_value=raw):
                report = campaign.preserve_failed_run("dummy", batch, Path(d))
            self.assertEqual(report["artifact"], "INCOMPLETE_OR_UNAVAILABLE")
            self.assertEqual((Path(d) / "remote_artifact_quarantine.zip").read_bytes(), raw)

    def test_find_run_exact_nonce(self):
        with patch.object(campaign, "request", return_value={
            "workflow_runs": [
                {"id": 1, "display_title": "openserp-wrong",
                 "head_branch": campaign.BRANCH, "event": "workflow_dispatch"},
                {"id": 2, "display_title": "openserp-correct",
                 "head_branch": campaign.BRANCH, "event": "workflow_dispatch"}]}):
            self.assertEqual(campaign.find_run("dummy", "correct")["id"], 2)


if __name__ == "__main__":
    unittest.main()
