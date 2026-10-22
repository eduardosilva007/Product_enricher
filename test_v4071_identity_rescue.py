import json
import unittest
from pathlib import Path

import pipeline_produtos as p


BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "pipeline_config.json").read_text(encoding="utf-8"))
POLICY = json.loads((BASE / "collection_quality_policy.json").read_text(encoding="utf-8"))
CFG["_collection_policy"] = POLICY


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.by_key = {p.business_key(b, m): payload for b, m, payload in rows}

    def catalog(self):
        return [(b, m, p.business_key(b, m)) for b, m, _ in self.rows]

    def get(self, brand, model):
        payload = self.by_key[p.business_key(brand, model)]
        return p.RawEvidence("RAW_OK", brand, model, payload=payload)


class IdentityRescueV4071Tests(unittest.TestCase):
    def test_anthbot_eu_suffix_is_structural_identity_core(self):
        suffixes = CFG["identity"].get("regional_suffixes", [])
        v1000 = dict(p._model_identity_variants("GENIE 1000 EU", suffixes))
        v3000 = dict(p._model_identity_variants("GENIE 3000 EU", suffixes))
        self.assertEqual(v1000["genie1000"], "STRUCTURAL_EDGE_DECORATOR")
        self.assertEqual(v3000["genie3000"], "STRUCTURAL_EDGE_DECORATOR")

    def test_anthbot_compact_published_title_becomes_strong(self):
        model = p.derive_search_model("ANTHBOT", "GENIE 1000 EU", CFG)
        meta = p._source_identity_for_result(
            "ANTHBOT",
            model,
            {
                "title": "ANTHBOT Genie1000 Robot Lawn Mower NO Wire",
                "url": "https://example.com/anthbot-genie1000",
                "content": "",
            },
            CFG,
        )
        self.assertEqual(meta["level"], "STRONG")
        self.assertEqual(meta["model_match_field"], "TITLE")

    def test_anthbot_does_not_cross_match_other_genie(self):
        model = p.derive_search_model("ANTHBOT", "GENIE 1000 EU", CFG)
        meta = p._source_identity_for_result(
            "ANTHBOT",
            model,
            {
                "title": "ANTHBOT Genie 3000 Robot Mower",
                "url": "https://example.com/genie-3000",
                "content": "",
            },
            CFG,
        )
        self.assertNotIn(meta["level"], {"STRONG", "PROBABLE"})

    def test_milwaukee_platform_suffix_is_not_required_for_sku_identity(self):
        model = "2446 21XC M12"
        meta = p._source_identity_for_result(
            "MILWAUKEE",
            model,
            {
                "title": "Milwaukee M12 2446-21XC Grease Gun Kit",
                "url": "https://example.com/2446-21xc",
                "content": "",
            },
            CFG,
        )
        self.assertEqual(meta["level"], "STRONG")

    def test_briggs_full_internal_code_rewrites_to_published_model_type_trim(self):
        self.assertEqual(
            p.derive_search_model(
                "BRIGGS & STRATTON",
                "44S9770046G1AF0001",
                CFG,
            ),
            "44S977 0046 G1",
        )
        self.assertEqual(
            p.derive_search_model(
                "BRIGGS & STRATTON",
                "08P6020022H5A0096",
                CFG,
            ),
            "08P602 0022 H5",
        )
        self.assertEqual(
            p.derive_search_model(
                "BRIGGS & STRATTON",
                "6134774205J1EB0001",
                CFG,
            ),
            "613477 4205 J1",
        )

    def test_briggs_wrong_type_does_not_cross_match(self):
        model = p.derive_search_model(
            "BRIGGS & STRATTON",
            "44S9770046G1AF0001",
            CFG,
        )
        good = p._source_identity_for_result(
            "BRIGGS & STRATTON",
            model,
            {
                "title": "44S977-0046-G1 Briggs & Stratton Engine",
                "url": "https://example.com/44s977-0046-g1",
                "content": "",
            },
            CFG,
        )
        wrong = p._source_identity_for_result(
            "BRIGGS & STRATTON",
            model,
            {
                "title": "Briggs & Stratton 44S977-0032-G1 Engine",
                "url": "https://example.com/44s977-0032-g1",
                "content": "",
            },
            CFG,
        )
        self.assertEqual(good["level"], "STRONG")
        self.assertNotIn(wrong["level"], {"STRONG", "PROBABLE"})

    def test_briggs_trim_suffix_is_not_seen_as_other_sku(self):
        model = p.derive_search_model("BRIGGS&STRATTON", "40U7770010", CFG)
        self.assertEqual(model, "40U777 0010")
        self.assertTrue(
            p._same_target_sku_token(
                "BRIGGS&STRATTON",
                model,
                "40U777-0010-G1",
                CFG,
            )
        )
        other = p._other_sku_spans(
            "Briggs & Stratton 40U777-0010-G1 OEM Parts",
            "BRIGGS&STRATTON",
            model,
            CFG,
            POLICY,
        )
        self.assertEqual(other, [])

    def test_briggs_isolation_no_longer_rejects_own_trim_as_competitor(self):
        model = p.derive_search_model("BRIGGS&STRATTON", "40U7770010", CFG)
        result = p._isolate_target_block(
            "BRIGGS&STRATTON",
            model,
            "Briggs & Stratton 40U777-0010-G1 OEM Parts",
            "https://example.com/40u777-0010-g1",
            "",
            "Briggs & Stratton 40U777-0010-G1 OEM Parts for vertical engine",
            CFG,
            POLICY,
        )
        self.assertTrue(result["decision"].startswith("ACCEPT_"))
        self.assertEqual(result["other_skus"], [])

    def test_safe_auxiliary_no_target_enters_rescue_queue(self):
        payload = {
            "results": [
                {
                    "title": "Briggs & Stratton 40U777-0010-G1 OEM Parts",
                    "url": "https://example.com/40u777",
                    "content": "",
                }
            ],
            "_collection": {
                "state": "NO_TARGET_EVIDENCE",
                "curated_results": [],
                "evidence_cache": [
                    {
                        "tier": "AUXILIARY_REVIEW_ONLY",
                        "identity": "STRONG",
                        "decision": "REJECT_NON_ISOLATABLE",
                        "eligible_for_llm_rescue": True,
                    }
                ],
            },
        }
        store = FakeStore([("BRIGGS&STRATTON", "40U7770010", payload)])
        key = p.business_key("BRIGGS&STRATTON", "40U7770010")
        cohort, targets, stats, _ = p.build_classification_cohort(
            store,
            processed=set(),
            manual=set(),
            hard_lock=set(),
            allowed_keys={key},
            allow_safe_auxiliary_rescue=True,
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(stats["classification_safe_aux_rescue_available"], 1)

    def test_compatibility_auxiliary_does_not_enter_rescue_queue(self):
        payload = {
            "results": [],
            "_collection": {
                "state": "NO_TARGET_EVIDENCE",
                "curated_results": [],
                "evidence_cache": [
                    {
                        "tier": "AUXILIARY_REVIEW_ONLY",
                        "identity": "STRONG",
                        "decision": "REJECT_COMPATIBILITY_ONLY",
                        "eligible_for_llm_rescue": True,
                    }
                ],
            },
        }
        store = FakeStore([("TEST", "ABC123", payload)])
        key = p.business_key("TEST", "ABC123")
        _, targets, stats, _ = p.build_classification_cohort(
            store,
            processed=set(),
            manual=set(),
            hard_lock=set(),
            allowed_keys={key},
            allow_safe_auxiliary_rescue=True,
        )
        self.assertEqual(targets, [])
        self.assertEqual(stats["classification_no_evidence_skipped"], 1)

    def test_classification_cohort_is_scoped_to_current_input(self):
        ready = {
            "results": [],
            "_collection": {
                "state": "READY_FOR_CLASSIFICATION",
                "curated_results": [{"title": "x"}],
                "evidence_cache": [],
            },
        }
        store = FakeStore([
            ("TEST", "A100", ready),
            ("TEST", "B200", ready),
        ])
        allowed = {p.business_key("TEST", "B200")}
        _, targets, stats, _ = p.build_classification_cohort(
            store,
            processed=set(),
            manual=set(),
            hard_lock=set(),
            allowed_keys=allowed,
        )
        self.assertEqual([x[1] for x in targets], ["B200"])
        self.assertEqual(stats["classification_outside_input_skipped"], 1)

    def test_unverified_review_context_excludes_dangerous_rejections(self):
        entries = [
            {
                "tier": "REJECTED",
                "decision": "REJECT_WEAK_IDENTITY",
                "identity": "WEAK",
                "title": "Potential product page",
                "url": "https://example.com/a",
                "evidence_excerpt": "ANTHBOT Genie1000 robot lawn mower",
            },
            {
                "tier": "REJECTED",
                "decision": "REJECT_COMPATIBILITY_ONLY",
                "identity": "STRONG",
                "title": "Compatible blade",
                "url": "https://example.com/b",
                "evidence_excerpt": "compatible with ANTHBOT Genie1000",
            },
        ]
        text = p._llm_unverified_review_context(
            "ANTHBOT", "GENIE 1000 EU", entries, max_sources=4
        )
        self.assertIn("Potential product page", text)
        self.assertNotIn("Compatible blade", text)
        self.assertIn("UNVERIFIED REVIEW CONTEXT", text)


if __name__ == "__main__":
    unittest.main()
