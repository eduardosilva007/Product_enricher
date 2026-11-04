# Product Enricher — domain glossary

**Input product**: One row from the requested input workbook, identified by Marca and Modelo; duplicate rows retain multiplicity.

**Evidence**: Source title, URL, extract and collection/identity metadata observed during search/cache processing. Rejected evidence remains traceable but cannot support automated classification.

**Reference title (Título Referência)**: Non-invented title from observed search/cache evidence, preserved for review. Not proof of SKU identity by itself.

**Real title (Título Real)**: Product title supported by an appropriate target source when classification is resolved. Unresolved records may retain an observed useful title without implying approval.

**Identity**: Separate proof that a source describes the requested Brand + Model, not merely a related SKU, compatible part or another variant.

**TYPE**: Canonical product type inferred by rules and/or constrained decision stages; not the same as a raw GLiNER span.

**Category General / FAMILY**: Controlled taxonomy pair assigned only according to business rules.

**Manual review**: A product whose automated classification is unresolved. It must remain present in the final output with all available non-fabricated evidence.

**Operational error**: Runtime, provider or configuration failure. Must not be conflated with insufficient product evidence.

**Gold**: Frozen manually reviewed reference used solely for evaluation of predictions; prevent overlap with training/test cohorts.

**Release gate**: Composed checks that must pass before classifying at scale. CI-only runtime verification does not prove local models/providers work.
