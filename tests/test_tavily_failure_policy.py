"""Tavily failure policy at scale. No HTTP, no Excel, disposable SQLite only.

Contract (policy kept: <=1 Search per SKU, NO automatic retry/fallback):
  * HTTP 429 is a RATE LIMIT, not exhausted quota: it must pause the run but
    never disable the account (432/433 are the real quota/plan limits).
  * Consecutive provider failures open a circuit breaker so one outage cannot
    tombstone and bill an entire batch.
  * Serper reservations with a CONFIRMED "not enough credits" response are the
    only attempts that may be re-queued for Tavily, and only with an audit event.
"""
import argparse
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

import pipeline_produtos as p


def _post(status, headers=None):
    response = MagicMock()
    response.status_code = status
    response.text = "body"
    response.headers = headers or {}
    response.json.return_value = {"results": []}
    return response


def _call_post(status, headers=None):
    with patch.object(requests, "post", return_value=_post(status, headers)):
        return p._provider_post_once(
            "tavily", "https://api.tavily.com/search", {}, {}, 5,
            p.RateLimiter(0), p.CollectionRequestBudget("tavily"))


def _good_payload():
    return {"results": [{"title": "ok"}],
            "_collection": {"state": "COLLECTED", "curated_results": [], "source_audit": [],
                            "query_audit": [], "usage": {"requests": 1}}}


class StatusClassificationTests(unittest.TestCase):
    def test_429_is_rate_limit_not_quota(self):
        with self.assertRaises(p.OperationalPause) as ctx:
            _call_post(429, {"Retry-After": "7"})
        self.assertEqual(ctx.exception.code, "TAVILY_RATE_LIMITED")
        self.assertFalse(ctx.exception.code.endswith("_LIMIT"))
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_432_and_433_remain_quota_limits(self):
        for status in (432, 433):
            with self.assertRaises(p.OperationalPause) as ctx:
                _call_post(status)
            self.assertEqual(ctx.exception.code, "TAVILY_LIMIT")

    def test_5xx_and_timeout_are_not_operational_pauses(self):
        for status in (500, 502, 504):
            with self.assertRaises(RuntimeError) as ctx:
                _call_post(status)
            self.assertNotIsInstance(ctx.exception, p.OperationalPause)


class PoolLoopTests(unittest.TestCase):
    def _run(self, db, skus, search):
        plan = SimpleNamespace(candidates=[
            SimpleNamespace(brand_original="B", model_original=m, raw_state="RAW_MISSING")
            for m in skus], summary={})
        args = argparse.Namespace(
            search_provider="tavily", auto_providers=False, tavily_pool=True,
            input="input.xlsx", cache=str(Path(db).parent / "cache.sqlite"), db=str(db),
            master="", final="", precollection_output="", recollect_empty=False,
            recollect_low_evidence=False, max_tavily_new=20, yes=True)
        cfg = {"files": {"input": "input.xlsx", "cache": args.cache,
                         "classification_db": str(db),
                         "collection_report": str(Path(db).parent / "r.xlsx")}}
        slot = p.Slot("tavily", "tavily:1", "TAVILY_API_KEY", "", 100, "tavily:1")
        calls = []

        def fake(brand, model, cfg_, limiter, provider):
            calls.append(model)
            return search(model, len(calls))

        with patch.object(p, "validate_collection_cost_policy", return_value=[]), \
             patch.object(p, "load_baseline_inventory", return_value={"processed": set()}), \
             patch.object(p, "build_precollection_plan", return_value=plan), \
             patch.object(p, "configured_slots", return_value=[slot]), \
             patch.object(p, "search_product_once", side_effect=fake), \
             patch.object(p, "TavilyWritableStore"), \
             patch.object(p, "_export_collection_quality_report"):
            completed = p.collect_products(args, cfg, SimpleNamespace(hash="t"))
        return completed, calls

    def _rows(self, db, sql):
        with closing(sqlite3.connect(db)) as cx:
            return cx.execute(sql).fetchall()

    def test_rate_limit_pauses_pool_but_never_disables_the_account(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite"

            def search(model, n):
                raise p.OperationalPause("TAVILY_RATE_LIMITED", "HTTP 429", retry_after=5)

            completed, calls = self._run(db, ["1", "2", "3"], search)
            self.assertFalse(completed)
            self.assertEqual(calls, ["1"])  # paused: later SKUs never touched
            self.assertEqual(self._rows(db, "SELECT account FROM collection_disabled_accounts"), [])
            # Policy preserved: the attempted SKU stays reserved (no auto retry).
            self.assertEqual([r[0] for r in self._rows(
                db, "SELECT business_key FROM collection_attempt_reservations")], ["b|1"])

    def test_real_quota_limit_still_disables_account_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite"

            def search(model, n):
                raise p.OperationalPause("TAVILY_LIMIT", "HTTP 432")

            completed, calls = self._run(db, ["1", "2"], search)
            self.assertFalse(completed)
            self.assertEqual(calls, ["1"])
            self.assertEqual(self._rows(db, "SELECT account FROM collection_disabled_accounts"),
                             [("tavily:1",)])

    def test_circuit_breaker_stops_after_three_consecutive_provider_failures(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite"

            def search(model, n):
                raise RuntimeError("TAVILY HTTP 502: bad gateway")

            completed, calls = self._run(db, [str(i) for i in range(1, 9)], search)
            self.assertFalse(completed)
            self.assertEqual(len(calls), 3)  # 5 SKUs are NOT burned
            self.assertEqual(len(self._rows(
                db, "SELECT 1 FROM collection_attempt_reservations")), 3)
            events = [r[0] for r in self._rows(
                db, "SELECT event_type FROM events WHERE event_type='PROVIDER_CIRCUIT_OPEN'")]
            self.assertEqual(events, ["PROVIDER_CIRCUIT_OPEN"])

    def test_circuit_breaker_resets_after_a_success(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite"
            pattern = {1: "F", 2: "F", 3: "S", 4: "F", 5: "F", 6: "S", 7: "S"}

            def search(model, n):
                if pattern[n] == "F":
                    raise RuntimeError("TAVILY REQUEST_OUTCOME_UNKNOWN: Timeout")
                return _good_payload()

            completed, calls = self._run(db, [str(i) for i in range(1, 8)], search)
            self.assertTrue(completed)
            self.assertEqual(len(calls), 7)


class SerperQuotaReleaseTests(unittest.TestCase):
    def _ledger(self, path):
        with closing(sqlite3.connect(path)) as cx, cx:
            cx.executescript("""
                CREATE TABLE collection_attempt_reservations (
                    business_key TEXT PRIMARY KEY, account TEXT, provider TEXT,
                    reserved_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    business_key TEXT, created_at TEXT, event_type TEXT, detail TEXT);
            """)
            for key, account, provider in (
                ("quota", "single:serper", "serper"), ("timeout", "single:serper", "serper"),
                ("tav", "tavily:1", "tavily"), ("saved", "single:serper", "serper")):
                cx.execute("INSERT INTO collection_attempt_reservations(business_key,account,provider)"
                           " VALUES(?,?,?)", (key, account, provider))
            for key, typ, detail in (
                ("quota", "SEARCH_R9.6_ERROR_NO_RETRY", "SERPER HTTP 400: not enough credits"),
                ("timeout", "SEARCH_R9.6_ERROR_NO_RETRY", "SERPER REQUEST_OUTCOME_UNKNOWN: Timeout"),
                ("tav", "SERPER_LIMIT", "x"),
                ("saved", "SERPER_LIMIT", "x"), ("saved", "SEARCH_R9.6_SAVED", "ok")):
                cx.execute("INSERT INTO events(run_id,business_key,created_at,event_type,detail)"
                           " VALUES('r',?,'2026-11-01',?,?)", (key, typ, detail))

    def test_only_confirmed_serper_quota_failures_are_releasable(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "l.sqlite"
            self._ledger(db)
            self.assertEqual(p.confirmed_serper_quota_keys(db), {"quota"})

    def test_release_is_scoped_audited_and_leaves_everything_else(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "l.sqlite"
            self._ledger(db)
            released = p.release_confirmed_serper_quota_reservations(db, {"quota", "timeout", "tav"})
            self.assertEqual(released, 1)
            with closing(sqlite3.connect(db)) as cx:
                left = {r[0] for r in cx.execute("SELECT business_key FROM collection_attempt_reservations")}
                audit = cx.execute("SELECT business_key FROM events WHERE event_type="
                                   "'SEARCH_RESERVATION_RECOVERED_SERPER_QUOTA'").fetchall()
            self.assertEqual(left, {"timeout", "tav", "saved"})
            self.assertEqual(audit, [("quota",)])

    def test_selector_reaches_released_serper_sku_only_with_authorization(self):
        audit = {"candidate_rows": [], "missing_rows": [],
                 "error_rows": [{"business_key": "quota", "provider": "serper",
                                 "prior_state": "REQUEST_ATTEMPTED_NO_AUTO_RETRY",
                                 "previous_error": "SERPER HTTP 400: not enough credits"}]}
        blocked, _ = p.select_overnight_eligible(audit, {"quota"}, allow_serper_quota_error=True)
        self.assertEqual(blocked, [])  # old behaviour: reservation hides it
        chosen, _ = p.select_overnight_eligible(audit, set(), allow_serper_quota_error=True)
        self.assertEqual(len(chosen), 1)
        chosen, _ = p.select_overnight_eligible(audit, set(), allow_serper_quota_error=False)
        self.assertEqual(chosen, [])


if __name__ == "__main__":
    unittest.main()
