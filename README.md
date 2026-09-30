# Product Enricher — R9.6p8.3 Product Definition Pilot


## R9.6p8.3 — eligible semantic cohort + validated LLM JSON contract

This patch fixes two issues revealed by the p8.2 pilot log:

1. `--max-items 10` previously meant the first 10 input rows, even when they had no usable cache or no target evidence. In p8.3 it means **10 eligible semantic-recovery items**: usable RAW cache + target evidence at least PROBABLE + Stage1 state UNRESOLVED or AMBIGUOUS.
2. NVIDIA may return syntactically valid JSON that does not fully respect the requested schema. p8.3 validates required fields/types/enums before accepting or caching the response. Invalid semantic cache entries are discarded and the call is redone.

The semantic LLM receives only PROBABLE/STRONG target sources; WEAK/contextual sources are excluded from the Product Definition prompt. The semantic prompt cache namespace is bumped to `v3` so the pilot starts clean against the corrected contract.

The terminal now prints cohort-selection telemetry before the first LLM call and includes the final `Reason Code` per item.

## R9.6p8.2 — product definition pilot telemetry

This release adds a **pilot-only LLM-first semantic layer** without replacing the current production classification path and without changing the Search cost invariant.

Pilot flow:

```text
Existing curated evidence
        ↓
LLM defines the primary product with the most specific evidence-supported technical/commercial name
        ↓
Grounding check: cited source + verbatim evidence phrase
        ↓
Role normalization (WHOLE_PRODUCT / COMPONENT_PART / ACCESSORY / ASSEMBLY)
        ↓
Local taxonomy retrieval from regras_negocio.json
        ↓
Deterministic match OR top candidate set
        ↓
LLM validates only the candidate set (or NONE)
        ↓
JSON maps Category General + FAMILY
```

The Product Definition pilot asks the LLM to **define the primary product**, not to pre-classify it: it must return the most specific technical/commercial product name supported by the supplied evidence, without seeing or forcing Category General/FAMILY.

The Product Definition pilot **does not update Base Final or Consolidado**. It writes `Piloto_Definicao_LLM_R9_6p8_3.xlsx` so the behavior can be audited before production adoption.

R9.6p8.2 adds operational telemetry for the pilot: every item logs START/END, every LLM definition/mapping call logs START/END with provider/model/cache/duration, provider pacing waits are visible, and a partial Excel snapshot is persisted every 5 items and on Ctrl+C/failure. The interactive pilot default is now 10 items instead of 100.

Important: R9.6p8.2 does **not** implement Collection Engine V2 yet. It tests whether richer use of the LLM can correctly interpret the evidence already present in cache. Collection V2 remains the next collection-focused step.

Run from the menu with option `7 - PILOTO DEFINIÇÃO LLM (NÃO ALTERA BASE)` or:

```powershell
python pipeline_produtos.py --mode semantic --max-items 10
```


## Core invariant

**One product = at most one Search API request.**

The collection layer fails closed if any provider path attempts a second Search request. Search retry, second query, provider failover, Tavily `/extract`, and Tavily `advanced` are not part of the active collection path.

## Current collection architecture

```text
Brand + Original Model
        ↓
Search Model (original values remain untouched)
        ↓
ONE Search provider
Tavily | Serper | Bright Data
        ↓
Query Integrity Gate
(Bright Data: general.query / detected_query / spelling)
        ↓
Source Sieve
Identity STRONG/PROBABLE + source-purpose checks
        ↓
Only approved sources may be enriched
        ↓
Evidence Enrichment
├─ Tavily: raw_content returned by the SAME basic Search
└─ Serper/Bright Data: local HTTP fetch + Trafilatura
        ↓
Target Isolation
        ↓
Collection Quality
READY / PARTIAL / LOW / NO_TARGET
        ↓
Cache
        ↓
Classification
```

## Search providers

### Tavily

- `search_depth=basic` is hardcoded in the executor.
- `auto_parameters=false`.
- `include_usage=true`.
- `include_raw_content="text"` is requested inside the same Search call.
- If Tavily reports `usage.credits > 1`, collection aborts with a cost-policy violation.
- No `/extract`, Advanced Search, second query, automatic retry, or provider failover.

### Serper

- One `POST /search` per product.
- Only `organic[]` is normalized.
- `title`, `link`, and `snippet` become the common search-result contract.
- If a source passes Source Sieve and the snippet is insufficient, local enrichment may fetch the approved URL without another Search API request.

### Bright Data

- **Only Google Light JSON is supported: `format=json` + `data_format=parsed_light`.**
- Raw HTML (`format=raw`), Full JSON, Parsed Bing, Markdown, and Screenshot are intentionally not supported in this pipeline.
- Only `organic[]` feeds source candidates.
- `general.query`, `general.detected_query`, and `spelling` are audited before Source Sieve.
- Unsafe query drift blocks the SERP before any URL enrichment.

## Source Sieve

A search result cannot be enriched just because it has a high provider score. It must first prove target identity.

Accepted for enrichment:
- `STRONG`
- `PROBABLE`

Rejected before enrichment:
- weak/contextual target identity
- compatibility-only pages
- related/similar-product context
- Bright Data query identity drift

Provider rank is only a secondary ordering signal.

## Evidence enrichment

### Tavily one-step evidence

Tavily raw content is returned by the same Search request and is used only after the result passes Source Sieve. Full raw content is not persisted to SQLite; only the isolated target evidence and a boolean availability marker are stored.

### Local HTML enrichment

For Serper/Bright Data, approved sources may be downloaded locally. This does not call a Search provider.

Default behavior:
- max 2 approved URLs per product
- one local GET attempt per URL
- no local retry
- HTML/text only
- response size cap
- Trafilatura preferred for main-content extraction
- stdlib HTML text extraction fallback if Trafilatura is unavailable
- browser/JavaScript crawler fallback disabled

Crawl4AI was evaluated but is not enabled in the default production path because it adds Chromium/browser operational complexity. It remains a future option for a measured residual set of JS-only sources.

## GitHub patterns incorporated

- Provider adapters follow the same simple normalization principle seen in Haystack and Qwen-Agent Serper integrations: provider-specific `organic` rows become one internal search-result shape.
- Trafilatura is used as the preferred local content extractor because its main extractor is designed to isolate useful page text and offers a precision-oriented mode.
- Search-provider behavior remains separate from product identity and taxonomy logic.

## Required environment variables

```text
TAVILY_API_KEY=
SERPER_API_KEY=
BRIGHTDATA_API_KEY=
BRIGHTDATA_ZONE=
```

Only the selected Search provider needs to be configured for a collection run.

## Install

```bash
pip install -r requirements.txt
```

## Main menu

```text
1 - COLETA / EXTRAÇÃO R9.6
2 - CLASSIFICAÇÃO
3 - PROCESSO COMPLETO
4 - RECUPERAR PENDÊNCIAS DO CACHE (SEM BUSCA)
5 - AUDITORIA / REPARO BASE (SEM LLM)
6 - CURAR CACHE LEGADO (SEM BUSCA / 0 CRÉDITOS)
7 - PILOTO DEFINIÇÃO LLM (NÃO ALTERA BASE)
8 - SAIR
```

For current operations, collection and classification should still be evaluated independently when changing providers or collection logic.

## R9.6p6 classification recovery

R9.6p6 separates model representations by purpose: the original model is never changed, identity keeps structural SKU syntax, and the Search query receives a punctuation-clean model with separators converted to spaces. It also adds deterministic TYPE recovery from existing taxonomy-evidence rules, explicit `POWER TOOL COMBO KIT` handling, and title preservation for unresolved items. The Base Final shows only `Título Real`; detailed title-reference metadata remains available in the consolidated/audit output.


## R9.6p7 import-noise taxonomy and approval gate

R9.6p7 hardens the classification stage for noisy import datasets. A classification can only become `AUTO_APROVADO` when the supporting source has `STRONG` identity: target Brand + structural Model must be anchored in the source TITLE/URL. Exact model matches without the target brand may still be retained for review, but cannot auto-approve.

Fan/cooler/industrial-blower noise is explicitly routed away from garden-equipment categories and into `OTHERS` with a physical family such as `AXIAL FAN`, `COOLING FAN`, `FAN BLADE`, `FAN MODULE`, `FAN MOTOR`, `SUCTION FAN`, `INDUSTRIAL BLOWER`, `VENTURI BLOWER`, or `COOLER`. Generic `BLOWER` is now garden/power-tool specific.

Non-water pump phrases such as windscreen/windshield washer pump, ink pump, fuel pump, oil pump, vacuum pump, and condensate pump are blocked from `WATERPUMP` and use dedicated `OTHERS` families when target identity is strong.

Resolved rows always export `Próxima Ação = CONCLUIDO`. `Título Real` is the title of the STRONG source that actually supported the resolved TYPE; unresolved rows retain the best reference title only for manual review.
