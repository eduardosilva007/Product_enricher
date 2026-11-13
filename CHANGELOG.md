# CHANGELOG

## 2026-11-11 — Controlled overnight interruption and durable resume

- `--campaign-pause-after-sublots 1` checkpoints a running campaign after its first fully audited sublot, without promoting an incomplete campaign or issuing any more API calls.
- Resume uses the same `campaign.json` directory via `--campaign-resume-dir`, original target and global/sub-batch limits; the second batch consumes only its own reserved allowances.
- Offline regression covers 5+5 SKU continuity, unique cohort keys, reserved call/token ledger, delayed promotion and `COMPLETE` after resume.
- A separate interruption regression verifies JEV classifications that have started are **not** automatically retried: `JEV_INCOMPLETE_REQUIRES_AUDIT` remains fail-closed. The checkpoint test does not claim mid-request crash recovery.
- User acceptance: pause at 5, observe `PAUSED_FOR_RESUME_TEST`, resume exact campaign, observe 10 unique audited products and `production_persisted=true`.

## 2026-11-11 — Canonical promotion: exact human-lock validation

- Corrected false `BI_OK_HARD_LOCK_VIOLATION` in a completed 50-SKU offline promotion. The erroneous comparison matched human-reviewed records solely by normalized `business_key`, conflating historic ANNOVI/BOSCH Brand+Model variants and comparing their different `Status` / `Revisão` values.
- Promotion now checks each protected physical row in its original position, including exact Brand+Model, type, power, Category General, FAMILY, title, Status, Revisão and Revisão B.I. Historic duplicate exact pairs are compared separately. Any real protected-field alteration remains blocking.
- Removed redundant promotion-only forensic diff grouped by normalized key. Original non-cohort rows remain checked individually and untouched by the input-scoped overlay.
- Added real negative tests for human revision mutation, exact-pair mutation and end-to-end promotion with colliding BI-approved variants, including immutable pre-commit backup recovery. No Search or JEV calls.

## 2026-11-11 — Canonical Base Final persistence for overnight campaigns

- Introduces offline `--mode overnight-promote --campaign-resume-dir <ID>` to import the previous completed 50-SKU campaign into the official Search cache, Consolidado and Base Final without new API calls.
- New `--mode overnight` campaigns automatically promote audited `COMPLETE` outputs into the same canonical files before returning success. A failed promotion makes overnight fail rather than silently reporting complete and persisted.
- Canonical `persist_baseline_excels` remains responsible for status, `Category General`, `FAMILY`, revision metadata and the absolute Manual / Revisão B.I=OK protection. The promotion routine does not designate proposals as human-approved.
- Promotions require matching sublot cohorts, PASS collection/classification/audits, all RAW_OK, and only in-cohort classification outputs. Missing classification rows remain measurable as pending rather than being invented.
- Source files are backed up before staged cache/Excel generation; source hashes, protected manual cells, final cohort coverage and cache persistence are checked before swapping canonical artifacts.
- The campaign manifest records `promotion.status=COMMITTED` and imported/missing classification counts, or fail-closed recovery state. Backups remain under the campaign for disaster recovery.
- Adds a real offline regression fixture covering canonical Final+cache persistence, protection of a BI-reviewed record and idempotent promotion.
- Synchronization is cohort-scoped and blocks any change to an unrelated Base Final record, including unreviewed rows; classifications and LLM checkpoints are also merged into the official SQLite.

## 2026-11-11 — Master overnight stabilization (branch: feature/overnight-master-stabilization-20261111)

- Centralizes the runnable overnight pipeline, provider account router, RAW_EMPTY cohort audit, protected-human-record forensic diff and campaign orchestration inside `pipeline_produtos.py`.
- Adds `--mode overnight` with `--campaign-total` (50, 100, 250, 500 or remaining SKU count), independent Search sublots capped at 20 SKUs, and explicit cumulative JEV-request/token budgets.
- Persists campaign progress and exact active-sublot resume in `campaign.json`; reserves upper-bound JEV usage before a new sublot, so failures cannot reissue a new budget for the same sublot.
- Carries forward prior SQLite Search reservations and disabled provider accounts across isolated sublots; duplicate SKUs, conflicting ledgers, quota exhausted and inconsistent RAW coverage block subsequent sublots.
- Runs `collect -> classify -> protected-source/coverage audit` for every sublot; never promotes unreviewed results to production.
- Retains `nightly_provider_router.py` and `run_controlled_night_pilot.py` solely as compatibility import/CLI shims; business rules and campaign logic reside in the master.
- Adds offline regression tests for cumulative quotas, disabled providers, master 50-SKU campaign budgeting and checkpoint/resume.
- The master migration is verified by GitHub Actions; first live 50-SKU assisted run remains a separate release gate.

## 2026-11-11 — Controlled nightly readiness and review safety

- Fixed `CLEF_INSUFFICIENT` returning `Review Required = NÃO` even when `Classification Outcome = REVIEW_CACHE_EVIDENCE`; now requires human review and blocks automatic approval.
- Historical impact: three rows in the first isolated 20-SKU pilot (CNH 47507106, 47945518 and 48137950) were flagged incorrectly. Their original Excel files remain unchanged as audit evidence; no automatic taxonomy or production-data backfill is authorized.
- The full `tests/` suite now runs in GitHub Actions with offline dependencies, including regression guards for the review states, Excel sanitization and account quotas.
- The multiprovider router is integrated into `collect_products` with SQLite reservations before HTTP. `billing_group` conflicting limits now fail closed.
- Isolated pilots now import preceding reservation ledgers into each new copied SQLite before collection so account usage does not reset between pilot runs; missing/conflicting prior reservations block new execution.
- Operational limits remain 20 Search SKUs per run, with no automatic promotion to production; taxonomy and historical-data remediation require separate audited approval.

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
