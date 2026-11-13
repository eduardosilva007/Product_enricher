"""Regression tests for durable cache status routing."""
import unittest
from types import SimpleNamespace
import pipeline_produtos as p

def raw(state,collection):
    return SimpleNamespace(state=state,payload={"results":[{"title":"Found"}],"_collection":collection})

class TestCacheTransition(unittest.TestCase):
    def test_raw_ok_with_curated_sources_routes_to_classify(self):
        self.assertEqual(p._cache_evidence_route(raw("RAW_OK", {
            "state":"READY_FOR_CLASSIFICATION",
            "curated_results":[{"title":"Exact model"}]
        })),("CLASSIFICAR_CACHE","PENDING_CLASSIFICATION","PENDENTE_CLASSIFICACAO"))

    def test_raw_ok_without_curated_sources_does_not_say_no_results(self):
        self.assertEqual(p._cache_evidence_route(raw("RAW_OK", {
            "state":"CACHE_RAW_SEM_CURADORIA","curated_results":[]
        })),("REVISAR_EVIDENCIA_CACHE","REVIEW_CACHE_EVIDENCE","REVISAR"))

    def test_search_results_alone_never_entitle_classification(self):
        self.assertEqual(p._cache_evidence_route(raw("RAW_OK", {
            "state":"PARTIAL_TARGET_EVIDENCE", "curated_results":[],
            "evidence_cache":[{"tier":"REJECTED","identity":"STRONG",
                               "decision":"REJECT_WRONG_MODEL"}]
        }))[0],"REVISAR_EVIDENCIA_CACHE")

    def test_eligible_safe_rescue_can_enter_review_classification(self):
        self.assertEqual(p._cache_evidence_route(raw("RAW_OK", {
            "state":"PARTIAL_TARGET_EVIDENCE","evidence_cache":[{
                "tier":"AUXILIARY_REVIEW_ONLY","identity":"PROBABLE",
                "decision":"ACCEPT_TARGET_ISOLATED","eligible_for_llm_rescue":True}]
        }))[0],"CLASSIFICAR_CACHE")

    def test_low_target_evidence_never_routes_directly(self):
        self.assertEqual(p._cache_evidence_route(raw("RAW_OK", {
            "state":"LOW_TARGET_EVIDENCE","curated_results":[{"title":"weak"}]
        }))[0],"REVISAR_EVIDENCIA_CACHE")

    def test_empty_cache_is_not_usable(self):
        with self.assertRaisesRegex(ValueError,"CACHE_ROUTE_REQUIRES_RAW_OK"):
            p._cache_evidence_route(raw("RAW_EMPTY", {}))

if __name__=="__main__":
    unittest.main()
