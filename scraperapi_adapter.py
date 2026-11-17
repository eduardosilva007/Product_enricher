"""ScraperAPI offline response adapter and credit preflight.

This module does NOT send HTTP, read API keys, write SQLite, or promote
classifications. It is a provider-specific staging contract for future integration.
Costs: https://docs.scraperapi.com/getting-started/quick-start/credits-and-requests-costs
"""
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


BASE_CREDITS = {"normal": 1, "ecommerce": 5, "google_serp": 25, "bing_serp": 25}


@dataclass(frozen=True)
class ScraperAPIBudget:
    remaining_credits: int
    maximum_credits_per_request: int = 25

    def authorize(self, estimated_cost: int) -> bool:
        return (self.remaining_credits >= 0
                and estimated_cost > 0
                and estimated_cost <= self.remaining_credits
                and estimated_cost <= self.maximum_credits_per_request)


def baseline_cost_for_url(url: str) -> int:
    """Conservative documented base-domain estimate, not authoritative billing."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("SCRAPERAPI_URL_INVALID")
    host = parsed.hostname.lower().removeprefix("www.")
    if host == "google.com" or host.endswith(".google.com"):
        return 25
    if host == "bing.com" or host.endswith(".bing.com"):
        return 25
    if host in ("amazon.com", "walmart.com", "ebay.com"):
        return 5
    return 1


def build_request_plan(url: str, budget: ScraperAPIBudget,
                       *, premium: bool = False, render: bool = False,
                       ultra_premium: bool = False) -> dict:
    """Fail closed for cost-changing options; live request must use urlcost first."""
    if premium or render or ultra_premium:
        raise ValueError("SCRAPERAPI_COST_MULTIPLIER_REQUIRES_URLCOST_PREFLIGHT")
    estimated = baseline_cost_for_url(url)
    if not budget.authorize(estimated):
        raise ValueError("SCRAPERAPI_BUDGET_EXCEEDED")
    return {"provider": "scraperapi", "target_url": url,
            "estimated_base_credits": estimated, "api_requests_max": 1,
            "retry": False, "render": False, "premium": False,
            "verified_cost": False, "status": "OFFLINE_PLAN_ONLY"}


def normalize_structured_serp(payload: Any) -> list[dict]:
    """Candidate-only interpretation. No assumptions about live endpoint schemas."""
    if not isinstance(payload, dict):
        raise ValueError("SCRAPERAPI_STRUCTURED_JSON_REQUIRED")
    rows = payload.get("organic_results", payload.get("organic", None))
    if not isinstance(rows, list):
        raise ValueError("SCRAPERAPI_UNKNOWN_SERP_SCHEMA")
    candidates = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("link") or row.get("url") or "").strip()
        parsed = urlsplit(url)
        if not title or parsed.scheme not in ("https", "http") or not parsed.netloc:
            continue
        candidates.append({
            "provider": "scraperapi", "section": "organic", "position": index + 1,
            "title": title, "url": url,
            "content": str(row.get("snippet") or row.get("description") or ""),
            "identity": "UNVERIFIED", "approval": "BLOCKED_PENDING_IDENTITY",
        })
    return candidates


def credit_audit(response_headers: dict) -> dict:
    """Read documented response charge without fabricating a zero when absent."""
    value = next((v for k, v in response_headers.items()
                  if str(k).lower() == "sa-credit-cost"), None)
    if value is None:
        return {"reported_credits": None, "cost_verified": False}
    try:
        cost = int(value)
    except (ValueError, TypeError):
        raise ValueError("SCRAPERAPI_CREDIT_HEADER_INVALID")
    if cost < 0:
        raise ValueError("SCRAPERAPI_CREDIT_HEADER_INVALID")
    return {"reported_credits": cost, "cost_verified": True}
