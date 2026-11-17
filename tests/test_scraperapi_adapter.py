import unittest
from scraperapi_adapter import (
    ScraperAPIBudget, baseline_cost_for_url, build_request_plan,
    normalize_structured_serp, credit_audit,
)


class ScraperAPIAdapterTests(unittest.TestCase):
    def test_google_cost_is_not_one_credit(self):
        self.assertEqual(baseline_cost_for_url("https://www.google.com/search?q=KRONER"), 25)
        self.assertEqual(baseline_cost_for_url("https://example.com/product"), 1)
        self.assertEqual(5000 // baseline_cost_for_url("https://www.google.com/search?q=x"), 200)

    def test_budget_and_cost_options_fail_closed(self):
        budget = ScraperAPIBudget(remaining_credits=24)
        with self.assertRaisesRegex(ValueError, "BUDGET_EXCEEDED"):
            build_request_plan("https://google.com/search?q=sku", budget)
        with self.assertRaisesRegex(ValueError, "URLCOST_PREFLIGHT"):
            build_request_plan("https://example.com", ScraperAPIBudget(5000), render=True)

    def test_structured_candidates_never_approved(self):
        payload = {"organic_results": [{"title": "KRONER KRCSC185", "link": "https://example.com/p",
                                        "snippet": "Serra circular"}]}
        rows = normalize_structured_serp(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approval"], "BLOCKED_PENDING_IDENTITY")
        with self.assertRaisesRegex(ValueError, "UNKNOWN_SERP_SCHEMA"):
            normalize_structured_serp({"unrecognized": []})

    def test_credit_header_not_fabricated(self):
        self.assertEqual(credit_audit({})["reported_credits"], None)
        self.assertEqual(credit_audit({"Sa-Credit-Cost": "25"})["reported_credits"], 25)


if __name__ == "__main__":
    unittest.main()
