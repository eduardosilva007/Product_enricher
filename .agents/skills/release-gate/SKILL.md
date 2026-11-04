---
name: release-gate
description: Orchestrate architecture, repository, tests, runtime, benchmark and final-workbook integrity checks before scale.
---

# Release Gate

Preflight (local):
    python tools/release_gate.py --phase preflight --mode local
Only READY_FOR_CONTROL_RUN permits a controlled classification run.

Postrun (local):
    python tools/release_gate.py --phase postrun --mode local --result CLASSIFICATION_RESULT.xlsx --input "Base de Produtos.xlsx" --final-output "Base Final Produtos Enriquecida.xlsx"
Only READY_FOR_SCALE permits unattended scale runs.

Preflight includes architecture, repository, change impact, unit/contract tests and runtime doctor.
Postrun additionally requires frozen acceptance evaluation AND workbook integrity checks.
The workbook gate checks input row multiplicity, input keys, preservation of supplied reference titles/URLs and that unresolved rows with available observed reference titles do not lose all titles.
Do not treat CI green as live provider/model verification. Do not mark PASS on missing input or final export.
Never turn an operational failure into an evidence quality status.
