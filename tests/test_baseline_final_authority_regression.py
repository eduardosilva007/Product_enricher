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

    def test_stale_no_results_with_confirmed_raw_is_reconciled(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from pipeline_produtos import Rules, persist_baseline_excels

        approved = record("BRAND", "APPROVED")
        stale = {
            "Marca": "COMET", "Modelo": "KS25010/150 CLASSIC",
            "Status": "Script", "Revisão": "REVISAR",
            "Próxima Ação": "REVISAR_SEM_RESULTADOS",
            "Collection State": "READY_FOR_CLASSIFICATION",
            "Resolution State": "REVIEW_NO_RESULTS",
            "Classification Identity": "WEAK",
            "Review Required": "SIM",
            "Revisão B.I": "",
        }

        class ReadOnlyCache:
            def __init__(self, path):
                pass

            def get(self, brand, model):
                return SimpleNamespace(
                    brand=brand, model=model, state="RAW_OK",
                    payload={"results": [], "_collection": {
                        "state": "READY_FOR_CLASSIFICATION",
                        "curated_results": [{"title": "Comet pressure washer",
                                             "url": "https://example.org/item"}]
                    }},
                )

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_path, final_path = root / "master.xlsx", root / "final.xlsx"
            input_path, cache_path = root / "cohort.xlsx", root / "cache.sqlite"
            pd.DataFrame([approved, stale]).to_excel(master_path, index=False)
            pd.DataFrame([approved]).to_excel(final_path, index=False)
            pd.DataFrame([{"Marca": "COMET", "Modelo": "KS25010/150 CLASSIC"}]).to_excel(
                input_path, index=False
            )
            cache_path.write_bytes(b"read-only-test-double")
            before = load_baseline_inventory(master_path, final_path)["processed"]
            with patch("pipeline_produtos.TavilyReadOnlyStore", ReadOnlyCache):
                stats = persist_baseline_excels(
                    rows=[], master_path=master_path, final_path=final_path,
                    input_path=input_path,
                    rules=Rules(Path(__file__).resolve().parents[1] / "regras_negocio.json"),
                    cache_path=cache_path,
                    cfg={"files": {"baseline_backup_dir": str(root / "diagnostic")}},
                )
            after = load_baseline_inventory(master_path, final_path)["processed"]
            self.assertFalse(before - after)
            self.assertGreaterEqual(stats["cache_pending_corrected"], 1)
            from pipeline_produtos import _read_business_sheet
            saved, _ = _read_business_sheet(master_path)
            comet = saved.loc[saved["Modelo"] == "KS25010/150 CLASSIC"].iloc[0]
            self.assertNotEqual(comet["Próxima Ação"], "REVISAR_SEM_RESULTADOS")
            self.assertEqual(comet["Raw State"], "RAW_OK")

    def test_bi_ok_does_not_spread_across_normalized_sku_aliases(self):
        from pipeline_produtos import Rules, persist_baseline_excels, _read_business_sheet
        approved = record("ANNOVI REVERBERI", "52180", bi="OK", review="MANUAL")
        approved["Status"] = "Manual"
        variant = record("Annovi-Reverberi", "52180", identity="WEAK", review="REVISAR")
        variant["Resolution State"] = "REVIEW"
        variant["Review Required"] = "SIM"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, final, inp = (root / name for name in
                                  ("master.xlsx", "final.xlsx", "input.xlsx"))
            pd.DataFrame([variant, approved]).to_excel(master, index=False)
            pd.DataFrame([approved, variant]).to_excel(final, index=False)
            pd.DataFrame([{"Marca": "NEW", "Modelo": "123"}]).to_excel(inp, index=False)
            persist_baseline_excels(
                rows=[], master_path=master, final_path=final, input_path=inp,
                rules=Rules(Path(__file__).resolve().parents[1] / "regras_negocio.json"),
                cache_path=None, cfg=None,
            )
            after, _ = _read_business_sheet(final)
            original = after.loc[
                (after["Marca"] == "ANNOVI REVERBERI") & (after["Modelo"] == "52180")
            ]
            alias = after.loc[
                (after["Marca"] == "Annovi-Reverberi") & (after["Modelo"] == "52180")
            ]
            self.assertEqual(len(original), 1)
            self.assertEqual(len(alias), 1)
            self.assertEqual(original.iloc[0]["Revisão B.I"], "OK")
            self.assertEqual(original.iloc[0]["Status"], "Manual")
            self.assertFalse(any(
                str(x).strip().upper() == "OK" for x in alias["Revisão B.I"]
            ), "human BI approval was incorrectly inherited by a compact-key alias")

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
