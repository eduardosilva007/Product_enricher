"""Guard against historical 100-SKU ceiling and first-page starvation.

No real API, model load, or database file is used. Tests the existing
build_classification_cohort, including processed/manual filtering BEFORE cap.
"""
import unittest
from types import SimpleNamespace
from pipeline_produtos import build_classification_cohort

class Catalog:
    def __init__(self,n=250):
        self.rows=[("BRAND",f"SKU{i:05d}",f"brand|sku{i:05d}") for i in range(n)]
    def catalog(self):
        return list(self.rows)
    def get(self,brand,model):
        return SimpleNamespace(state="RAW_OK",payload={
           "_collection":{"state":"READY_FOR_CLASSIFICATION",
                          "curated_results":[{"title":f"{brand} {model}"}]}
        })

class FullQueueRegressions(unittest.TestCase):
    def test_zero_means_all_eligible_not_first_100(self):
        store=Catalog(250)
        cohort,targets,stats,_=build_classification_cohort(
            store,processed=set(),manual=set(),max_items=0)
        self.assertEqual(len(targets),250)
        self.assertEqual(stats["cohort_pending"],250)
        self.assertEqual(len({t[2] for t in targets}),250)

    def test_max_items_is_applied_after_processed_and_manual_filters(self):
        store=Catalog(250)
        processed={x[2] for x in store.rows[:125]}
        manual={x[2] for x in store.rows[125:150]}
        _,targets,stats,_=build_classification_cohort(
            store,processed=processed,manual=manual,max_items=100)
        self.assertEqual(len(targets),100)
        self.assertTrue(all(x[2] not in processed|manual for x in targets))
        self.assertEqual(stats["cohort_already_classified"],125)
        self.assertEqual(stats["cohort_manual_lock"],25)

    def test_no_cap_after_first_batch_is_processed(self):
        store=Catalog(250)
        _,first,_,_=build_classification_cohort(
            store,processed=set(),manual=set(),max_items=100)
        done={x[2] for x in first}
        _,remaining,stats,_=build_classification_cohort(
            store,processed=done,manual=set(),max_items=0)
        self.assertEqual(len(remaining),150)
        self.assertEqual(stats["cohort_already_classified"],100)
        self.assertTrue(done.isdisjoint({x[2] for x in remaining}))

    def test_explicit_input_scope_is_respected_without_silent_first_page(self):
        store=Catalog(250)
        allowed={x[2] for x in store.rows[120:]}
        _,targets,stats,_=build_classification_cohort(
            store,processed=set(),manual=set(),
            max_items=0,allowed_keys=allowed)
        self.assertEqual(len(targets),130)
        self.assertEqual(stats["classification_outside_input_skipped"],120)

if __name__=="__main__":unittest.main()
