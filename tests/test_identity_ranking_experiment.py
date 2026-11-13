"""Regressões de blocking/TF-IDF e hard negatives: sem chamadas externas."""
import unittest
from identity_ranking_experiment import evaluate, tfidf_cosine

class IdentityRankingTests(unittest.TestCase):
    def test_equivalent_sku_formatting(self):
        result = evaluate("BOSCH", "GWS900100", "BOSCH", "GWS 900-100")
        self.assertTrue(result.eligible)
        self.assertGreater(result.score, 0)

    def test_hard_negatives_never_accepted_by_similarity(self):
        for a, b in [("2446-21XC", "2446-20"),
                     ("CTVLI20018", "CGTLI20018"),
                     ("GSR18V", "GSB18V")]:
            with self.subTest(a=a,b=b):
                r = evaluate("BOSCH", a, "BOSCH", b)
                self.assertFalse(r.eligible)
                self.assertEqual(r.reason,"SKU_MISMATCH")

    def test_brand_mismatch_not_rescued_by_same_sku(self):
        r = evaluate("BOSCH","GWS900100","MAKITA","GWS900100")
        self.assertFalse(r.eligible)
        self.assertEqual(r.reason,"BRAND_MISMATCH")

    def test_verified_equivalence_is_explicit(self):
        self.assertFalse(evaluate("STIHL","FS120","STIHL","FS 120 R").eligible)
        self.assertTrue(evaluate("STIHL","FS120","STIHL","FS 120 R",
                        verified_equivalences=[("FS120","FS120R")]).eligible)

    def test_ngram_is_ranking_only(self):
        corpus=["BOSCH GWS900100", "BOSCH GWS900120", "BOSCH GSR18V"]
        self.assertGreater(tfidf_cosine(corpus[0],corpus[0],corpus),
                           tfidf_cosine(corpus[0],corpus[1],corpus))
        self.assertFalse(evaluate("BOSCH","GWS900100","BOSCH","GWS900120",corpus).eligible)

if __name__=="__main__":
    unittest.main()
