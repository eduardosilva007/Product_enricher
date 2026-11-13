"""Gate executed by the real --mode classify entrypoint before using APIs."""
import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pipeline_produtos import Rules, run_quality_gates, validate_classification_flow_policy

class ClassificationEntrypointGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules=Rules(ROOT/"regras_negocio.json")
        cls.cfg=json.loads((ROOT/"pipeline_config.json").read_text(encoding="utf-8-sig"))

    def test_full_runtime_quality_gate_accepts_strict_auto_policy(self):
        result=run_quality_gates(self.rules,self.cfg)
        self.assertGreater(result["synthetic_cases"],0)
        self.assertEqual(result["identity_cases"],result["identity_passed"])
        self.assertEqual(result["real_explicit"],result["real_passed"])

    def test_classification_flow_contract_remains_valid(self):
        self.assertEqual(validate_classification_flow_policy(self.cfg),[])

if __name__=="__main__":unittest.main()
