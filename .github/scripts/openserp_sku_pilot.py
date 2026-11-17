#!/usr/bin/env python3
"""GitHub Actions-only pilot: no secrets, no production imports or writes."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENGINE = os.getenv("OPENSERP_ENGINE", "google")
if ENGINE not in ("google", "bing", "duckduckgo"):
    raise SystemExit("Blocked unsupported search engine")
BASE = "http://127.0.0.1:7000"
QUERIES = [
    ("stihl_ms382", "STIHL MS382"),
    ("stihl_fs250", "STIHL FS250"),
    ("novabot_n2000", "NOVABOT N2000"),
]
OUT = Path("openserp-lab-results")
OUT.mkdir(exist_ok=True)
reports = []

# A browser-backed service needs a little time to initialize.
for attempt in range(30):
    try:
        with urllib.request.urlopen(BASE + "/mega/engines", timeout=5) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError):
        pass
    time.sleep(2)
else:
    raise SystemExit("OpenSERP health check failed; inspect Docker startup logs")

for slug, query in QUERIES:
    url = BASE + "/" + ENGINE + "/search?" + urllib.parse.urlencode(
        {"text": query, "limit": 10}
    )
    status = "ERROR"
    error = None
    payload = {}
    try:
        with urllib.request.urlopen(url, timeout=100) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            payload = json.load(response)
        status = "CAPTURED" if payload.get("results") else "EMPTY"
    except Exception as exc:
        error = str(exc)[:300]
    raw_path = OUT / f"{ENGINE}_{slug}.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    organic = [
        {"rank": item.get("rank"), "title": item.get("title"),
         "url": item.get("url"), "snippet": item.get("snippet")}
        for item in payload.get("results", [])
        if item.get("type") == "organic"
    ]
    row = {
        "provider": "openserp", "engine": ENGINE, "query": query, "status": status,
        "organic_count": len(organic), "error": error, "organic": organic,
    }
    reports.append(row)
    print(f"{query}: {status}, organic={len(organic)}, error={error}", flush=True)
    time.sleep(2)

(OUT / "pilot_summary.json").write_text(
    json.dumps({"results": reports, "note": "Unverified evidence; no classification"},
               ensure_ascii=False, indent=2),
    encoding="utf-8",
)
if not any(row["status"] == "CAPTURED" for row in reports):
    raise SystemExit("Pilot failed: all searches returned error/empty results")
