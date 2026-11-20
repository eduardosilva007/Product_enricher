# Overnight quality hardening — 2026-11-20

## Status

This branch is **not approved for unassisted large-scale collection**. It adds an
independent exported-title check to the overnight post-run audit and canonical
promotion. The change is deliberately fail-closed: an otherwise auto-eligible
row is rejected if its displayed `Título Real` does not structurally identify
the requested `Modelo`. Human-review proposals remain proposals.

This is a conservative SKU-specific evidence requirement. Some legitimate
manufacturer catalogues publish a parent series in the title and the exact SKU
only in a PDF/body or URL. Those rows should be sent for review, **not** auto
approved. Adjusting this policy requires an explicitly audited alternative
evidence field and negative regression tests, not a silent exemption.

## Required offline checks (PowerShell)

```powershell
cd C:\p\Product_enricher
git status --short
git branch --show-current
git rev-parse HEAD
# Preserve all local uncommitted changes before any pull/switch.
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_overnight_ksb_title_identity.py" -v
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_overnight_quality_gates.py" -v
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_overnight_review_mode.py" -v
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode validate
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode overnight
```

Without `--execute-live`, the last command is a preflight, not a paid
collection. **Do not interpret a syntax PASS or an offline integrity PASS as
semantic taxonomy approval.**

## Required before autonomous operation

1. Diagnose the failing GitHub Actions workflows against the base branch.
2. Run the negative/positive KSB fixtures and the original identity tests.
3. Review any newly blocked non-KSB cases: titles may be generic, but must
   never be promoted as automatically verified exact-SKU classifications.
4. Confirm per-provider / shared billing-group quotas, cumulative reservation
   ledger, immutable source backups, campaign checkpoint and resume settings.
5. Perform a small supervised live cohort with sufficient real collection
   evidence, then inspect `quality_audit.json` and exported `Resultados`.
6. Promote only after the campaign audit passes. Promotion persists proposals,
   **not** human taxonomy approval.
7. Scale through bounded `--campaign-total`, `--search-budget`,
   `--campaign-jev-budget`, `--campaign-token-budget` and resume using
   the original campaign directory. A JEV worker interrupted mid-request is
   intentionally not auto-replayed.

See `docs/ARGENTINA_NIGHTLY_READINESS.md` for operational resume and budget
constraints. Avoid unrestricted overnight execution while CI and live
semantics remain unverified.
