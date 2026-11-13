"""Offline runner contracts: no external APIs and all stage invocations are valid."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import run_model_benchmark_sequence as runner

class ModelSequenceContracts(unittest.TestCase):
    def fake_args(self):
        return SimpleNamespace(
            gold="Base Final Produtos Enriquecida.xlsx",
            cache="piloto_argentina/cache.sqlite",
            bge_model=r"C:\p\models\bge-m3",
            reranker_model=r"C:\p\models\bge-reranker-v2-m3",
            gliner_model="",batch_size=16,reranker_max_test=150,gliner_max_items=30
        )

    def test_all_stages_have_separate_outputs_and_expected_flags(self):
        args=self.fake_args()
        with tempfile.TemporaryDirectory() as temp:
            stages=runner.stages_for(args,Path(temp))
            self.assertEqual([x[0] for x in stages],[
                "tfidf_cache_ablation","bge_m3_supervised",
                "reranker_tfidf_topk","gliner_evidence_diagnostic"
            ])
            paths=[]
            for name,model,cmd in stages:
                self.assertEqual(cmd[0],runner.sys.executable)
                self.assertIn("--output",cmd)
                paths.append(cmd[cmd.index("--output")+1])
                self.assertNotIn("--llm-provider",cmd)
                self.assertNotIn("--search-provider",cmd)
            self.assertEqual(len(set(paths)),4)

    def test_gliner_without_local_model_is_explicitly_optional(self):
        args=self.fake_args()
        stages=runner.stages_for(args,Path("test_out"))
        self.assertIsNone(stages[3][1])
        self.assertEqual(stages[3][2][stages[3][2].index("--model")+1],"")

if __name__=="__main__":unittest.main()
