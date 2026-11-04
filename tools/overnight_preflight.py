#!/usr/bin/env python3
"""Preflight somente-leitura, sem Search ou LLM. Bloqueia execucao longa insegura."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p


def validate_environment(root: Path, cfg: dict, min_input: int = 1000,
                         min_accepted: int = 9270, min_free_gb: float = 3.0) -> dict:
    if "onedrive" in str(root.resolve()).casefold():
        raise RuntimeError("ONE_DRIVE_ACTIVE_WORKTREE: execucao operacional exige pasta local")
    files = cfg["files"]

    def resolve(k: str) -> Path:
        value = Path(files[k])
        return value if value.is_absolute() else root / value

    paths = {k: resolve(k) for k in ("input", "cache", "master", "final", "classification_db")}
    missing = [str(v) for v in paths.values() if not v.is_file()]
    if missing:
        raise RuntimeError("RUNTIME_FILES_MISSING: " + ", ".join(missing))

    rows = p.read_input_rows(paths["input"])
    valid = [r for r in rows if r.valid]
    if len(valid) < min_input:
        raise RuntimeError(
            f"INPUT_SCOPE_TOO_SMALL: validas={len(valid)}, minimo={min_input}. "
            "Atualize a entrada para o catalogo pretendido; nao amplie implicitamente."
        )

    baseline = p.load_baseline_inventory(paths["master"], paths["final"])
    final_count = len(baseline["final_accepted"])
    missing_gold = baseline["processed"] - baseline["final_accepted"]
    if final_count < min_accepted or missing_gold:
        raise RuntimeError(
            f"GOLD_REGRESSION: final_accepted={final_count}; "
            f"minimo={min_accepted}; missing={len(missing_gold)}"
        )

    # URI de SQLite com '?' etc nos caminhos Windows pode ser mal interpretada.
    # Conexao read-only nao cria banco nem altera seu conteudo.
    with sqlite3.connect(f"file:{paths['cache'].as_posix()}?mode=ro", uri=True, timeout=5) as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("CACHE_SQLITE_FAILED: " + str(integrity))
    free_gb = shutil.disk_usage(root).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise RuntimeError(f"LOW_DISK: {free_gb:.2f} GB < {min_free_gb:.2f} GB")

    return {
        "input_rows": len(rows), "input_valid": len(valid),
        "input_unique_keys": len({r.key for r in valid}),
        "final_accepted": final_count, "missing_gold": len(missing_gold),
        "bi_locked": len(baseline["bi_locked"]), "sqlite": integrity,
        "free_gb": round(free_gb, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="pipeline_config.json")
    parser.add_argument("--min-input", type=int, default=1000)
    parser.add_argument("--min-accepted", type=int, default=9270)
    parser.add_argument("--min-free-gb", type=float, default=3)
    args = parser.parse_args()
    path = Path(args.config)
    if not path.is_absolute():
        path = ROOT / path
    try:
        stats = validate_environment(
            ROOT, json.loads(path.read_text(encoding="utf-8")),
            args.min_input, args.min_accepted, args.min_free_gb
        )
    except Exception as exc:
        print("24H_PREFLIGHT_BLOCKED:", exc, file=sys.stderr)
        return 2
    print("24H_PREFLIGHT_OK:", json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
