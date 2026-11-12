"""Offline regression for Final-authoritative baseline reconciliation.

No HTTP, JEV, provider, or production files are touched.
Run: python -m unittest tests.test_baseline_final_authority_regression -v
"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from pipeline_produtos import (
    _baseline_row_accepted,
    _sync_final_into_master,
    _ensure_master_columns,
    load_baseline_inventory,
)


def record(brand, model, identity="STRONG", review="OK", bi=""):
    return {
        "Marca": brand,
        "Modelo": model,
        "O que é (Tipo)": "CHAINSAW",
        "Category General": "PETROL HANDHELD",
        "FAMILY": "PETROL CHAINSAW",
        "Status": "Script",
        "Status Processamento": "PROCESSADO",
        "Resolution State": "RESOLVED",
        "Review Required": "NÃO",
        "Classification Identity": identity,
        "Revisão": review,
        "Revisão B.I": bi,
    }


class FinalAuthorityRegression(unittest.TestCase):
    def test_existing_key_cannot_inherit_weak_identity(self):
        stale = record("BRAND", "MODEL", identity="WEAK", review="REVISAR")
        stale["Resolution State"] = "REVIEW"
        approved = record("BRAND", "MODEL")
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "final.xlsx"
            pd.DataFrame([approved]).to_excel(final_path, index=False)
            frame, b, m = _ensure_master_columns(pd.DataFrame([stale]))
            merged, _ = _sync_final_into_master(frame, final_path, b, m)
            self.assertEqual(len(merged), 1)
            self.assertTrue(_baseline_row_accepted(merged.iloc[0], merged))
            self.assertEqual(merged.iloc[0]["Classification Identity"], "STRONG")
            self.assertEqual(merged.iloc[0]["Revisão"], "OK")

    def test_real_persistence_keeps_final_authority(self):
        from pipeline_produtos import Rules, persist_baseline_excels
        stale = record("BRAND", "MODEL", identity="WEAK", review="REVISAR")
        stale["Resolution State"] = "REVIEW"
        approved = record("BRAND", "MODEL")
        additional = record("OTHER", "FINAL-ONLY")
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "master.xlsx"
            final_path = Path(tmp) / "final.xlsx"
            input_path = Path(tmp) / "input.xlsx"
            pd.DataFrame([stale]).to_excel(master_path, index=False)
            pd.DataFrame([approved, additional]).to_excel(final_path, index=False)
            pd.DataFrame([{"Marca": "NEW", "Modelo": "SKU"}]).to_excel(
                input_path, index=False
            )
            before = load_baseline_inventory(master_path, final_path)
            result = persist_baseline_excels(
                rows=[], master_path=master_path, final_path=final_path,
                input_path=input_path,
                rules=Rules(Path(__file__).resolve().parents[1] / "regras_negocio.json"),
                cache_path=None, cfg=None,
            )
            after = load_baseline_inventory(master_path, final_path)
            self.assertEqual(before["processed"] - after["final_accepted"], set())
            self.assertGreaterEqual(result["final_accepted_total"], 2)

    def test_final_only_accepted_survives(self):
        original = record("OTHER", "OLD")
        approved = record("BRAND", "FINAL-ONLY")
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "final.xlsx"
            pd.DataFrame([original, approved]).to_excel(final_path, index=False)
            frame, b, m = _ensure_master_columns(pd.DataFrame([original]))
            merged, _ = _sync_final_into_master(frame, final_path, b, m)
            accepted = merged.apply(lambda row: _baseline_row_accepted(row, merged), axis=1)
            self.assertEqual(int(accepted.sum()), 2)

    def test_final_bi_ok_is_not_downgraded(self):
        stale = record("BRAND", "MODEL", identity="WEAK")
        approved = record("BRAND", "MODEL", identity="WEAK", review="REVISAR", bi="OK")
        approved["Category General"] = "OTHERS"
        approved["FAMILY"] = "OTHERS"
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "final.xlsx"
            pd.DataFrame([approved]).to_excel(final_path, index=False)
            frame, b, m = _ensure_master_columns(pd.DataFrame([stale]))
            merged, _ = _sync_final_into_master(frame, final_path, b, m)
            row = merged.iloc[0]
            self.assertEqual(row["Revisão B.I"], "OK")
            self.assertEqual(row["Category General"], "OTHERS")
            self.assertEqual(row["Classification Identity"], "WEAK")
            self.assertTrue(_baseline_row_accepted(row, merged))


if __name__ == "__main__":
    unittest.main()
