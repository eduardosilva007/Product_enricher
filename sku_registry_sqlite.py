"""Audited Excel -> canonical pipeline_v3.sqlite SKU registry, zero paid APIs.

No new operational files. Neither cache_tavily.sqlite nor classification
checkpoints are changed. Human decisions and physical aliases are preserved.
A normalized key is a LOOKUP ID, not an authorization to merge conflicting SKUs.
"""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
import unicodedata


PUBLIC = ("Marca", "Modelo", "O que é (Tipo)", "Alimentação",
          "Category General", "FAMILY", "Título Real", "Próxima Ação",
          "Status", "Revisão", "Revisão B.I")
TECHNICAL = ("Resolution State", "Review Required", "Classification Identity")


def _clean(value):
    return "" if value is None else str(value).strip()


def _compact(value):
    norm = unicodedata.normalize("NFKD", _clean(value)).casefold()
    return re.sub(r"[^a-z0-9]", "", "".join(
        char for char in norm if not unicodedata.combining(char)))


def _key(brand, model):
    return _compact(brand) + "|" + _compact(model)


def _sha(path):
    h = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path, preferred, source):
    """Linear-time workbook input; no random access in read-only mode."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[preferred] if preferred in wb.sheetnames else None
        if ws is None:
            # Legacy/staged baseline sheets can have arbitrary names.
            # Select by the ACTUAL business columns, never worksheet title.
            for candidate in wb.worksheets:
                headers = next(candidate.iter_rows(min_row=1, max_row=1,
                                                   values_only=True), ())
                if {"Marca", "Modelo"}.issubset({_clean(x) for x in headers}):
                    ws = candidate
                    break
        if ws is None:
            raise RuntimeError("SQLITE_MERGE_BUSINESS_SHEET_MISSING:" + source)
        values = iter(ws.iter_rows(values_only=True))
        try:
            header = [_clean(v) for v in next(values)]
        except StopIteration as exc:
            raise RuntimeError("SQLITE_MERGE_EMPTY_WORKBOOK:" + source) from exc
        if len(set(name for name in header if name)) != len(
                [name for name in header if name]):
            raise RuntimeError("SQLITE_MERGE_DUPLICATE_HEADERS:" + source)
        columns = {name: i for i, name in enumerate(header)}
        if "Marca" not in columns or "Modelo" not in columns:
            raise RuntimeError("SQLITE_MERGE_MISSING_IDENTITY:" + source)
        result = []
        for row_no, row in enumerate(values, 2):
            def get(name):
                i = columns.get(name)
                return _clean(row[i]) if i is not None and i < len(row) else ""
            brand, model = get("Marca"), get("Modelo")
            if not brand or not model:
                continue
            sku_key = _key(brand, model)
            if sku_key == "|":
                raise RuntimeError("SQLITE_MERGE_EMPTY_KEY:" + source)
            record = {name: get(name) for name in PUBLIC + TECHNICAL}
            record.update(row_no=row_no, sku_key=sku_key, source=source)
            result.append(record)
        return result
    finally:
        wb.close()


def _chosen(entries, final_entries):
    """Prefer human-approved exact row, then Final classification, then Master.

    An unapproved Master classification can be exposed as PROPOSAL, never B.I=OK.
    No label is manufactured or pushed into the human Final by this operation.
    """
    def human(row):
        return row["Revisão B.I"].upper() == "OK" or row["Status"].upper() == "MANUAL"
    def complete(row):
        return (bool(row["O que é (Tipo)"] and row["Category General"] and row["FAMILY"])
                and row["O que é (Tipo)"].upper() not in {"REVISAR", "UNKNOWN"}
                and (row["Category General"].upper(), row["FAMILY"].upper()) !=
                    ("OTHERS", "OTHERS"))
    ranked = sorted(entries, key=lambda r: (
        int(human(r)), int(r["source"] == "final"),
        int(complete(r)), int(r["source"] == "master"), -r["row_no"],
    ), reverse=True)
    # If no human source exists, a more complete Master row may be considered
    # only when its ORIGINAL Marca+Modelo matches the Final exactly.
    chosen = ranked[0]
    if (not human(chosen) and not complete(chosen)
            and chosen["source"] == "final"):
        candidates = [r for r in entries if r["source"] == "master"
                      and (r["Marca"], r["Modelo"]) ==
                          (chosen["Marca"], chosen["Modelo"]) and complete(r)]
        if candidates:
            chosen = candidates[0]
    locked = human(chosen)
    status = ("BI_OK_LOCK" if chosen["Revisão B.I"].upper() == "OK"
              else "MANUAL_LOCK" if chosen["Status"].upper() == "MANUAL"
              else "COMPLETE_PROPOSAL" if complete(chosen)
              else "PENDING")
    return chosen, int(locked), status


def audit_sqlite_merge(final_path, master_path, db_path, campaign_keys=(), *,
                       commit=False):
    """Rebuild ONLY SQLite registry tables, atomically, from official baselines.

    This is a versioned, provenance-preserving canonical lookup projection.
    It does not rewrite Excel or existing classification_results/tavily_cache.
    """
    final_path, master_path, db_path = map(
        lambda p: Path(p).resolve(), (final_path, master_path, db_path))
    for name, path in (("final", final_path), ("master", master_path),
                       ("classification_db", db_path)):
        if not path.is_file():
            raise RuntimeError("SQLITE_MERGE_OFFICIAL_FILE_MISSING:" + name)
    hashes = {"final": _sha(final_path), "master": _sha(master_path)}
    final = _rows(final_path, "Base Final", "final")
    master = _rows(master_path, "Consolidado", "master")
    final_by_key = defaultdict(list)
    all_by_key = defaultdict(list)
    for rec in final:
        final_by_key[rec["sku_key"]].append(rec)
    for rec in final + master:
        all_by_key[rec["sku_key"]].append(rec)
    required = set(campaign_keys)
    missing = sorted(required - set(final_by_key))
    target_ambiguous = []
    aggregate = []
    ambiguities = 0
    pending = proposals = human_locks = 0
    for sku_key, final_rows in sorted(final_by_key.items()):
        all_rows = all_by_key[sku_key]
        # The compact identity may hide different physical models or competing
        # labels; preserve all such source records and BLOCK the lookup.
        physical_pairs = {(r["Marca"], r["Modelo"]) for r in final_rows}
        taxonomy = {
            (r["O que é (Tipo)"], r["Category General"], r["FAMILY"])
            for r in final_rows
            if r["O que é (Tipo)"] and r["Category General"] and r["FAMILY"]
        }
        ambiguous = len(physical_pairs) > 1 or len(taxonomy) > 1
        if ambiguous:
            ambiguities += 1
            if sku_key in required:
                target_ambiguous.append(sku_key)
        selected, locked, status = _chosen(all_rows, final_rows)
        if status == "PENDING":
            pending += 1
        elif status == "COMPLETE_PROPOSAL":
            proposals += 1
        else:
            human_locks += 1
        aggregate.append((sku_key, selected, locked, status,
                          "AMBIGUOUS" if ambiguous else "OK",
                          len(physical_pairs), len(final_rows)))
    if missing or target_ambiguous:
        status = "BLOCKED_MISSING_OR_AMBIGUOUS"
    else:
        status = "DRY_RUN_PASS" if not commit else "READY_TO_COMMIT"
    summary = {
        "status": status, "sqlite": str(db_path),
        "final_rows": len(final), "master_rows": len(master),
        "unique_final_keys": len(final_by_key),
        "campaign_expected": len(required),
        "campaign_found_in_final": len(required) - len(missing),
        "campaign_missing": missing[:20],
        "campaign_ambiguous": target_ambiguous[:20],
        "global_ambiguous": ambiguities,
        "lookup_safe_keys": len(aggregate) - ambiguities,
        "lookup_pending": pending,
        "lookup_proposals": proposals, "lookup_human_locked": human_locks,
        "search_calls": 0, "llm_calls": 0,
    }
    if status == "BLOCKED_MISSING_OR_AMBIGUOUS" or not commit:
        return summary
    # Prevent a stale Excel source and an out-of-band database replacement.
    if _sha(final_path) != hashes["final"] or _sha(master_path) != hashes["master"]:
        raise RuntimeError("SQLITE_MERGE_SOURCE_CHANGED_DURING_AUDIT")
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLITE_MERGE_DB_INTEGRITY_FAIL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS sku_registry_source_v1 (
            source TEXT NOT NULL, row_no INTEGER NOT NULL,
            sku_key TEXT NOT NULL, brand TEXT NOT NULL, model TEXT NOT NULL,
            payload_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            PRIMARY KEY(source, row_no))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_sku_registry_source_key_v1
                        ON sku_registry_source_v1(sku_key)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sku_registry_lookup_v1 (
            sku_key TEXT PRIMARY KEY,
            brand TEXT NOT NULL, model TEXT NOT NULL,
            product_type TEXT, power TEXT, category_general TEXT, family TEXT,
            title_real TEXT, next_action TEXT, status TEXT, review TEXT,
            review_bi TEXT, lookup_state TEXT NOT NULL,
            classification_state TEXT NOT NULL, human_locked INTEGER NOT NULL,
            chosen_source TEXT NOT NULL, row_no INTEGER NOT NULL,
            variants INTEGER NOT NULL, physical_occurrences INTEGER NOT NULL,
            final_sha256 TEXT NOT NULL, master_sha256 TEXT NOT NULL,
            synced_at TEXT NOT NULL)""")
        conn.execute("DELETE FROM sku_registry_source_v1")
        conn.execute("DELETE FROM sku_registry_lookup_v1")
        import json
        source_rows = [
            (r["source"], r["row_no"], r["sku_key"], r["Marca"], r["Modelo"],
             json.dumps({name: r[name] for name in PUBLIC + TECHNICAL},
                        ensure_ascii=False, sort_keys=True),
             hashes[r["source"]])
            for r in final + master
        ]
        conn.executemany(
            "INSERT INTO sku_registry_source_v1 VALUES (?,?,?,?,?,?,?)",
            source_rows)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        insert_rows = []
        for sku_key, r, locked, classification, lookup_state, variants, physical in aggregate:
            insert_rows.append((
                sku_key, r["Marca"] if lookup_state == "OK" else "",
                r["Modelo"] if lookup_state == "OK" else "",
                r["O que é (Tipo)"], r["Alimentação"], r["Category General"],
                r["FAMILY"], r["Título Real"], r["Próxima Ação"], r["Status"],
                r["Revisão"], r["Revisão B.I"], lookup_state, classification,
                locked, r["source"], r["row_no"], variants, physical,
                hashes["final"], hashes["master"], timestamp,
            ))
        conn.executemany(
            "INSERT INTO sku_registry_lookup_v1 VALUES (" +
            ",".join("?" for _ in range(22)) + ")", insert_rows)
        conn.execute("""CREATE VIEW IF NOT EXISTS v_sku_lookup_safe_v1 AS
            SELECT sku_key, brand || '|' || model AS original_pair,
                   product_type, category_general, family, title_real,
                   classification_state, human_locked
            FROM sku_registry_lookup_v1 WHERE lookup_state='OK'""")
        if conn.execute("SELECT count(*) FROM sku_registry_lookup_v1").fetchone()[0] != len(aggregate):
            raise RuntimeError("SQLITE_MERGE_COUNTS_MISMATCH")
        checked = {x[0] for x in conn.execute(
            "SELECT sku_key FROM v_sku_lookup_safe_v1 WHERE sku_key IN (" +
            ",".join("?" for _ in sorted(required)) + ")",
            sorted(required))} if required else set()
        if checked != required:
            raise RuntimeError("SQLITE_MERGE_COHORT_NOT_QUERYABLE")
        if _sha(final_path) != hashes["final"] or _sha(master_path) != hashes["master"]:
            raise RuntimeError("SQLITE_MERGE_SOURCE_CHANGED_BEFORE_COMMIT")
        conn.commit()
        summary["status"] = "SQLITE_MERGED"
        summary["sql_query"] = (
            "SELECT sku_key, original_pair, product_type, category_general, family, "
            "classification_state FROM v_sku_lookup_safe_v1")
        return summary
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
