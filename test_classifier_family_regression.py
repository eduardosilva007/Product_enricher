"""Taxonomy regression contract: type specificity must survive classification."""
import unittest
from pathlib import Path
import pipeline_produtos as p

RULES = p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")

class SpecificFamilyRegressionTests(unittest.TestCase):
    def test_mini_chainsaw_is_not_demoted_to_battery_chainsaw(self):
        category, family, _ = RULES.map_taxonomy(
            "MINI CHAINSAW", "BATTERY", "mini motosserra a bateria")
        self.assertEqual((category, family), ("OTHERS", "MINI CHAINSAW"))

    def test_generic_chainsaw_uses_existing_battery_family(self):
        category, family, _ = RULES.map_taxonomy(
            "CHAINSAW", "BATTERY", "motosierra a bateria")
        self.assertEqual((category, family), ("BATTERY PRODUCT", "BATTERY CHAINSAW"))

    def test_specificity_priority_does_not_discard_mini(self):
        engine = p.DeterministicEngine.__new__(p.DeterministicEngine)
        engine.rules = RULES
        self.assertEqual(engine._apply_specificity(["MINI CHAINSAW", "CHAINSAW"]),
                         ["MINI CHAINSAW"])

    def test_unknown_size_cannot_imply_mini_family(self):
        category, family, _ = RULES.map_taxonomy(
            "CHAINSAW", "BATTERY", "motosierra a bateria sin tamano")
        self.assertNotEqual(family, "MINI CHAINSAW")

if __name__ == "__main__":
    unittest.main()
