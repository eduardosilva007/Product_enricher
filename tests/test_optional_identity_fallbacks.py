"""Offline regression: Gold and GLiNER cannot bypass identity or auto-approve."""
import unittest
from gold_failure_recovery import suggest_on_failure
from gliner_raw_fallback import raw_texts, extract_on_failure, clef_candidate_context
from identity_ranking_experiment import tfidf_cosine

class MockGLiNER:
    calls=0
    def predict_entities(self, text, labels, threshold):
        self.calls += 1
        return [{"label":"model","text":"FS 120","score":0.9},
                {"label":"product type","text":"brushcutter","score":0.83}]

class FallbackRegressions(unittest.TestCase):
    def test_gold_never_runs_for_success(self):
        gold=[{"Marca":"STIHL","Modelo":"FS120","Category General":"PETROL HANDHELD","FAMILY":"BRUSHCUTTER"}]
        self.assertEqual(suggest_on_failure("STIHL","FS120",False,gold),[])

    def test_gold_exact_identity_only(self):
        gold=[{"Marca":"BOSCH","Modelo":"GSR18V","Category General":"OTHERS","FAMILY":"DRILL"},
              {"Marca":"BOSCH","Modelo":"GSB18V","Category General":"OTHERS","FAMILY":"HAMMER DRILL"}]
        out=suggest_on_failure("BOSCH","GSR18V",True,gold)
        self.assertEqual(len(out),1)
        self.assertEqual(out[0].model,"GSR18V")
        self.assertIn("REQUIRES_REVALIDATION",out[0].reason)

    def test_gold_conflict_stays_review_only(self):
        gold=[{"Marca":"BOSCH","Modelo":"GSR18V","Category General":"OTHERS","FAMILY":"DRILL"},
              {"Marca":"BOSCH","Modelo":"GSR18V","Category General":"OTHERS","FAMILY":"SAW"}]
        out=suggest_on_failure("BOSCH","GSR18V",True,gold)
        self.assertTrue(all(x.reason=="CONFLICTING_GOLD_LABELS_REVIEW_ONLY" for x in out))

    def test_raw_extraction_only_uses_local_payload(self):
        raw={"results":[{"title":"STIHL FS 120","content":"Gasoline brushcutter"}]}
        model=MockGLiNER()
        self.assertEqual(extract_on_failure(raw,False,model),[])
        self.assertEqual(model.calls,0)
        extracted=extract_on_failure(raw,True,model)
        self.assertEqual({x.label for x in extracted},{"model","product type"})
        context=clef_candidate_context(extracted)
        self.assertFalse(context["verified"])
        self.assertFalse(context["may_override_identity_gate"])

    def test_char_tfidf_still_prefers_exact_sku(self):
        corpus=["BOSCH GWS900100","BOSCH GWS900120"]
        self.assertGreater(tfidf_cosine(corpus[0],corpus[0],corpus),
                           tfidf_cosine(corpus[0],corpus[1],corpus))

if __name__=="__main__":
    unittest.main()
