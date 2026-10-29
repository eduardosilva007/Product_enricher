param(
  [string]$Gold="C:\p\Gold_9070.xlsx",
  [string]$Python="C:\p\venv\Scripts\python.exe",
  [string]$Model="urchade/gliner_multi-v2.1"
)
$ErrorActionPreference="Stop"
$root=(Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
if(!(Test-Path $Gold)){throw "Gold not found: $Gold"}
$base=Join-Path $root "V52_gliner_chain"
New-Item -ItemType Directory -Force $base | Out-Null
$env:HF_HUB_DISABLE_PROGRESS_BARS="1"
$jobs=@(
 @{id="01_ner_1200";max=1200;threshold="0.4"}
)
$status=@()
foreach($job in $jobs){
 $dir=Join-Path $base $job.id
 New-Item -ItemType Directory -Force $dir | Out-Null
 $marker=Join-Path $dir "_SUCCESS"
 $json=Join-Path $dir "summary.json"
 if((Test-Path $marker) -and (Test-Path $json)){Write-Host "[SKIP] $($job.id)";continue}
 $script=Join-Path $PSScriptRoot "benchmark_gliner_ablation.py"
 $args=@("-u",('"' + $script + '"'),"--input",('"' + $Gold + '"'),"--output-dir",('"' + $dir + '"'),"--model",$Model,"--max-rows",$job.max,"--threshold",$job.threshold)
 Write-Host "[START] $($job.id)"
 $exitCode=-1;$state="FAILED"
 try{
  $proc=Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $root -RedirectStandardOutput (Join-Path $dir "stdout.log") -RedirectStandardError (Join-Path $dir "stderr.log") -PassThru -Wait -NoNewWindow
  $exitCode=$proc.ExitCode
  if($exitCode -eq 0 -and (Test-Path $json)){$state="OK";Set-Content $marker "ok"}
 }catch{ ($_|Out-String)|Add-Content (Join-Path $dir "stderr.log") }
 $status+= [pscustomobject]@{test=$job.id;status=$state;exit_code=$exitCode}
 $status|Export-Csv (Join-Path $base "execution_status.csv") -Delimiter ";" -NoTypeInformation -Encoding UTF8
 Write-Host "[$state] $($job.id) exit=$exitCode"
}
Write-Host "[FINAL] V52_gliner_chain\01_ner_1200\summary.json"
