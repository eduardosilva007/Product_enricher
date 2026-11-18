#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recover an ALREADY CAPTURED OpenSERP staging artifact into central RAW cache.

Read-only by default; --commit is explicit. No Search requests, no retries,
no Excel updates, no classification. Preserves durable attempt reservations.
"""
from __future__ import annotations

import argparse
import copy
import json
from contextlib import closing
import sqlite3
from pathlib import Path


def recover(*, artifact: Path, brand: str, model: str, commit: bool,
            config: Path, policy: Path, cache: Path, db: Path) -> dict:
    import pipeline_produtos as pp
    from search_provider_extensions import load_openserp_staging

    pp.load_env_file(Path(__file__).resolve().parent / ".env")
    cfg = pp.read_json(config)
    cfg["_collection_policy"] = pp.read_json(policy)
    cfg["_openserp_artifact_path"] = str(artifact.resolve())
    key = pp.business_key(brand, model)
    staged = pp.read_json(artifact)
    cohorts = staged.get("cohorts") if isinstance(staged, dict) else None
    if not isinstance(cohorts, list) or len(cohorts) != 1:
        raise ValueError("REPLAY_REQUIRES_EXACTLY_ONE_STAGED_SKU")
    cohort = cohorts[0]
    if not isinstance(cohort, dict) or pp.business_key(
        pp.safe(cohort.get("brand")), pp.safe(cohort.get("model"))
    ) != key:
        raise ValueError("REPLAY_COHORT_IDENTITY_MISMATCH")
    if (pp.safe(cohort.get("brand")).casefold(), pp.safe(cohort.get("model")).casefold()) != (
        brand.casefold(), model.casefold()
    ):
        raise ValueError("REPLAY_REQUIRES_LITERAL_BRAND_AND_MODEL")
    if not cache.is_file() or not db.is_file():
        raise RuntimeError("REPLAY_REQUIRES_EXISTING_CENTRAL_SQLITE_FILES")
    cache_uri = f"file:{cache.resolve().as_posix()}?mode=ro"
    db_uri = f"file:{db.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(cache_uri, uri=True)) as conn, conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM tavily_cache WHERE cache_key=?",
            ("v3|" + key,),
        ).fetchone()[0]
    if existing:
        raise RuntimeError("REPLAY_CACHE_ALREADY_EXISTS; refusing overwrite")
    with closing(sqlite3.connect(db_uri, uri=True)) as conn, conn:
        reservations = conn.execute(
            "SELECT provider, reserved_at FROM collection_attempt_reservations "
            "WHERE business_key=?", (key,)
        ).fetchall()
    # This replays a captured artifact, NOT another Search request.
    # A reservation is useful audit history, but never a prerequisite:
    # GitHub transport may have completed before the central ledger reserved.
    # The staging SKU/query and the central-cache absence are the hard gates.
    # Do not create or delete reservations during recovery.
    files = cfg.get("files", {})
    master = Path(files.get("master", ""))
    final = Path(files.get("final", ""))
    # Existing accepted/manual/B.I=OK classifications must never be overwritten.
    if not master.is_file() or not final.is_file():
        raise RuntimeError("REPLAY_BASELINE_FILES_MISSING")
    protected = pp.load_baseline_inventory(master, final)["processed"]
    if key in protected:
        raise RuntimeError("REPLAY_PROTECTED_CLASSIFICATION")
    cfg["collection"] = copy.deepcopy(cfg.get("collection") or {})
    cfg["collection"]["local_enrichment"] = {
        **(cfg["collection"].get("local_enrichment") or {}), "enabled": False
    }
    query_model = pp.derive_query_model(brand, model, cfg)
    template = pp.safe(cfg["collection"].get("query_template")) or "{brand} {search_model}"
    query = template.format(brand=brand, model=model,
                            search_model=query_model).strip()
    rows, meta = load_openserp_staging(str(artifact), brand, model, query)
    # The source-only pilot must be auditable even with zero accepted sources.
    result = {
        "recovery_kind": "EXISTING_ARTIFACT_NO_SEARCH",
        "brand": brand, "model": model, "business_key": key,
        "artifact_sha256": meta["artifact_sha256"],
        "staged_raw_results": len(rows),
        "previous_attempts": [{"provider": r[0], "reserved_at": r[1]} for r in reservations],
        "network_search_calls": 0, "official_excel_writes": 0,
        "status": "PREFLIGHT_OK", "committed": False,
    }
    if not commit:
        return result
    # Perform the normal sieve/collection pipeline on the previously captured
    # artifact, with local HTTP enrichment disabled. Does not invoke new Search.
    payload = pp.search_product_once(brand, model, cfg, pp.RateLimiter(100), "openserp")
    collection = payload.get("_collection") or {}
    usage = collection.get("usage") or {}
    search_meta = payload.get("_search_meta") or {}
    if (int(search_meta.get("network_requests", -1)) != 0
            or int(search_meta.get("artifact_searches_reused", 0)) != 1
            or search_meta.get("artifact_sha256") != meta["artifact_sha256"]
            or int(usage.get("requests", 0)) != 1):
        raise RuntimeError("REPLAY_NETWORK_OR_ARTIFACT_INTEGRITY_FAILURE")
    if not isinstance(payload.get("results"), list):
        raise RuntimeError("REPLAY_INVALID_RESULT_STRUCTURE")
    # Recheck under the writer transaction; no forced overwrite of existing RAW.
    writer = pp.TavilyWritableStore(cache)
    try:
        if writer.conn.execute(
            "SELECT 1 FROM tavily_cache WHERE cache_key=?", ("v3|" + key,)
        ).fetchone():
            raise RuntimeError("REPLAY_CACHE_CONCURRENT_WRITE")
        writer.put(brand, model, payload, archive_reason="OPENSERP_EXISTING_ARTIFACT_RECOVERY")
        # A successful put is not sufficient: verify the authoritative cache
        # row inside the same connection before reporting COMMITTED.
        persisted = writer.conn.execute(
            "SELECT response_json FROM tavily_cache WHERE cache_key=?",
            ("v3|" + key,),
        ).fetchone()
        if persisted is None:
            raise RuntimeError("REPLAY_POSTCOMMIT_CACHE_MISSING")
        saved = json.loads(persisted[0])
        saved_meta = saved.get("_search_meta") or {}
        if (saved_meta.get("artifact_sha256") != meta["artifact_sha256"]
                or int(saved_meta.get("network_requests", -1)) != 0
                or len(saved.get("results") or []) != len(payload["results"])):
            raise RuntimeError("REPLAY_POSTCOMMIT_INTEGRITY_FAILURE")
    finally:
        writer.close()
    result.update({
        "committed": True, "status": "RAW_COMMITTED_CLASSIFICATION_NOT_EXECUTED",
        "raw_results": len(payload["results"]),
        "curated_sources": len(collection.get("curated_results") or []),
        "collection_state": collection.get("state", ""),
        "ai_overview_unverified": bool(search_meta.get("ai_overview_unverified")),
    })
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Safely replay an existing OpenSERP SKU artifact")
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--brand", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--config", type=Path, default=Path("pipeline_config.json"))
    p.add_argument("--policy", type=Path, default=Path("collection_quality_policy.json"))
    p.add_argument("--cache", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--commit", action="store_true", help="Explicitly write RAW cache only")
    args = p.parse_args()
    root = Path(__file__).resolve().parent
    cfg = json.loads((root / args.config).read_text(encoding="utf-8-sig"))
    files = cfg["files"]
    cache = args.cache or Path(files["cache"])
    db = args.db or Path(files["classification_db"])
    def path(v):
        return v if v.is_absolute() else root / v
    result = recover(
        artifact=path(args.artifact), brand=args.brand, model=args.model,
        commit=args.commit, config=path(args.config), policy=path(args.policy),
        cache=path(cache), db=path(db)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
