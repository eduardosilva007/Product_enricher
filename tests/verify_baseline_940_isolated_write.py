"""Offline full-persistence dry run of the failed 940 pilot (NO APIs).

Rebuilds temporary copies of the isolated Master and Final. Does not consume
the 20-SKU output and does not promote anything into production.

Usage:
 python tests/verify_baseline_940_isolated_write.py \
   piloto_argentina/controlled_night/20261112T190958Z_68f61e00
"""
import hashlib
from collections import Counter
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline_produtos import (
    Rules, load_baseline_inventory, persist_baseline_excels,
    _load_bi_lock_snapshot, _read_business_sheet, _is_bi_ok_value,
    _is_manual_status, find_col, safe,
)




_HUMAN_FIELDS = {
    "O que é (Tipo)": ("O que é (Tipo)", "Tipo", "Product Type"),
    "Alimentação": ("Alimentação",),
    "Category General": ("Category General",),
    "FAMILY": ("FAMILY", "Family"),
    "Título Real": ("Título Real",),
    "Status": ("Status",),
    "Revisão": ("Revisão", "Revisao"),
    "Revisão B.I": ("Revisão B.I", "Revisao B.I"),
}


def exact_human_rows(path):
    """Count *physical* human-approved rows by exact SKU, including duplicates.

    A compact-key dictionary changes meaning when one SKU has multiple
    differently spelled human-approved variants. That legacy audit produced
    362 misleading mismatches on the 940 preflight.
    """
    df, _ = _read_business_sheet(path)
    brand = find_col(df, ("Marca", "Fabricante", "Brand", "Manufacturer"))
    model = find_col(df, ("Modelo", "Model", "SKU", "Part Number"))
    columns = {
        field: find_col(df, aliases, required=False)
        for field, aliases in _HUMAN_FIELDS.items()
    }
    counter = Counter()
    for _, row in df.iterrows():
        bi_col = columns["Revisão B.I"]
        status_col = columns["Status"]
        if not ((bi_col and _is_bi_ok_value(row.get(bi_col)))
                or (status_col and _is_manual_status(row.get(status_col)))):
            continue
        signature = (
            safe(row.get(brand)), safe(row.get(model)),
            *(safe(row.get(columns[field])) if columns[field] else ""
              for field in _HUMAN_FIELDS),
        )
        counter[signature] += 1
    return counter


def human_examples(counter, limit=12):
    return [
        {"Marca": signature[0], "Modelo": signature[1],
         **dict(zip(_HUMAN_FIELDS, signature[2:])),
         "occurrences": count}
        for signature, count in list(counter.items())[:limit]
    ]

def main(run_dir: Path):
    run_dir = run_dir.resolve()
    cfg = json.loads((run_dir / "isolated_config.json").read_text(encoding="utf-8"))
    # The real classify/promotion persistence always receives the isolated
    # SQLite. Omitting it leaves stale legacy REVIEW_NO_RESULTS values that
    # cannot be reconciled even when RAW_OK evidence already exists.
    paths = {k: Path(cfg["files"][k]) for k in ("master", "final", "input", "cache")}
    if any(not p.is_file() for p in paths.values()):
        raise RuntimeError("BASELINE_PREFLIGHT_MISSING_ISOLATED_INPUT_OR_CACHE")
    def sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    source_hashes = {name: sha256(path) for name, path in paths.items()}
    expected = load_baseline_inventory(paths["master"], paths["final"])["processed"]
    expected_bi = _load_bi_lock_snapshot(paths["final"])
    original_human = exact_human_rows(paths["final"])
    with tempfile.TemporaryDirectory(prefix="baseline_940_offline_") as temp_dir:
        temp = Path(temp_dir)
        master = temp / ("master" + paths["master"].suffix)
        final = temp / ("final" + paths["final"].suffix)
        cohort = temp / ("input" + paths["input"].suffix)
        for key, dest in (("master", master), ("final", final), ("input", cohort)):
            shutil.copy2(paths[key], dest)
        before = load_baseline_inventory(master, final)
        local_cfg = dict(cfg)
        local_cfg["files"] = dict(cfg["files"])
        local_cfg["files"]["baseline_backup_dir"] = str(temp / "audit")
        stats = persist_baseline_excels(
            rows=[], master_path=master, final_path=final, input_path=cohort,
            rules=Rules(ROOT / "regras_negocio.json"),
            cache_path=paths["cache"], cfg=local_cfg,
        )
        after = load_baseline_inventory(master, final)
        lost = expected - after["processed"]
        final_lost = expected - after["final_accepted"]
        bi_after = _load_bi_lock_snapshot(final)
        # Keep the old normalized-key count for diagnostic continuity;
        # NEVER use it to approve/reject physical human-lock preservation.
        legacy_normalized_differences = {
            k for k, value in expected_bi.items()
            if bi_after.get(k) != value
        }
        final_human = exact_human_rows(final)
        missing_human = original_human - final_human
        added_human = final_human - original_human
        introduced_accepted = after["processed"] - expected
        original_changes = [
            name for name, path in paths.items()
            if sha256(path) != source_hashes[name]
        ]
        report = {
            "expected_accepted": len(expected),
            "accepted_after": len(after["processed"]),
            "lost_from_union": len(lost),
            "lost_from_final": len(final_lost),
            "unexpected_accepted_without_classification": len(introduced_accepted),
            "unexpected_accepted_examples": sorted(introduced_accepted)[:20],
            "legacy_normalized_bi_snapshot_diff": len(legacy_normalized_differences),
            "protected_human_physical_rows_before": sum(original_human.values()),
            "protected_human_physical_rows_after": sum(final_human.values()),
            "protected_human_original_rows_missing_or_modified": sum(missing_human.values()),
            "protected_human_rows_newly_marked_or_modified": sum(added_human.values()),
            "protected_missing_examples": human_examples(missing_human),
            "protected_added_examples": human_examples(added_human),
            "bi_lock_count": len(expected_bi),
            "baseline_stats": stats,
            "api_calls": 0,
            "isolated_cache_readonly": str(paths["cache"]),
            "original_sources_modified": original_changes,
            "wrote_only_temporary_copies": True,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0 if not (lost or final_lost or missing_human or added_human
                         or introduced_accepted or original_changes) else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/verify_baseline_940_isolated_write.py RUN_DIR")
    raise SystemExit(main(Path(sys.argv[1])))
