import copy
import json
import unittest
from pathlib import Path

import pipeline_produtos as p


BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "pipeline_config.json").read_text(encoding="utf-8"))
POLICY = json.loads((BASE / "collection_quality_policy.json").read_text(encoding="utf-8"))
CFG["_collection_policy"] = POLICY


class IdentityRuntimeGuardV4072Tests(unittest.TestCase):
    def test_runtime_contract_passes_release_config(self):
        self.assertEqual(p.validate_identity_runtime_contract(CFG), [])

    def test_milwaukee_hidden_brand_character_still_gets_platform_suffix_removed(self):
        self.assertEqual(
            p.derive_search_model("MILWAUKEE\u200b", "2446 21XC M12", CFG),
            "2446 21XC",
        )

    def test_milwaukee_runtime_contract_fails_if_suffix_rule_drifts(self):
        bad = copy.deepcopy(CFG)
        bad["identity"]["operational_suffixes_by_brand"]["MILWAUKEE"] = []
        errors = p.validate_identity_runtime_contract(bad)
        self.assertTrue(any("2446 21XC M12" in x for x in errors))

    def test_body_target_is_rejected_when_title_is_another_explicit_sku(self):
        result = p._isolate_target_block(
            "ANTHBOT",
            "GENIE 3000",
            "Amazon.com: ANTHBOT Genie 600 Robotic Lawn Mower NO Wire",
            "https://example.com/genie600",
            (
                "ANTHBOT Genie 3000 supports up to 3600 square meters and "
                "uses RTK plus vision for boundary-free mowing."
            ),
            "",
            CFG,
            POLICY,
        )
        self.assertEqual(result["decision"], "REJECT_TITLE_OTHER_SKU_BODY_ONLY")
        self.assertTrue(result["other_skus"])

    def test_generic_family_title_can_still_use_isolated_target_body(self):
        result = p._isolate_target_block(
            "ANTHBOT",
            "GENIE 3000",
            "ANTHBOT Genie Robot Lawn Mower | ANTHBOT US",
            "https://anthbot.com/products/genie-robot-lawn-mower",
            (
                "Genie 600 small lawn. Genie 1000 medium lawn. "
                "Genie 3000 large lawn up to 3600m2 with 30 zones."
            ),
            "",
            CFG,
            POLICY,
        )
        self.assertNotEqual(result["decision"], "REJECT_TITLE_OTHER_SKU_BODY_ONLY")

    def test_policy_requires_title_other_sku_rejection_decision(self):
        self.assertIn("REJECT_TITLE_OTHER_SKU_BODY_ONLY", POLICY["source_decisions"])
        self.assertEqual(p.validate_collection_policy(POLICY), [])


if __name__ == "__main__":
    unittest.main()
