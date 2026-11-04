# Evaluation protocol (Python-first)

Inspired by https://github.com/mattpocock/evalite; do not install TypeScript dependencies.

## Frozen data
The acceptance/v42_gladiator_10.csv cohort is a **smoke regression**, not evidence of population accuracy.
Manually curated training records must be split by normalized brand + SKU family **before fitting**:
train for fitting, validation for tuning, held-out test for final evaluation.
Near variants cannot cross partitions. Keep the frozen holdout independent from production rules and prompts.
Never copy a predicted label into Gold, and never introduce SKU-specific production exceptions to pass acceptance.

## Evaluation checks
- Exact Category General + FAMILY pair accuracy, with per-FAMILY support and failures.
- Separate detection of missing output, duplicate product keys, empty Gold, missing reference labels and ambiguous results.
- Future pilots should separately report identity, type, power, classification coverage/abstentions, wrong auto-approval and error reasons.
- Compare errors to previous accepted baseline, tracking version, rules hash, cohort and split seed.
- Do not infer 23k-item performance from the ten-item smoke cohort.
- Do not commit proprietary Gold XLSX or confidential product evidence.

## CLI
python tools/classification_eval.py --result "pilot.xlsx" --gold acceptance/v42_gladiator_10.csv --report-json "reports/eval.json"

Run postrun release gate with matching input, classifier result and Base Final output; a green smoke benchmark alone does not authorize scaling.
