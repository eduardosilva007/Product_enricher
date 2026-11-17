"""Provider lab smoke tests, no live HTTP calls or canonical stores."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_live_lab as lab
from provider_lab import inspect, capture_payload


class ProviderLiveLabTests(unittest.TestCase):
    def test_dotenv_reader_no_extra_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# comment\\n'
                'BRIGHTDATA_API_KEY_1="slot-one"\\n'
                'BRIGHTDATA_ZONE_1=zone # comment\\n'
                'SCRAPERAPI_LIMIT_1=5000\\n'
                'OTHER_SECRET=ignored\\n',
                encoding="utf-8"
            )
            result = lab.read_lab_env(path)
            self.assertEqual(result["BRIGHTDATA_API_KEY_1"], "slot-one")
            self.assertEqual(result["BRIGHTDATA_ZONE_1"], "zone")
            self.assertEqual(result["SCRAPERAPI_LIMIT_1"], "5000")
            self.assertNotIn("OTHER_SECRET", result)

    def test_dotenv_reader_missing_file_is_safe(self):
        self.assertEqual(lab.read_lab_env(Path("not_existing_test_env_file")), {})

    def test_brightdata_builds_full_json_with_slot_two_only(self):
        method, url, headers, kw = lab.prepare(
            "brightdata", "BRAND NEW", {"BRIGHTDATA_API_KEY_2": "abc", "BRIGHTDATA_ZONE_2": "z2"}, brightdata_slot=2)
        self.assertEqual(method, "POST")
        self.assertIn("brd_json=1", kw["json"]["url"])
        self.assertEqual(kw["json"]["format"], "json")
        self.assertEqual(kw["json"]["zone"], "z2")

    def test_brightdata_default_slot_one(self):
        _, _, _, kw = lab.prepare(
            "brightdata", "NEW SKU", {
                "BRIGHTDATA_API_KEY_1": "abc", "BRIGHTDATA_ZONE_1": "z1"
            })
        self.assertEqual(kw["json"]["zone"], "z1")

    def test_dry_run_without_any_credentials(self):
        with patch.object(lab.requests.Session, "request", side_effect=AssertionError("HTTP forbidden")):
            with patch.dict("os.environ", {
                "BRIGHTDATA_API_KEY_1": "", "BRIGHTDATA_ZONE_1": "",
                "SCRAPERAPI_API_KEY": "", "SERPER_API_KEY": ""
            }):
                for provider in ("brightdata", "scraperapi", "serper"):
                    with patch("sys.stdout"):
                        self.assertEqual(lab.run(["--provider", provider, "--query", "NEW SKU"]), 0)

    def test_explicit_missing_env_file_rejected(self):
        with self.assertRaisesRegex(ValueError, "ENV_FILE_NOT_FOUND"):
            lab.run(["--provider", "scraperapi", "--query", "NEW SKU",
                     "--env-file", "nonexistent_provider_lab.env"])

    def test_scraperapi_and_serper_are_independent(self):
        m, url, _, kw = lab.prepare("scraperapi", "NEW SKU", {"SCRAPERAPI_API_KEY": "key"})
        self.assertEqual(m, "GET")
        self.assertEqual(kw["params"]["query"], "NEW SKU")
        m, url, _, kw = lab.prepare("serper", "NEW SKU", {"SERPER_API_KEY": "key"})
        self.assertEqual(m, "POST")
        self.assertEqual(kw["json"]["q"], "NEW SKU")

    def test_scraperapi_shape_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"organic_results": [
                {"title": "Product", "link": "https://example.com/product", "snippet": "Tool"}
            ]}
            report = inspect("scraperapi", payload)
            self.assertEqual(report["candidate_total"], 1)
            location = capture_payload("scraperapi", payload, "NEW SKU", Path(directory))
            self.assertTrue(location.is_file())

    def test_octoparse_raw_array_export_offline(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "octoparse.json"
            original.write_text(json.dumps([
                {"title": "Tool", "url": "https://example.com/tool"}
            ]), encoding="utf-8")
            with patch("sys.stdout"):
                self.assertEqual(lab.run(["--provider", "octoparse",
                                           "--octoparse-json", str(original)]), 0)

    def test_dry_run_never_calls_http(self):
        with patch.object(lab.requests.Session, "request", side_effect=AssertionError("No live HTTP")):
            with patch.dict("os.environ", {"SERPER_API_KEY": "dummy"}):
                with patch("sys.stdout"):
                    self.assertEqual(lab.run(["--provider", "serper", "--query", "TEST NEW"]), 0)

    def test_scraperapi_limit_blocks_paid_request(self):
        with patch.object(lab.requests.Session, "request", side_effect=AssertionError("HTTP forbidden")):
            with patch.dict("os.environ", {
                "SCRAPERAPI_API_KEY": "dummy", "SCRAPERAPI_LIMIT_1": "24"
            }):
                with self.assertRaisesRegex(ValueError, "BUDGET_EXCEEDED"):
                    lab.run(["--provider", "scraperapi", "--query", "NEW UNIQUE SKU",
                             "--execute-live", "--ack-distinct-new-sku"])

    def test_scraperapi_missing_limit_blocks_paid_request(self):
        with patch.object(lab.requests.Session, "request", side_effect=AssertionError("HTTP forbidden")):
            with patch.dict("os.environ", {
                "SCRAPERAPI_API_KEY": "dummy",
                "SCRAPERAPI_LIMIT_1": "", "SCRAPERAPI_LIMIT": ""
            }):
                with self.assertRaisesRegex(ValueError, "SCRAPERAPI_LIMIT_REQUIRED"):
                    lab.run(["--provider", "scraperapi", "--query", "NEW UNIQUE SKU",
                             "--execute-live", "--ack-distinct-new-sku"])

    def test_live_requires_distinct_sku_ack_before_http(self):
        with patch.object(lab.requests.Session, "request", side_effect=AssertionError("No live HTTP")):
            with patch.dict("os.environ", {"SERPER_API_KEY": "dummy"}):
                with self.assertRaisesRegex(ValueError, "ACK_DISTINCT_NEW_SKU"):
                    lab.run(["--provider", "serper", "--query", "TEST NEW", "--execute-live"])


if __name__ == "__main__":
    unittest.main()
