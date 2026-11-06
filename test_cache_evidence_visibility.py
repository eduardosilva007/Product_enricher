"""Contrato: cache com fontes permanece visivel e nunca pede recoleta automatica."""
import unittest
import pandas as pd

import pipeline_produtos as p


class CachedEvidenceReviewTests(unittest.TestCase):
    def review(self, *, raw="RAW_OK", state="LOW_TARGET_EVIDENCE", resolution="NEEDS_RECOLLECTION"):
        row = {
            "Marca": "ACQUA LIFE", "Modelo": "SPRAYER 02", "Status": "Script",
            "Revisão B.I": "", "Revisão": "PENDENTE_RECOLETA",
            "Raw State": raw, "Collection State": state,
            "Resolution State": resolution, "Título Real": "",
            "Category General": "", "FAMILY": "", "O que é (Tipo)": "",
        }
        df = pd.DataFrame([row])
        return p._derive_final_review(df.iloc[0], df)

    def test_cache_weak_target_requires_review_not_recollection(self):
        self.assertEqual(self.review(), "REVISAR")

    def test_cache_ready_stays_classification_pending(self):
        self.assertEqual(self.review(state="READY_FOR_CLASSIFICATION"), "PENDENTE_CLASSIFICACAO")

    def test_empty_search_is_review_not_repeat_search(self):
        self.assertEqual(self.review(raw="RAW_EMPTY", state="RAW_EMPTY"), "REVISAR")

    def test_raw_missing_is_only_case_needing_collection(self):
        self.assertEqual(self.review(raw="RAW_MISSING", state="", resolution="PENDING_COLLECTION"), "PENDENTE_COLETA")

    def test_all_sources_are_visible_even_if_rejected(self):
        raw = p.RawEvidence(
            "RAW_OK", "ACQUA LIFE", "SPRAYER 02",
            payload={"results": [
                {"title": f"Fonte {i} sprayer 02", "url": f"https://example.org/p/{i}",
                 "content": f"Descrição visível {i}"} for i in range(10)
            ], "_collection": {"state": "LOW_TARGET_EVIDENCE"}}
        )
        bundle = p._manual_review_bundle(raw)
        self.assertEqual(bundle["Fontes Encontradas"], "10")
        self.assertIn("Fonte 9 sprayer 02", bundle["Resumo Evidências"])
        self.assertIn("Descrição visível 0", bundle["Resumo Evidências"])

    def test_cohort_gate_does_not_count_clef_proposal_as_accepted(self):
        key = p.business_key("STIHL", "FS120")
        clef_proposal = {
            "Marca": "STIHL", "Modelo": "FS120",
            "Resolution State": "CLASSIFIED_CLEF",
            "O que é (Tipo)": "BRUSHCUTTER",
            "Category General": "PETROL HANDHELD", "FAMILY": "BRUSHCUTTER",
            "Título Real": "STIHL FS 120",
            "Review Required": "SIM",
        }
        result = p._expected_cohort_accepted_after_run({key}, set(), [clef_proposal])
        self.assertEqual(result, set())

    def test_cohort_gate_counts_resolved_accepted_record(self):
        key = p.business_key("STIHL", "FS120")
        accepted = {
            "Marca": "STIHL", "Modelo": "FS120",
            "Resolution State": "RESOLVED", "Review Required": "NAO",
            "Classification Identity": "STRONG",
            "O que é (Tipo)": "BRUSHCUTTER",
            "Category General": "PETROL HANDHELD", "FAMILY": "BRUSHCUTTER",
            "Título Real": "STIHL FS 120",
        }
        result = p._expected_cohort_accepted_after_run({key}, set(), [accepted])
        self.assertEqual(result, {key})

    def test_final_export_materializes_cached_titles_and_sources(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            cache = folder / "cache.sqlite"
            final = folder / "final.xlsx"
            entry = folder / "entrada.xlsx"
            brand, model = "ACQUA LIFE", "SPRAYER 02"
            pd.DataFrame([{"Marca": brand, "Modelo": model}]).to_excel(entry, index=False)
            payload = {
                "results": [
                    {"title": f"ACQUA LIFE SPRAYER 02 produto fonte {i}",
                     "url": f"https://example.org/catalog/{i}",
                     "content": f"Descricao da fonte {i}"} for i in range(10)
                ],
                "_collection": {"state": "NO_TARGET_EVIDENCE"},
            }
            store = p.TavilyWritableStore(cache)
            try:
                store.put(brand, model, payload)
            finally:
                store.close()
            master = pd.DataFrame([{
                "Marca": brand, "Modelo": model, "Status": "Script",
                "Revisão": "PENDENTE_RECOLETA", "Revisão B.I": "",
                "Resolution State": "NEEDS_RECOLLECTION",
            }])
            p._export_base_final(master, final, input_path=entry, cache_path=cache)
            result = pd.read_excel(final).iloc[0]
            self.assertEqual(result["Revisão"], "REVISAR")
            self.assertEqual(result["Fontes Encontradas"], 10)
            sources = json.loads(result["Fontes Pesquisa"])
            self.assertEqual(len(sources), 10)
            self.assertIn("produto fonte 9", result["Resumo Evidências"])
            self.assertTrue(result["Título Referência"])
            self.assertEqual(result["Próxima Ação"], "REVISAR_EVIDENCIA_CACHE")

    def test_sync_final_restores_accepted_record_over_stale_master_review(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            final = Path(td) / "Base Final.xlsx"
            final_row = {
                "Marca": "MAKITA", "Modelo": "HG6030",
                "Status": "Script", "Revisão": "AUTO_APROVADO",
                "Revisão B.I": "", "Resolution State": "RESOLVED",
                "Status Processamento": "PROCESSADO",
                "Review Required": "NÃO",
                "O que é (Tipo)": "HEAT GUN",
                "Category General": "OTHERS", "FAMILY": "HEAT GUN",
            }
            pd.DataFrame([final_row]).to_excel(final, index=False)
            old = dict(final_row)
            old.update({
                "Revisão": "REVISAR",
                "Resolution State": "REVIEW_CACHE_EVIDENCE",
                "Status Processamento": "REVIEW_CACHE_EVIDENCE",
                "Review Required": "SIM",
            })
            master = pd.DataFrame([old])
            merged, _ = p._sync_final_into_master(master, final, "Marca", "Modelo")
            self.assertTrue(p._baseline_row_accepted(merged.iloc[0], merged))
            self.assertEqual(merged.iloc[0]["Resolution State"], "RESOLVED")
            self.assertEqual(merged.iloc[0]["Review Required"], "NÃO")

    def test_recovery_skips_cached_clef_proposal_without_final_version_column(self):
        from types import SimpleNamespace
        rules = SimpleNamespace(hash="current", version="test")
        key = p.business_key("ACQUA LIFE", "SPRAYER 02")
        df = pd.DataFrame([{
            "Marca": "ACQUA LIFE", "Modelo": "SPRAYER 02",
            "Resolution State": "CLASSIFIED_CLEF", "Revisão": "PROVÁVEL",
            "Revisão B.I": "", "Status": "Script",
            "Review Required": "SIM",
        }])
        meta = {key: {"rules_hash": "current", "pipeline_version": p.PIPELINE_VERSION}}
        self.assertEqual(p._cache_recovery_reason(df.iloc[0], df, key, meta, rules), "")

    def test_recovery_keeps_reviewed_cache_eligible_with_same_rules(self):
        from types import SimpleNamespace
        rules = SimpleNamespace(hash="current", version="test")
        key = p.business_key("ACQUA LIFE", "SPRAYER 02")
        df = pd.DataFrame([{
            "Marca": "ACQUA LIFE", "Modelo": "SPRAYER 02",
            "Resolution State": "REVIEW_CACHE_EVIDENCE", "Revisão": "REVISAR",
            "Revisão B.I": "", "Status": "Script",
        }])
        meta = {key: {"rules_hash": "current", "pipeline_version": p.PIPELINE_VERSION}}
        self.assertEqual(p._cache_recovery_reason(df.iloc[0], df, key, meta, rules), "PENDENCIA_ATUAL")
        meta[key]["rules_hash"] = "previous"
        self.assertEqual(p._cache_recovery_reason(df.iloc[0], df, key, meta, rules), "PENDENCIA_ATUAL")

    def test_recollection_flags_are_rejected_before_any_search(self):
        from pathlib import Path
        with self.assertRaisesRegex(ValueError, "RECOLLECTION_DISABLED"):
            p.build_precollection_plan(
                Path("unused_input.xlsx"), Path("unused_cache.sqlite"),
                Path("unused_report.xlsx"), recollect_empty=True,
            )
        with self.assertRaisesRegex(ValueError, "RECOLLECTION_DISABLED"):
            p.build_precollection_plan(
                Path("unused_input.xlsx"), Path("unused_cache.sqlite"),
                Path("unused_report.xlsx"), recollect_low_evidence=True,
            )

    def test_raw_empty_no_longer_marks_recollection(self):
        rec = p.record_for_raw_state(p.RawEvidence("RAW_EMPTY", "ACQUA LIFE", "SPRAYER 02"))
        self.assertEqual(rec["Resolution State"], "REVIEW_NO_RESULTS")


if __name__ == "__main__":
    unittest.main()
