"""Regression: collection titles remain visible even for review-only RAW evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import pandas as pd

import pipeline_produtos as p


class ReviewTitleVisibilityTests(unittest.TestCase):
    def test_curated_title_is_not_dropped_when_raw_title_is_absent(self):
        raw = SimpleNamespace(payload={
            "results": [{"url": "https://shop.example/brand", "title": ""}],
            "_collection": {
                "source_audit": [],
                "curated_results": [{"title": "Exact brand model D100",
                                     "url": "https://manufacturer.example/D100"}],
                "evidence_cache": [],
            },
        })
        bundle = p._manual_review_bundle(raw)
        self.assertEqual(bundle["Título Referência"], "Exact brand model D100")
        self.assertIn("Exact brand model D100", bundle["Fontes Pesquisa"])

    def test_rejected_weak_title_is_exposed_but_stays_in_review(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cache_file = root / "cache.sqlite"
            final_file = root / "final.xlsx"
            writer = p.TavilyWritableStore(cache_file)
            try:
                writer.put("BRAND", "D100", {
                    "results": [{"title": "BRAND D100 related replacement", "url": "https://example.com/d100", "content": "compatible with many models"}],
                    "_collection": {
                        "state": "NO_TARGET_EVIDENCE",
                        "source_audit": [{"url": "https://example.com/d100",
                                          "decision": "REJECT_COMPATIBILITY_ONLY",
                                          "identity_after": "WEAK"}],
                        "curated_results": [],
                        "evidence_cache": [],
                    },
                })
            finally:
                writer.close()
            master = pd.DataFrame([{
                "Marca": "BRAND", "Modelo": "D100",
                "Status": "Script", "Revisão": "REVISAR",
                "Próxima Ação": "REVISAR_EVIDENCIA_CACHE",
                "Resolution State": "REVIEW_CACHE_EVIDENCE",
                "Raw State": "RAW_OK",
                "Collection State": "NO_TARGET_EVIDENCE",
                "Review Required": "SIM",
                "O que é (Tipo)": "REVISAR",
            }])
            p._export_base_final(master, final_file, cache_path=cache_file)
            public, _ = p._read_business_sheet(final_file)
            self.assertEqual(len(public), 1)
            row = public.iloc[0]
            self.assertEqual(p.safe(row["Título Real"]), "BRAND D100 related replacement")
            self.assertEqual(p.safe(row["Revisão"]), "REVISAR")
            self.assertEqual(p.safe(row["Próxima Ação"]), "REVISAR_EVIDENCIA_CACHE")
            self.assertFalse(p._baseline_row_accepted(row, public))


if __name__ == "__main__":
    unittest.main()
