# R9.6p8.3 — Eligible Semantic Cohort + LLM Contract Validation (2026-09-30)

- Changes semantic-pilot sampling from the first N input rows to the first N **eligible** semantic-recovery rows.
- Eligibility requires usable RAW cache, target identity >= PROBABLE, and Stage1 state UNRESOLVED or AMBIGUOUS.
- Adds cohort-scan telemetry so skipped cache/evidence rows no longer consume the pilot sample.
- Filters Product Definition prompt sources to PROBABLE/STRONG only; WEAK/contextual sources are excluded.
- Adds runtime validation of required fields, types and enum values for semantic JSON responses.
- Invalid semantic cache objects are discarded instead of being silently reused.
- Invalid live semantic JSON is retried under the existing parse-retry policy and is never cached as valid output.
- Bumps the Product Definition semantic cache namespace to v3.
- Adds Reason Code to per-item END telemetry and renames the counter to `live_llm_calls`.
- Keeps Base Final/Consolidado read-only and performs zero Search-provider requests in option 7.
- Preserves the one-product/one-Search-provider-request invariant.

# R9.6p8.2 — Semantic Pilot Telemetry + Safe Partial Output (2026-09-30)

- Adds START/END logging for every pilot item, including branches that previously exited before the progress log.
- Adds explicit LLM DEFINE and LLM MAP START/END logs with provider, model, cache hit, decision and elapsed time.
- Makes provider pacing waits visible instead of silently sleeping between requests.
- Writes a safe partial pilot Excel snapshot every 5 processed items by default.
- Writes an INTERRUPTED/FAILED_PARTIAL snapshot on Ctrl+C or runtime failure when rows are available.
- Keeps completed LLM responses in SQLite and makes restart behavior visible.
- Changes the interactive semantic-pilot default from 100 to 10 items for architecture validation before scale.
- Does not change Product Definition prompt semantics, taxonomy, Search policy, Base Final, Consolidado, or Revisão B.I hard locks.
- Preserves the absolute one-product/one-Search-provider-request invariant.

# R9.6p8.1 — Product Definition Prompt Contract (2026-09-29)

- Reframes the semantic LLM stage from “identify the physical product” to “define the primary product represented by the evidence”.
- Requires the most specific technical/commercial product name supported by the supplied evidence.
- Explicitly forbids forcing the answer into the internal taxonomy or returning only an industry/use/function/generic class when a specific product is evidenced.
- Keeps Category General and FAMILY outside the Product Definition prompt; taxonomy remains a downstream controlled step.
- Renames the pilot output column to `Produto Definido LLM`.
- Bumps the semantic prompt namespace so prior p8 semantic responses are not reused from cache.
- Keeps the pilot read-only for Base Final/Consolidado and performs zero Search-provider calls.

# R9.6p8 — Semantic Product Pilot (2026-09-29)

- Adds a pilot-only LLM-first product identification stage using existing curated evidence.
- LLM returns `product_type_raw`, product role, domain hint, power, cited source IDs and a verbatim evidence phrase.
- Rejects semantic output when the evidence phrase is not literally grounded in a cited target source.
- Keeps full taxonomy authority in `regras_negocio.json`; LLM never creates Category General or FAMILY.
- Adds local semantic candidate retrieval over canonical types, aliases and families; only the top candidates are exposed to the taxonomy validator LLM.
- Adds `role_overrides` so component/module/fixed-part evidence can normalize to `PART` without losing the richer raw physical description.
- Adds semantic bridge rules for explosion-proof axial blower, blower assembly and rotary tiller.
- Adds semantic-only taxonomy evidence rules for the six B.I.-validated deviations from the 22 classified items.
- `OTHERS|OTHERS` is allowed only as an explicit evidence-driven proposal for authorized types (`PART`) and is not enabled as a generic production fallback.
- Adds `semantic` mode and menu option 7; the pilot never writes Base Final or Consolidado.
- Preserves max 1 Search-provider request per product and does not change the collection path.
- All previous regression tests plus 6/6 semantic gold cases pass.

# CHANGELOG

## R9.6p7 — Import Noise Taxonomy + Strong Identity (2026-09-29)
- Requires STRONG Brand+Model identity for deterministic, funnel, combo-kit, and LLM final approval.
- Keeps PROBABLE sources available for evidence/review but blocks them from `AUTO_APROVADO`.
- Normalizes legal company suffixes such as SA/LTD/LLC for brand identity without fuzzy matching.
- Normalizes punctuation before TYPE/POWER lexical matching, improving matches such as `Pneumatic Blower, Venturi`.
- Restricts garden `BLOWER` to explicit leaf/garden/handheld/backpack/power-tool wording.
- Adds import-noise taxonomy under `OTHERS`: INDUSTRIAL BLOWER, VENTURI BLOWER, AXIAL FAN, COOLING FAN, FAN BLADE, FAN MODULE, FAN MOTOR, SUCTION FAN, and COOLER.
- Adds non-water pump guards and dedicated OTHERS families for WINDSCREEN WASHER PUMP, INK PUMP, FUEL PUMP, OIL PUMP, VACUUM PUMP, and CONDENSATE PUMP.
- Adds `TILLERS | TILLERS` fallback for TILLER when fuel is not explicitly proven.
- Adds multilingual `POLISHER` coverage for dual-action polishers, plus PRESS TOOL and generic MIXER fallbacks.
- Makes specific fan/pump physical families outrank generic PART when both are explicitly present.
- Ensures every truly RESOLVED row has `Próxima Ação = CONCLUIDO`.
- Ensures resolved `Título Real` comes from the STRONG source that supported the TYPE; unresolved rows still preserve a useful reference title.
- Preserves the one-product/one-Search-request invariant and all Revisão B.I hard locks.

## 3.9.5-hf3.1p5 — Bright Data JSON Contract Fix

### Critical collection fix
- Corrects Bright Data SERP request payload to `format=json` + `data_format=parsed_light`.
- Adds fail-closed validation that rejects any Bright Data `format` other than `json`.
- Adds regression coverage asserting the exact outgoing Bright Data payload.
- Keeps the one-product/one-Search-request invariant unchanged.
- No taxonomy, LLM, business-rule, cache, or classification behavior was changed.

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

## R9.6p6 — Classification Recovery + Clean Identity (2026-09-29)
- Separates `Search Model` from `Query Model`: identity keeps structural punctuation while Search queries normalize separators to spaces.
- Adds configurable removal of trailing operational phrases such as `TYPE BM` without altering the original model or business key.
- Reuses `TAXONOMY_EVIDENCE_RULES` as deterministic TYPE candidate recovery when brand/model/evidence already support the same canonical type.
- Adds `POWER TOOL COMBO KIT` with explicit multi-tool detection; a single tool + battery/charger/accessories does not become a combo kit.
- Adds `ROTARY TOOL` and evidence-specific `MULTISTAGE CENTRIFUGAL PUMP` coverage.
- Aligns `HAMMER DRILL` business family to `IMPACT DRILL`.
- Base Final now shows a single useful `Título Real`; detailed reference-title audit remains in the consolidated/master output.
- When classification cannot close, the best Search title is preserved as `Título Real` for human review.
- Keeps Search cost invariant unchanged: max 1 provider request per product, no automatic retry/fallback/failover/Extract/Advanced.
