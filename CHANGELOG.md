# CHANGELOG — R9.7 Cache-First Free-Tier

## 2026-09-30

### Added
- New `cachefirst` runtime mode and interactive option 8.
- Evidence Pack builder over the existing Tavily SQLite cache.
- Generic SERP-title rejection for domain/homepage titles.
- Cross-brand primary-source detection using the local cache brand catalogue.
- Target-model evidence windows from cached raw content/snippets.
- Local taxonomy retrieval with backend cascade:
  - local Sentence Transformers when already installed/cached;
  - scikit-learn TF-IDF;
  - stdlib lexical fallback.
- Local result checkpoint table `cache_first_results` in `pipeline_v3.sqlite`.
- JSONL evidence-pack export for future batch/grounded-model processing.
- Deterministic `LOCAL_EXACT_ALIAS_ACCEPTED` gate requiring STRONG identity and explicit specific alias.
- New taxonomy types/families:
  - CROSS FLOW FAN
  - PROPELLER FAN
  - SOLDERING STATION
  - WINDSCREEN WASHER RESERVOIR
- New synthetic regression cases for the four taxonomy additions.

### Changed
- Build label moved to `v3.9.5-HF3.1p9 R9.7 Cache-First Free-Tier`.
- Rules version moved to `2026-09-30-v3.0.12-r9.7-cache-first-free-tier`.
- Cache-first cohort is deterministic and sampled across unresolved/ambiguous records by hash, avoiding first-N clustering.
- `WINDSCREEN WASHER PUMP` now has reservoir-specific negative aliases.
- Generic titles are no longer surfaced as `Título Real` in cache-first output.
- FAN negatives include CROSS FLOW / TANGENTIAL / PROPELLER contexts to preserve specific fan types.

### Preserved
- One Search-provider request per product hard limit.
- No automatic Search retry / fallback / second query / Tavily Extract.
- `Revisão B.I=OK` absolute hard lock.
- Existing R9.6 source-sieve and collection-cost policies.
- Existing deterministic and semantic regression gates.
