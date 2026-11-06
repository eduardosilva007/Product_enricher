"""Offline regression tests for provider selection and JEV endpoint isolation."""
import unittest
from unittest.mock import patch

import pipeline_produtos as p


class JevRoutingAuditTests(unittest.TestCase):
    def test_jev_default_endpoint(self):
        self.assertEqual(p._jev_endpoint(""), "https://api.typesafe.ai/v1/systemone")

    def test_jev_endpoint_explicit_base(self):
        self.assertEqual(p._jev_endpoint("https://api.typesafe.ai/"), "https://api.typesafe.ai/v1/systemone")

    def test_jev_endpoint_does_not_append_twice(self):
        self.assertEqual(p._jev_endpoint("https://api.typesafe.ai/v1/systemone/"), "https://api.typesafe.ai/v1/systemone")

    def test_jev_endpoint_rejects_insecure_http(self):
        with self.assertRaisesRegex(ValueError, "JEV_BASE_URL_INVALID"):
            p._jev_endpoint("http://api.typesafe.ai")

    def test_jev_endpoint_never_becomes_cloudflare(self):
        self.assertNotIn("cloudflare", p._jev_endpoint("https://api.typesafe.ai"))

    def test_explicit_jev_menu_selection(self):
        with patch("builtins.input", return_value="1"):
            self.assertEqual(p._menu_llm_provider(), "jev")

    def test_explicit_clef_menu_selection(self):
        with patch("builtins.input", return_value="2"):
            self.assertEqual(p._menu_llm_provider(), "clef")

    def test_explicit_local_menu_selection(self):
        with patch("builtins.input", return_value="3"):
            self.assertEqual(p._menu_llm_provider(), "local")

    def test_classification_menu_has_no_forced_http_budget(self):
        from pathlib import Path
        with patch("builtins.input", side_effect=["2", "", "1"]):
            args = p.interactive_menu(Path("."))
        self.assertEqual(args.mode, "classify")
        self.assertEqual(args.llm_provider, "jev")
        self.assertEqual(args.max_api_calls, 0)
        self.assertEqual(args.max_tokens, 0)

    def test_recovery_menu_has_no_forced_http_budget(self):
        from pathlib import Path
        with patch("builtins.input", side_effect=["4", "", "1"]):
            args = p.interactive_menu(Path("."))
        self.assertEqual(args.mode, "recover")
        self.assertEqual(args.llm_provider, "jev")
        self.assertEqual(args.max_api_calls, 0)
        self.assertEqual(args.max_tokens, 0)



if __name__ == "__main__":
    unittest.main()
