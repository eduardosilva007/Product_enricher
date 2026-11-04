# ADR 0001 — Preserve unresolved products and evidence

Status: Accepted
Date: 2026-11-04

## Context
Previous releases surfaced cases where products had cached/source titles but unresolved classifications appeared without reviewable titles. Loss of rows or source detail makes manual recovery expensive.

## Decision
Every input record must survive the final export, including duplicates and unclassified products. The final output must retain observed reference titles, URLs and source evidence whenever available; a weak/rejected source is only for review, never for auto-approval. Empty title is acceptable only when no observed usable title exists. No fallback may fabricate evidence or a taxonomy.

Require an explicit workbook input/output contract check in the postrun release gate before READY_FOR_SCALE.

## Consequences
Postrun requires access to the same input and final workbook, not just classifier benchmark results. Producers must preserve output columns and manual review artifacts. Existing preflight/CI remain independent from actual runtime/export verification.
