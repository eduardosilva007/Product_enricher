"""Offline routes for the 414 cache-evidence review SKUs."""
import unittest
from types import SimpleNamespace
import pandas as pd

from scripts.audit_414_cache_recovery import route_one


def evidence(state="RAW_OK", collection=None, results=None):
    return SimpleNamespace(state=state, payload={
        "results": results if results is not None else [{"title": "Correct exact model"}],
        "_collection": collection if collection is not None else {
            "state": "READY_FOR_CLASSIFICATION",
            "curated_results": [{"title": "Exact brand model"}],
        },
    })


class EvidenceReview414Tests(unittest.TestCase):
    def test_bi_lock_wins_over_ready_cache(self):
        rec = route_one(pd.Series({"Revisão B.I": "OK", "Status": "Script"}),
                        evidence(), pd.Series({"Resolution State": "REVIEW"}))
        self.assertEqual(rec["Route"], "01_HUMAN_LOCK")

    def test_master_complete_proposal_is_not_reclassified(self):
        rec = route_one({"Revisão B.I": "", "Status": "Script"}, evidence(),
                        pd.Series({
                            "Resolution State": "CLASSIFIED_CLEF",
                            "O que é (Tipo)": "DRILL",
                            "Category General": "OTHERS", "FAMILY": "DRILLERS",
                        }))
        self.assertEqual(rec["Route"], "02_MASTER_ALREADY_CLASSIFIED")

    def test_classifiable_curated_evidence_without_search(self):
        rec = route_one({}, evidence())
        self.assertEqual(rec["Route"], "03_CLASSIFIABLE_NO_SEARCH")
        self.assertEqual(rec["Curated Sources"], 1)

    def test_raw_with_wrong_evidence_requires_recuration_not_paid_jev(self):
        rec = route_one({}, evidence(collection={
            "state": "NO_TARGET_EVIDENCE", "curated_results": [],
        }, results=[{"title": "Wrong model"}]))
        self.assertEqual(rec["Route"], "04_RECURATE_OFFLINE")
        self.assertTrue(rec["Cache Title"])

    def test_missing_raw_is_not_classifiable(self):
        rec = route_one({}, SimpleNamespace(state="RAW_MISSING", payload=None))
        self.assertEqual(rec["Route"], "08_RAW_MISSING")

    def test_old_empty_is_not_recollected(self):
        rec = route_one({}, SimpleNamespace(state="RAW_EMPTY", payload=None))
        self.assertEqual(rec["Route"], "07_RAW_EMPTY_ATTEMPTED")


if __name__ == "__main__":
    unittest.main()
