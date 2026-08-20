<#
  codecalc - Windows 11 verification bootstrap (THE-818 / THE-829 / THE-802)

  Runs on NATIVE Windows PowerShell (the job-object behaviour under test does
  NOT exist inside WSL2 - it must be measured on real Windows). One command:
  builds the executor, copies it fresh into bin\, and runs every probe, writing
  each output to a results folder you can paste back (or a WSL2 Claude session
  can read).

  ASCII-only on purpose: Windows PowerShell 5.x decodes a BOM-less .ps1 as the
  ANSI codepage, so any non-ASCII byte becomes mojibake and breaks parsing.

  Usage (from the repo root, or pass -Repo):
      powershell -ExecutionPolicy Bypass -File scripts\win-verify.ps1
      powershell -ExecutionPolicy Bypass -File scripts\win-verify.ps1 -Repo E:\Projects\codecalc

  diag_windows_job.py exit contract:
      0  ceiling BOUND        -> THE-818 fix WORKS (the win we want)
      3  ceiling did NOT bind -> bug reproduced; the table says how
      2  not Windows / binary missing -> nothing measured (setup problem)
      1  never returned (so a harness error cannot look like a finding)
#>

[CmdletBinding()]
param(
    [string]$Repo = (Get-Location).Path,
    [int]$Limit = 24,
    [switch]$SkipBuild
)

# Continue, not Stop: native tools (uv, cargo) write normal progress to stderr,
# and under Stop a `2>&1` merge would turn that into a terminating error. Native
# failures are detected by $LASTEXITCODE below; critical cmdlets get -EA Stop.
$ErrorActionPreference = "Continue"
function Section($t) { Write-Host ("`n==================== " + $t + " ====================") -ForegroundColor Cyan }
function Note($t)    { Write-Host $t -ForegroundColor DarkGray }

Set-Location -Path $Repo -ErrorAction Stop
$stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$results = Join-Path $Repo ("win-verify-results\" + $stamp)
New-Item -ItemType Directory -Force -Path $results -ErrorAction Stop | Out-Null
Write-Host ("Results dir: " + $results) -ForegroundColor Green

# ---- 0. prerequisites -------------------------------------------------------
Section "0. prerequisites"
$ok = $true
foreach ($t in "cargo","python","uv","git") {
    $p = Get-Command $t -ErrorAction SilentlyContinue
    if ($p) { Write-Host ("  {0,-7} {1}" -f $t, $p.Source) }
    else    { Write-Host ("  MISSING: " + $t) -ForegroundColor Red; $ok = $false }
}
if (-not $ok) { Write-Host "Install the missing tool(s) and re-run." -ForegroundColor Red; exit 2 }

# ---- 1. build + place the executor (mirrors CI) -----------------------------
Section "1. build executor into bin (fresh: a stale bin binary would be measured silently)"
Note "  uv sync..."
uv sync --locked --all-extras 2>&1 | Out-File -FilePath (Join-Path $results "uv-sync.log")
if (-not $SkipBuild) {
    Note "  building executor (first build can take a couple of minutes; progress in cargo-build.log)..."
    cargo build --release --manifest-path executor\Cargo.toml 2>&1 | Out-File -FilePath (Join-Path $results "cargo-build.log")
    if ($LASTEXITCODE -ne 0) { Write-Host "cargo build FAILED - see cargo-build.log" -ForegroundColor Red; exit 2 }
}
$exe = "executor\target\release\codecalc-exec.exe"
if (-not (Test-Path $exe)) { Write-Host ("no " + $exe + " (build first, or drop -SkipBuild)") -ForegroundColor Red; exit 2 }
New-Item -ItemType Directory -Force -Path "bin" | Out-Null
Copy-Item $exe "bin\codecalc-exec.exe" -Force
Write-Host ("  copied " + $exe + " -> bin\codecalc-exec.exe") -ForegroundColor Green
$venvPy = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { $venvPy = (Get-Command python).Source }
Note ("  python: " + $venvPy)

# helper: set env, run a python script with output redirected to files (no pipe
# deadlock), restore env, return exit code + output path.
function Invoke-Probe($name, $pyArgs, $envMap) {
    $outFile = Join-Path $results ($name + ".txt")
    $saved = @{}
    foreach ($k in $envMap.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k)
        Set-Item -Path ("Env:" + $k) -Value ([string]$envMap[$k])
    }
    $rc = $null
    try {
        # Call operator + 2>&1 | Out-File: $LASTEXITCODE reliably reflects the
        # native exit code (Start-Process -PassThru .ExitCode comes back null with
        # redirected output). Under $ErrorActionPreference=Continue the merged
        # stderr does not throw.
        & $venvPy $pyArgs 2>&1 | Out-File -FilePath $outFile -Encoding utf8
        $rc = $LASTEXITCODE
    } finally {
        foreach ($k in $envMap.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item -Path ("Env:" + $k) -ErrorAction SilentlyContinue }
            else { Set-Item -Path ("Env:" + $k) -Value $saved[$k] }
        }
    }
    Add-Content -Path $outFile -Value ("--- exit " + $rc + " ---")
    return @{ rc = $rc; out = $outFile }
}

$env818 = @{
    CODECALC_WIN_JOB_AT_CREATION = "1"
    CODECALC_MAX_PROCESSES       = "$Limit"
    CODECALC_DIAG_JOB            = (Join-Path $results "jobdiag-ps.txt")
}

# ---- 2. THE-818 - ceiling binds? (plain PowerShell context) -----------------
Section "2. THE-818 job-object ceiling probe (PowerShell context), JOB_AT_CREATION=1"
$r = Invoke-Probe "the818-powershell" "scripts\diag_windows_job.py" $env818
switch ($r.rc) {
    0 { Write-Host "  -> EXIT 0: CEILING BOUND. THE-818 fix WORKS in the PowerShell context." -ForegroundColor Green }
    3 { Write-Host ("  -> EXIT 3: ceiling did NOT bind (bug reproduced). See " + $r.out) -ForegroundColor Red }
    default { Write-Host ("  -> EXIT " + $r.rc + ": setup problem (not Windows / binary missing). See " + $r.out) -ForegroundColor Yellow }
}

# ---- 2b. THE-818 - portable probe in the test suite -------------------------
Section "2b. THE-818 tests\test_security.py (portable process-limit probe + fork-bomb)"
$rsec = Invoke-Probe "the818-test_security" "tests\test_security.py" $env818
$c = if ($rsec.rc -eq 0) { "Green" } else { "Red" }
Write-Host ("  test_security.py EXIT " + $rsec.rc + " (0 = pass). See " + $rsec.out) -ForegroundColor $c

# ---- 3. THE-818 - Task Scheduler context (the decisive second launcher) ------
Section "3. THE-818 same probe from Task Scheduler (no agent in the parent chain)"
Note "  Writes a .bat, registers a one-shot task to run it, reads its exit code, removes it."
$taskName = "codecalc-jobprobe-" + $stamp
$tsOut    = Join-Path $results "the818-taskscheduler.txt"
$tsDiag   = Join-Path $results "jobdiag-ts.txt"
$batPath  = Join-Path $results "ts-probe.bat"
$bat = @(
    "@echo off",
    "set CODECALC_WIN_JOB_AT_CREATION=1",
    ("set CODECALC_MAX_PROCESSES=" + $Limit),
    ("set CODECALC_DIAG_JOB=" + $tsDiag),
    ('cd /d "' + $Repo + '"'),
    ('"' + $venvPy + '" scripts\diag_windows_job.py > "' + $tsOut + '" 2>&1'),
    "exit /b %ERRORLEVEL%"
) -join "`r`n"
Set-Content -Path $batPath -Value $bat -Encoding ascii
$rc = 999
try {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "' + $batPath + '"')
    Register-ScheduledTask -TaskName $taskName -Action $action -Force -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Note "  waiting for the task to finish..."
    for ($i = 0; $i -lt 240; $i++) {
        Start-Sleep -Milliseconds 500
        if ((Get-ScheduledTask -TaskName $taskName).State -ne "Running") { break }
    }
    Start-Sleep -Milliseconds 500
    $rc = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
} catch {
    Write-Host ("  Task Scheduler step error: " + $_.Exception.Message) -ForegroundColor Yellow
    Note "  If this is a permissions error, re-run PowerShell as Administrator, or skip - the PowerShell-context result above already tells us most of the story."
} finally {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
switch ($rc) {
    0 { Write-Host "  -> Task Scheduler EXIT 0: CEILING BOUND here too. This is the decisive case." -ForegroundColor Green }
    3 { Write-Host ("  -> Task Scheduler EXIT 3: ceiling did NOT bind from the Schedule service. See " + $tsOut) -ForegroundColor Red }
    default { Write-Host ("  -> Task Scheduler EXIT " + $rc + ": no clean result (see the note above / " + $tsOut + ")") -ForegroundColor Yellow }
}

# ---- 4. THE-829 - AppContainer path runs + discloses ------------------------
Section "4. THE-829 tests\test_appcontainer.py (path taken + discloses unverified OR fails closed)"
Note "  Proves the AppContainer path RUNS and is honest; does NOT yet prove the deep"
Note "  isolation (SID / can't-read-secrets / --no-net egress). Those probes come next."
$rac = Invoke-Probe "the829-test_appcontainer" "tests\test_appcontainer.py" @{}
$c2 = if ($rac.rc -eq 0) { "Green" } else { "Red" }
Write-Host ("  test_appcontainer.py EXIT " + $rac.rc + " (0 = contract holds). See " + $rac.out) -ForegroundColor $c2

# ---- summary ----------------------------------------------------------------
function Verdict818($x) { if ($x -eq 0) { "BOUND (good)" } elseif ($x -eq 3) { "ESCAPED (bug)" } else { "setup/none" } }
Section "SUMMARY"
Write-Host ("  THE-818 PowerShell probe : EXIT {0}  {1}" -f $r.rc,   (Verdict818 $r.rc))
Write-Host ("  THE-818 test_security    : EXIT {0}  {1}" -f $rsec.rc, $(if ($rsec.rc -eq 0) { "pass" } else { "fail" }))
Write-Host ("  THE-818 TaskScheduler    : EXIT {0}  {1}" -f $rc,     (Verdict818 $rc))
Write-Host ("  THE-829 test_appcontainer: EXIT {0}  {1}" -f $rac.rc, $(if ($rac.rc -eq 0) { "contract holds" } else { "fail" }))
Write-Host ("`nAll outputs are in: " + $results) -ForegroundColor Green
Write-Host "Paste that folder's *.txt back (or point the Cave session at it) and I'll read the measurements." -ForegroundColor Green
