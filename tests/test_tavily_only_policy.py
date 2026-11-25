"""Tavily-only contract (decision 2026-11-20): OpenSERP and other providers are
discontinued as live search options; accidentally reviving them must fail a test."""
import unittest
from unittest.mock import patch
from pathlib import Path

import pipeline_produtos as pp

ROOT = Path(__file__).resolve().parents[1]


class TavilyOnlyPolicyTests(unittest.TestCase):
    def test_parser_rejects_openserp(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            pp.build_parser().parse_args(["--mode", "collect", "--search-provider", "openserp"])

    def test_openserp_is_not_an_allowed_provider(self):
        self.assertNotIn("openserp", pp.ALLOWED_SEARCH_PROVIDERS)
        with self.assertRaises(ValueError):
            pp._resolve_search_provider(
                type("A", (), {"search_provider": "openserp"})(), {})

    def test_menu_collect_and_full_are_tavily_pool_only(self):
        with patch("builtins.input", side_effect=["1", "5"]):
            args = pp.interactive_menu(ROOT)
        self.assertEqual((args.mode, args.search_provider, args.tavily_pool),
                         ("collect", "tavily", True))
        self.assertEqual(args.max_tavily_new, 5)

    def test_tavily_pool_batch_must_be_1_to_1000(self):
        from types import SimpleNamespace
        cfg = pp.read_json(ROOT / "pipeline_config.json")
        args = pp.build_parser().parse_args(
            ["--mode", "collect", "--search-provider", "tavily", "--tavily-pool",
             "--max-search-new", "1001"])
        with self.assertRaisesRegex(ValueError, "TAVILY_POOL_REQUIRES_EXPLICIT_BATCH_1_TO_1000"):
            pp.collect_products(args, cfg, SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
