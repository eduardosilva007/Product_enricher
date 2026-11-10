"""Mass readiness safety regression tests (offline, isolated, no HTTP)."""
import unittest
import pandas as pd
import numpy as np
from experiments.local_classifier_v5.mass_readiness_audit import rank_gold_fallback

class MassReadinessTests(unittest.TestCase):
    def test_gold_fallback_uses_train_only_and_never_approves(self):
        df=pd.DataFrame([
            {"__brand":"BOSCH","__model":"GSR18V","__label":"OTHERS || DRILL"},
            {"__brand":"BOSCH","__model":"GSB18V","__label":"OTHERS || HAMMER DRILL"},
            {"__brand":"STIHL","__model":"FS120","__label":"PETROL || BRUSHCUTTER"},
            {"__brand":"BOSCH","__model":"GSR18V-NEW","__label":"OTHERS || DRILL"},
        ])
        a,metrics=rank_gold_fallback(df,np.array([0,1,2]),np.array([3]),np.array([3]))
        self.assertEqual(metrics["tested"],1)
        self.assertEqual(a[0]["Decision"],"REVIEW_ONLY_NOT_AUTO_APPROVED")
        self.assertFalse(a[0]["Strict Identity Match"])
        self.assertNotIn("GSR18V-NEW",a[0]["Suggested Top5"])

    def test_gold_fallback_empty_when_not_triggered(self):
        df=pd.DataFrame([{"__brand":"BOSCH","__model":"GSR18V","__label":"OTHERS || DRILL"}])
        rows,metrics=rank_gold_fallback(df,np.array([0]),np.array([],dtype=int),np.array([],dtype=int))
        self.assertEqual(rows,[])
        self.assertEqual(metrics["tested"],0)

if __name__=="__main__":
    unittest.main()
