# RAW_EMPTY controlled recovery — night runbook (2026-11-10)

Scope: only RAW_EMPTY in the configured input/cache. The estimated ~1,130
records are **not** a verified count until the audit completes. Remaining ~18K
are outside this campaign. No autoapproval of enriched predictions.

## Preparations

- Latest branch \`feature/argentina-nightly-readiness-20261109\`.
- Back up original inputs externally. Close Excel files.
- Review \`.env\` locally without sharing API credentials.
- The pilot requires positive per-account Search limits (for example
  \`TAVILY_LIMIT_1=20\` for \`TAVILY_API_KEY\`); all providers with configured
  accounts and budgets are eligible for rotation. Any group sharing a billing
  quota should use the same billing group and conservative shared cap.
- JEV needs its configured API key (default env \`TYPESAFE_API_KEY\`).
- Run from \`C:\p\Product_enricher\`; use \`C:\p\venv\Scripts\python.exe\`.
- Never use \`--max-search-new 0\`: the pipeline interprets it as unrestricted.

## Offline audit (no APIs)

\`\`\`powershell
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_raw_empty_night_contract.py" -v
& C:\p\venv\Scripts\python.exe .\run_controlled_night_pilot.py
\`\`\`

Inspect \`piloto_argentina\controlled_night\raw_empty_audit_precheck.json\`:
true/no-prior-error RAW_EMPTY; previous provider errors; protected keys,
RAW_MISSING, RAW_CORRUPT; the observed count may differ from ~1,130.

## Controlled REAL first batch (explicit paid authorization)

\`\`\`powershell
& C:\p\venv\Scripts\python.exe .\run_controlled_night_pilot.py --execute-live --search-budget 20 --llm-budget 60 --token-budget 120000 --timeout-minutes 120
\`\`\`

- A new isolated run directory includes the frozen 20-SKU cohort, SQLite
  snapshots, independent reports and logs. Original inputs are hash-checked.
- Search budget *for the cohort* is at most 20 distinct SKU attempts; each
  attempted SKU is reserved before HTTP and never silently repeated after
  crash. Account caps are independent and can stop the campaign earlier.
- Local model/JEV classification is capped separately. HTTP retries/repairs
  on JEV may consume additional calls inside the JEV budget.
- The parent allows one automatic restart of unexpectedly terminated/timed-out
  **collection** worker using the identical isolated SQLite ledger. API
  policy errors and provider pauses are **not** automatically restarted.
- No automatic restart of JEV classification: it lacks the same HTTP-level
  reservation guarantee and a blind retry could duplicate billable requests.
- The lock prevents two simultaneous live campaigns and is OS-released after
  a parent crash. If the parent itself exits, explicitly resume the SAME run.

## Manual resume after a parent crash

Read \`run_report.json\` and reuse the same limits and run directory:

\`\`\`powershell
& C:\p\venv\Scripts\python.exe .\run_controlled_night_pilot.py --execute-live --search-budget 20 --llm-budget 60 --token-budget 120000 --timeout-minutes 120 --resume-dir "C:\p\Product_enricher\piloto_argentina\controlled_night\<RUN_ID>"
\`\`\`

The resume command must match original budgets. It is never permissible to
start a fresh campaign to retry failed SKUs. New campaigns exclude keys from
previous runs that attempted collection, even if results were empty.

## Release gates

Before scaling, inspect \`run_report.json\`, \`quality_audit.json\`, both
stage logs and captured Search request counts. Operational PASS is **not**
taxonomy accuracy. Compare audited labels against a reviewed sample before
releasing more cohorts. Review RAW_EMPTY previous errors separately; they
are not automatically retried. Verify API balance in provider dashboard.

There is NO automatic promotion from isolated Excel/SQLite to original files,
and NO automatic progression to the remaining 18K. After a human validates
the results, promotion/sync requires a separate, audited procedure.

Production mass authorization: **BLOCKED**, even on all PASS reports.


## 2026-11-10 forensic finding / mandatory operational-error separation

The actual offline audit of configured input has **1,030 unique keys**, all
\`RAW_EMPTY\` due to Serper HTTP 400 \`Not enough credits\`, with
\`REQUEST_ATTEMPTED_NO_AUTO_RETRY\`. Consequently normal eligible count is **0**.
This is NOT search quality. The original ~1,130 was an estimate, not the
observed count of this input.

Never run the previous live command without authorization for prior errors:
it will correctly find no eligible SKUs.

The pipeline now records operational errors ONLY in the separate SQLite
attempt history, not in \`tavily_cache\`. Future \`RAW_MISSING\` keys with a prior
attempt ledger record are also excluded from automatic collection.

To explicitly pilot up to 20 of the previously failed Serper cases using
a DIFFERENT provider, add \`--allow-prior-serper-quota-error\` to the original
bounded \`--execute-live\` command. The pilot clears Serper API keys in the
worker and requires positive budgeted Tavily or Bright Data credentials.
Review account billing and cross-provider charges before consenting.

The legacy error-only tombstones remain untouched until optional audited
migration. Do NOT migrate before selecting the pilot if the goal is to
pilot previously failed Serper records, because migration will register
all previous attempts in the normal ledger and deliberately block automatic
requeries.

After the explicitly reviewed pilot, use the following **dry-run first**
migration to remove *only* error-only legacy payloads from the RAW cache and
persist every failed attempt in the classification SQLite ledger:

\`\`\`powershell
& C:\p\venv\Scripts\python.exe .\migrate_legacy_search_errors.py
\`\`\`

After checking the counts and choosing to retain the historical no-retry lock:

\`\`\`powershell
& C:\p\venv\Scripts\python.exe .\migrate_legacy_search_errors.py --apply
\`\`\`

\`--apply\` creates SQLite backup snapshots and **does not send any Search
requests**. This migration makes previous errors become \`RAW_MISSING\` in
the evidence cache, but the separate ledger ensures they do **not** become
eligible for automatic collection. Future rescue requires a separately
audited and approved operation.
