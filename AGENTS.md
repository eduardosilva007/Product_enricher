# Product Enricher — agent contracts

Read GLOSSARY.md and relevant docs/adr before modifying production behavior.
Treat .agents/skills as the source of operational procedures.

## Non-negotiable invariants
- Never remove a product from the final review output because classification failed.
- Preserve available source titles, source URLs, evidence and diagnostic rejection reasons for manual review. Rejected sources remain non-authoritative.
- Preserve Brand + Model identities and duplicate multiplicity; do not silently deduplicate inputs.
- Do not invent titles or elevate weak / wrong-model evidence to automatic approval.
- Revisão B.I = OK remains a hard lock.
- The Gold benchmark is evaluation-only, never production training/inference context.
- No SKU-specific fixes to make the benchmark pass.
- Provider failures are operational errors, not product evidence failures.
- Never expose credentials or raw .env values.

## Mandatory change loop
1. Reproduce the reported symptom with a deterministic failing test through a real interface.
2. Document the hypothesis and affected seams; check change impact.
3. Fix one vertical slice and make the test pass.
4. Run affected tests, architecture and repository checks.
5. Review both coding standards and requirements against the diff.
6. Run release preflight. For scale, run postrun with --input, --final-output and --result.
7. Never merge or run long classification jobs if the release gate is BLOCKED.

See .agents/skills/diagnosing-bugs/SKILL.md and .agents/skills/code-review/SKILL.md.
