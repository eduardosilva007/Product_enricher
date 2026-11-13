"""Offline contracts for production-available title and hierarchical TF-IDF."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import numpy as np
import pandas as pd
from experiments.local_classifier_v5 import benchmark_cache_title_ablation as ab

class CacheTitleAblationTests(unittest.TestCase):
    def test_only_curated_title_enters_features_and_cache_remains_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)/"cache.sqlite"
            # sqlite3.Connection.__exit__ commits but DOES NOT close the handle.
            # Windows refuses TemporaryDirectory cleanup while it is open.
            with closing(sqlite3.connect(cache)) as db:
                db.execute("CREATE TABLE tavily_cache (brand TEXT,model TEXT,response_json TEXT,created_at TEXT)")
                payload={"results":[{"title":"BAD UNVERIFIED TITLE"}],
                         "_collection":{"curated_results":[{"title":"VERIFIED CNH 123"}]}}
                db.execute("INSERT INTO tavily_cache VALUES(?,?,?,?)",("CNH","123",json.dumps(payload),"2026-11-10"))
                db.commit()
            titles,stats=ab.load_cache_titles(cache)
            self.assertEqual(titles["CNH|123"],"VERIFIED CNH 123")
            self.assertEqual(stats["keys_with_titles"],1)
            # On Windows this read/rename is also a practical open-handle check.
            renamed = cache.with_name("cache_renamed.sqlite")
            cache.rename(renamed)
            renamed.rename(cache)

    def test_gold_title_not_used_in_cache_title_mode(self):
        df=pd.DataFrame([{"__brand":"CNH","__model":"123","__key":"CNH|123",
                         "Título Real":"GOLD SECRET LABEL"}])
        cols={"title":"Título Real"}
        texts=ab.make_texts(df,"cache_title",cols,{"CNH|123":"CACHE VERIFIED TITLE"})
        self.assertIn("CACHE VERIFIED TITLE",texts[0])
        self.assertNotIn("GOLD SECRET",texts[0])
        no_cache=ab.make_texts(df,"cache_title",cols,{})
        self.assertNotIn("TITLE:",no_cache[0])
        oracle=ab.make_texts(df,"gold_title_oracle",cols,{})
        self.assertIn("GOLD SECRET",oracle[0])

    def test_hierarchy_predicts_only_valid_training_pairs(self):
        texts=["BRAND: A MODEL: 1","BRAND: A MODEL: 2",
               "BRAND: B MODEL: 3","BRAND: B MODEL: 4",
               "BRAND: C MODEL: 5","BRAND: C MODEL: 6"]
        labels=np.array(["OTHERS || DRILL","OTHERS || DRILL",
                         "OTHERS || SAW","OTHERS || SAW",
                         "PETROL || ENGINE","PETROL || ENGINE"])
        train=np.array([0,1,2,3]);val=np.array([4]);test=np.array([5])
        classes,v,t=ab.model_probabilities(texts,labels,train,val,test,True)
        self.assertEqual(set(classes),{"OTHERS || DRILL","OTHERS || SAW"})
        self.assertTrue(np.allclose(v.sum(axis=1),1))
        self.assertTrue(np.allclose(t.sum(axis=1),1))

if __name__=="__main__":
    unittest.main()
