"""No API calls, no canonical writes."""
import unittest

from provider_lab import inspect, evidence_from_payload
from octoparse_adapter import OctoparseMapping, estimate_allowed_rows, normalize_task_rows


class ProviderLabTests(unittest.TestCase):
    def test_brightdata_full_json_inspects_other_blocks_without_promoting_them(self):
        payload = {"status_code": 200, "body": {
            "organic": [], "general": {"query": "KRONER KRCSC185"},
            "knowledge": {"title": "Serra circular"},
            "shopping": [{"title": "KRONER KRCSC185", "link": "https://example.com/kr"}],
            "people_also_ask": [{"question": "O que é?"}],
        }}
        report = inspect("brightdata", payload)
        self.assertEqual(report["candidate_counts"], {"shopping": 1})
        self.assertEqual(report["other_blocks"]["knowledge"], "dict")
        self.assertEqual(report["other_blocks"]["people_also_ask"], 1)
        self.assertEqual(evidence_from_payload("brightdata", payload)[0]["status"],
                         "CANDIDATE_UNVERIFIED")

    def test_brightdata_wrapper_json_string(self):
        report = inspect("brightdata", {"body": '{"organic":[{"title":"T","link":"https://example.com"}]}'})
        self.assertEqual(report["candidate_total"], 1)

    def test_no_ai_text_without_source_url_is_candidate(self):
        payload = {"body": {"organic": [], "ai_overview": {"text": "A serra X tem 1500W"}}}
        self.assertEqual(evidence_from_payload("brightdata", payload), [])

    def test_octoparse_task_mapping_explicit(self):
        rows = [{"product_name": "KRONER KRCSC185", "product_url": "https://example.com/p",
                 "sku": "KRCSC185"}, {"product_name": "X", "product_url": "bad"}]
        mapping = OctoparseMapping("task-1", "product_name", "product_url", model_field="sku")
        out = normalize_task_rows(rows, mapping)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["model_hint"], "KRCSC185")
        self.assertEqual(out[0]["approval"], "BLOCKED_PENDING_IDENTITY")
        with self.assertRaisesRegex(ValueError, "TASK_ID_REQUIRED"):
            normalize_task_rows(rows, OctoparseMapping("", "product_name", "product_url"))

    def test_opt_in_raw_capture_outside_repository(self):
        import json
        import tempfile
        from pathlib import Path
        from provider_lab import capture_payload
        with tempfile.TemporaryDirectory() as directory:
            original = {"status_code": 200, "body": {"organic": [], "knowledge": {"title": "X"}}}
            path = capture_payload("brightdata", original, "KRONER KRCSC185", Path(directory))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertNotIn("KRCSC185", path.name)
            self.assertEqual(inspect("brightdata", original)["candidate_total"], 0)

    def test_snapshot_is_opt_in_in_brightdata_request_path(self):
        import inspect as pyinspect
        import pipeline_produtos as pipeline
        source = pyinspect.getsource(pipeline._provider_request_once)
        self.assertIn('os.getenv("PROVIDER_RAW_CAPTURE_DIR"', source)
        self.assertIn('capture_payload(', source)

    def test_octoparse_quota_is_records_not_skus(self):
        self.assertEqual(estimate_allowed_rows(500, 1999, 100), 100)
        self.assertEqual(estimate_allowed_rows(100, 0, 100), 0)


if __name__ == "__main__":
    unittest.main()
