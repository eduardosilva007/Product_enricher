#Requires -Version 5.1
<#
.SYNOPSIS
  Bounded, sequential FULL runs against one large (18k-20k) SKU input.
  Backups first; stop at first failure; preserve output per batch.
.EXAMPLE
  .\scripts\run_bulk_full_night.ps1 -InputFile "C:\p\Product_enricher\Produtos_19k.xlsx" -ExecuteLive
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputFile,
    [int]$MinInputRows = 18000,
    [int]$MaxBatches = 38,
    [int]$SearchPerBatch = 500,
    [int]$ClassifyPerBatch = 700,
    [int]$CallsPerBatch = 800,
    [int]$TokensPerBatch = 1500000,
    [int]$TotalSearchBudget = 19000,
    [int]$TotalCallsBudget = 14000,
    [int]$TotalTokensBudget = 30000000,
    [switch]$ExecuteLive
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path (Split-Path -Parent $root) 'venv\Scripts\python.exe'
Set-Location $root

foreach ($n in @($MaxBatches,$SearchPerBatch,$ClassifyPerBatch,$CallsPerBatch,$TokensPerBatch,$TotalSearchBudget,$TotalCallsBudget,$TotalTokensBudget)) {
    if ($n -le 0) { throw 'Todos os limites devem ser positivos (nunca SEM_LIMITE).' }
}
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { throw "PYTHON_NOT_FOUND: $py" }
if (-not (Test-Path -LiteralPath $InputFile -PathType Leaf)) { throw "INPUT_NOT_FOUND: $InputFile" }
$inputAbs = (Resolve-Path -LiteralPath $InputFile).Path
$config = Get-Content -LiteralPath (Join-Path $root 'pipeline_config.json') -Raw | ConvertFrom-Json

# This check never calls providers or modifies any file.
$readInput = @'
import sys
from pathlib import Path
import pipeline_produtos as p
rs = p.read_input_rows(Path(sys.argv[1]))
valid = [r for r in rs if r.valid and r.key]
unique = len({r.key for r in valid})
print(f"INPUT CHECK | rows={len(rs)} | valid={len(valid)} | unique_skus={unique} | invalid={len(rs)-len(valid)}")
if len(valid) < int(sys.argv[2]) or len(valid) - unique > 100:
    print("BLOCKED: less than requested input size or >100 duplicate physical SKU rows")
    raise SystemExit(2)
'@
$readInput | & $py - $inputAbs ([string]$MinInputRows)
if ($LASTEXITCODE -ne 0) { throw 'INPUT_COHORT_CHECK_BLOCKED' }

$maxSearch = [Math]::Min(($MaxBatches * $SearchPerBatch),$TotalSearchBudget)
$maxCalls = [Math]::Min(($MaxBatches * $CallsPerBatch),$TotalCallsBudget)
$maxTokens = [Math]::Min(($MaxBatches * $TokensPerBatch),$TotalTokensBudget)
Write-Host "PLANEJAMENTO: lotes=$MaxBatches | teto Search=$maxSearch | teto JEV HTTP=$maxCalls | teto tokens=$maxTokens"
Write-Host "ENTRADA: $inputAbs"
Write-Host 'Cada lote realiza coleta -> classificação -> gravação -> validação; falha bloqueia os seguintes.'
Write-Host 'A revisão humana continua obrigatória para PROVÁVEL / DOUBLE_CHECK.'
if (-not $ExecuteLive) {
    Write-Host 'DRY_RUN_PASS: 0 APIs, 0 arquivos oficiais alterados. Adicione -ExecuteLive para iniciar.'
    exit 0
}
# Snapshot immutable workbooks and consistent SQLite backups BEFORE any paid request.
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$folder = Join-Path $root "auditorias\full_19k_$stamp"
$backup = Join-Path $folder 'backup'
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($name in @([string]$config.files.master,[string]$config.files.final)) {
    $src = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "BASELINE_NOT_FOUND: $src" }
    Copy-Item -LiteralPath $src -Destination (Join-Path $backup ([IO.Path]::GetFileName($src))) -ErrorAction Stop
}
Copy-Item -LiteralPath $inputAbs -Destination (Join-Path $backup 'input_original.xlsx') -ErrorAction Stop
# Operate against a frozen copy; later edits to the source workbook cannot
# silently change the cohort after a paid overnight batch has started.
$frozenInput = Join-Path $backup 'input_original.xlsx'
$sqliteSnapshot = @'
import sqlite3,sys
from pathlib import Path
source=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True)
try:
    target=sqlite3.connect(sys.argv[2])
    try: source.backup(target)
    finally: target.close()
finally: source.close()
'@
foreach ($name in @([string]$config.files.cache,[string]$config.files.classification_db)) {
    $src = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "DATABASE_NOT_FOUND: $src" }
    $sqliteSnapshot | & $py - $src (Join-Path $backup ([IO.Path]::GetFileName($src)))
    if ($LASTEXITCODE -ne 0) { throw "SQLITE_BACKUP_FAILED: $src" }
}
$manifest = [ordered]@{
    input = $inputAbs
    branch = (& git rev-parse --abbrev-ref HEAD)
    commit = (& git rev-parse HEAD)
    started = (Get-Date).ToString('o')
    search_budget = $TotalSearchBudget
    llm_call_budget = $TotalCallsBudget
    token_budget = $TotalTokensBudget
    batches = @()
}
$manifestPath = Join-Path $folder 'manifest.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "BACKUP CONCLUIDO: $backup"
Write-Host "LOGS: $folder"

$searchUsed = 0
$callsUsed = 0
$tokensUsed = 0
$attemptCountCode = @'
import sqlite3,sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True) as db:
    print(db.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0])
'@
$dbPath = Join-Path $root ([string]$config.files.classification_db)
$summaryCode = @'
import json,sys
import pandas as pd
df=pd.read_excel(sys.argv[1],sheet_name="Resumo",dtype=object,keep_default_na=False)
mapping={str(row["Métrica"]):row["Valor"] for _,row in df.iterrows()}
def n(name):
    value=mapping.get(name,0)
    try: return int(float(value))
    except (ValueError,TypeError): raise RuntimeError("METRIC_NOT_NUMERIC:"+name)
print(json.dumps({"calls":n("Clef API Calls"),"tokens":n("Clef Total Tokens"),
                  "eligible":n("Elegíveis")}))
'@
$exitCode = 0
for ($lot=1; $lot -le $MaxBatches; $lot++) {
    $leftSearch = $TotalSearchBudget - $searchUsed
    $leftCalls = $TotalCallsBudget - $callsUsed
    $leftTokens = $TotalTokensBudget - $tokensUsed
    if ($leftSearch -le 0 -or $leftCalls -lt 100 -or $leftTokens -lt 250000) {
        Write-Warning "ORCAMENTO_ESGOTADO: search_restante=$leftSearch jev_restante=$leftCalls tokens_restantes=$leftTokens"
        break
    }
    $searchLimit = [Math]::Min($SearchPerBatch,$leftSearch)
    $callsLimit = [Math]::Min($CallsPerBatch,$leftCalls)
    $tokenLimit = [Math]::Min($TokensPerBatch,$leftTokens)
    $n = '{0:D3}' -f $lot
    $log = Join-Path $folder "lote_$n.log"
    $before = [int]($attemptCountCode | & $py - $dbPath)
    if ($LASTEXITCODE -ne 0) { throw 'SQLITE_LEDGER_READ_FAILED' }

    Write-Host "LOTE $n | search_cap=$searchLimit | classificacao_max=$ClassifyPerBatch | jev_cap=$callsLimit | tokens_cap=$tokenLimit"
    $cmd = @(
        '.\pipeline_produtos.py', '--mode', 'full',
        '--input', $frozenInput, '--search-provider','tavily','--llm-provider','jev',
        '--max-search-new', [string]$searchLimit,
        '--max-items', [string]$ClassifyPerBatch,
        '--max-api-calls', [string]$callsLimit,
        '--max-tokens', [string]$tokenLimit,
        '--yes'  # Previously consulted RAW_EMPTY is skipped, not recollected.
    )
    # Python's logging uses stderr; avoid PowerShell 5.1 treating log lines as terminating errors.
    $ErrorActionPreference='Continue'
    & $py @cmd 2>&1 | Tee-Object -FilePath $log
    $status = $LASTEXITCODE
    $ErrorActionPreference='Stop'

    foreach ($entry in @(@([string]$config.files.output,'classificacao'),
                         @([string]$config.files.collection_report,'coleta'))) {
        $source = Join-Path $root $entry[0]
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $folder "$($entry[1])_$n.xlsx") -Force
        }
    }
    $after = [int]($attemptCountCode | & $py - $dbPath)
    if ($LASTEXITCODE -ne 0) { throw 'SQLITE_LEDGER_POST_READ_FAILED' }
    $used = $after - $before
    if ($used -lt 0 -or $used -gt $searchLimit) { throw "SEARCH_LEDGER_DELTA_INVALID:$used" }
    $searchUsed += $used

    if ($status -ne 0) {
        Write-Warning "BLOQUEADO NO LOTE $n | exit=$status | dados preservados; conferir $log"
        $exitCode = [int]$status
        break
    }
    $resPath = Join-Path $root ([string]$config.files.output)
    if (-not (Test-Path -LiteralPath $resPath -PathType Leaf)) { throw 'RESULT_EXCEL_NOT_FOUND' }
    $jsonText = & $py -c $summaryCode $resPath
    if ($LASTEXITCODE -ne 0) { throw 'RESULT_SUMMARY_UNREADABLE' }
    $metrics = $jsonText | ConvertFrom-Json
    # Verify every complete classified SKU against the actual public Final,
    # not merely against a successful subprocess return code.
    $verifyFinal = @'
import sys
import pandas as pd
import pipeline_produtos as p
results=pd.read_excel(sys.argv[1],sheet_name="Resultados",dtype=object,keep_default_na=False)
final,_=p._read_business_sheet(__import__("pathlib").Path(sys.argv[2]))
pairs={}
for _,x in final.iterrows():
    pair=(p.safe(x.get("Marca")),p.safe(x.get("Modelo")))
    pairs.setdefault(pair,[]).append(x)
errors=[]
checked=0
for _,x in results.iterrows():
    if p.safe(x.get("Resolution State")).upper() not in {"RESOLVED","CLASSIFIED_CLEF"}:
        continue
    cols=("O que é (Tipo)","Category General","FAMILY")
    if not all(p.safe(x.get(col)) for col in cols):
        continue
    pair=(p.safe(x.get("Marca")),p.safe(x.get("Modelo")))
    checked+=1
    if not any(all(p.safe(v.get(col))==p.safe(x.get(col)) for col in cols)
               for v in pairs.get(pair,[])):
        errors.append(pair)
print(f"POST_LOT_FINAL_INTEGRITY | complete={checked} | missing_or_conflicted={len(errors)}")
if errors:
    print("EXAMPLES:",errors[:8])
    raise SystemExit(2)
'@
    $verifyFinal | & $py - $resPath (Join-Path $root ([string]$config.files.final))
    if ($LASTEXITCODE -ne 0) { throw "POST_LOT_FINAL_INTEGRITY_FAILED: $log" }
    if ($metrics.calls -lt 0 -or $metrics.calls -gt $callsLimit -or
        $metrics.tokens -lt 0 -or $metrics.tokens -gt $tokenLimit) {
        throw 'OBSERVED_LLM_BUDGET_EXCEEDED'
    }
    $callsUsed += [int]$metrics.calls
    $tokensUsed += [int]$metrics.tokens

    $lotInfo = [ordered]@{
        lot = $lot; search_reserved = $used; llm_calls = [int]$metrics.calls
        llm_tokens = [int]$metrics.tokens; eligible = [int]$metrics.eligible
        status = 'COMPLETE'; finished = (Get-Date).ToString('o'); log = $log
    }
    $manifest.batches += $lotInfo
    $manifest.search_used = $searchUsed
    $manifest.calls_used = $callsUsed
    $manifest.tokens_used = $tokensUsed
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Write-Host "CONFERIDO lote $n | search=$used | targets=$($metrics.eligible) | jev=$($metrics.calls) | tokens=$($metrics.tokens)"
    if ($used -eq 0 -and [int]$metrics.eligible -eq 0) {
        Write-Host 'FILA ESGOTADA: nenhum Search nem classificação pendentes.'
        break
    }
}
Write-Host "RESUMO: Search reservado=$searchUsed | JEV calls=$callsUsed | tokens=$tokensUsed"
Write-Host "PASTA DE AUDITORIA: $folder"
exit $exitCode
