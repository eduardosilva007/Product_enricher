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


def prepare(provider: str, query: str, environ: dict) -> tuple[str, str, dict, dict]:
    if not query.strip():
        raise ValueError("NONEMPTY_QUERY_REQUIRED")
    if provider == "brightdata":
        # Single account explicitly selected for this independent lab.
        key = environ.get("BRIGHTDATA_API_KEY_2", "").strip()
        zone = environ.get("BRIGHTDATA_ZONE_2", "").strip()
        if not key or not zone:
            raise ValueError("BRIGHTDATA_ACCOUNT_2_KEY_AND_ZONE_REQUIRED")
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
    ap.add_argument("--octoparse-json", type=Path)
    ap.add_argument("--output-dir", type=Path,
                    default=Path.home() / "ProductEnricher_ProviderLab")
    ap.add_argument("--execute-live", action="store_true")
    ap.add_argument("--ack-distinct-new-sku", action="store_true")
    args = ap.parse_args(argv)
    if args.provider == "octoparse":
        if args.execute_live:
            raise ValueError("OCTOPARSE_LIVE_NOT_IMPLEMENTED: use task export JSON")
        if not args.octoparse_json:
            print("OCTOPARSE: exporte JSON de uma tarefa; use --octoparse-json CAMINHO")
            return 0
        payload = json.loads(args.octoparse_json.read_text(encoding="utf-8-sig"))
        report = inspect("octoparse", payload)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    method, url, headers, kwargs = prepare(args.provider, args.query, os.environ)
    if not args.execute_live:
        print(json.dumps({"provider": args.provider, "mode": "DRY_RUN",
                          "requests": 0, "target_host": url.split("/")[2],
                          "output_dir": str(args.output_dir)}, ensure_ascii=False))
        return 0
    if not args.ack_distinct_new_sku:
        raise ValueError("REQUIRE_ACK_DISTINCT_NEW_SKU: do not reuse a Tavily SKU or historical attempt")
    if args.provider == "scraperapi":
        # Structured Google SERP may cost 25+ API credits. No optional render/premium.
        print("SCRAPERAPI: 1 structured Google SERP request, budget up to documented base 25 credits.")
    # requests.Session trust_env=False prevents requests honoring ambient proxies
    # with unexpected interceptors. Zero redirects; zero retries.
    with requests.Session() as session:
        session.trust_env = False
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
