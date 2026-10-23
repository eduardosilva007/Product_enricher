import json
import tempfile
import unittest
from pathlib import Path

import pipeline_produtos as p


BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "pipeline_config.json").read_text(encoding="utf-8"))
POLICY = json.loads((BASE / "collection_quality_policy.json").read_text(encoding="utf-8"))
CFG["_collection_policy"] = POLICY
RULES = p.Rules(BASE / "regras_negocio.json")


class FakeDF:
    def __init__(self, columns):
        self.columns = list(columns)


class FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.by_key = {
            p.business_key(brand, model): payload
            for brand, model, payload in self.rows
        }

    def catalog(self):
        return [
            (brand, model, p.business_key(brand, model))
            for brand, model, _ in self.rows
        ]

    def get(self, brand, model):
        payload = self.by_key[p.business_key(brand, model)]
        return p.RawEvidence("RAW_OK", brand, model, payload=payload)


def curated_payload(rows):
    return {
        "results": [],
        "_collection": {
            "state": "READY_FOR_CLASSIFICATION",
            "curated_results": rows,
            "evidence_cache": [],
        },
    }


def curated(title, url, content, identity="STRONG", decision="ACCEPT_TARGET_CLEAN"):
    return {
        "title": title,
        "url": url,
        "content": content,
        "score": 0.9,
        "_collection_identity": identity,
        "_collection_decision": decision,
        "_collection_quality_flags": [],
        "_collection_source_mode": "TAVILY_SEARCH_RAW_CONTENT",
    }


class FlowStateClassifierV408Tests(unittest.TestCase):
    def test_release_and_flow_contracts_validate_together(self):
        self.assertEqual(p.validate_collection_policy(POLICY), [])
        self.assertEqual(RULES.validate(), [])
        self.assertEqual(p.validate_release_contract(CFG, RULES, POLICY), [])
        self.assertEqual(p.validate_classification_flow_policy(CFG), [])
        self.assertEqual(p.validate_identity_runtime_contract(CFG), [])

    def test_classified_clef_is_pending_review_not_accepted(self):
        rec = {
            "Resolution State": "CLASSIFIED_CLEF",
            "Review Required": "SIM",
            "O que é (Tipo)": "ENGINE",
            "Category General": "ENGINES",
            "FAMILY": "ENGINE",
            "Classification Identity": "STRONG",
        }
        self.assertEqual(
            p._record_lifecycle_state(rec),
            p.LIFECYCLE_CLASSIFIED_PENDING_REVIEW,
        )
        self.assertFalse(p._record_is_accepted(rec))
        self.assertEqual(p._record_process_state(rec), ("PROCESSADO", "PROVÁVEL"))

    def test_probable_roundtrip_is_not_baseline_accepted(self):
        df = FakeDF([
            "Marca", "Modelo", "O que é (Tipo)", "Category General", "FAMILY",
            "Status", "Revisão", "Revisão B.I", "Lifecycle State",
            "Resolution State", "Review Required",
        ])
        row = {
            "Marca": "BRIGGS & STRATTON",
            "Modelo": "44S9770046G1AF0001",
            "O que é (Tipo)": "ENGINE",
            "Category General": "ENGINES",
            "FAMILY": "ENGINE",
            "Status": "Script",
            "Revisão": "PROVÁVEL",
            "Revisão B.I": "",
            "Lifecycle State": "CLASSIFIED_PENDING_REVIEW",
            "Resolution State": "CLASSIFIED_CLEF",
            "Review Required": "SIM",
        }
        self.assertEqual(
            p._baseline_row_lifecycle(row, df),
            p.LIFECYCLE_CLASSIFIED_PENDING_REVIEW,
        )
        self.assertFalse(p._baseline_row_accepted(row, df))
        row["Revisão B.I"] = "OK"
        self.assertEqual(
            p._baseline_row_lifecycle(row, df),
            p.LIFECYCLE_ACCEPTED_HUMAN,
        )
        self.assertTrue(p._baseline_row_accepted(row, df))

    def test_engine_taxonomy_does_not_leak_brand_specific_pairs(self):
        generic = p._taxonomy_pairs_for_type(RULES, "ENGINE")
        briggs = p._taxonomy_pairs_for_type(
            RULES, "ENGINE", "BRIGGS & STRATTON", "500 E"
        )
        koop = p._taxonomy_pairs_for_type(RULES, "ENGINE", "KOOP", "192F")
        self.assertIn(("ENGINES", "ENGINE"), generic)
        self.assertNotIn(("OTHERS", "ENGINE"), generic)
        self.assertNotIn(("OTHERS", "ENGINE"), briggs)
        self.assertIn(("OTHERS", "ENGINE"), koop)

    def test_semantic_guard_cannot_reintroduce_part_from_parent_parts_page(self):
        src = p.Source(
            1,
            "Briggs And Stratton 550ex Parts",
            "replacement parts for 550EX",
            "https://example.com/550ex-parts",
            "STRONG",
            quality_flags=["PARTS_CATALOG_TITLE"],
        )
        s1 = p.Stage1Result(
            state="UNRESOLVED",
            product_type=None,
            power=None,
            type_candidates=[],
            power_candidates=[],
            sources=[src],
            reasons=[],
        )
        guarded = p.ClefFunnelClassifier._apply_source_purpose_semantic_guard(
            ["PART", "ENGINE", "LAWN MOWER"], s1
        )
        self.assertNotIn("PART", guarded)
        self.assertIn("ENGINE", guarded)

    def test_550ex_parts_catalog_cannot_make_parent_product_part(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS & STRATTON",
            "550 EX",
            curated_payload([
                curated(
                    "Briggs And Stratton 550ex Parts",
                    "https://example.com/briggs-stratton-550ex-parts",
                    "Carburetor, filter and replacement parts for Briggs 550EX.",
                )
            ]),
        )
        self.assertNotIn("PART", result.type_candidates)
        self.assertNotEqual(result.product_type, "PART")

    def test_550ex_official_engine_evidence_wins_over_parts_catalog(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS & STRATTON",
            "550 EX",
            curated_payload([
                curated(
                    "Briggs And Stratton 550ex Parts",
                    "https://example.com/briggs-stratton-550ex-parts",
                    "Replacement parts for Briggs 550EX.",
                ),
                curated(
                    "Briggs & Stratton 550EX Series Engine",
                    "https://www.briggsandstratton.com/products/engines/550ex",
                    "Briggs & Stratton 550EX gasoline engine, 4-stroke.",
                ),
            ]),
        )
        self.assertIn("ENGINE", result.type_candidates)
        self.assertNotIn("PART", result.type_candidates)
        self.assertEqual(result.product_type, "ENGINE")
        self.assertEqual(result.power, "PETROL")
        self.assertEqual(result.taxonomy[:2], ("ENGINES", "ENGINE"))

    def test_44s977_engine_stays_canonical_engine_taxonomy(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS & STRATTON",
            "44S9770046G1AF0001",
            curated_payload([
                curated(
                    "Briggs & Stratton 44S977-0046-G1 Vertical Engine",
                    "https://example.com/44s977-0046-g1",
                    "Briggs & Stratton vertical gasoline engine model 44S977-0046-G1.",
                )
            ]),
        )
        self.assertEqual(result.product_type, "ENGINE")
        self.assertEqual(result.power, "PETROL")
        self.assertEqual(result.taxonomy[:2], ("ENGINES", "ENGINE"))

    def test_all_early_exit_records_receive_lifecycle_metadata(self):
        rec = {
            "Marca": "TEST",
            "Modelo": "M1",
            "Resolution State": "NEEDS_RECOLLECTION",
            "Review Required": "NÃO",
        }
        stamped = p._stamp_classification_record(rec, "cohort-123")
        self.assertEqual(stamped["Lifecycle State"], p.LIFECYCLE_NEEDS_RECOLLECTION)
        self.assertEqual(stamped["Classification Semantic Version"], p.CLASSIFICATION_SEMANTIC_VERSION)
        self.assertEqual(stamped["Collection Cohort ID"], "cohort-123")

    def test_08p602_moteur_is_engine_not_part(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS & STRATTON",
            "08P6020022H5A0096",
            curated_payload([
                curated(
                    "Moteur Briggs & Stratton 08P602-0022-H5",
                    "https://example.com/08p602-0022-h5",
                    "Moteur essence Briggs & Stratton 08P602-0022-H5, 4 temps.",
                )
            ]),
        )
        self.assertIn("ENGINE", result.type_candidates)
        self.assertNotIn("PART", result.type_candidates)
        self.assertEqual(result.product_type, "ENGINE")
        self.assertEqual(result.power, "PETROL")

    def test_40u777_engine_title_does_not_become_part(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS&STRATTON",
            "40U7770010",
            curated_payload([
                curated(
                    "18HP Briggs & Stratton 40U777-0010 Professional Engine",
                    "https://example.com/40u777-0010-g1",
                    "V-Twin gasoline engine. Engine parts are available separately.",
                )
            ]),
        )
        self.assertIn("ENGINE", result.type_candidates)
        self.assertNotIn("PART", result.type_candidates)
        self.assertEqual(result.product_type, "ENGINE")
        self.assertEqual(result.power, "PETROL")

    def test_613477_collection_identity_is_not_downgraded(self):
        engine = p.DeterministicEngine(RULES, CFG)
        result = engine.classify(
            "BRIGGS & STRATTON",
            "6134774205J1EB0001",
            curated_payload([
                curated(
                    "Briggs & Stratton Replacement Engine",
                    "https://example.com/replacement-engine-6134774205j1eb0001",
                    "Briggs & Stratton replacement engine model 613477-4205-J1.",
                    identity="STRONG",
                )
            ]),
        )
        self.assertIn("ENGINE", result.type_candidates)
        self.assertEqual(result.identity_best, "STRONG")
        self.assertNotEqual(result.product_type, "PART")

    def test_authoritative_brand_domain_beats_repair_video_for_real_title(self):
        sources = [
            p.Source(
                1,
                "Repairing Briggs & Stratton 500 E Series Lawn Mower",
                "repair guide",
                "https://www.youtube.com/watch?v=test",
                "STRONG",
                quality_flags=["REPAIR_TITLE", "NEGATIVE_TITLE_TERM"],
                identity_score=100,
                source_weight=1.0,
                tavily_score=0.95,
            ),
            p.Source(
                2,
                "Briggs & Stratton | 500E Series Lawn mower engine",
                "500E Series engine",
                "https://www.briggsandstratton.com/en-gb/products/engines-500e-series",
                "STRONG",
                quality_flags=[],
                identity_score=100,
                source_weight=1.0,
                tavily_score=0.80,
            ),
        ]
        s1 = p.Stage1Result(
            state="AMBIGUOUS",
            product_type="ENGINE",
            power="PETROL",
            type_candidates=["ENGINE"],
            power_candidates=["PETROL"],
            sources=sources,
            reasons=[],
        )
        title, url = p._best_target_title(s1, [1, 2], False, brand="BRIGGS & STRATTON")
        self.assertIn("500E Series", title)
        self.assertIn("briggsandstratton.com", url)

    def test_manifest_scope_yields_exact_same_18_targets_not_historical_cache(self):
        ready = curated_payload([
            curated(
                "TEST product",
                "https://example.com/product",
                "test product engine",
            )
        ])
        cohort_rows = [("TEST", f"M{i:03d}", ready) for i in range(18)]
        historical_rows = [("OLD", f"H{i:03d}", ready) for i in range(7)]
        store = FakeStore(cohort_rows + historical_rows)
        allowed = {p.business_key(brand, model) for brand, model, _ in cohort_rows}

        cohort, targets, stats, _ = p.build_classification_cohort(
            store,
            processed=set(),
            manual=set(),
            hard_lock=set(),
            allowed_keys=allowed,
            allow_safe_auxiliary_rescue=True,
        )

        self.assertEqual(len(cohort), 18)
        self.assertEqual(len(targets), 18)
        self.assertEqual({x[2] for x in targets}, allowed)
        self.assertEqual(stats["classification_outside_input_skipped"], 7)

    def test_manifest_survives_input_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_file = root / "Base de Produtos.xlsx"
            input_file.write_bytes(b"cohort-A")
            rows = []
            for idx in range(18):
                brand = "TEST"
                model = f"M{idx:03d}"
                rows.append(p.InputProductRow(
                    row_number=idx + 2,
                    brand_original=brand,
                    model_original=model,
                    brand_canonical=brand,
                    model_canonical=model,
                    key=p.business_key(brand, model),
                    valid=True,
                    representative=True,
                    raw_state="RAW_OK",
                    action="JA_NO_CACHE",
                ))
            plan = p.PreCollectionPlan(
                input_path=input_file,
                report_path=root / "pre.xlsx",
                rows=rows,
                representatives=rows,
                candidates=[],
                summary={},
            )
            manifest = p._build_collection_manifest(
                plan, CFG, status="NO_SEARCH_NEEDED", provider="tavily"
            )
            final_manifest = p._build_collection_manifest(
                plan, CFG, status="COMPLETED", provider="tavily",
                cohort_id=manifest["cohort_id"],
            )
            self.assertEqual(manifest["cohort_id"], final_manifest["cohort_id"])
            path = root / "manifest.json"
            p._write_collection_manifest(path, manifest)
            input_file.write_bytes(b"now-only-one-product")
            loaded = p._load_collection_manifest(path)
            self.assertEqual(len(loaded["business_keys"]), 18)
            self.assertEqual(
                loaded["cohort_hash"],
                manifest["cohort_hash"],
            )

    def test_checkpoint_id_does_not_change_with_pipeline_excel_version(self):
        original = p.PIPELINE_VERSION
        try:
            first = p.ClassificationDB.checkpoint_id(
                "K", "RAW", "RULES", "clef-route"
            )
            p.PIPELINE_VERSION = "9.9.9-only-excel-change"
            second = p.ClassificationDB.checkpoint_id(
                "K", "RAW", "RULES", "clef-route"
            )
            self.assertEqual(first, second)
        finally:
            p.PIPELINE_VERSION = original


if __name__ == "__main__":
    unittest.main()
