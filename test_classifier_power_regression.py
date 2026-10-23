"""Regressions: ambiguous power must not be auto-approved."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import pipeline_produtos as p

class PowerConflictRegressionTests(unittest.TestCase):
    def engine(self, powers):
        source = p.Source(source_id=1, title="Gerador alvo", content="", url="", relevance="STRONG", source_weight=1.0)
        engine = p.DeterministicEngine.__new__(p.DeterministicEngine)
        engine.rules = SimpleNamespace(passive_types=set(), specificity_groups=[],
            valid_pairs={("GENERATORS", "GENERATOR")},
            map_taxonomy=lambda *_: ("GENERATORS", "GENERATOR", "TEST"))
        engine.build_sources = Mock(return_value=[source])
        engine._type_hits_for_source = Mock(return_value=({"GENERATOR":["generador"]},{"GENERATOR":["TITLE"]}))
        engine._power_hits_for_source = Mock(return_value=({k:[k] for k in powers},{k:["TITLE"] for k in powers}))
        engine._evidence_is_target_anchored = Mock(return_value=True)
        engine._type_requires_specific_power = Mock(return_value=False)
        return engine

    def test_conflicting_power_not_auto_resolved(self):
        result = self.engine(["CORDED","PETROL"]).classify("BRANIK","4800IE",{})
        self.assertEqual(result.state, "AMBIGUOUS")
        self.assertIsNone(result.power)
        self.assertIn("POWER_CONFLICT_REQUIRES_ADJUDICATION", result.reasons)

    def test_single_explicit_power_still_resolved(self):
        result = self.engine(["PETROL"]).classify("BLACK PANTHER","BP HE7HP",{})
        self.assertEqual(result.state, "RESOLVED_RULE")
        self.assertEqual(result.power, "PETROL")

    def test_absent_power_behaviour_not_changed(self):
        result = self.engine([]).classify("BRANIK","BK15RSE",{})
        self.assertEqual(result.state, "RESOLVED_RULE")
        self.assertEqual(result.power, "UNKNOWN")

if __name__ == "__main__":
    unittest.main()
