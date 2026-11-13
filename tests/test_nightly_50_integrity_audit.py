"""Offline end-to-end regression of the 50-SKU campaign audit.

No Search, JEV, canonical databases, or non-temporary Excel are accessed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from scripts.audit_nightly_50 import audit, export


def write_json(path, content):
    path.write_text(json.dumps(content), encoding="utf-8")


def write_excel(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Resultados"
    headers = list(rows[0]) if rows else ["Marca", "Modelo"]
    ws.append(headers)
    for row in rows:
        ws.append([row.get(k) for k in headers])
    wb.save(path)


class Nightly50AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="night50_audit_")
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "controlled_night"
        self.root = self.out / "campaigns" / "c50"
        self.root.mkdir(parents=True)
        self.batches = []
        self.models = []
        for batch_number, length in enumerate((20, 20, 10), 1):
            run = self.out / f"run{batch_number}"
            run.mkdir(parents=True)
            cohort = []
            classifications = []
            for n in range(sum((20, 20, 10)[:batch_number-1]),
                           sum((20, 20, 10)[:batch_number])):
                sku = f"SKU{n:04d}"
                self.models.append(sku)
                cohort.append({"Marca": "TEST", "Modelo": sku})
                classifications.append({
                    "Marca": "TEST", "Modelo": sku, "O que é (Tipo)": "DRILL",
                    "Alimentação": "CORDED", "Category General": "OTHERS",
                    "FAMILY": "DRILLERS", "Classification Identity": "STRONG",
                    "Resolution State": "RESOLVED", "Review Required": "NÃO",
                    "Auto Eligible": "SIM"})
            cohort_path = run / "raw_empty_cohort.xlsx"
            output_path = run / "classification_output.xlsx"
            write_excel(cohort_path, cohort)
            write_excel(output_path, classifications)
            write_json(run / "isolated_config.json",
                       {"files": {"input": str(cohort_path), "output": str(output_path)}})
            keys = [f"test|sku{sku[-4:]}" for sku in [r["Modelo"] for r in cohort]]
            write_json(run / "run_report.json", {
                "cohort_count": length, "cohort_keys": keys, "all_stages_pass": True,
                "stages": {"collection": {"status": "PASS"},
                           "classification": {"status": "PASS"}}})
            write_json(run / "quality_audit.json", {
                "status": "PASS", "useful_raw_count": length,
                "cache_states_after": {"RAW_OK": length},
                "protected_changes_total": 0, "original_source_modified": []})
            self.batches.append({
                "run_dir": str(run), "cohort_count": length, "cohort_keys": keys,
                "raw_ok": length, "status": "AUDIT_PASS"})
        self.manifest = {
            "status": "COMPLETE", "limits": {"target": 50},
            "review_before_promotion": True, "audited_products": 50,
            "batches": self.batches, "active_run": "", "pending_run_creation": False}
        self.write_manifest()

    def write_manifest(self):
        write_json(self.root / "campaign.json", self.manifest)

    def change_result(self, run_number, updater):
        path = self.out / f"run{run_number}" / "classification_output.xlsx"
        from openpyxl import load_workbook
        wb = load_workbook(path)
        ws = wb["Resultados"]
        updater(ws)
        wb.save(path)

    def test_full_50_is_read_only_and_requires_human_review(self):
        summary, rows = audit(self.root)
        self.assertEqual(summary["status"], "PASS_INTEGRITY_REVIEW_PENDING")
        self.assertEqual(summary["classification_results"], 50)
        self.assertEqual(summary["automatic_proposals"], 50)
        self.assertEqual(summary["missing_classification_results"], 0)
        self.assertEqual(summary["errors"], [])
        self.assertFalse(summary["canonical_promotion_permitted"])
        self.assertFalse(summary["human_taxonomy_approved"])
        self.assertEqual(len(rows), 50)
        outputs = export(self.root, summary, rows)
        self.assertTrue(all(x.is_file() for x in outputs))

    def test_no_result_is_visible_not_silently_passed_as_classified(self):
        self.change_result(3, lambda ws: ws.delete_rows(ws.max_row))
        summary, rows = audit(self.root)
        self.assertEqual(summary["classification_results"], 49)
        self.assertEqual(summary["missing_classification_results"], 1)
        self.assertTrue(any(x["Status de Auditoria"] == "SEM_SAIDA_DE_CLASSIFICACAO"
                            for x in rows))
        self.assertFalse(summary["canonical_promotion_permitted"])

    def test_auto_without_strong_identity_blocks_integrity(self):
        def corrupt(ws):
            cols = {cell.value: cell.column for cell in ws[1]}
            ws.cell(2, cols["Classification Identity"]).value = "WEAK"
        self.change_result(1, corrupt)
        summary, rows = audit(self.root)
        self.assertEqual(summary["status"], "BLOCKED_INTEGRITY_FAILURE")
        self.assertTrue(any("FALSE_AUTO_APPROVAL" in x for x in summary["errors"]))
        self.assertEqual(rows[0]["Status de Auditoria"], "FALSA_APROVACAO_AUTOMATICA")

    def test_unreviewed_automatic_promotion_flag_is_rejected(self):
        self.manifest["review_before_promotion"] = False
        self.write_manifest()
        summary, _ = audit(self.root)
        self.assertIn("HUMAN_REVIEW_FLAG_NOT_ENABLED", summary["errors"])

    def test_existing_promotion_is_rejected_for_audit(self):
        self.manifest["promotion"] = {"status": "COMMITTED"}
        self.write_manifest()
        summary, _ = audit(self.root)
        self.assertIn("ALREADY_PROMOTED_BEFORE_INDEPENDENT_AUDIT", summary["errors"])

    def test_duplicate_cross_batch_fails(self):
        # Even when underlying output files are present, duplicate keys in the
        # manifest must be rejected before any suggestion of approval.
        self.batches[2]["cohort_keys"][0] = self.batches[0]["cohort_keys"][0]
        self.write_manifest()
        summary, _ = audit(self.root)
        self.assertEqual(summary["status"], "BLOCKED_INTEGRITY_FAILURE")
        self.assertTrue(any("COHORT_INTEGRITY" in e for e in summary["errors"]))


if __name__ == "__main__":
    unittest.main()
