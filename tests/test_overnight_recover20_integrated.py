"""Fail-closed contracts for the integrated offline 20-SKU recovery mode."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline_produtos as core


class IntegratedRecovery20ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="integrated_recovery20_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "controlled_night"
        self.job = self.out / "20261112T190958Z_68f61e00" / "recovery_promotion_20_test"
        self.job.mkdir(parents=True)
        self.manifest = self.job / "promotion_manifest.json"
        self.out_patch = patch.object(core, "OUT", self.out)
        self.out_patch.start()
        self.addCleanup(self.out_patch.stop)

    def test_rejects_outside_controlled_night(self):
        foreign = self.root / "other" / "recovery_promotion_20_test" / "promotion_manifest.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "INVALID_MANIFEST_LOCATION"):
            core.overnight_recover_prepared_twenty(foreign)

    def test_rejects_missing_manifest(self):
        with self.assertRaises(FileNotFoundError):
            core.overnight_recover_prepared_twenty(self.manifest)

    def test_rejects_previous_commit(self):
        self.manifest.write_text(json.dumps({"status": "COMMITTED"}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "NOT_PREPARED_OR_ALREADY_COMMITTED"):
            core.overnight_recover_prepared_twenty(self.manifest)

    def test_rejects_wrong_cohort_before_io(self):
        self.manifest.write_text(json.dumps({
            "status": "PREPARED_NOT_COMMITTED", "cohort_count": 10,
            "calls_search": 0, "calls_jev": 0,
            "human_taxonomy_approved": False,
        }), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "MANIFEST_CONTRACT_INVALID"):
            core.overnight_recover_prepared_twenty(self.manifest)

    def test_rejects_external_api_flag_contract(self):
        self.manifest.write_text(json.dumps({
            "status": "PREPARED_NOT_COMMITTED", "cohort_count": 20,
            "calls_search": 1, "calls_jev": 0,
            "human_taxonomy_approved": False,
        }), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "MANIFEST_CONTRACT_INVALID"):
            core.overnight_recover_prepared_twenty(self.manifest)


if __name__ == "__main__":
    unittest.main()
