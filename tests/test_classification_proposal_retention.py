"""Regression: proposal columns persist through full canonical export."""
import unittest
import pandas as pd
from pipeline_produtos import _restore_missing_final_proposal_fields


class ClassificationRetentionTests(unittest.TestCase):
    def test_restores_only_missing_business_fields_without_bi_approval(self):
        master = pd.DataFrame([{
            "Marca": "FIXTEC", "Modelo": "FCD2001X 1",
            "O que é (Tipo)": "DRILL", "Alimentação": "BATTERY",
            "Category General": "OTHERS", "FAMILY": "DRILLERS",
            "Título Real": "FIXTEC cordless drill",
            "Resolution State": "CLASSIFIED_CLEF", "Review Required": "SIM",
            "Status": "Script", "Revisão B.I": "",
        }])
        final = pd.DataFrame([{
            "Marca": "FIXTEC", "Modelo": "FCD2001X 1",
            "O que é (Tipo)": "DRILL", "Alimentação": "",
            "Category General": "", "FAMILY": "",
            "Título Real": "", "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
            "Status": "Script", "Revisão": "REVISAR", "Revisão B.I": "",
        }], dtype=object)
        stats = _restore_missing_final_proposal_fields(master, final)
        self.assertEqual(stats["fields_restored"], 4)
        self.assertEqual(final.loc[0, "FAMILY"], "DRILLERS")
        self.assertEqual(final.loc[0, "Alimentação"], "BATTERY")
        self.assertEqual(final.loc[0, "Revisão"], "PROVÁVEL")
        self.assertEqual(final.loc[0, "Próxima Ação"], "DOUBLE_CHECK")
        self.assertFalse(final.loc[0, "Revisão B.I"])

    def test_conflicting_taxonomy_blocks_export_without_overwriting(self):
        master = pd.DataFrame([{
            "Marca": "FIXTEC", "Modelo": "FCD2001X 1",
            "O que é (Tipo)": "DRILL", "Alimentação": "BATTERY",
            "Category General": "OTHERS", "FAMILY": "DRILLERS",
            "Resolution State": "CLASSIFIED_CLEF", "Review Required": "SIM",
            "Status": "Script",
        }])
        final = pd.DataFrame([{
            "Marca": "FIXTEC", "Modelo": "FCD2001X 1",
            "O que é (Tipo)": "CHAINSAW", "Alimentação": "",
            "Category General": "", "FAMILY": "",
            "Status": "Script", "Revisão": "REVISAR",
        }], dtype=object)
        with self.assertRaisesRegex(RuntimeError, "PROPOSAL_CONFLICT"):
            _restore_missing_final_proposal_fields(master, final)
        self.assertEqual(final.loc[0, "O que é (Tipo)"], "CHAINSAW")

    def test_exact_pair_not_alias_and_human_lock(self):
        master = pd.DataFrame([{
            "Marca": "A", "Modelo": "AB-01",
            "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
            "FAMILY": "DRILLERS", "Resolution State": "RESOLVED",
            "Review Required": "SIM", "Status": "Script",
        }])
        final = pd.DataFrame([{
            "Marca": "A", "Modelo": "AB-01", "O que é (Tipo)": "ENGINE",
            "Category General": "OTHERS", "FAMILY": "ENGINE",
            "Status": "Manual", "Revisão B.I": "OK",
        }], dtype=object)
        stats = _restore_missing_final_proposal_fields(master, final)
        self.assertEqual(stats["human_locks_skipped"], 1)
        self.assertEqual(final.loc[0, "FAMILY"], "ENGINE")
        self.assertEqual(final.loc[0, "Revisão B.I"], "OK")
        final.at[0, "Modelo"] = "AB01"
        with self.assertRaisesRegex(RuntimeError, "PROPOSAL_ROW_MISSING"):
            _restore_missing_final_proposal_fields(master, final)


if __name__ == "__main__":
    unittest.main()
