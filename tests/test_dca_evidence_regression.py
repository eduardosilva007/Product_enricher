"""Regression from the DCA ten-SKU evidence incident; no APIs or baseline writes."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline_produtos import (Rules, DeterministicEngine, Source, Stage1Result,
                               _combo_kit_components)

class DcaEvidenceRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = Rules(ROOT / "regras_negocio.json")
        cls.engine = DeterministicEngine(cls.rules, {})

    def source(self, title, body, types=None):
        src = Source(source_id=1,title=title,content=body,url="https://example.org",
                     relevance="STRONG",model_match_field="TITLE",source_weight=1.0)
        src.type_hits = types or {}
        return src

    def test_rated_input_spec_detected_in_watt_variants(self):
        for title, body in (
            ("DCA AMB82 electric planer", "Rated Power Input (W): 500"),
            ("DCA AJZ03-16A drill", "Rated Power Input: 1010W"),
            ("DCA AMP04-6 trimmer", "Input: 550W"),
            ("DCA AQW5.5/10 washer", "Specifications: Rated Power Input (W): 1,600"),
        ):
            with self.subTest(title=title):
                hits, fields = self.engine._power_hits_for_source(self.source(title,body))
                self.assertIn("CORDED", hits, (title,body,hits))
                self.assertIn("BODY", fields["CORDED"])

    def test_bare_wattage_does_not_prove_corded(self):
        src = self.source("DCA AQB06-2000 2000W heat gun","High-quality heating core")
        hits, _ = self.engine._power_hits_for_source(src)
        self.assertNotIn("CORDED", hits)

    def test_battery_context_blocks_rated_input_inference(self):
        src = self.source("DCA AQF32 Electric Blower", "Runs on a lithium battery. Input: 680W")
        hits, _ = self.engine._power_hits_for_source(src)
        self.assertNotIn("CORDED", hits)

    def test_unrelated_product_in_snippet_is_not_a_combo(self):
        src = self.source("DCA AMQ02-65 Jig Saw",
                          "Original circular saw 7 inch", {"JIGSAW":["jig saw"],"CIRCULAR SAW":["circular saw"]})
        src.type_scores = {"JIGSAW":4.0,"CIRCULAR SAW":1.0}
        src.type_evidence_fields = {"JIGSAW":["TITLE"],"CIRCULAR SAW":["BODY"]}
        s1=Stage1Result(state="AMBIGUOUS", product_type=None, power=None,
                        type_candidates=["JIGSAW","CIRCULAR SAW"],power_candidates=[],
                        sources=[src],reasons=[])
        self.assertEqual(_combo_kit_components(self.rules,s1),[])

    def test_explicit_combo_title_remains_eligible(self):
        src = self.source("DCA drill and angle grinder combo kit",
                          "Contains drill and angle grinder",
                          {"DRILL":["drill"],"ANGLE GRINDER":["angle grinder"]})
        src.type_scores = {"DRILL":4.0,"ANGLE GRINDER":4.0}
        src.type_evidence_fields = {"DRILL":["TITLE"],"ANGLE GRINDER":["TITLE"]}
        s1=Stage1Result(state="AMBIGUOUS", product_type=None, power=None,
                        type_candidates=["DRILL","ANGLE GRINDER"],power_candidates=[],
                        sources=[src],reasons=[])
        self.assertEqual(set(_combo_kit_components(self.rules,s1)),{"DRILL","ANGLE GRINDER"})

if __name__ == "__main__":
    unittest.main()
