"""Regressões do contrato de evidências para revisão humana (sem rede/LLM)."""
import json
import unittest
from types import SimpleNamespace

import pipeline_produtos as p


class ManualReviewBundleTests(unittest.TestCase):
    def test_rejected_weak_source_is_visible_but_not_approved(self):
        raw = SimpleNamespace(payload={
            "results": [{"title": "Possible FS120 manual", "url": "https://example.org/fs120",
                         "content": "STIHL FS 120 brochure"}],
            "_collection": {
                "state": "NO_TARGET_EVIDENCE",
                "source_audit": [{"url": "https://example.org/fs120",
                                  "decision": "REJECT_WEAK_IDENTITY", "identity_after": "WEAK"}],
                "evidence_cache": [],
                "curated_results": [],
            },
        })
        bundle = p._manual_review_bundle(raw)
        self.assertEqual(bundle["Título Referência"], "Possible FS120 manual")
        self.assertEqual(bundle["Qualidade Referência"], "REJECT_WEAK_IDENTITY")
        self.assertIn("STIHL FS 120 brochure", bundle["Resumo Evidências"])
        self.assertEqual(bundle["Fontes Encontradas"], "1")

    def test_wrong_model_is_displayed_with_rejection_reason(self):
        raw = SimpleNamespace(payload={
            "results": [
                {"title": "Wrong model 40U777-0032", "url": "https://example.org/wrong", "content": "wrong"},
                {"title": "Briggs 40U777-0010", "url": "https://example.org/right", "content": "right"}
            ],
            "_collection": {"source_audit": [
                {"url": "https://example.org/wrong", "decision": "REJECT_WRONG_MODEL", "identity_after": "STRONG"},
                {"url": "https://example.org/right", "decision": "ACCEPT_TARGET_CLEAN", "identity_after": "PROBABLE"}
            ]}
        })
        bundle = p._manual_review_bundle(raw)
        self.assertEqual(bundle["Título Referência"], "Briggs 40U777-0010")
        all_sources = json.loads(bundle["Fontes Pesquisa"])
        self.assertEqual(len(all_sources), 2)
        self.assertEqual(all_sources[0]["decision"], "REJECT_WRONG_MODEL")

    def test_no_raw_sources_yields_no_fabricated_title(self):
        raw = SimpleNamespace(payload={"results": [], "_collection": {"evidence_cache": []}})
        bundle = p._manual_review_bundle(raw)
        self.assertFalse(bundle["Título Referência"])
        self.assertEqual(bundle["Fontes Encontradas"], "0")

    def test_ten_sources_remain_traceable(self):
        raw = SimpleNamespace(payload={
            "results": [{"title": f"Title {i}", "url": f"https://example.org/{i}",
                         "content": f"Snippet {i}"} for i in range(10)]
        })
        bundle = p._manual_review_bundle(raw)
        self.assertEqual(len(json.loads(bundle["Fontes Pesquisa"])), 10)
        self.assertIn("Snippet 9", bundle["Resumo Evidências"])


if __name__ == "__main__":
    unittest.main()
