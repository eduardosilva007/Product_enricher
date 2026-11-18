#!/usr/bin/env python3
"""Isolated OpenSERP batch collector that emits import-ready staging inputs.

No production imports, cache writes or database writes occur here.
Input contract is explicit BRAND|MODEL, one SKU per line, max 10.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SKUS = ("STIHL|MS382", "STIHL|FS250", "NOVABOT|N2000")
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
    components = tokens(model)
    if not components:
        return False
    squashed = "".join(components)
    words = tokens(haystack)
    for start in range(len(words)):
        for stop in range(start + 1, min(start + 5, len(words)) + 1):
            if "".join(words[start:stop]) == squashed:
                return True
    return False


def parse_skus(value: str) -> list[dict[str, str]]:
    rows = [line.strip() for line in value.splitlines() if line.strip()]
    if not rows or len(rows) > MAX_SKUS:
        raise ValueError("SKU_COUNT_OUTSIDE_1_TO_10")
    output = []
    seen = set()
    for raw in rows:
        if len(raw) > 180 or any(ord(char) < 32 for char in raw):
            raise ValueError("INVALID_SKU_CHARACTERS")
        if raw.count("|") != 1:
            raise ValueError("EXPECTED_EXPLICIT_BRAND_PIPE_MODEL: " + raw)
        brand, model = (part.strip() for part in raw.split("|", 1))
        if not brand or not model or BLOCKED.search(brand) or BLOCKED.search(model):
            raise ValueError("BLANK_OR_PLACEHOLDER_SKU: " + raw)
        if not any(char.isdigit() for char in model):
            raise ValueError("MODEL_MUST_CONTAIN_DIGIT: " + raw)
        key = (
            re.sub(r"[^a-z0-9]+", "", brand.casefold()),
            re.sub(r"[^a-z0-9]+", "", model.casefold()),
        )
        if not all(key) or key in seen:
            raise ValueError("DUPLICATE_OR_INVALID_SKU: " + raw)
        seen.add(key)
        query = f"{brand} {model}".strip()
        output.append({"brand": brand, "model": model, "query": query})
    return output


# Compatibility name used by workflow validation.
def parse_queries(value: str) -> list[dict[str, str]]:
    return parse_skus(value)


def classify_candidate(brand: str, model: str, query: str, result: dict) -> dict:
    title = str(result.get("title") or "")
    snippet = str(result.get("snippet") or "")
    url = str(result.get("url") or "")
    domain = str(result.get("domain") or "")
    body = title + " " + snippet
    brand_tokens = tokens(brand)
    body_tokens = set(tokens(body))
    title_tokens = set(tokens(title))
    brand_ok = bool(brand_tokens) and all(t in body_tokens for t in brand_tokens)
    model_ok = model_present(body, model)
    title_match = all(t in title_tokens for t in brand_tokens) and model_present(title, model)
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
        "classification_approved": False, "evidence_approved": False,
    }


def ai_overview_unverified(payload: dict) -> dict | None:
    """Retain AI Overview as untrusted context, never as source evidence."""
    features = payload.get("serp_features")
    if not isinstance(features, list):
        return None
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "ai_summary":
            continue
        body = feature.get("text")
        if not isinstance(body, str) or not body.strip():
            continue
        # Do not promote UI boilerplate or AI-generated citations to verified URLs.
        # Preserve the original excerpt for deterministic provenance auditing.
        return {
            "source": "google_ai_overview",
            "status": "UNVERIFIED_CONTEXT_ONLY",
            "text": body,
            "truncated": False,
            "may_approve_identity": False,
            "may_approve_classification": False,
        }
    return None


def process_response(brand: str, model: str, query: str, engine: str, payload: dict) -> dict:
    results = payload.get("results", [])
    meta = payload.get("meta") or {}
    engine_failed = bool(meta.get("engines_failed"))
    candidates = []
    organic = []
    seen = set()
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict) or result.get("type") != "organic":
            continue
        candidate = classify_candidate(brand, model, query, result)
        candidate["engine"] = engine
        key = (candidate["source_url"], candidate["title"].casefold())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        organic.append({
            "type": "organic",
            "rank": result.get("rank"),
            "title": candidate["title"],
            "url": candidate["source_url"],
            "snippet": candidate["snippet"],
        })
    status = ("ENGINE_FAILED" if engine_failed else "CAPTURED" if candidates else "EMPTY")
    return {
        "provider": "openserp", "engine": engine,
        "brand": brand, "model": model, "query": query, "sku": query,
        "sku_key": hashlib.sha256((brand.casefold()+"|"+model.casefold()).encode()).hexdigest()[:16],
        "status": status, "results": candidates, "organic": organic, "source_meta": meta,
        "identity_candidates": sum(x["reason"] == "IDENTITY_CANDIDATE" for x in candidates),
        "ai_overview": ai_overview_unverified(payload),
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
    parser.add_argument("--skus", default="\n".join(DEFAULT_SKUS))
    parser.add_argument("--output-dir", default="openserp-lab-results")
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument("--offline-dir", default="", help="Normalize existing local raw JSON, no HTTP")
    args = parser.parse_args()

    skus = parse_skus(args.skus)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    if not args.offline_dir:
        if args.base_url != BASE:
            raise ValueError("REMOTE_BASE_URL_BLOCKED")
        for _ in range(30):
            try:
                get_json(BASE + "/mega/engines", timeout=5)
                break
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(2)
        else:
            raise RuntimeError("LOCAL_SERVICE_NOT_READY")

    summaries = []
    for sku in skus:
        brand, model, query = sku["brand"], sku["model"], sku["query"]
        key = hashlib.sha256((brand.casefold()+"|"+model.casefold()).encode()).hexdigest()[:16]
        raw_path = output / f"openserp_{args.engine}_{key}.json"
        error = None
        try:
            if args.offline_dir:
                raw_path = Path(args.offline_dir) / raw_path.name
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
            else:
                params = urllib.parse.urlencode({"text": query, "limit": 10})
                payload = get_json(BASE + f"/{args.engine}/search?" + params, 100)
                raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            row = process_response(brand, model, query, args.engine, payload)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            error = str(exc)[:280]
            row = {
                "provider": "openserp", "engine": args.engine,
                "brand": brand, "model": model, "query": query, "sku": query,
                "sku_key": key, "status": "ERROR", "results": [], "organic": [],
                "identity_candidates": 0, "classification_approved": False,
                "evidence_approved": False,
            }
        row["error"] = error
        summaries.append(row)
        print(f"{query}: {row['status']}; candidate_links={row['identity_candidates']}; error={error}", flush=True)
        if not args.offline_dir:
            time.sleep(3)

    (output / "normalized_batch.json").write_text(
        json.dumps({
            "schema_version": "lab-2", "items": summaries,
            "policy": "UNVERIFIED_NO_AUTO_APPROVAL_NO_PRODUCTION_WRITE"
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    importable = []
    for x in summaries:
        if x["status"] == "CAPTURED":
            importable.append({
                "provider": "openserp",
                "query": x["query"],
                "status": "CAPTURED",
                "organic": x["organic"],
                "ai_overview": x.get("ai_overview"),
            })
    (output / "pilot_summary.json").write_text(
        json.dumps({"results": importable}, ensure_ascii=False, indent=2), encoding="utf-8")

    with (output / "openserp_manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["query", "brand", "model"])
        writer.writeheader()
        for x in summaries:
            if x["status"] == "CAPTURED":
                writer.writerow({"query": x["query"], "brand": x["brand"], "model": x["model"]})

    manifest_items = [{
        "brand": x["brand"], "model": x["model"], "query": x["query"],
        "status": x["status"], "candidate_count": x["identity_candidates"], "error": x["error"]
    } for x in summaries]
    (output / "manifest.json").write_text(
        json.dumps({"engine": args.engine, "submitted": len(skus), "items": manifest_items},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    if not importable:
        raise SystemExit("ZERO_SUCCESSFUL_SEARCHES")


if __name__ == "__main__":
    run()
