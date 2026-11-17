"""Offline provider payload inspection. Never calls providers or changes canonical stores.

Examples:
  python -m provider_lab inspect path/to/brightdata.json --provider brightdata
  python -m provider_lab inspect path/to/octoparse.json --provider octoparse

JSON content may contain personal or commercial data: stdout intentionally
reports field paths, counts and evidence metadata, never complete body text.
"""
import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def unwrap(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    body = payload.get("body", payload)
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError("JSON envelope body must be an object")
    return body


def shape(value: Any, depth: int = 0, max_depth: int = 5) -> Any:
    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): shape(v, depth + 1, max_depth) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return {"kind": "list", "count": len(value),
                "sample_shape": shape(value[0], depth + 1, max_depth) if value else None}
    return type(value).__name__


def _external_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    # Explicit http(s) links only. Do not expose secrets in URL query strings.
    return url if parsed.scheme in ("https", "http") and parsed.netloc else ""


def evidence_from_payload(provider: str, payload: dict) -> list[dict]:
    """Candidate discovery ONLY; every candidate needs downstream identity checks."""
    body = unwrap(payload)
    provider = provider.lower()
    if provider == "brightdata":
        # Organic results are not the only blocks in Full JSON. We inspect
        # additional sections offline, but do NOT auto-approve/promote them.
        sources = [("organic", body.get("organic", []))]
        for field in ("shopping", "popular_products"):
            section = body.get(field, [])
            if isinstance(section, list):
                sources.append((field, section))
        out = []
        for section, rows in sources:
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = _external_url(row.get("link") or row.get("url"))
                title = str(row.get("title") or row.get("name") or "").strip()
                if not url or not title:
                    continue
                out.append({"provider": provider, "section": section, "title": title,
                            "url": url, "snippet": str(row.get("description") or row.get("snippet") or ""),
                            "status": "CANDIDATE_UNVERIFIED"})
        return out
    if provider == "tavily":
        rows, key = body.get("results", []), "results"
    elif provider == "serper":
        rows, key = body.get("organic", []), "organic"
    elif provider == "octoparse":
        # Task-specific exports must have a configured mapping in production.
        rows = body.get("data", body.get("records", []))
        if isinstance(rows, dict):
            rows = rows.get("records", rows.get("rows", []))
        key = "task_rows"
    else:
        raise ValueError("Unknown provider")
    if not isinstance(rows, list):
        raise ValueError(f"{provider}: {key} must be a list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _external_url(row.get("url") or row.get("link") or row.get("product_url"))
        title = str(row.get("title") or row.get("name") or row.get("product_name") or "").strip()
        if url and title:
            out.append({"provider": provider, "section": key, "title": title,
                        "url": url, "snippet": str(row.get("description") or row.get("snippet") or ""),
                        "status": "CANDIDATE_UNVERIFIED"})
    return out


def inspect(provider: str, payload: dict) -> dict:
    body = unwrap(payload)
    out = {
        "provider": provider.lower(),
        "wrapper_keys": sorted(payload),
        "body_keys": sorted(body),
        "structure": shape(body),
        "status_code": payload.get("status_code"),
        "candidate_counts": {},
        "candidate_total": 0,
        "note": "Candidates are unverified. Never promote without source/identity validation.",
    }
    candidates = evidence_from_payload(provider, payload)
    for candidate in candidates:
        section = candidate["section"]
        out["candidate_counts"][section] = out["candidate_counts"].get(section, 0) + 1
    out["candidate_total"] = len(candidates)
    # Explicitly count non-evidence SERP blocks to identify unexploited data.
    if provider.lower() == "brightdata":
        out["other_blocks"] = {
            key: (len(value) if isinstance(value, list) else type(value).__name__)
            for key, value in body.items()
            if key not in {"organic", "shopping", "popular_products"}
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["inspect"])
    ap.add_argument("path", type=Path)
    ap.add_argument("--provider", choices=["brightdata", "tavily", "serper", "octoparse"], required=True)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)
    payload = json.loads(args.path.read_text(encoding="utf-8-sig"))
    result = inspect(args.provider, payload)
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data + "\n", encoding="utf-8")
    else:
        print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
