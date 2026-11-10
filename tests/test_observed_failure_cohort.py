"""Tests for observed failed-state cohort without guessing failure from no title."""
import unittest
import pandas as pd
from experiments.local_classifier_v5.observed_failure_cohort import observed_failure_rows

class FailureCohortTests(unittest.TestCase):
    def test_only_explicit_failed_states_enter_cohort(self):
        data=pd.DataFrame([
           {"Marca":"CNH","Modelo":"1","Resolution State":"UNRESOLVED","Collection State":"READY_FOR_CLASSIFICATION"},
           {"Marca":"CNH","Modelo":"2","Resolution State":"RESOLVED","Collection State":"READY_FOR_CLASSIFICATION"},
           {"Marca":"CNH","Modelo":"3","Resolution State":"AMBIGUOUS","Collection State":"PARTIAL_TARGET_EVIDENCE"},
        ])
        columns,rows=observed_failure_rows(data)
        self.assertIn("Resolution State",columns)
        self.assertEqual([r["Modelo"] for r in rows],["1","3"])
        self.assertIn("AMBIGUOUS",rows[1]["Observed Failure Fields"])

    def test_no_failure_column_never_infers_failed_products(self):
        columns,rows=observed_failure_rows(pd.DataFrame([{"Marca":"CNH","Modelo":"4","Título Real":""}]))
        self.assertEqual(columns,[])
        self.assertEqual(rows,[])

if __name__=="__main__":unittest.main()
