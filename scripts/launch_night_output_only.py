"""Run approved output-only overnight using archived code and ORIGINAL baseline.

No worktree, no branch switch, no local source edits. The executable Python
comes from the clean git archive. New campaign sublots are recorded in the
ORIGINAL repo's controlled_night history. Canonical Excel remains immutable.

Example:
 python scripts/launch_night_output_only.py --repo C:\\p\\Product_enricher --target 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


SNAPSHOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SNAPSHOT))
import pipeline_produtos as core


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true",
                        help="Explicitly authorize paid Search/JEV; omit for no-API preflight")
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--per-sublot", type=int, default=20)
    parser.add_argument("--llm-budget", type=int, default=20)
    parser.add_argument("--token-budget", type=int, default=100000)
    parser.add_argument("--campaign-jev-budget", type=int, default=20)
    parser.add_argument("--campaign-token-budget", type=int, default=100000)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--timeout-minutes", type=int, default=120)
    args = parser.parse_args()

    original = args.repo.resolve()
    if SNAPSHOT == original:
        raise RuntimeError("ARCHIVED_CODE_REQUIRED: never run the dirty original pipeline")
    if not (SNAPSHOT / "pipeline_produtos.py").is_file():
        raise RuntimeError("SNAPSHOT_PIPELINE_MISSING")
    if not 1 <= args.target <= 940:
        raise RuntimeError("TARGET_MUST_BE_BETWEEN_1_AND_940")
    if not 1 <= args.per_sublot <= 20:
        raise RuntimeError("SUBLELOT_BUDGET_MUST_BE_1_TO_20")
    if not (1 <= args.llm_budget <= 100 and 1 <= args.token_budget <= 200000):
        raise RuntimeError("SUBLELOT_JEV_TOKEN_CAP_INVALID")
    if not (1 <= args.campaign_jev_budget and 1 <= args.campaign_token_budget):
        raise RuntimeError("EXPLICIT_CAMPAIGN_BUDGET_REQUIRED")

    config_path = original / "pipeline_config.json"
    if not config_path.is_file():
        raise RuntimeError(f"ORIGINAL_PIPELINE_CONFIG_MISSING: {config_path}")
    cfg = core.read_json(config_path)
    files = dict(cfg.get("files", {}))
    required = ("input", "cache", "master", "final", "classification_db")
    for name in required:
        value = files.get(name)
        if not value:
            raise RuntimeError(f"CANONICAL_FILE_CONFIG_MISSING: {name}")
        path = Path(value)
        resolved = (path if path.is_absolute() else original / path).resolve()
        if name != "classification_db" and not resolved.is_file():
            raise RuntimeError(f"CANONICAL_FILE_NOT_FOUND: {name}: {resolved}")
        files[name] = str(resolved)
    cfg["files"] = files

    env_path = args.env_file.resolve() if args.env_file else original / ".env"
    if env_path.is_file():
        core.load_env_file(env_path)
    if not core.configured_slots():
        raise RuntimeError(
            "NO_CONFIGURED_SEARCH_PROVIDER_WITH_POSITIVE_ACCOUNT_LIMIT: "
            "check .env, TAVILY_LIMIT_1 / BRIGHTDATA_LIMIT_1 / SERPER_LIMIT_1 "
            "or pass --env-file to the existing key file"
        )

    historical_root = original / "piloto_argentina" / "controlled_night"
    if not historical_root.is_dir():
        raise RuntimeError("ORIGINAL_CONTROLLED_NIGHT_HISTORY_MISSING")
    sources = {name: Path(files[name]) for name in required}
    audit = core.overnight_audit(
        sources["input"], sources["cache"], sources["master"], sources["final"]
    )
    history_reports = sorted(historical_root.glob("*/run_report.json"))
    prior_attempts = set()
    for report_path in history_reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("stages", {}).get("collection", {}).get("attempts", 0):
            prior_attempts.update(report.get("cohort_keys") or [])
    available = [
        row for row in audit["candidate_rows"]
        if row["business_key"] not in prior_attempts
    ]
    # A temporary, disposable ledger exercises the complete historic
    # reservation/account audit without modifying any real database.
    with tempfile.TemporaryDirectory(prefix="night_preflight_") as temp:
        reservation_audit = core.import_prior_reservations(
            Path(temp) / "audit.sqlite", historical_root
        )
    print("NO-API ELIGIBILITY | raw_empty_new=%d | prior_attempted=%d | remaining=%d"
          % (len(audit["candidate_rows"]), len(prior_attempts), len(available)), flush=True)
    print("NO-API HISTORY |", json.dumps(reservation_audit, ensure_ascii=False), flush=True)
    if len(available) < args.target:
        raise RuntimeError(
            "INSUFFICIENT_ELIGIBLE_UNTRIED_SKUS: "
            f"requested={args.target}, available={len(available)}. "
            "Use the exact remaining amount; never reissue prior Search."
        )
    if not args.execute_live:
        print("NIGHT_PREFLIGHT_PASS_NO_APIS; zero paid calls. "
              "Add --execute-live only after reviewing the budgets.", flush=True)
        return 0

    # Important: the classifier executable is always from the archive, but
    # reservation and run reports must remain alongside previous real pilots.
    core.OUT = historical_root

    working_config = SNAPSHOT / "night_output_only_local_config.json"
    if working_config.exists():
        prior_cfg = json.loads(working_config.read_text(encoding="utf-8"))
        if prior_cfg != cfg:
            raise RuntimeError("SNAPSHOT_CONFIG_CHANGED: use a fresh archived copy")
    else:
        working_config.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    print("MODE: ISOLATED OUTPUT-ONLY / NO CANONICAL PROMOTION", flush=True)
    print("SOURCE REPO:", original, flush=True)
    print("HISTORY:", historical_root, flush=True)
    print("CODE:", SNAPSHOT, flush=True)
    print("TARGET:", args.target, flush=True)
    print("SEARCH PER SUBLOT:", args.per_sublot, flush=True)
    print("JEV CAMPAIGN MAX:", args.campaign_jev_budget, flush=True)
    print("TOKEN CAMPAIGN MAX:", args.campaign_token_budget, flush=True)
    # parser and internal runtime will validate the same limits again.
    sys.argv = [
        str(SNAPSHOT / "pipeline_produtos.py"),
        "--mode", "overnight", "--execute-live",
        "--config", str(working_config),
        "--rules", str(SNAPSHOT / "regras_negocio.json"),
        "--campaign-total", str(args.target),
        "--search-budget", str(args.per_sublot),
        "--llm-budget", str(args.llm_budget),
        "--token-budget", str(args.token_budget),
        "--campaign-jev-budget", str(args.campaign_jev_budget),
        "--campaign-token-budget", str(args.campaign_token_budget),
        "--timeout-minutes", str(args.timeout_minutes),
        "--campaign-review-before-promotion",
        "--night-output-only",
        "--yes",
    ]
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
