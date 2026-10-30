param(
 [string]$Gold="C:\p\Gold_9070.xlsx",
 [string]$Python="C:\p\venv\Scripts\python.exe"
)
$ErrorActionPreference="Stop"
$root=(Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$dir=Join-Path $root "V54_decision"
New-Item -ItemType Directory -Force $dir|Out-Null
$log=Join-Path $dir "execution_status.csv"
$status=@()
$prior=Join-Path $PSScriptRoot "RUN_V53_DECISION.ps1"
if(!(Test-Path (Join-Path $root "V53_decision\02_supervised_no_title\_SUCCESS")) -or !(Test-Path (Join-Path $root "V53_decision\01_supervised_title\_SUCCESS"))){
  Write-Host "[START] V5.3 supervised flat vs hierarchical"
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $prior -Gold $Gold -Python $Python
  $status += [pscustomobject]@{test="V53";exit_code=$LASTEXITCODE}
}else{
  Write-Host "[SKIP] V5.3 complete"
  $status += [pscustomobject]@{test="V53";exit_code=0}
}
$script=Join-Path $PSScriptRoot "benchmark_feature_ablation.py"
$out=Join-Path $dir "01_feature_ablation"
New-Item -ItemType Directory -Force $out|Out-Null
$summary=Join-Path $out "summary.json"
if((Test-Path (Join-Path $out "_SUCCESS")) -and (Test-Path $summary)){
 Write-Host "[SKIP] V5.4 completed";$exitCode=0
}else{
 Write-Host "[START] V5.4 five text variants"
 $args=@("-u",('"' + $script + '"'),"--input",('"' + $Gold + '"'),"--output-dir",('"' + $out + '"'))
 $exitCode=-1
 try {
  $p=Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $root -RedirectStandardOutput (Join-Path $out "stdout.log") -RedirectStandardError (Join-Path $out "stderr.log") -Wait -PassThru -NoNewWindow
  $exitCode=$p.ExitCode
  if($exitCode -eq 0 -and (Test-Path $summary)){Set-Content (Join-Path $out "_SUCCESS") "ok"}
 }catch{($_|Out-String)|Add-Content (Join-Path $out "stderr.log")}
}
$status += [pscustomobject]@{test="V54";exit_code=$exitCode}
$status | Export-Csv $log -Delimiter ";" -Encoding UTF8 -NoTypeInformation
if(Test-Path $summary){
 $d=Get-Content $summary -Raw|ConvertFrom-Json
 $table=@()
 foreach($r in $d.results){
  $gate=$r.gates_test.'0.95'
  $table += [pscustomobject]@{variant=$r.variant;category=$r.test.category;family=$r.test.family;pair=$r.test.pair;coverage95=$gate.coverage;precision95=$gate.precision;selected95=$gate.selected}
 }
 $table|Export-Csv (Join-Path $dir "comparison.csv") -Delimiter ";" -Encoding UTF8 -NoTypeInformation
}
Write-Host "[FINAL] V54_decision\comparison.csv and V53_decision\comparison.csv"
