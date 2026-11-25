"""Regression tests for real historical reservation ledgers and Stage1 POWER audit.
All tests use only disposable SQLite and JSON files. No API calls.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import fields
from pathlib import Path

from pipeline_produtos import Stage1Result, import_prior_reservations


ROOT = Path(__file__).resolve().parents[1]


def history_run(root: Path, label: str, keys: list[str],
                reservations: list[tuple[str, str, str]]) -> None:
    folder = root / label
    folder.mkdir()
    sqlite = folder / "history.sqlite"
    with closing(sqlite3.connect(sqlite)) as connection, connection:
        connection.execute(
            "CREATE TABLE collection_attempt_reservations ("
            "business_key TEXT PRIMARY KEY, account TEXT, provider TEXT)"
        )
        connection.executemany(
            "INSERT INTO collection_attempt_reservations VALUES (?,?,?)",
            reservations,
        )
    (folder / "run_report.json").write_text(
        json.dumps({
            "stages": {"collection": {"attempts": 1}},
            "cohort_keys": keys,
        }), encoding="utf-8",
    )
    (folder / "isolated_config.json").write_text(
        json.dumps({"files": {"classification_db": str(sqlite)}}),
        encoding="utf-8",
    )


class HistoricalReservationTests(unittest.TestCase):
    def test_overlapping_cohorts_with_matching_reservations_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_run(root, "first", ["A|123"], [("A|123", "tavily:1", "tavily")])
            history_run(root, "second", ["A|123", "B|456"], [
                ("A|123", "tavily:1", "tavily"),
                ("B|456", "brightdata:1", "brightdata"),
            ])
            destination = root / "new.sqlite"
            result = import_prior_reservations(destination, root)
            self.assertEqual(result["historical_cohort_keys"], 2)
            self.assertEqual(result["historical_overlapping_keys"], 1)
            self.assertEqual(result["historical_reservation_keys"], 2)
            with closing(sqlite3.connect(destination)) as db, db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservations"
                ).fetchone()[0], 2)

    def test_conflicting_account_provider_is_charged_not_hidden(self):
        """A SKU attempted on two accounts stays reserved ONCE (never re-queried)
        and BOTH account exposures are charged. Superseded the old hard failure;
        see test_overnight_history_conflict_budget for the budget side."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_run(root, "first", ["A|123"], [("A|123", "tavily:1", "tavily")])
            history_run(root, "second", ["A|123"], [("A|123", "brightdata:1", "brightdata")])
            stats = import_prior_reservations(root / "new.sqlite", root)
            self.assertEqual(stats["historical_account_conflicts"], 1)
            self.assertEqual(stats["historical_extra_account_exposures"], 1)
            with closing(sqlite3.connect(root / "new.sqlite")) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0], 1)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservation_exposures").fetchone()[0], 1)

    def test_ledger_without_reserved_at_still_blocks_requery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_run(root, "legacy", ["A|123"], [("A|123", "tavily:1", "tavily")])
            import_prior_reservations(root / "new.sqlite", root)
            with closing(sqlite3.connect(root / "new.sqlite")) as db:
                row = db.execute("SELECT account,reserved_at FROM collection_attempt_reservations").fetchone()
            self.assertEqual(row[0], "tavily:1")
            self.assertTrue(row[1])

    def test_missing_current_run_reservation_never_borrows_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_run(root, "first", ["A|123"], [("A|123", "tavily:1", "tavily")])
            history_run(root, "second", ["A|123"], [])
            with self.assertRaisesRegex(RuntimeError, "Unaccounted previous collection attempts"):
                import_prior_reservations(root / "new.sqlite", root)

    def test_duplicate_inside_same_run_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_run(root, "first", ["A|123", "A|123"],
                        [("A|123", "tavily:1", "tavily")])
            with self.assertRaisesRegex(RuntimeError, "Invalid earlier campaign cohort"):
                import_prior_reservations(root / "new.sqlite", root)


class PowerSupportTests(unittest.TestCase):
    def test_stage1_declares_and_retains_power_support(self):
        names = {field.name for field in fields(Stage1Result)}
        self.assertIn("power_support", names)
        result = Stage1Result(
            state="AMBIGUOUS", product_type="BLOWER", power=None,
            type_candidates=["BLOWER"], power_candidates=["PETROL"],
            sources=[], reasons=[], power_support={"PETROL": 2},
        )
        self.assertEqual(result.power_support["PETROL"], 2)

    def test_stage1_classification_passes_calculated_power_support(self):
        source = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        matches = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Stage1Result"
        ]
        self.assertTrue(matches, "Stage1Result constructor not found")
        self.assertTrue(any(
            any(keyword.arg == "power_support"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "power_support"
                for keyword in call.keywords)
            for call in matches
        ), "calculated power_support discarded before result")


if __name__ == "__main__":
    unittest.main()
