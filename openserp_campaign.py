#!/usr/bin/env python3
"""OpenSERP GitHub Actions -> staging -> optional full pipeline.

No GitHub CLI or extra Python packages. Fail-closed, no automatic re-dispatch.
Input is an explicit, reviewed CSV with columns brand,model. It is NOT an
automatic eligibility selector, and never touches production in dry-run mode.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPO = "eduardosilva007/Product_enricher"
WORKFLOW = "openserp-batch-lab.yml"
BRANCH = "fix/openserp-durable-failure-20261118"
MAX_BATCH = 10
MAX_ZIP_BYTES = 30_000_000
MAX_MEMBER_BYTES = 20_000_000


def github_token():
    """Use an existing environment credential, or a token from the local .env.

    This intentionally reads only GH_TOKEN/GITHUB_TOKEN, without exporting
    unrelated secrets. Existing process environment takes precedence.
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.is_file():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in {"GH_TOKEN", "GITHUB_TOKEN"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value:
            return value
    return ""


def key(brand, model):
    import re
    return (re.sub(r"[^a-z0-9]", "", brand.casefold()),
            re.sub(r"[^a-z0-9]", "", model.casefold()))


def read_skus(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"brand", "model"}.issubset(reader.fieldnames or []):
            raise ValueError("INPUT_REQUIRES_COLUMNS_brand_model")
        skus = []
        seen = set()
        for row in reader:
            brand = str(row.get("brand") or "").strip()
            model = str(row.get("model") or "").strip()
            if not brand or not model or "|" in brand + model or "\n" in brand + model:
                raise ValueError("BLANK_OR_INVALID_SKU")
            if not any(c.isdigit() for c in model):
                raise ValueError("MODEL_REQUIRES_DIGIT")
            k = key(brand, model)
            if not all(k) or k in seen:
                raise ValueError("DUPLICATE_SKU")
            seen.add(k)
            skus.append({"brand": brand, "model": model})
    if not skus:
        raise ValueError("EMPTY_MANIFEST")
    return skus


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def request(token, method, route, payload=None):
    url = "https://api.github.com/repos/" + REPO + route
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ProductEnricher-OpenSERP-Orchestrator",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    with urllib.request.urlopen(req, timeout=45) as response:
        body = response.read(MAX_ZIP_BYTES + 1)
        if len(body) > MAX_ZIP_BYTES:
            raise ValueError("GITHUB_RESPONSE_TOO_LARGE")
        if not body:
            return {}
        if response.headers.get("Content-Type", "").startswith("application/zip"):
            return body
        if body.startswith(bytes((80, 75, 3, 4))):
            return body
        return json.loads(body)


def find_run(token, nonce):
    data = request(token, "GET", "/actions/workflows/" + WORKFLOW +
                   "/runs?event=workflow_dispatch&branch=" + BRANCH + "&per_page=100")
    matches = [
        item for item in data.get("workflow_runs", [])
        if item.get("display_title") == "openserp-" + nonce
        and item.get("head_branch") == BRANCH
        and item.get("event") == "workflow_dispatch"
    ]
    if len(matches) > 1:
        raise RuntimeError("AMBIGUOUS_WORKFLOW_RUN")
    return matches[0] if matches else None


def wait_run(token, nonce, max_seconds):
    end = time.monotonic() + max_seconds
    while time.monotonic() < end:
        run = find_run(token, nonce)
        if run and run.get("status") == "completed":
            # Return terminal failures too: caller must durably record run_id
            # before raising. The run may have already performed searches.
            return run
        time.sleep(15)
    raise RuntimeError("WORKFLOW_WAIT_TIMEOUT_NO_REDISPATCH")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expose the GitHub 302 so the PAT is never sent to blob storage."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_artifact_zip(token, artifact_id):
    api_url = f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(api_url, method="GET", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ProductEnricher-OpenSERP-Orchestrator",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=45) as response:
            # A direct ZIP response is also supported.
            raw = response.read(MAX_ZIP_BYTES + 1)
            if len(raw) > MAX_ZIP_BYTES:
                raise RuntimeError("ARTIFACT_TOO_LARGE")
            return raw
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location", "")
        parsed = urllib.parse.urlsplit(location)
        host = (parsed.hostname or "").lower()
        # The redirect must go to HTTPS GitHub Actions storage, never to an
        # arbitrary hostname. Never forward PAT/Authorization to the blob.
        safe = (host == "results-receiver.actions.githubusercontent.com"
                or host.endswith(".blob.core.windows.net"))
        if parsed.scheme != "https" or not safe or parsed.username or parsed.password:
            raise RuntimeError("UNTRUSTED_ARTIFACT_REDIRECT_HOST")
        storage_request = urllib.request.Request(location, headers={
            "User-Agent": "ProductEnricher-OpenSERP-Orchestrator",
        })
        with urllib.request.urlopen(storage_request, timeout=90) as response:
            raw = response.read(MAX_ZIP_BYTES + 1)
        if len(raw) > MAX_ZIP_BYTES:
            raise RuntimeError("ARTIFACT_TOO_LARGE")
        if not raw.startswith(bytes((80, 75, 3, 4))):
            raise RuntimeError("ARTIFACT_NOT_ZIP")
        return raw


def run_failure_diagnostics(token, run_id):
    """Read-only job summary, safe to persist; no search or workflow retry."""
    data = request(token, "GET", f"/actions/runs/{run_id}/jobs?per_page=100")
    return {
        "run_id": run_id,
        "jobs": [
            {"id": job.get("id"), "name": job.get("name"),
             "status": job.get("status"), "conclusion": job.get("conclusion"),
             "failed_steps": [
                 {"name": step.get("name"), "conclusion": step.get("conclusion")}
                 for step in job.get("steps", [])
                 if step.get("conclusion") not in ("success", "skipped", None)
             ]}
            for job in data.get("jobs", [])
        ],
    }


def preserve_failed_run(token, batch, folder):
    """Preserve job diagnostics and a validated full artifact, never redrive."""
    folder.mkdir(parents=True, exist_ok=True)
    report = {"run_id": batch["run_id"], "run_url": batch.get("run_url"),
              "conclusion": batch.get("run_conclusion"), "artifact": "NOT_CHECKED"}
    try:
        report["job_diagnostics"] = run_failure_diagnostics(token, batch["run_id"])
    except Exception as exc:
        report["job_diagnostics_error"] = type(exc).__name__ + ":" + str(exc)[:180]
    try:
        raw = download_artifact(token, batch["run_id"])
        # Keep the original ZIP for forensic recovery, including partial files.
        # It is never imported automatically when the complete cohort fails.
        (folder / "remote_artifact_quarantine.zip").write_bytes(raw)
        report["raw_zip_sha256"] = hashlib.sha256(raw).hexdigest()
        # This check prevents importing partial or mismatched cohorts.
        report["artifact_sha256"] = unpack_checked(raw, folder, batch["skus"])
        report["artifact"] = "COMPLETE_VALIDATED"
    except Exception as exc:
        # Never assume zero searches occurred if the artifact is incomplete.
        report["artifact"] = "INCOMPLETE_OR_UNAVAILABLE"
        report["artifact_error"] = type(exc).__name__ + ":" + str(exc)[:180]
    write_state(folder / "failed_run_diagnostics.json", report)
    return report


def download_artifact(token, run_id):
    response = request(token, "GET", f"/actions/runs/{run_id}/artifacts?per_page=100")
    items = [a for a in response.get("artifacts", [])
             if a.get("name") == f"openserp-batch-{run_id}" and not a.get("expired")]
    if len(items) != 1:
        raise RuntimeError("ARTIFACT_MISSING_OR_AMBIGUOUS")
    return _download_artifact_zip(token, items[0]["id"])


def unpack_checked(raw, folder, expected):
    folder.mkdir(parents=True, exist_ok=True)
    if not isinstance(raw, bytes):
        raise ValueError("EXPECTED_ZIP_BYTES")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        if len(names) > 30 or len(names) != len(set(names)):
            raise ValueError("UNSAFE_ARTIFACT_FILE_LIST")
        for member in z.infolist():
            name = member.filename
            if (name.startswith("/") or "\\" in name or
                    "/" in name or name in {".", ".."} or
                    member.file_size > MAX_MEMBER_BYTES):
                raise ValueError("UNSAFE_ARTIFACT_MEMBER")
        required = {"pilot_summary.json", "openserp_manifest.csv", "manifest.json"}
        if not required.issubset(set(names)):
            raise ValueError("MISSING_IMPORT_ARTIFACTS")
        doc = json.loads(z.read("pilot_summary.json"))
        rows = list(csv.DictReader(io.StringIO(z.read("openserp_manifest.csv").decode("utf-8-sig"))))
        actual = [key(r["brand"].strip(), r["model"].strip()) for r in rows]
        desired = [key(r["brand"], r["model"]) for r in expected]
        if (len(actual) != len(desired) or len(set(actual)) != len(actual)
                or set(actual) != set(desired)
                or len(doc.get("results", [])) != len(desired)):
            raise RuntimeError("INCOMPLETE_OR_WRONG_SKU_ARTIFACT")
        by_query = {r["query"] for r in rows}
        if (len(by_query) != len(rows) or
                any(item.get("status") != "CAPTURED" or
                    item.get("provider") != "openserp" or
                    item.get("query") not in by_query
                    for item in doc["results"])):
            raise RuntimeError("ARTIFACT_RESULT_CONTRACT_FAIL")
        for member in z.infolist():
            if not member.is_dir():
                (folder / member.filename).write_bytes(z.read(member))
    return hashlib.sha256(raw).hexdigest()


def execute_pipeline(folder, size, max_api_calls, max_tokens, classify, persist_cache):
    root = Path(__file__).resolve().parent
    staging = folder / "openserp_staging.json"
    commands = [[sys.executable, str(root / "import_openserp_staging.py"),
                 "--input", str(folder / "pilot_summary.json"),
                 "--manifest", str(folder / "openserp_manifest.csv"),
                 "--output", str(staging)]]
    if persist_cache:
        commands.append([sys.executable, str(root / "pipeline_produtos.py"),
                         "--mode", "full" if classify else "collect", "--search-provider", "openserp",
                         "--openserp-artifact", str(staging), "--llm-provider", "jev",
                         "--max-search-new", str(size), "--max-items", str(size),
                         "--max-api-calls", str(max_api_calls),
                         "--max-tokens", str(max_tokens)])
    for cmd in commands:
        subprocess.run(cmd, cwd=root, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="Explicit reviewed CSV: brand,model")
    ap.add_argument("--campaign-dir", required=True)
    ap.add_argument("--batch-size", type=int, default=3)
    ap.add_argument("--engine", choices=["google", "bing", "duckduckgo"], default="google")
    ap.add_argument("--execute-live", action="store_true")
    ap.add_argument("--classify", action="store_true", help="Classify as well as write the central cache")
    ap.add_argument("--persist-cache", action="store_true",
                    help="Write curated evidence using existing pipeline and central SQLite")
    ap.add_argument("--max-api-calls-per-batch", type=int, default=15)
    ap.add_argument("--max-tokens-per-batch", type=int, default=30000)
    ap.add_argument("--wait-seconds", type=int, default=1800)
    args = ap.parse_args()
    if not 1 <= args.batch_size <= MAX_BATCH:
        ap.error("BATCH_SIZE_MUST_BE_1_TO_10")
    if args.classify and not args.persist_cache:
        ap.error("CLASSIFY_REQUIRES_PERSIST_CACHE")
    if (args.classify or args.persist_cache) and not args.execute_live:
        ap.error("CENTRAL_WRITES_REQUIRE_EXECUTE_LIVE")
    if args.max_api_calls_per_batch < 1 or args.max_tokens_per_batch < 1:
        ap.error("BUDGET_LIMITS_REQUIRED")
    path = Path(args.manifest).resolve()
    skus = read_skus(path)
    manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    total_batches = (len(skus) + args.batch_size - 1) // args.batch_size
    if not args.execute_live:
        print(json.dumps({"status": "DRY_RUN", "skus": len(skus), "batches": total_batches,
                          "batch_size": args.batch_size, "api_calls_max": total_batches *
                          args.max_api_calls_per_batch, "production_writes": False}, indent=2))
        return
    token = github_token()
    if not token:
        raise RuntimeError("GITHUB_TOKEN_REQUIRED_IN_ENV_NO_INSTALL")
    directory = Path(args.campaign_dir).resolve()
    state_path = directory / "campaign.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected = {"manifest_sha256": manifest_hash, "batch_size": args.batch_size,
                    "engine": args.engine, "classify": args.classify,
                    "persist_cache": args.persist_cache,
                    "max_api_calls": args.max_api_calls_per_batch,
                    "max_tokens": args.max_tokens_per_batch}
        if state.get("settings") != expected:
            raise RuntimeError("CAMPAIGN_SETTINGS_DRIFT")
    else:
        if directory.exists() and any(directory.iterdir()):
            raise RuntimeError("CAMPAIGN_DIR_NOT_EMPTY")
        state = {"settings": {"manifest_sha256": manifest_hash,
                             "batch_size": args.batch_size, "engine": args.engine,
                             "classify": args.classify,
                             "persist_cache": args.persist_cache,
                             "max_api_calls": args.max_api_calls_per_batch,
                             "max_tokens": args.max_tokens_per_batch},
                 "batches": [], "status": "READY"}
        for i in range(total_batches):
            part = skus[i * args.batch_size:(i + 1) * args.batch_size]
            state["batches"].append({"index": i, "skus": part,
                                     "nonce": uuid.uuid4().hex,
                                     "status": "READY"})
        write_state(state_path, state)

    for batch in state["batches"]:
        if batch["status"] == "DONE":
            continue
        folder = directory / f"batch_{batch['index']:04d}"
        if batch["status"] == "READY":
            # The durable intent is written BEFORE the dispatch. If an HTTP
            # response is lost, reruns find the nonce rather than dispatch again.
            batch["status"] = "DISPATCH_INTENT"
            batch["dispatch_attempted"] = False
            write_state(state_path, state)
        if batch["status"] == "DISPATCH_INTENT":
            existing = find_run(token, batch["nonce"])
            if not existing:
                if batch.get("dispatch_attempted"):
                    raise RuntimeError("UNCERTAIN_DISPATCH_NO_RETRY_RECONCILE_MANUALLY")
                # Durably mark before network I/O: a lost POST response must
                # never cause a silent duplicate search on resume.
                batch["dispatch_attempted"] = True
                write_state(state_path, state)
                payload = {"ref": BRANCH, "inputs": {
                    "engine": args.engine, "run_nonce": batch["nonce"],
                    "sku_lines": "\n".join(x["brand"] + "|" + x["model"]
                                           for x in batch["skus"])}}
                request(token, "POST", "/actions/workflows/" + WORKFLOW + "/dispatches", payload)
            batch["status"] = "DISPATCHED"
            write_state(state_path, state)
        if batch["status"] == "DISPATCHED":
            run = wait_run(token, batch["nonce"], args.wait_seconds)
            batch["run_id"] = int(run["id"])
            batch["run_url"] = str(run.get("html_url") or "")
            batch["run_conclusion"] = str(run.get("conclusion") or "")
            if run.get("conclusion") != "success":
                # Persist a terminal, non-retriable state even when the remote
                # runner failed after issuing some or all search requests.
                batch["status"] = "RUN_FAILED_NO_RETRY"
                state["status"] = "PAUSED_REMOTE_FAILURE"
                write_state(state_path, state)
                report = preserve_failed_run(token, batch, folder)
                batch["failure_artifact"] = report["artifact"]
                batch["failure_diagnostics"] = str(folder / "failed_run_diagnostics.json")
                write_state(state_path, state)
                raise RuntimeError(
                    "WORKFLOW_NOT_SUCCESS:" + batch["run_conclusion"]
                    + ":run_id=" + str(batch["run_id"])
                    + ":url=" + batch["run_url"]
                    + ":NO_REDISPATCH"
                )
            batch["status"] = "RUN_SUCCESS"
            write_state(state_path, state)
        if batch["status"] == "RUN_FAILED_NO_RETRY":
            raise RuntimeError(
                "PREVIOUS_WORKFLOW_FAILURE_NO_REDISPATCH:run_id="
                + str(batch.get("run_id")) + ":inspect remote logs and ledger"
            )
        if batch["status"] == "RUN_SUCCESS":
            raw = download_artifact(token, batch["run_id"])
            batch["artifact_sha256"] = unpack_checked(raw, folder, batch["skus"])
            batch["status"] = "ARTIFACT_VALIDATED"
            write_state(state_path, state)
        if batch["status"] == "ARTIFACT_VALIDATED":
            # Pre-mark the possibly side-effecting phase. Never auto-replay it
            # after interruption, especially once SQLite may have been changed.
            batch["status"] = "LOCAL_PROCESSING_UNCERTAIN"
            write_state(state_path, state)
            execute_pipeline(folder, len(batch["skus"]),
                             args.max_api_calls_per_batch,
                             args.max_tokens_per_batch, args.classify, args.persist_cache)
            batch["status"] = "DONE"
            write_state(state_path, state)
        if batch["status"] != "DONE":
            raise RuntimeError("UNSAFE_RESUME_REQUIRES_MANUAL_RECONCILIATION:" + batch["status"])
    state["status"] = "COMPLETE"
    write_state(state_path, state)
    print(json.dumps({"status": "COMPLETE", "batches": total_batches,
                      "classify": args.classify,
                      "persist_cache": args.persist_cache}, indent=2))


if __name__ == "__main__":
    main()
