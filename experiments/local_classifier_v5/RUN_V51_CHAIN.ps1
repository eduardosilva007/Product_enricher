param(
    [string]$Gold = "C:\p\Gold_9070.xlsx",
    [string]$Python = "C:\p\venv\Scripts\python.exe",
    [string]$BgeModel = "C:\p\models\bge-m3",
    [string]$SmallModel = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
if (!(Test-Path $Gold)) { throw "Gold nao encontrado: $Gold" }
if (!(Test-Path $Python)) { throw "Python nao encontrado: $Python" }
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:PYTHONUNBUFFERED = "1"
$results = Join-Path $root "V51_chain"
New-Item -ItemType Directory -Path $results -Force | Out-Null
$jobs = @(
    @{Id="01_tfidf_brand_model"; Script="benchmark_retrieval_rerank.py"; Params=@("--feature-mode","brand_model")},
    @{Id="02_tfidf_brand_model_title"; Script="benchmark_retrieval_rerank.py"; Params=@("--feature-mode","brand_model_title")},
    @{Id="03_small_brand_model"; Script="benchmark_dense_retrieval.py"; Params=@("--candidate","small","--feature-mode","brand_model","--small-model",$SmallModel)},
    @{Id="04_small_brand_model_title"; Script="benchmark_dense_retrieval.py"; Params=@("--candidate","small","--feature-mode","brand_model_title","--small-model",$SmallModel)},
    @{Id="05_bge_brand_model"; Script="benchmark_dense_retrieval.py"; Params=@("--candidate","bge","--feature-mode","brand_model","--bge-model",$BgeModel)},
    @{Id="06_bge_brand_model_title"; Script="benchmark_dense_retrieval.py"; Params=@("--candidate","bge","--feature-mode","brand_model_title","--bge-model",$BgeModel)}
)
$summary = @()
foreach ($job in $jobs) {
    $dir = Join-Path $results $job.Id
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $done = Join-Path $dir "_SUCCESS"
    $json = Join-Path $dir "summary.json"
    if ((Test-Path $done) -and (Test-Path $json)) {
        Write-Host "[SKIP] $($job.Id) concluido anteriormente"
        $summary += [pscustomobject]@{test=$job.Id;status="SKIPPED_SUCCESS";exit_code=0}
        continue
    }
    $script = Join-Path $PSScriptRoot $job.Script
    $stdout = Join-Path $dir "stdout.log"
    $stderr = Join-Path $dir "stderr.log"
    $argsArray = @("-u", ('"' + $script + '"'), "--input", ('"' + $Gold + '"'), "--output-dir", ('"' + $dir + '"')) + $job.Params
    Write-Host "[START] $($job.Id)"
    $started = Get-Date
    $status = "FAILED"; $exitCode = -1
    try {
        $proc = Start-Process -FilePath $Python -ArgumentList $argsArray -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -Wait -PassThru -NoNewWindow
        $exitCode = $proc.ExitCode
        if ($exitCode -eq 0 -and (Test-Path $json)) {
            Set-Content -Path $done -Value ((Get-Date).ToString("o"))
            $status = "OK"
        }
    } catch {
        $_ | Out-String | Add-Content -Path $stderr
    }
    $seconds = [int]((Get-Date)-$started).TotalSeconds
    Write-Host "[$status] $($job.Id) exit=$exitCode seconds=$seconds"
    $summary += [pscustomobject]@{test=$job.Id;status=$status;exit_code=$exitCode;seconds=$seconds}
    $summary | Export-Csv -Path (Join-Path $results "execution_status.csv") -Delimiter ";" -NoTypeInformation -Encoding UTF8
}
$report=@()
foreach ($job in $jobs) {
    $file=Join-Path (Join-Path $results $job.Id) "summary.json"
    if (Test-Path $file) {
        try {
            $data=Get-Content $file -Raw | ConvertFrom-Json
            if ($data.tfidf) {
                $v=$data.tfidf | Where-Object {$_.subset -eq "test"}
                $report += [pscustomobject]@{experiment=$job.Id;category=$v.top1_category;family=$v.top1_family;pair=$v.top1_pair;recall10=$v.recall_pair_at_10;recall50=$v.recall_pair_at_50;seconds=$data.elapsed_seconds}
            } else {
                $v=$data.test
                $report += [pscustomobject]@{experiment=$job.Id;category=$v.category_accuracy;family=$v.family_accuracy;pair=$v.pair_recall_at_1;recall10=$v.pair_recall_at_10;recall50=$v.pair_recall_at_50;seconds=$data.elapsed_seconds}
            }
        } catch {}
    }
}
$report | Export-Csv -Path (Join-Path $results "comparison.csv") -Delimiter ";" -NoTypeInformation -Encoding UTF8
Write-Host "[FINAL] Reports: V51_chain\comparison.csv and V51_chain\execution_status.csv"
