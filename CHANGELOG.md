# CHANGELOG

## 3.9.5-hf3.1p4 — Collection R9.6p4 Source Sieve + Evidence Enrichment

### Collection architecture
- Keeps the absolute hard limit of one Search API request per product.
- Inserts a Source Sieve before any page-level enrichment.
- Source Sieve requires STRONG/PROBABLE target identity and rejects compatibility/related-only sources before enrichment.
- Target Isolation now runs after the approved evidence-enrichment layer.

### Tavily
- Keeps `search_depth=basic` and `auto_parameters=false` hardcoded.
- Enables `include_raw_content="text"` inside the same Tavily Search request.
- Keeps `include_usage=true` and aborts if reported Search usage exceeds one credit.
- No Tavily `/extract`, Advanced Search, fallback query, second query, or automatic Search retry exists in the active collection path.
- Full raw content is used transiently and is not persisted; only target-isolated evidence is stored.

### Bright Data
- Locks the adapter to Google Light JSON / `parsed_light` only.
- Removes schema fallback to Full JSON-like `results[]`.
- Uses only `organic[]` as source candidates.
- Adds Query Integrity audit using `general.query`, `general.detected_query`, and `spelling`.
- Blocks unsafe query drift before Source Sieve/local enrichment.

### Serper / provider adapters
- Keeps one Search request and normalizes only organic results.
- Provider adapters use a common internal shape inspired by mature Haystack/Qwen-Agent search integrations.

### Local evidence enrichment
- Adds local HTTP enrichment only after Source Sieve.
- Uses Trafilatura as preferred main-content extractor.
- Adds stdlib HTML text fallback if Trafilatura is unavailable.
- Does not perform Search API calls, retries, or provider failover.
- Crawl4AI browser fallback remains disabled by default and was not added to the production path.

### Audit
- Adds sieve decision/reason, enrichment attempted/mode/reason, query integrity, and local-enrichment counts to Collection Quality reports.
- Removes the obsolete active `_curate_search_results` path that still carried legacy extract/retry concepts.

### Compatibility
- Keeps `PIPELINE_VERSION=3.9.5-hf3.1` to preserve existing classification/LLM checkpoints.
- Business rules, taxonomy, Candidate Funnel, LLM routing, and Revisão B.I hard locks are unchanged.
