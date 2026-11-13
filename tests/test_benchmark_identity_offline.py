"""Read-only synthetic integration tests for offline benchmark; zero HTTP."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
from benchmark_identity_offline import run, rank_sources
from gliner_raw_fallback import raw_texts

class OfflineBenchmarkTests(unittest.TestCase):
    def test_gliner_uses_persisted_evidence_excerpt(self):
        payload = {"results":[{"title":"Product","content":"Short snippet","raw_content":None}],
                   "_collection":{"evidence_cache":[{"title":"Product","evidence_excerpt":"Important technical specs"}]}}
        texts = raw_texts(payload)
        self.assertTrue(any("Important technical specs" in text for _,text in texts))

    def test_tfidf_never_leapfrogs_higher_identity_tier(self):
        rows = [
            {"title":"CNH 123 unrelated text","url":"https://example.org/a",
             "score":0.1,"_collection_identity":"STRONG"},
            {"title":"CNH 123","url":"https://example.org/b",
             "score":0.9,"_collection_identity":"PROBABLE"},
        ]
        old,new=rank_sources("CNH","123",rows)
        self.assertEqual(old[0][0]["_collection_identity"],"STRONG")
        self.assertEqual(new[0][0]["_collection_identity"],"STRONG")

    def test_readonly_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cache=root/"cache.sqlite"
            conn=sqlite3.connect(cache)
            conn.execute("CREATE TABLE tavily_cache (cache_key TEXT,brand TEXT,model TEXT,response_json TEXT,created_at TEXT)")
            data={"results":[{"title":"CNH 123","url":"https://example.org/123","content":"Part"}],
                  "_search_meta":{"provider":"tavily"},
                  "_collection":{"state":"READY_FOR_CLASSIFICATION",
                      "curated_results":[{"title":"CNH 123","url":"https://example.org/123",
                          "score":0.8,"_collection_identity":"STRONG"}]}}
            conn.execute("INSERT INTO tavily_cache VALUES(?,?,?,?,?)",
                ("v3|cnh|123","CNH","123",json.dumps(data),"2026-11-10"))
            conn.commit();conn.close()
            gold=root/"gold.xlsx"
            pd.DataFrame([{"Marca":"CNH","Modelo":"123","Category General":"OTHERS",
                          "FAMILY":"PART","Título Real":"CNH 123"}]).to_excel(gold,index=False)
            out=root/"out"
            run(SimpleNamespace(cache=cache,gold=gold,output=out,gliner_model="",max_sources=2))
            summary=json.loads((out/"benchmark_resumo.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["counts"]["cache_products"],1)
            self.assertEqual(summary["counts"]["baseline_top1_reference_matches"],1)
            self.assertEqual(summary["counts"]["tfidf_top1_reference_matches"],1)
            self.assertTrue((out/"benchmark_evidencias.csv").exists())

if __name__=="__main__":
    unittest.main()
