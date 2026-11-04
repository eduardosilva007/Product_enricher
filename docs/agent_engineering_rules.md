# Selected agent engineering rules

Selected adaptations from https://github.com/mattpocock/agent-rules-books
(including ideas from legacy-code, data-intensive-systems and release-reliability practices).
These are tailored operational instructions, not a copied rule corpus.

## Legacy code
- Before modifying the large legacy pipeline, characterize current behavior at an observable interface.
- Refactoring and behavior changes belong in separate commits.
- Prefer tests for the exporter, SQLite reader, identity gate, and classifier interface over internal implementation mocks.

## Consistency
- Define each field's authority: input vs cache vs evidence vs classification vs human-reviewed output.
- Preserve the input's Brand/Model multiplicity and traceability; avoid silent deduplication.
- Treat Excel writes and SQLite changes as recoverable operations, backing up reviewed data before replacement.
- Verify idempotency on repeated runs, including interruption and crash recovery.
- Distinguish missing evidence, rejected evidence, and provider/runtime errors.

## Release
- Cover failure modes: SQLite contention, partial output, provider outage, unavailable model, bad taxonomy.
- Demand tests and a controlled real workbook export before unattended processing.
- A green CI run cannot prove locally installed models or credentials work.
- Record rollback and preserve source evidence for manual fallback.
