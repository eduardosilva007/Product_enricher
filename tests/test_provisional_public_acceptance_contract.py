"""Regression: provisional JEV proposals cannot become autoaccepted in public Final."""
import unittest
import pandas as pd

from pipeline_produtos import (
    _baseline_row_accepted, _record_is_accepted, business_key,
    _expected_cohort_accepted_after_run,
)


class PublicProposalAcceptanceContractTests(unittest.TestCase):
    def setUp(self):
        self.proposal = {
            "Marca": "FIXTEC", "Modelo": "FCD2001X 1",
            "O que é (Tipo)": "DRILL", "Alimentação": "BATTERY",
            "Category General": "OTHERS", "FAMILY": "DRILLERS",
            "Título Real": "FIXTEC drill", "Status": "Script",
            "Revisão": "PROVÁVEL", "Revisão B.I": "",
            "Próxima Ação": "DOUBLE_CHECK",
        }
        self.key = business_key("FIXTEC", "FCD2001X 1")

    def test_proposal_is_not_autoapproved_by_stripped_11_column_final(self):
        source = dict(self.proposal, **{
            "Resolution State": "CLASSIFIED_CLEF",
            "Review Required": "SIM",
            "Classification Identity": "PROBABLE",
        })
        public = pd.DataFrame([self.proposal])
        self.assertFalse(_record_is_accepted(source))
        self.assertFalse(_baseline_row_accepted(public.loc[0], public))
        self.assertNotIn(self.key, _expected_cohort_accepted_after_run(
            {self.key}, set(), [source]))

    def test_equivalent_alias_waiting_for_review_is_not_accepted(self):
        alias = dict(self.proposal, **{
            "Próxima Ação": "AGUARDAR_REVISAO_PRODUTO_CANONICO",
        })
        df = pd.DataFrame([alias])
        self.assertFalse(_baseline_row_accepted(df.loc[0], df))

    def test_real_human_lock_is_still_accepted(self):
        for changes in ({"Revisão B.I": "OK"}, {"Status": "Manual"}):
            df = pd.DataFrame([dict(self.proposal, **changes)])
            self.assertTrue(_baseline_row_accepted(df.loc[0], df))

    def test_legacy_probable_without_review_action_keeps_existing_semantics(self):
        df = pd.DataFrame([dict(self.proposal, **{
            "Próxima Ação": "CONCLUIDO",
        })])
        self.assertTrue(_baseline_row_accepted(df.loc[0], df))


if __name__ == "__main__":
    unittest.main()
