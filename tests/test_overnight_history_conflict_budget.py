"""Offline regressions for historical conflicting account reservations.

Reproduces the failure involving dca|ajet15 without network or production files.
"""
import json
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pipeline_produtos import Slot, DurableRouter, import_prior_reservations


def make_ledger(path, reservations):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE collection_attempt_reservations (
            business_key TEXT PRIMARY KEY, account TEXT NOT NULL,
            provider TEXT NOT NULL, reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.executemany(
            "INSERT INTO collection_attempt_reservations "
            "(business_key,account,provider,reserved_at) VALUES (?,?,?,?)",
            [(key, account, provider, "2026-11-20 09:00:00")
             for key, account, provider in reservations])
        conn.commit()


def make_history(root, name, reservations, cohort=("dca|ajet15",)):
    folder = root / name
    folder.mkdir()
    ledger = folder / "reservations.sqlite"
    make_ledger(ledger, reservations)
    (folder / "run_report.json").write_text(
        json.dumps({"stages": {"collection": {"attempts": 1}},
                    "cohort_keys": list(cohort)}), encoding="utf-8")
    (folder / "isolated_config.json").write_text(
        json.dumps({"files": {"classification_db": str(ledger)}}), encoding="utf-8")
    return ledger


class OvernightHistoricalReservationConflictTests(unittest.TestCase):
    def test_production_historical_conflict_preserves_primary_and_charges_secondary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [("dca|ajet15", "tavily:1", "tavily")])
            history = root / "runs"
            history.mkdir()
            make_history(history, "run_01", [("dca|ajet15", "tavily:2", "tavily")])
            stats = import_prior_reservations(production, history)
            self.assertEqual(stats["historical_account_conflicts"], 1)
            with closing(sqlite3.connect(production)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT account FROM collection_attempt_reservations "
                    "WHERE business_key='dca|ajet15'").fetchone()[0], "tavily:1")
                self.assertEqual(conn.execute(
                    "SELECT account FROM collection_attempt_reservation_exposures "
                    "WHERE business_key='dca|ajet15'").fetchall(), [("tavily:2",)])
            slots = [
                Slot("tavily", "tavily:1", "TAVILY_API_KEY_1", "", 1, "g1"),
                Slot("tavily", "tavily:2", "TAVILY_API_KEY_2", "", 1, "g2"),
                Slot("tavily", "tavily:3", "TAVILY_API_KEY_3", "", 1, "g3"),
            ]
            router = DurableRouter(production, slots)
            try:
                self.assertTrue(router.already_reserved("dca|ajet15"))
                self.assertIsNone(router.choose("dca|ajet15"))
                self.assertEqual(router.reserve("fresh-sku").account, "tavily:3")
            finally:
                router.close()

    def test_import_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [("dca|ajet15", "tavily:1", "tavily")])
            history = root / "runs"
            history.mkdir()
            make_history(history, "run_01", [("dca|ajet15", "tavily:2", "tavily")])
            import_prior_reservations(production, history)
            import_prior_reservations(production, history)
            with closing(sqlite3.connect(production)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservation_exposures").fetchone()[0], 1)

    def test_shared_billing_group_counts_conflict_exposures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [("dca|ajet15", "tavily:1", "tavily")])
            history = root / "runs"
            history.mkdir()
            make_history(history, "run_01", [("dca|ajet15", "tavily:2", "tavily")])
            import_prior_reservations(production, history)
            slots = [
                Slot("tavily", "tavily:1", "TAVILY_API_KEY_1", "", 2, "shared"),
                Slot("tavily", "tavily:2", "TAVILY_API_KEY_2", "", 2, "shared"),
            ]
            router = DurableRouter(production, slots)
            try:
                self.assertIsNone(router.reserve("new-sku"))
            finally:
                router.close()

    def test_conflicting_historical_runs_preserve_both_exposures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [])
            history = root / "runs"
            history.mkdir()
            make_history(history, "run_01", [("dca|ajet15", "tavily:1", "tavily")])
            make_history(history, "run_02", [("dca|ajet15", "tavily:2", "tavily")])
            stats = import_prior_reservations(production, history)
            self.assertEqual(stats["historical_account_conflicts"], 1)
            with closing(sqlite3.connect(production)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservations "
                    "WHERE business_key='dca|ajet15'").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservation_exposures "
                    "WHERE business_key='dca|ajet15'").fetchone()[0], 1)

    def test_nonlive_preflight_runs_same_import_and_preserves_production(self):
        import pipeline_produtos as pp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [("dca|ajet15", "tavily:1", "tavily")])
            history = root / "runs"
            history.mkdir()
            make_history(history, "run_01", [("dca|ajet15", "tavily:2", "tavily")])
            args = SimpleNamespace(
                execute_live=False, campaign_resume_dir="", resume_dir="",
                night_include_raw_missing=True, allow_prior_serper_quota_error=False,
                night_allow_previous_errors=False,
            )
            audit = {
                "candidate_rows": [], "error_rows": [],
                "missing_rows": [{"business_key": "new-sku", "brand": "A", "model": "B"}],
            }
            before = production.read_bytes()
            buffer = io.StringIO()
            with patch.object(pp, "OUT", history), \
                 patch.object(pp, "source_paths", return_value={
                     "input": root / "input.xlsx", "cache": root / "cache.sqlite",
                     "master": root / "master.xlsx", "final": root / "final.xlsx",
                     "classification_db": production}), \
                 patch.object(pp, "overnight_audit", return_value=audit), \
                 redirect_stdout(buffer):
                result = pp.overnight_campaign(args, {})
            self.assertEqual(result, 0)
            output = json.loads(buffer.getvalue())
            self.assertEqual(output["eligible"], 1)
            self.assertEqual(output["historical_reservations"]["historical_account_conflicts"], 1)
            self.assertEqual(output["search_calls"], 0)
            self.assertEqual(before, production.read_bytes())

    def test_missing_prior_ledger_blocks_before_new_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "production.sqlite"
            make_ledger(production, [])
            history = root / "runs"
            history.mkdir()
            folder = history / "run_01"
            folder.mkdir()
            (folder / "run_report.json").write_text(
                json.dumps({"stages": {"collection": {"attempts": 1}},
                            "cohort_keys": ["dca|ajet15"]}), encoding="utf-8")
            (folder / "isolated_config.json").write_text(
                json.dumps({"files": {"classification_db": str(folder / "missing.sqlite")}}),
                encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Missing previous reservation ledger"):
                import_prior_reservations(production, history)


if __name__ == "__main__":
    unittest.main()
