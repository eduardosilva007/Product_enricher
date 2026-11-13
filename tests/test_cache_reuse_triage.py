"""Offline cache triage reason tests; never connect to API."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

script = Path(__file__).resolve().parents[1] / "scripts" / "audit_cache_reuse.py"
spec = importlib.util.spec_from_file_location("audit_cache_reuse", script)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class CacheReuseTriageTest(unittest.TestCase):
    def test_raw_empty_with_existing_classification_not_approved_as_cache(self):
        state, reason = audit.cache_triage(SimpleNamespace(state="RAW_EMPTY"),
                                            True, True, 0, 0, 0, 0)
        self.assertEqual(state, "RAW_EMPTY_WITH_LEGACY_CLASSIFICATION")
        self.assertIn("não comprova",reason)

    def test_ready_curated_does_not_auto_approve(self):
        state, reason = audit.cache_triage(SimpleNamespace(state="RAW_OK"),
                                            True, False, 2, 0, 1, 0)
        self.assertEqual(state, "READY_CURATED_RECLASSIFICATION")
        self.assertIn("gates",reason)

    def test_safe_auxiliary_is_review_only(self):
        state, reason = audit.cache_triage(SimpleNamespace(state="RAW_OK"),
                                            True, False, 0, 1, 0, 1)
        self.assertEqual(state, "SAFE_TITLE_RESCUE_CANDIDATE")
        self.assertIn("não autoriza aprovação automática",reason)

    def test_only_titles_never_auto_approve(self):
        state, _ = audit.cache_triage(SimpleNamespace(state="RAW_OK"),
                                       True, False, 0, 0, 0, 0)
        self.assertEqual(state, "CACHE_EVIDENCE_REVIEW")

if __name__ == "__main__":
    unittest.main()
