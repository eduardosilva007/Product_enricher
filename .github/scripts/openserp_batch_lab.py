#!/usr/bin/env python3
"""Isolated OpenSERP batch pilot. No production imports, cache or database writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SKUS = ("STIHL MS382", "STIHL FS250", "NOVABOT N2000")
ENGINES = ("google", "bing", "duckduckgo")
MAX_SKUS = 10
BASE = "http://127.0.0.1:7000"
BLOCKED = re.compile(r"(?i)(substitua|marca.real|modelo.real|modelo.novo|brand.model|^teste$)")
ACCESSORY = re.compile(
    r"(?i)\b(parts?|spares?|replacement|carburetor|piston|cover|case|"
    r"accessor(?:y|ies)|peças?|reposição|capa|cobertura|kit|"
    r"diagram(?:s)?|manual holder)\b"
)


def tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def model_present(haystack: str, model: str) -> bool:
    # Compare compact model as a sequence of adjacent alphanumeric tokens.
    components = tokens(model)
    if not components:
        return False
    squashed = "".join(components)
    words = tokens(haystack)
    for start in range(len(words)):
        for stop in range(start + 1, min(start + 4, len(words)) + 1):
            if "".join(words[start:stop]) == squashed:
                return True
    return False


def parse_queries(value: str) -> list[str]:
    rows = [line.strip() for line in value.splitlines() if line.strip()]
    if not rows or len(rows) > MAX_SKUS:
        raise ValueError("SKU_COUNT_OUTSIDE_1_TO_10")
    normalized = []
    seen = set()
    for item in rows:
        if len(item) > 120 or any(ord(char) < 32 for char in item):
            raise ValueError("INVALID_SKU_CHARACTERS")
        pieces = item.split()
        if len(pieces) < 2 or not any(char.isdigit() for char in " ".join(pieces[1:])):
            raise ValueError("EXPECTED_BRAND_AND_ALPHANUMERIC_MODEL: " + item)
        if BLOCKED.search(item):
            raise ValueError("PLACEHOLDER_BLOCKED")
        identifier = re.sub(r"[^a-z0-9]+", "", item.lower())
        if identifier in seen:
            raise ValueError("DUPLICATE_SKU: " + item)
        seen.add(identifier)
        normalized.append(item)
    return normalized


def classify_candidate(query: str, result: dict) -> dict:
    words = query.split()
    brand, model = words[0], " ".join(words[1:])
    title = str(result.get("title") or "")
    snippet = str(result.get("snippet") or "")
    url = str(result.get("url") or "")
    domain = str(result.get("domain") or "")
    body = title + " " + snippet
    brand_ok = brand.lower() in tokens(body)
    model_ok = model_present(body, model)
    # A strong match must be in the title, not only in the snippet.
    title_match = brand.lower() in tokens(title) and model_present(title, model)
    is_accessory = bool(ACCESSORY.search(title))
    parsed = urllib.parse.urlsplit(url)
    safe_direct = (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.hostname not in ("google.com", "www.google.com")
        and not parsed.username and not parsed.password
    )
    if not (brand_ok and model_ok):
        reason = "BRAND_MODEL_MISMATCH"
    elif is_accessory:
        reason = "ACCESSORY_OR_PART"
    elif not title_match:
        reason = "TITLE_IDENTITY_UNCONFIRMED"
    elif not safe_direct:
        reason = "UNRESOLVED_OR_UNSAFE_URL"
    else:
        reason = "IDENTITY_CANDIDATE"
    return {
        "query": query, "rank": result.get("rank"), "title": title,
        "snippet": snippet, "source_url": url,
        "direct_url": url if safe_direct else None, "display_domain": domain,
        "engine": result.get("engine"), "reason": reason,
        "brand_match": brand_ok, "model_match": model_ok,
        "title_match": title_match, "accessory": is_accessory,
        "classification_approved": False,
        "evidence_approved": False,
    }


def process_response(query: str, engine: str, payload: dict) -> dict:
    results = payload.get("results", [])
    meta = payload.get("meta") or {}
    engine_failed = bool(meta.get("engines_failed"))
    candidates = []
    seen = set()
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict) or result.get("type") != "organic":
            continue
        candidate = classify_candidate(query, result)
        candidate["engine"] = engine
        key = (candidate["source_url"], candidate["title"].casefold())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    status = ("ENGINE_FAILED" if engine_failed else
              "CAPTURED" if candidates else "EMPTY")
    return {
        "provider": "openserp", "engine": engine, "sku": query,
        "sku_key": hashlib.sha256(query.lower().encode()).hexdigest()[:16],
        "status": status, "results": candidates, "source_meta": meta,
        "identity_candidates": sum(x["reason"] == "IDENTITY_CANDIDATE" for x in candidates),
        "classification_approved": False, "evidence_approved": False,
    }


def get_json(url: str, timeout: int) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP_" + str(resp.status))
        obj = json.load(resp)
        if not isinstance(obj, dict):
            raise ValueError("INVALID_RESPONSE_OBJECT")
        return obj


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=ENGINES, default="google")
    parser.add_argument("--skus", default="\\n".join(DEFAULT_SKUS))
    parser.add_argument("--output-dir", default="openserp-lab-results")
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument("--offline-dir", default="", help="Normalize existing local raw JSON, no HTTP")
    args = parser.parse_args()
    queries = parse_queries(args.skus)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if not args.offline_dir:
        if args.base_url != BASE:
            raise ValueError("REMOTE_BASE_URL_BLOCKED")
        for attempt in range(30):
            try:
                get_json(BASE + "/mega/engines", timeout=5)
                break
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(2)
        else:
            raise RuntimeError("LOCAL_SERVICE_NOT_READY")
    summaries = []
    for query in queries:
        key = hashlib.sha256(query.lower().encode()).hexdigest()[:16]
        raw_path = output / f"openserp_{args.engine}_{key}.json"
        error = None
        payload = {}
        try:
            if args.offline_dir:
                raw_path = Path(args.offline_dir) / raw_path.name
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
            else:
                params = urllib.parse.urlencode({"text": query, "limit": 10})
                payload = get_json(BASE + f"/{args.engine}/search?" + params, 100)
                raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            row = process_response(query, args.engine, payload)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            error = str(exc)[:280]
            row = {"provider": "openserp", "engine": args.engine, "sku": query,
                   "sku_key": key, "status": "ERROR", "results": [],
                   "identity_candidates": 0, "classification_approved": False,
                   "evidence_approved": False}
        row["error"] = error
        summaries.append(row)
        print(f"{query}: {row['status']}; candidate_links={row['identity_candidates']}; error={error}", flush=True)
        if not args.offline_dir:
            time.sleep(3)
    (output / "normalized_batch.json").write_text(
        json.dumps({"schema_version": "lab-1", "items": summaries,
                    "policy": "UNVERIFIED_NO_AUTO_APPROVAL_NO_PRODUCTION_WRITE"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [{"sku": x["sku"], "status": x["status"],
              "candidate_count": x["identity_candidates"], "error": x["error"]}
             for x in summaries]
    (output / "manifest.json").write_text(
        json.dumps({"engine": args.engine, "submitted": len(queries), "items": lines},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if not any(item["status"] == "CAPTURED" for item in summaries):
        raise SystemExit("ZERO_SUCCESSFUL_SEARCHES")


if __name__ == "__main__":
    run()
