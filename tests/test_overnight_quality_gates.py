"""Offline regression tests for overnight quality gates.

No providers, network, Excel writes, or production databases.
"""
import ast
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pipeline_produtos.py"


def function_source(name):
    source = SOURCE.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    found = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"Expected one definition of {name}; got {len(found)}")
    return ast.get_source_segment(source, found[0])


class OvernightQualityGatesTests(unittest.TestCase):
    def test_rule_classification_requires_verified_power_on_active_types(self):
        source = SOURCE.read_text(encoding="utf-8-sig")
        start = source.index("def classify(self, brand: str, model: str, payload:")
        segment = source[start:source.index("        return Stage1Result(", start)]
        self.assertIn('power != "UNKNOWN" or product_type in self.rules.passive_types', segment)
        self.assertIn('and power_verified_for_auto', segment)
        self.assertIn('state = "RESOLVED_RULE"', segment)

    def test_clef_cannot_auto_approve_active_type_unknown_power(self):
        src = "from __future__ import annotations\n" + function_source("_clef_auto_gate")
        ns = {
            "safe": lambda x: str(x or "").strip(),
            "_unit_score": lambda x: float(x or 0.0),
            "_taxonomy_pairs_for_type": lambda rules, typ: {("OTHERS", "DRILL")},
        }
        exec(compile(src, "<gate-test>", "exec"), ns)
        gate = ns["_clef_auto_gate"]
        rules = types.SimpleNamespace(
            types={"DRILL", "PASSIVE"}, passive_types={"PASSIVE"},
            valid_pairs={("OTHERS", "DRILL")},
        )
        state = types.SimpleNamespace(identity_best="STRONG")
        values = dict(decision="RESOLVED", product_type="DRILL",
                      power="UNKNOWN", category_general="OTHERS", family="DRILL")
        accepted, reason = gate(values, {}, rules, state, {})
        self.assertFalse(accepted)
        self.assertEqual(reason, "CLEF_POWER_UNVERIFIED")
        values["power"] = "CORDED"
        accepted, reason = gate(values, {}, rules, state, {})
        self.assertFalse(accepted)
        self.assertTrue(reason.startswith("CLEF_TAX_PROB_LOW"))
        # Passive products can legitimately have no applicable power source.
        values["product_type"] = "PASSIVE"
        values["power"] = "UNKNOWN"
        accepted, reason = gate(values, {}, rules, state, {})
        self.assertFalse(accepted)
        self.assertNotEqual(reason, "CLEF_POWER_UNVERIFIED")

    def test_audit_checks_all_raw_and_output_integrity(self):
        src = function_source("outcome_audit")
        self.assertIn('useful_raw==len(cohort)', src)
        self.assertIn('not result_errors', src)
        self.assertIn('FALSE_AUTO_ELIGIBLE', src)
        self.assertIn('RESULT_COHORT_MISMATCH', src)
        self.assertIn('set(result_keys).issubset(cohort_keys)', src)
        # Isolated output-only work must account for every attempted SKU;
        # conventional runs still permit audited partial classification.
        self.assertIn('if output_only and set(result_keys) != cohort_keys:', src)
        self.assertIn('NIGHT_RESULT_EXACT_COHORT_COVERAGE_FAILED', src)


if __name__ == "__main__":
    unittest.main()
