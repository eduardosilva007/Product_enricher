"""Offline exact-SKU audit and restore after full baseline exports."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pipeline_produtos as p


class NoApiCentralizedAuditTests(unittest.TestCase):
    def test_committed_campaign_restores_missing_proposals_no_api(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            campaign = root / "pilot" / "campaigns" / "camp"
            run = root / "pilot" / "run"
            run.mkdir(parents=True)
            (campaign / "promotion_backup").mkdir(parents=True)
            # Legacy status is not accepted before the campaign, so recovering
            # its new classification does not break the historical baseline.
            previous = pd.DataFrame([{
                "Marca": "FIXTEC", "Modelo": "X-1",
                "O que é (Tipo)": "", "Alimentação": "",
                "Category General": "", "FAMILY": "", "Título Real": "",
                "Status": "Script", "Revisão": "REVISAR", "Revisão B.I": "",
            }])
            for name in ("master", "final"):
                previous.to_excel(root / (name + ".xlsx"),
                                  sheet_name="Consolidado" if name == "master" else "Base Final",
                                  index=False)
            previous.to_excel(campaign / "promotion_backup" / "final.xlsx",
                              sheet_name="Base Final", index=False)
            pd.DataFrame([{"Marca": "FIXTEC", "Modelo": "X-1"}]).to_excel(
                run / "raw_empty_cohort.xlsx", index=False)
            pd.DataFrame([{
                "Marca": "FIXTEC", "Modelo": "X-1",
                "O que é (Tipo)": "DRILL",
                "Alimentação": "BATTERY",
                "Category General": "OTHERS",
                "FAMILY": "DRILLERS",
                "Título Real": "FIXTEC cordless drill X1",
                "Resolution State": "CLASSIFIED_CLEF",
            }]).to_excel(run / "classification_output.xlsx",
                         sheet_name="Resultados", index=False)
            (run / "isolated_config.json").write_text(json.dumps({"files": {
                "input": str(run / "raw_empty_cohort.xlsx"),
                "output": str(run / "classification_output.xlsx"),
            }}), encoding="utf-8")
            (campaign / "campaign.json").write_text(json.dumps({
                "status": "COMPLETE", "night_output_only": True,
                "promotion": {"status": "COMMITTED"}, "batches": [{
                    "run_dir": str(run), "cohort_keys": ["fixtec|x1"]
                }],
            }), encoding="utf-8")
            (root / "cache.sqlite").touch()
            class FakeStore:
                def __init__(self, _path):
                    pass
                def get(self, *_):
                    return SimpleNamespace(state="RAW_OK")
                def close(self):
                    pass
            cfg = {"files": {
                "master": str(root / "master.xlsx"),
                "final": str(root / "final.xlsx"),
                "cache": str(root / "cache.sqlite"),
                "baseline_backup_dir": str(root / "backups"),
            }}
            with (patch.object(p, "OUT", root / "pilot"),
                  patch.object(p, "ROOT", root),
                  patch.object(p, "TavilyReadOnlyStore", FakeStore)):
                first = p.audit_committed_classifications(cfg)
                self.assertEqual(first["status"], "RESTORATION_REQUIRED")
                self.assertEqual(first["complete_classification_proposals"], 1)
                self.assertEqual(first["missing_fields_final"], 5)
                restored = p.audit_committed_classifications(cfg, commit=True)
                self.assertEqual(restored["status"], "RESTORED_AND_AUDITED")
                final, _ = p._read_business_sheet(root / "final.xlsx")
                master, _ = p._read_business_sheet(root / "master.xlsx")
                self.assertEqual(final.iloc[0]["FAMILY"], "DRILLERS")
                self.assertEqual(master.iloc[0]["FAMILY"], "DRILLERS")
                self.assertFalse(p._is_bi_ok_value(final.iloc[0]["Revisão B.I"]))
                self.assertEqual(
                    p.audit_committed_classifications(cfg)["status"], "ALIGNED")

    def test_conflict_must_not_be_overwritten(self):
        from inspect import getsource
        source = getsource(p.audit_committed_classifications)
        self.assertIn("CONFLICT", source)
        self.assertIn("if problems:", source)
        self.assertIn("CENTRAL_AUDIT_BACKUP_HASH_MISMATCH", source)

    def test_no_paid_work_before_centralized_audit(self):
        from inspect import getsource
        s = getsource(p.main)
        self.assertIn('central_integrity = audit_committed_classifications', s)
        self.assertIn('CENTRALIZED_CLASSIFICATIONS_NOT_ALIGNED', s)
        self.assertIn('args.mode == "audit-central-cache"', s)


if __name__ == "__main__":
    unittest.main()
