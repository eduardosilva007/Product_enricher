"""Testes do roteador offline (sem rede, sem credenciais)."""
import unittest
from provider_quota_planner import ProviderAccount, QuotaPlanner


class QuotaPlannerTests(unittest.TestCase):
    def test_rollover_to_next_provider(self):
        q = QuotaPlanner([
            ProviderAccount("tavily", "tav1", 1),
            ProviderAccount("brightdata", "bright1", 1),
            ProviderAccount("brightdata", "bright2", 1),
            ProviderAccount("serper", "serp1", 1),
        ])
        self.assertEqual(q.reserve("sku1").credential_id, "tav1")
        q.record_attempt("sku1")
        self.assertEqual(q.reserve("sku2").credential_id, "bright1")
        q.record_attempt("sku2")
        self.assertEqual(q.reserve("sku3").credential_id, "bright2")
        q.record_attempt("sku3")
        self.assertEqual(q.reserve("sku4").credential_id, "serp1")
        q.record_attempt("sku4")
        self.assertIsNone(q.reserve("sku5"))

    def test_no_repeat_for_attempted_sku(self):
        q = QuotaPlanner([ProviderAccount("tavily", "a", 3)])
        q.reserve("sku1")
        q.record_attempt("sku1")
        self.assertIsNone(q.reserve("sku1"))

    def test_unknown_outcome_must_consume_local_budget(self):
        q = QuotaPlanner([ProviderAccount("tavily", "a", 1),
                          ProviderAccount("serper", "b", 1)])
        q.reserve("sku1")
        q.record_attempt("sku1")  # Timeout com resultado incerto
        self.assertIsNone(q.reserve("sku1"))
        self.assertEqual(q.reserve("sku2").credential_id, "b")

    def test_shared_billing_group_does_not_double_count_quota(self):
        q = QuotaPlanner([
            ProviderAccount("brightdata", "bright1", 2, billing_group="shared"),
            ProviderAccount("brightdata", "bright2", 2, billing_group="shared"),
        ])
        q.reserve("sku1")
        q.record_attempt("sku1")
        q.reserve("sku2")
        q.record_attempt("sku2")
        self.assertIsNone(q.reserve("sku3"))

    def test_attempt_without_reservation_fails_without_consuming_quota(self):
        q = QuotaPlanner([ProviderAccount("tavily", "a", 2)])
        with self.assertRaisesRegex(ValueError, "No active reservation"):
            q.record_attempt("unreserved")
        self.assertEqual(q.reserve("unreserved").credential_id, "a")

    def test_disable_bad_credential(self):
        q = QuotaPlanner([
            ProviderAccount("tavily", "a", 10),
            ProviderAccount("serper", "b", 10),
        ])
        q.disable("a")
        self.assertEqual(q.reserve("sku1").credential_id, "b")


if __name__ == "__main__":
    unittest.main()
