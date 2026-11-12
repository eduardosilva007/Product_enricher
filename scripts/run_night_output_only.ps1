# Safe 5..20 SKU assisted night pilot. PowerShell 5.1 compatible.
# Code lives in a clean git-archive, NEVER switches/overwrites the user's work branch.
[CmdletBinding()]
param(
    [string]$RepoPath = "C:\\p\\Product_enricher",
    [int]$Target = 5,
    [int]$SearchPerSublot = 5,
    [int]$JevBudget = 20,
    [int]$TokenBudget = 100000,
    [string]$EnvFile = "",
    [switch]$ExecuteLive
)
$ErrorActionPreference = "Stop"
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
$CodeRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = "C:\\p\\venv\\Scripts\\python.exe"
$history = Join-Path $RepoPath "piloto_argentina\\controlled_night"
if (-not (Test-Path -LiteralPath $python)) { throw "PYTHON_DO_VENV_NAO_ENCONTRADO" }
if ($CodeRoot -eq $RepoPath) { throw "CODIGO_PRECISA_ESTAR_EM_GIT_ARCHIVE_NAO_NA_BRANCH_SUJA" }
if (-not (Test-Path -LiteralPath $history)) { throw "HISTORICO_ORIGINAL_AUSENTE" }
if ($Target -lt 1 -or $Target -gt 940) { throw "TARGET_INVALIDO_1_A_940" }
if ($SearchPerSublot -lt 1 -or $SearchPerSublot -gt 20) { throw "SUBLELOT_SEARCH_INVALIDO_1_A_20" }
if ($JevBudget -lt 1 -or $TokenBudget -lt 1) { throw "ORCAMENTO_TOTAL_OBRIGATORIO" }
Push-Location $CodeRoot
try {
    Write-Host "ETAPA 1/4: Compilacao Python" -ForegroundColor Cyan
    & $python -m py_compile ".\\pipeline_produtos.py" ".\\scripts\\launch_night_output_only.py"
    if ($LASTEXITCODE -ne 0) { throw "PYTHON_COMPILE_FAILED; API_CALLS=0" }

    Write-Host "ETAPA 2/4: Suíte completa offline" -ForegroundColor Cyan
    & $python -m unittest discover -s tests -p "test_*.py" -q
    if ($LASTEXITCODE -ne 0) { throw "UNIT_TESTS_FAILED; API_CALLS=0" }

    Write-Host "ETAPA 3/4: Historico de tentativas (offline)" -ForegroundColor Cyan
    & $python ".\\scripts\\audit_overnight_history_offline.py" $history
    if ($LASTEXITCODE -ne 0) { throw "HISTORICO_INCONSISTENTE; API_CALLS=0" }

    $params = @(
        ".\\scripts\\launch_night_output_only.py",
        "--repo", $RepoPath,
        "--target", ([string]$Target),
        "--per-sublot", ([string]$SearchPerSublot),
        "--llm-budget", ([string]$JevBudget),
        "--token-budget", ([string]$TokenBudget),
        "--campaign-jev-budget", ([string]$JevBudget),
        "--campaign-token-budget", ([string]$TokenBudget)
    )
    if ($EnvFile) { $params += @("--env-file", $EnvFile) }

    Write-Host "ETAPA 4/4: Elegibilidade, cotas e reservas; nenhum request" -ForegroundColor Cyan
    & $python @params
    if ($LASTEXITCODE -ne 0) { throw "PREFLIGHT_REPROVADO; API_CALLS=0" }

    if (-not $ExecuteLive) {
        Write-Host "PREFLIGHT PASS: API_CALLS=0. Nenhum piloto foi iniciado." -ForegroundColor Green
        Write-Host "Para autorizar piloto pago: reexecute com -ExecuteLive."
        return
    }
    Write-Host "PRECHECK PASS. Piloto ISOLADO: $Target SKUs, Search <= $Target, JEV <= $JevBudget" -ForegroundColor Yellow
    Write-Host "Nao promove nem substitui planilhas oficiais."
    $choice = Read-Host "Para iniciar Search e JEV pagos, digite EXECUTAR"
    if ($choice -cne "EXECUTAR") {
        Write-Host "NAO_EXECUTADO; SEM CHAMADAS PAGAS"
        return
    }
    $paid = @($params)
    $paid += "--execute-live"
    & $python @paid
    if ($LASTEXITCODE -ne 0) {
        throw "PILOTO_ISOLADO_FALHOU; nao repita as chamadas. Inspecione run_report.json no historico"
    }
    Write-Host "PILOTO ISOLADO AUDITADO: PASS; PLANILHAS CANONICAS NAO PROMOVIDAS" -ForegroundColor Green
    Write-Host "RUNS/HISTORICO: $history"
}
finally {
    Pop-Location
}
