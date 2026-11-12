"""Safely apply the reviewed baseline-only patch to an existing local branch.

No worktrees, clones, new branches, Search, JEV, or automatic promotion.
Original local pipeline is archived in Backups_Baseline. Stop on any drift.

Usage (from existing repository root):
  python scripts/apply_baseline_940_local.py --check --run RUN_DIR
  python scripts/apply_baseline_940_local.py --apply --run RUN_DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REF = "origin/fix/baseline-preservation-nightly-940-20261112"
SOURCE = "pipeline_produtos.py"
FILES = (
    "tests/test_baseline_final_authority_regression.py",
    "tests/verify_baseline_940_readonly.py",
    "tests/verify_baseline_940_isolated_write.py",
    "scripts/replay_failed_940_offline.py",
)


def abort(reason):
    raise RuntimeError("PATCH_940_ABORTED: " + reason)


def git(root, *args):
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=False
    )
    if completed.returncode:
        abort("git " + " ".join(args) + " : " +
              completed.stderr.decode("utf-8", errors="replace")[:300])
    return completed.stdout


def sha256(data):
    return hashlib.sha256(data).hexdigest()


PATCHES = (
    (
        "final_authority",
        '''            if src_manual or src_accepted:
                _copy_business_values(src, final_df, master_df, idx)
                if src_manual:
''',
        '''            if src_manual or src_accepted:
                _copy_business_values(src, final_df, master_df, idx)
                if src_accepted and not src_manual:
                    # An approved Final row must not inherit rejection or WEAK
                    # identity metadata from a stale Consolidado row.
                    for dest, aliases in (
                        ("O que é (Tipo)", ("O que é (Tipo)", "Tipo", "Product Type")),
                        ("Category General", ("Category General",)),
                        ("FAMILY", ("FAMILY", "Family")),
                        ("Classification Identity", ("Classification Identity",)),
                        ("Revisão", ("Revisão", "Revisao")),
                    ):
                        src_col = find_col(final_df, aliases, required=False)
                        if dest not in master_df.columns:
                            master_df[dest] = pd.NA
                        master_df.at[idx, dest] = src.get(src_col) if src_col else pd.NA
                if src_manual:
''',
    ),
    (
        "preservation_diagnostic",
        '''        dropped = before_accepted - after_accepted
        if dropped:
            raise RuntimeError(
                "BASELINE_ACCEPTED_PRESERVATION_FAILED: "
                f"{len(dropped)} classificacoes aceitas seriam removidas. "
                "Nenhum Excel foi regravado."
            )''',
        '''        dropped = before_accepted - after_accepted
        if dropped:
            # Do not waive the gate. Save exact affected keys under the
            # baseline backup directory for deterministic offline diagnosis.
            audit_root = Path((cfg or {}).get("files", {}).get(
                "baseline_backup_dir", "Backups_Baseline"))
            audit_root.mkdir(parents=True, exist_ok=True)
            report = audit_root / "baseline_preservation_blocked.json"
            report.write_text(json.dumps({
                "before_accepted": len(before_accepted),
                "after_accepted": len(after_accepted),
                "dropped": len(dropped),
                "dropped_keys": sorted(dropped),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            raise RuntimeError(
                "BASELINE_ACCEPTED_PRESERVATION_FAILED: "
                f"{len(dropped)} classificacoes aceitas seriam removidas. "
                f"Auditoria: {report}. Nenhum Excel foi regravado."
            )''',
    ),
    (
        "backup_location",
        '''        "baseline_backup_dir":"backups",''',
        '''        "baseline_backup_dir":str(ROOT/"Backups_Baseline"/"controlled_night"/run.name),''',
    ),
    (
        "final_export_acceptance_contract",
        '''        if approved and not complete:
            out.at[idx, "Revisão"] = "REVISAR"
            out.at[idx, "Próxima Ação"] = "REVISAR_CLASSIFICACAO_INCOMPLETA"
            repaired_approval += 1''',
        '''        # Do not downgrade a classification that satisfies the canonical
        # baseline acceptance gate merely because a legacy row lacks Título
        # Real. Title completeness is a review/enrichment signal; it is not
        # an acceptance condition in _baseline_row_accepted. Applying a
        # different rule here caused approved Final rows to disappear.
        accepted_under_contract = _baseline_row_accepted(row, out)
        if approved and not complete and not accepted_under_contract:
            out.at[idx, "Revisão"] = "REVISAR"
            out.at[idx, "Próxima Ação"] = "REVISAR_CLASSIFICACAO_INCOMPLETA"
            repaired_approval += 1''',
    ),
)


def patch_text(source):
    changes = []
    for label, old, new in PATCHES:
        if source.count(new) == 1:
            changes.append((label, "ALREADY_PRESENT"))
        elif source.count(old) == 1:
            source = source.replace(old, new, 1)
            changes.append((label, "APPLY"))
        else:
            abort(f"{label}: expected one exact original or patched block; "
                  "local changes must be reviewed manually (no file touched)")
    compile(source, SOURCE, "exec")
    return source, changes


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()

    root = Path.cwd().resolve()
    if not (root / ".git").exists():
        abort("run from existing C:\\p\\Product_enricher repository root")
    top = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != root:
        abort("current directory is not the repository root")
    branch = git(root, "branch", "--show-current").decode().strip()
    if not branch or branch == "main":
        abort("refusing to modify main or detached HEAD; use existing work branch")
    run = args.run.resolve()
    if not all((run / name).is_file() for name in (
        "run_report.json", "isolated_config.json", "classification_output.xlsx",
    )):
        abort("failed 20-SKU cohort artifacts missing; no changes applied")

    latest = git(root, "rev-parse", REF).decode().strip()
    original_path = root / SOURCE
    original_bytes = original_path.read_bytes()
    # Maintain original LF/CRLF settings to minimize unrelated diffs.
    newline = "\r\n" if b"\r\n" in original_bytes else "\n"
    original = original_bytes.decode("utf-8-sig").replace("\r\n", "\n")
    patched, change_plan = patch_text(original)
    patched_bytes = patched.replace("\n", newline).encode("utf-8")
    dependent = {}
    for name in FILES:
        source_bytes = git(root, "show", f"{REF}:{name}")
        target = root / name
        if target.exists() and target.read_bytes() != source_bytes:
            abort(f"{name}: conflicting local file; preserving without overwrite")
        dependent[name] = source_bytes

    print("REPOSITORY:", root)
    print("BRANCH:", branch)
    print("REMOTE_PATCH_REF:", latest)
    for label, action in change_plan:
        print("PATCH:", label, action)
    print("ORIGINAL_SHA256:", sha256(original_bytes))
    print("PATCHED_SHA256:", sha256(patched_bytes))
    print("ADDITIONAL_TEST_FILES:", len(dependent))
    if args.check:
        print("PATCH_940_CHECK_PASS: no files were modified")
        return 0

    folder = root / "Backups_Baseline" / (
        "pre_patch_940_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    if folder.exists():
        abort("backup folder collision; no changes applied")
    folder.mkdir(parents=True)
    shutil.copy2(original_path, folder / SOURCE)
    (folder / "manifest.json").write_text(json.dumps({
        "original_sha256": sha256(original_bytes),
        "patched_sha256": sha256(patched_bytes),
        "branch": branch,
        "source_remote_sha": latest,
        "changed_sections": change_plan,
        "no_external_api_calls": True,
    }, indent=2), encoding="utf-8")

    # A backup already exists before any source mutation.
    staged = folder / "pipeline_produtos.patched.py"
    staged.write_bytes(patched_bytes)
    os.replace(staged, original_path)
    for name, data in dependent.items():
        target = root / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    tests = (
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_baseline_final_authority_regression.py", "-v"],
        [sys.executable, "tests/verify_baseline_940_readonly.py", str(run)],
        [sys.executable, "tests/verify_baseline_940_isolated_write.py", str(run)],
        [sys.executable, "scripts/replay_failed_940_offline.py", str(run)],
    )
    for command in tests:
        print("\nTEST:", " ".join(command), flush=True)
        done = subprocess.run(command, cwd=root, check=False)
        if done.returncode:
            abort("offline test failed (exit " + str(done.returncode) +
                  "). Local source backup: " + str(folder / SOURCE) +
                  ". No live campaign was started; do not rerun overnight.")
    print("PATCH_940_OFFLINE_TESTS_PASS")
    print("BACKUP:", folder)
    print("NOTE: campaign checkpoint remains BLOCKED; no API calls or promotion.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)
