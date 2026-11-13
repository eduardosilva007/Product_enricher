"""Offline unresolved-model tests independent of neural checkpoint availability."""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EXP=ROOT/"experiments"/"local_classifier_v5"

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class UnresolvedModelRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gl=load("unresolved_gliner",EXP/"benchmark_gliner_cache.py")
        cls.rr=load("unresolved_reranker",EXP/"benchmark_unresolved_evidence.py")
        cls.runner=load("unresolved_runner",ROOT/"run_unresolved_models_sequence.py")

    def test_gliner2_response_shape(self):
        sample={"entities":{"brand":["CNH"],"model":[{"text":"47507106","confidence":0.91}],"product type":[]}}
        result=self.gl.parse_entities(sample)
        self.assertEqual(len(result),2)
        self.assertEqual(result[0],{"label":"brand","text":"CNH"})
        self.assertEqual(result[1]["confidence"],0.91)

    def test_gliner_does_not_invent_entities(self):
        self.assertEqual(self.gl.parse_entities({"entities":{}}),[])
        with self.assertRaises(ValueError):
            self.gl.parse_entities({"entities":[]})

    def test_evidence_relevance_is_not_identity_approval(self):
        self.assertEqual(self.rr.compact("4750-7106"),self.rr.compact("47507106"))
        content=(EXP/"benchmark_unresolved_evidence.py").read_text(encoding="utf-8")
        self.assertIn('"auto_approval":False',content)
        self.assertIn('local_files_only=True',content)

    def test_real_unresolved_cohort_and_no_api(self):
        self.assertEqual(len(self.runner.DEFAULT_SKUS.split(",")),6)
        for name in ("run_unresolved_models_sequence.py",):
            content=(ROOT/name).read_text(encoding="utf-8")
            self.assertNotIn("requests.post(",content)
            self.assertNotIn("llm-provider",content)

if __name__=="__main__":unittest.main()
