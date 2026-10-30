param(
 [string]$Gold="C:\p\Gold_9070.xlsx",
 [string]$Python="C:\p\venv\Scripts\python.exe"
)
$ErrorActionPreference="Stop"
$root=(Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
if(!(Test-Path $Gold)){throw "Gold ausente: $Gold"}
if(!(Test-Path $Python)){throw "Python ausente: $Python"}
$dir=Join-Path $root "V53_decision"
New-Item -ItemType Directory -Force $dir|Out-Null
$env:PYTHONUNBUFFERED="1"
$jobs=@(
 @{id="01_supervised_title";mode="brand_model_title"},
 @{id="02_supervised_no_title";mode="brand_model"}
)
$statuses=@()
foreach($job in $jobs){
 $out=Join-Path $dir $job.id
 New-Item -ItemType Directory -Force $out|Out-Null
 $summary=Join-Path $out "summary.json"
 $done=Join-Path $out "_SUCCESS"
 if((Test-Path $done) -and (Test-Path $summary)){
   $state="SKIPPED_SUCCESS";$exitCode=0
 }else{
   Write-Host "[START] $($job.id)"
   $script=Join-Path $PSScriptRoot "benchmark_hierarchical.py"
   $args=@("-u",('"' + $script + '"'),"--input",('"' + $Gold + '"'),"--output-dir",('"' + $out + '"'),"--feature-mode",$job.mode)
   $state="FAILED";$exitCode=-1
   try{
     $p=Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $root -RedirectStandardOutput (Join-Path $out "stdout.log") -RedirectStandardError (Join-Path $out "stderr.log") -Wait -PassThru -NoNewWindow
     $exitCode=$p.ExitCode
     if($exitCode -eq 0 -and (Test-Path $summary)){$state="OK";Set-Content $done "ok"}
   }catch{($_|Out-String)|Add-Content (Join-Path $out "stderr.log")}
 }
 $statuses += [pscustomobject]@{test=$job.id;status=$state;exit_code=$exitCode}
 $statuses|Export-Csv (Join-Path $dir "execution_status.csv") -NoTypeInformation -Delimiter ";" -Encoding UTF8
 Write-Host "[$state] $($job.id) exit=$exitCode"
}
$comparison=@()
foreach($job in $jobs){
 $path=Join-Path (Join-Path $dir $job.id) "summary.json"
 if(Test-Path $path){
   $x=Get-Content $path -Raw|ConvertFrom-Json
   foreach($kind in @("flat","hierarchical")){
     $m=$x.$kind.test
     $g=$x.$kind.gate_test.'0.95'
     $comparison += [pscustomobject]@{experiment=$job.id;model=$kind;category=$m.category_accuracy;family=$m.family_accuracy;pair=$m.pair_accuracy;auto_coverage_95=$g.coverage;auto_precision_95=$g.precision;auto_selected_95=$g.selected;seconds=$x.elapsed_seconds}
   }
 }
}
$comparison|Export-Csv (Join-Path $dir "comparison.csv") -NoTypeInformation -Delimiter ";" -Encoding UTF8
Write-Host "[FINAL] V53_decision\comparison.csv"
