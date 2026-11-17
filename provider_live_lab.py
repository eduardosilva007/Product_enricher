"""Independent provider contract lab; never touches pipeline cache, Excel or SQLite.

Dry-run by default. Live mode performs at most ONE HTTP request to ONE chosen
provider and records raw JSON + response metadata outside the repository.

Use distinct, previously unsearched SKUs. This tool deliberately does not read
the production attempt ledger; caller must avoid conflicts with an active run.
Octoparse: inspect a task-exported JSON through provider_lab.py instead.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests

from provider_lab import capture_payload, inspect



def read_lab_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv entries without an extra dependency.

    Supports quoted values, comments and UTF-8 BOM. Ambient environment wins.
    Only provider lab keys are imported; never modifies os.environ.
    """
    if not path.is_file():
        return {}
    allowed = ("BRIGHTDATA_", "SERPER_", "SCRAPERAPI_")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name.startswith(allowed):
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            if len(value) < 2 or quote not in value[1:]:
                raise ValueError("LAB_ENV_UNCLOSED_QUOTE")
            value = value[1:value.index(quote, 1)]
        else:
            value = value.split(" #", 1)[0].strip()
        values[name] = value
    return values


def prepare(provider: str, query: str, environ: dict, brightdata_slot: int = 1) -> tuple[str, str, dict, dict]:
    if not query.strip():
        raise ValueError("NONEMPTY_QUERY_REQUIRED")
    if provider == "brightdata":
        if brightdata_slot not in (1, 2, 3, 4):
            raise ValueError("BRIGHTDATA_SLOT_MUST_BE_1_TO_4")
        key = (environ.get(f"BRIGHTDATA_API_KEY_{brightdata_slot}") or (
            environ.get("BRIGHTDATA_API_KEY") if brightdata_slot == 1 else ""
        ) or "").strip()
        zone = (environ.get(f"BRIGHTDATA_ZONE_{brightdata_slot}") or (
            environ.get("BRIGHTDATA_ZONE") if brightdata_slot == 1 else ""
        ) or "").strip()
        if not key or not zone:
            raise ValueError(f"BRIGHTDATA_SLOT_{brightdata_slot}_KEY_AND_ZONE_REQUIRED")
        target = "https://www.google.com/search?" + urlencode({"q": query, "brd_json": 1})
        return ("POST", "https://api.brightdata.com/request",
                {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                {"json": {"zone": zone, "url": target, "format": "json"}})
    if provider == "serper":
        key = (environ.get("SERPER_API_KEY_1") or environ.get("SERPER_API_KEY") or "").strip()
        if not key:
            raise ValueError("SERPER_API_KEY_REQUIRED")
        return ("POST", "https://google.serper.dev/search",
                {"X-API-KEY": key, "Content-Type": "application/json"},
                {"json": {"q": query, "num": 10}})
    if provider == "scraperapi":
        key = environ.get("SCRAPERAPI_API_KEY", "").strip()
        if not key:
            raise ValueError("SCRAPERAPI_API_KEY_REQUIRED")
        return ("GET", "https://api.scraperapi.com/structured/google/search", {},
                {"params": {"api_key": key, "query": query}})
    raise ValueError("UNSUPPORTED_LIVE_PROVIDER")


def run(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["brightdata", "serper", "scraperapi", "octoparse"], required=True)
    ap.add_argument("--query", default="")
    ap.add_argument("--brightdata-slot", type=int, choices=[1, 2, 3, 4], default=1)
    ap.add_argument("--env-file", type=Path, help="Explicit local .env path; never printed")
    ap.add_argument("--octoparse-json", type=Path)
    ap.add_argument("--output-dir", type=Path,
                    default=Path.home() / "ProductEnricher_ProviderLab")
    ap.add_argument("--execute-live", action="store_true")
    ap.add_argument("--ack-distinct-new-sku", action="store_true")
    args = ap.parse_args(argv)
    if args.env_file:
        if not args.env_file.is_file():
            raise ValueError("ENV_FILE_NOT_FOUND")
        env_path = args.env_file
    else:
        env_path = Path(__file__).resolve().parent / ".env"
    # Dependency-free and read-only. Shell environment overrides file values.
    lab_env = {**read_lab_env(env_path), **os.environ}
    if args.provider == "octoparse":
        if args.execute_live:
            raise ValueError("OCTOPARSE_LIVE_NOT_IMPLEMENTED: use task export JSON")
        if not args.octoparse_json:
            print("OCTOPARSE: exporte JSON de uma tarefa; use --octoparse-json CAMINHO")
            return 0
        payload = json.loads(args.octoparse_json.read_text(encoding="utf-8-sig"))
        if isinstance(payload, list):
            payload = {"data": payload}
        report = inspect("octoparse", payload)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # Offline mode must not depend on API keys or available credits.
    if not args.execute_live:
        if not args.query.strip():
            raise ValueError("NONEMPTY_QUERY_REQUIRED")
        endpoints = {
            "brightdata": "api.brightdata.com",
            "serper": "google.serper.dev",
            "scraperapi": "api.scraperapi.com",
        }
        print(json.dumps({
            "provider": args.provider, "mode": "DRY_RUN", "requests": 0,
            "target_host": endpoints[args.provider],
            "brightdata_slot": args.brightdata_slot if args.provider == "brightdata" else None,
            "env_file_exists": env_path.is_file(),
            "output_dir": str(args.output_dir)
        }, ensure_ascii=False))
        return 0
    if not args.ack_distinct_new_sku:
        raise ValueError("REQUIRE_ACK_DISTINCT_NEW_SKU: do not reuse a Tavily SKU or historical attempt")
    method, url, headers, kwargs = prepare(args.provider, args.query, lab_env, args.brightdata_slot)
    if args.provider == "scraperapi":
        # A single Google SERP can cost 25 credits. This is a per-request
        # authorization gate, NOT a persisted remaining-balance counter.
        # Never silently interpret the LIMIT as number of free searches.
        from scraperapi_adapter import ScraperAPIBudget, build_request_plan
        raw_limit = (lab_env.get("SCRAPERAPI_LIMIT_1") or
                     lab_env.get("SCRAPERAPI_LIMIT") or "").strip()
        if not raw_limit:
            raise ValueError("SCRAPERAPI_LIMIT_REQUIRED: set credit allowance in .env")
        try:
            available_credits = int(raw_limit)
        except ValueError as exc:
            raise ValueError("SCRAPERAPI_LIMIT_NOT_INTEGER") from exc
        build_request_plan(
            "https://www.google.com/search?q=sku",
            ScraperAPIBudget(remaining_credits=available_credits),
        )
        print("SCRAPERAPI: request authorized by configured credit cap (25 base credits); "
              "no automatic cumulative balance tracking.")
    # Zero redirects; no application-layer retries.
    with requests.Session() as session:
        response = session.request(method, url, headers=headers, timeout=75,
                                   allow_redirects=False, **kwargs)
    # Do not log full request URL: ScraperAPI key is a query parameter.
    status = int(response.status_code)
    metadata = {"provider": args.provider, "http_status": status,
                "content_type": response.headers.get("content-type", ""),
                "scraperapi_reported_credits": response.headers.get("sa-credit-cost"),
                "upstream_status": None}
    if response.status_code >= 300:
        print(json.dumps({**metadata, "result": "HTTP_ERROR_NO_RETRY"}))
        return 2
    try:
        payload = response.json()
    except ValueError:
        print(json.dumps({**metadata, "result": "NON_JSON_RESPONSE_NO_RETRY"}))
        return 3
    if not isinstance(payload, dict):
        print(json.dumps({**metadata, "result": "NON_OBJECT_JSON_NO_RETRY"}))
        return 3
    if args.provider == "brightdata":
        metadata["upstream_status"] = payload.get("status_code")
    path = capture_payload(args.provider, payload, args.query, args.output_dir)
    report = inspect(args.provider, payload)
    print(json.dumps({**metadata, "result": "CAPTURED", "raw_json_path": str(path),
                      "shape": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (ValueError, requests.RequestException) as exc:
        print(f"LAB_STOPPED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
