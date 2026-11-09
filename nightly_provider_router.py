"""Quota scheduler for distinct products, with durable SQLite reservations.

No retry and no fallback for an already-attempted SKU. Explicit local budgets
are required. A reservation is committed *before* any network call; ambiguous
failures are counted conservatively. No credentials are stored in SQLite.
"""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Slot:
    provider: str
    account: str
    key_env: str
    zone_env: str
    limit: int
    group: str

def configured_slots() -> list[Slot]:
    slots = []
    for provider, prefix in (("tavily","TAVILY"),("brightdata","BRIGHTDATA"),("serper","SERPER")):
        for n in (1, 2, 3, 4):
            suffix = f"_{n}"
            key_env = f"{prefix}_API_KEY{suffix}"
            if n == 1 and not os.getenv(key_env):
                key_env = f"{prefix}_API_KEY"
            if not os.getenv(key_env):
                continue
            limit_raw = os.getenv(f"{prefix}_LIMIT{suffix}", "")
            if not limit_raw:
                # An unconfigured cap must NEVER imply unlimited spending.
                continue
            try:
                limit = int(limit_raw)
            except ValueError:
                raise ValueError(f"Invalid request budget: {prefix}_LIMIT{suffix}")
            if limit <= 0:
                continue
            zone_env = f"{prefix}_ZONE{suffix}"
            if provider == "brightdata" and not os.getenv(zone_env):
                zone_env = "BRIGHTDATA_ZONE"
            if provider == "brightdata" and not os.getenv(zone_env):
                continue
            group = os.getenv(f"{prefix}_BILLING_GROUP{suffix}", f"{provider}:{n}")
            slots.append(Slot(provider, f"{provider}:{n}", key_env,zone_env,limit,group))
    return slots

class DurableRouter:
    def __init__(self, path: Path, slots: list[Slot]):
        if not slots:
            raise ValueError("No accounts with positive explicitly configured budget")
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS collection_attempt_reservations (
            business_key TEXT PRIMARY KEY, account TEXT NOT NULL,
            provider TEXT NOT NULL, reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS collection_disabled_accounts (
            account TEXT PRIMARY KEY, reason TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.commit()
        self.slots=slots

    def choose(self, key: str) -> Optional[Slot]:
        if self.conn.execute("SELECT 1 FROM collection_attempt_reservations WHERE business_key=?", (key,)).fetchone():
            return None
        for slot in self.slots:
            if self.conn.execute("SELECT 1 FROM collection_disabled_accounts WHERE account=?", (slot.account,)).fetchone():
                continue
            used = self.conn.execute("SELECT COUNT(*) FROM collection_attempt_reservations WHERE account=?", (slot.account,)).fetchone()[0]
            if used >= slot.limit:
                continue
            # A shared billing group requires *the same total cap* configured on each key.
            group_slots = [s for s in self.slots if s.group == slot.group]
            group_limit = min(s.limit for s in group_slots)
            group_used = self.conn.execute(
                "SELECT COUNT(*) FROM collection_attempt_reservations WHERE account IN (" +
                ",".join("?" for _ in group_slots) + ")",
                [s.account for s in group_slots]).fetchone()[0]
            if group_used >= group_limit:
                continue
            return slot
        return None

    def reserve(self, key: str) -> Optional[Slot]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            slot = self.choose(key)
            if slot:
                self.conn.execute(
                    "INSERT INTO collection_attempt_reservations(business_key,account,provider) VALUES(?,?,?)",
                    (key,slot.account,slot.provider))
            self.conn.commit()
            return slot
        except BaseException:
            self.conn.rollback()
            raise

    def disable(self, account: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO collection_disabled_accounts(account,reason) VALUES(?,?) "
            "ON CONFLICT(account) DO UPDATE SET reason=excluded.reason,updated_at=CURRENT_TIMESTAMP",
            (account,reason))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
