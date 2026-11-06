#!/usr/bin/env python3
"""Weekend cache recovery: strictly bounded JEV, no provider Search calls.

Run from the project root:
  C:\\p\\venv\\Scripts\\python.exe tools/weekend_cache_runner.py --max-http 300

The runner stops on baseline errors, API pauses, failed tests/preflight or any
regression in accepted / human-locked counts. Retry-safe keys live in a
single ledger file in the existing project directory.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipeline_produtos.py"
PREFLIGHT = ROOT / "tools" / "overnight_preflight.py"
LEDGER = ROOT / "Weekend_Recovery_Attempts.txt"
LOG = ROOT / "Weekend_Recovery.log"
LOCK = ROOT / "Weekend_Recovery.lock"
PREFLIGHT_RE = re.compile(r"24H_PREFLIGHT_OK:\s*(\{[^\r\n]+\})")


def execute(args: list[str], *, log: bool = False) -> str:
    """Mirror subprocess output and raise on any nonzero exit."""
    proc = subprocess.Popen(
        [sys.executable, *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    fragments = []
    with LOG.open("a", encoding="utf-8") if log else open(__file__, encoding="utf-8") as sink:
        for line in proc.stdout:
            print(line, end="", flush=True)
            fragments.append(line)
            if log:
                sink.write(line)
                sink.flush()
    rc = proc.wait()
    if rc:
        raise RuntimeError(f"Comando retornou exit code {rc}: {args[0]}")
    return "".join(fragments)


def preflight(min_input: int, reference: dict | None = None) -> dict:
    result = execute([str(PREFLIGHT), "--min-input", str(min_input)])
    match = PREFLIGHT_RE.search(result)
    if not match:
        raise RuntimeError("Nao foi possivel ler 24H_PREFLIGHT_OK")
    data = json.loads(match.group(1))
    if data.get("sqlite") != "ok" or data.get("missing_gold") != 0:
        raise RuntimeError(f"Falha de integridade: {data}")
    if reference:
        for field in ("final_accepted", "bi_locked"):
            if int(data[field]) < int(reference[field]):
                raise RuntimeError(f"REGRESSAO {field}: {reference[field]} -> {data[field]}")
        if int(data["input_unique_keys"]) != int(reference["input_unique_keys"]):
            raise RuntimeError("Entrada alterada durante o processamento")
    return data


def main() -> int:
    p = argparse.ArgumentParser(description="Recuperacao segura, sequencial e limitada do cache")
    p.add_argument("--batch-size", type=int, default=15)
    p.add_argument("--max-http", type=int, default=300,
                   help="Teto conservador de requisicoes LLM reservadas em todo o ciclo")
    p.add_argument("--http-per-batch", type=int, default=40)
    p.add_argument("--max-tokens-total", type=int, default=1000000)
    p.add_argument("--tokens-per-batch", type=int, default=100000)
    p.add_argument("--max-batches", type=int, default=20)
    p.add_argument("--max-hours", type=float, default=60)
    args = p.parse_args()
    if min(args.batch_size, args.max_http, args.http_per_batch,
           args.tokens_per_batch, args.max_tokens_total,
           args.max_batches) <= 0 or args.max_hours <= 0:
        p.error("Todos os limites devem ser positivos")
    if args.batch_size > args.http_per_batch:
        p.error("http-per-batch precisa ser >= batch-size")

    if LOCK.exists():
        raise RuntimeError(f"Outra execucao pode estar ativa: {LOCK}. Verifique antes de remover.")
    with LOCK.open("x", encoding="utf-8") as fh:
        fh.write(f"started_at={datetime.now().isoformat()}\n")
    try:
        print("WEEKEND SAFE MODE | Search=DESABILITADO | provider=JEV | backups+preflight=HABILITADOS")
        print(f"MAX HTTP RESERVADO={args.max_http}; lote={args.batch_size}; max_lotes={args.max_batches}")
        baseline = preflight(min_input=1)
        deadline = datetime.now() + timedelta(hours=args.max_hours)
        remaining = args.max_http
        tokens_remaining = args.max_tokens_total
        completed = 0
        unproductive_batches = 0
        while (completed < args.max_batches and remaining > 0 and
               tokens_remaining > 0 and datetime.now() < deadline):
            used = min(remaining, args.http_per_batch)
            tokens_reserved = min(tokens_remaining, args.tokens_per_batch)
            # Cada lote reserva todo o orçamento configurado, mesmo que use menos.
            # Assim o teto global nunca pode ser ultrapassado.
            before = len(set(LEDGER.read_text(encoding="utf-8").splitlines())) if LEDGER.exists() else 0
            print(f"\nWEEKEND BATCH {completed + 1} | http_reservado={used} | "
                  f"restante_apos={remaining-used} | ledger={before}")
            execute([
                str(PIPELINE), "--mode", "recover", "--max-items", str(min(args.batch_size, used)),
                "--llm-provider", "jev", "--max-api-calls", str(used),
                "--max-tokens", str(tokens_reserved),
                "--recovery-ledger", str(LEDGER),
            ], log=True)
            after = len(set(LEDGER.read_text(encoding="utf-8").splitlines())) if LEDGER.exists() else 0
            if after <= before:
                print("FILA ESGOTADA ou nenhum item novo; encerrando.")
                break
            preflight(min_input=int(baseline["input_unique_keys"]), reference=baseline)
            # Monitorar a qualidade real: propostas completas tambem contam como
            # progresso, mesmo que aguardem Double Check. REVIEW puro nao conta.
            import pandas as pd
            results = pd.read_excel(ROOT / "Classificacao_v3.xlsx", sheet_name="Resultados")
            resolutions = (results["Resolution State"].fillna("").astype(str).str.upper()
                           if "Resolution State" in results else pd.Series(dtype=str))
            useful = int(resolutions.isin({"RESOLVED", "CLASSIFIED_CLEF"}).sum())
            unproductive_batches = unproductive_batches + 1 if useful == 0 else 0
            completed += 1
            remaining -= used
            tokens_remaining -= tokens_reserved
            print(f"LOTE_OK={completed} | novos_skus={after-before} | classificados_ou_propostos={useful} "
                  f"| http_reservado_total={args.max_http-remaining} "
                  f"| tokens_reservados_total={args.max_tokens_total-tokens_remaining}")
            if unproductive_batches >= 2:
                print("QUALITY_STOP: dois lotes sem propostas validas; interrompendo para revisao.")
                break
            time.sleep(1)
        print(f"FIM_SEGURO | lotes={completed} | http_reservado={args.max_http-remaining} "
              f"| tokens_reservados={args.max_tokens_total-tokens_remaining} "
              f"| arquivo_log={LOG.name} | ledger={LEDGER.name}")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FALHA_FECHADA: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
