import unittest

import pipeline_produtos as p


POLICY = {
    "identity": {"direct_accept_levels": ["STRONG", "PROBABLE"]},
    "isolation": {
        "target_window_chars": 850,
        "minimum_block_chars": 12,
        "max_other_skus_after_isolation": 0,
        "chunk_separator_regex": r"\s*\[\.\.\.\]\s*",
        "allow_dedicated_page_chunks_without_repeated_sku": True,
    },
    "compatibility_terms": ["compatible with", "replacement for", "fits model"],
    "related_section_terms": ["related products", "similar products"],
    "generic_page_terms": ["catalog", "product list"],
    "body_identity_rescue": {
        "enabled": True,
        "identity_window_chars": 2400,
        "require_brand_near_model": True,
        "brand_model_max_distance_chars": 500,
        "max_identity_level": "PROBABLE",
        "block_compatibility_only": True,
        "block_related_only": True,
    },
    "persistence": {
        "persist_target_evidence_window": True,
        "evidence_excerpt_chars": 1200,
        "max_cached_sources": 10,
        "raw_content_full_persisted": False,
    },
    "cleaning": {
        "drop_duplicate_lines": True,
        "max_boilerplate_line_chars": 180,
        "max_target_occurrences": 2,
        "boilerplate_terms": ["cookie policy", "all rights reserved", "menu"],
        "boilerplate_line_regexes": [r"^copyright\b"],
    },
    "source_decisions": [
        "ACCEPT_TARGET_CLEAN",
        "ACCEPT_TARGET_ISOLATED",
        "REJECT_WRONG_MODEL",
        "REJECT_COMPATIBILITY_ONLY",
        "REJECT_RELATED_ONLY",
        "REJECT_GENERIC_NO_TARGET_BLOCK",
        "REJECT_NON_ISOLATABLE",
        "REJECT_WEAK_IDENTITY",
        "REJECT_EXTRACT_FAILED",
        "ACCEPT_SOURCE_SIEVE",
        "ACCEPT_SOURCE_SIEVE_BODY_RESCUE",
    ],
}

CFG = {
    "_collection_policy": POLICY,
    "identity": {
        "regional_suffixes": [],
        "short_model_requires_brand_length": 5,
    },
    "evidence": {"max_results": 5, "content_chars": 2400},
}


class EvidenceCacheV407Tests(unittest.TestCase):
    def test_cleaner_removes_html_boilerplate_and_duplicates(self):
        raw = """
        <nav>Menu</nav>
        <script>alert('x')</script>
        <h1>STIHL FS 120 petrol brushcutter</h1>
        STIHL FS 120 petrol brushcutter
        Cookie Policy
        <footer>All rights reserved</footer>
        """
        cleaned = p._clean_semantic_text(raw, POLICY)
        self.assertIn("STIHL FS 120 petrol brushcutter", cleaned)
        self.assertNotIn("alert", cleaned)
        self.assertNotIn("Cookie Policy", cleaned)
        self.assertNotIn("All rights reserved", cleaned)
        self.assertEqual(cleaned.count("STIHL FS 120 petrol brushcutter"), 1)

    def test_body_identity_rescue_is_probable_not_strong(self):
        raw = (
            "Official product data. STIHL FS 120 professional petrol brushcutter "
            "with 30.8 cm3 displacement and bike handle."
        )
        rescue = p._body_identity_rescue("STIHL", "FS 120", raw, CFG, POLICY)
        self.assertIsNotNone(rescue)
        self.assertEqual(rescue["level"], "PROBABLE")
        self.assertEqual(rescue["identity_origin"], "BODY_RESCUE")

    def test_body_identity_rescue_blocks_compatibility_only(self):
        raw = "Replacement guard compatible with STIHL FS 120. Spare part only."
        rescue = p._body_identity_rescue("STIHL", "FS 120", raw, CFG, POLICY)
        self.assertIsNone(rescue)

    def test_persistence_drops_full_raw_content(self):
        rows = [{
            "title": "<b>STIHL FS 120</b>",
            "url": "https://example.com/fs120",
            "content": "STIHL FS 120 petrol brushcutter",
            "raw_content": "FULL BODY THAT MUST NOT BE PERSISTED",
        }]
        persisted = p._prepare_results_for_persistence(rows, CFG)
        self.assertEqual(persisted[0]["raw_content"], None)
        self.assertTrue(persisted[0]["_raw_content_available"])
        self.assertNotIn("<b>", persisted[0]["title"])

    def test_evidence_cache_separates_primary_and_auxiliary(self):
        rows = [
            {
                "title": "STIHL FS 120 Brushcutter",
                "url": "https://example.com/official",
                "content": "STIHL FS 120 petrol brushcutter",
                "score": 0.9,
                "raw_content": "STIHL FS 120 petrol brushcutter 30.8 cm3",
                "_provider_position": 1,
            },
            {
                "title": "FS 120 spare parts",
                "url": "https://example.net/parts",
                "content": "Replacement parts for STIHL FS 120",
                "score": 0.5,
                "raw_content": "Replacement parts compatible with STIHL FS 120",
                "_provider_position": 2,
            },
        ]
        audits = [
            {
                "url": "https://example.com/official",
                "decision": "ACCEPT_TARGET_CLEAN",
                "identity_after": "STRONG",
                "quality_flags": [],
            },
            {
                "url": "https://example.net/parts",
                "decision": "REJECT_COMPATIBILITY_ONLY",
                "identity_after": "WEAK",
                "quality_flags": ["COMPATIBILITY_ONLY"],
            },
        ]
        curated = [{
            "title": "STIHL FS 120 Brushcutter",
            "url": "https://example.com/official",
            "content": "STIHL FS 120 petrol brushcutter 30.8 cm3",
            "score": 0.9,
            "_collection_identity": "STRONG",
            "_collection_source_mode": "TAVILY_SEARCH_RAW_CONTENT",
        }]
        entries = p._build_evidence_cache_entries(
            "STIHL", "FS 120", rows, audits, curated, CFG, "tavily"
        )
        self.assertEqual(entries[0]["tier"], "PRIMARY")
        rejected = [x for x in entries if x["url"].endswith("/parts")]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["tier"], "REJECTED")
        self.assertFalse(rejected[0]["eligible_for_llm_rescue"])

    def test_policy_contract_rejects_full_raw_persistence(self):
        bad = dict(POLICY)
        bad["persistence"] = dict(POLICY["persistence"])
        bad["persistence"]["raw_content_full_persisted"] = True
        errors = p.validate_collection_policy(bad)
        self.assertTrue(any("raw_content_full_persisted" in x for x in errors))


if __name__ == "__main__":
    unittest.main()
