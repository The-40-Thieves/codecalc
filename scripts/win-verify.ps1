<#
  codecalc — Windows 11 verification bootstrap (THE-818 / THE-829 / THE-802)

  Runs on NATIVE Windows PowerShell (the job-object behaviour under test does
  NOT exist inside WSL2 — it must be measured on real Windows). One command:
  builds the executor, copies it fresh into bin\, and runs every probe, writing
  each output to a results folder you can paste back (or a WSL2 Claude session
  can read).

  Usage (from the repo root, or pass -Repo):
      powershell -ExecutionPolicy Bypass -File win-verify.ps1
      powershell -ExecutionPolicy Bypass -File win-verify.ps1 -Repo C:\path\to\codecalc

  What the exit codes from diag_windows_job.py mean (its own contract):
      0  the ceiling BOUND      -> THE-818 fix WORKS (the win we want)
      3  the ceiling did NOT bind -> bug reproduced; the table says how
      2  not Windows / binary missing -> nothing measured (setup problem)
      1  never returned (so a harness error can't look like a finding)
#>

[CmdletBinding()]
param(
    [string]$Repo = (Get-Location).Path,
    [int]$Limit = 24,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
function Section($t) { Write-Host "`n==================== $t ====================" -ForegroundColor Cyan }
function Note($t)    { Write-Host $t -ForegroundColor DarkGray }

Set-Location $Repo
$stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$results = Join-Path $Repo "win-verify-results\$stamp"
New-Item -ItemType Directory -Force -Path $results | Out-Null
Write-Host "Results dir: $results" -ForegroundColor Green

# ---- 0. prerequisites -------------------------------------------------------
Section "0. prerequisites"
$ok = $true
foreach ($t in "cargo","python","uv","git") {
    $p = Get-Command $t -ErrorAction SilentlyContinue
    if ($p) { Write-Host ("  {0,-7} {1}" -f $t, $p.Source) }
    else    { Write-Host "  MISSING: $t" -ForegroundColor Red; $ok = $false }
}
if (-not $ok) { Write-Host "Install the missing tool(s) and re-run." -ForegroundColor Red; exit 2 }

# ---- 1. build + place the executor (mirrors CI) -----------------------------
Section "1. build executor into bin\ (fresh — a stale bin\ binary would be measured silently)"
uv sync --locked --all-extras 2>&1 | Tee-Object -FilePath "$results\uv-sync.log" | Out-Null
if (-not $SkipBuild) {
    cargo build --release --manifest-path executor\Cargo.toml 2>&1 | Tee-Object -FilePath "$results\cargo-build.log"
    if ($LASTEXITCODE -ne 0) { Write-Host "cargo build FAILED — see $results\cargo-build.log" -ForegroundColor Red; exit 2 }
}
$exe = "executor\target\release\codecalc-exec.exe"
if (-not (Test-Path $exe)) { Write-Host "no $exe (build first, or drop -SkipBuild)" -ForegroundColor Red; exit 2 }
New-Item -ItemType Directory -Force -Path "bin" | Out-Null
Copy-Item $exe "bin\codecalc-exec.exe" -Force
Write-Host "  copied $exe -> bin\codecalc-exec.exe" -ForegroundColor Green
$venvPy = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { $venvPy = (Get-Command python).Source }  # fallback
Note "  venv python: $venvPy"

# helper: run a python command with a given env map, capture stdout+stderr+rc
function Invoke-Probe($name, $script, $env) {
    $out = "$results\$name.txt"
    $full = @{}
    (Get-Item Env:).ForEach({ $full[$_.Name] = $_.Value })
    $env.GetEnumerator().ForEach({ $full[$_.Key] = $_.Value })
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $venvPy
    $psi.Arguments = $script
    $psi.WorkingDirectory = $Repo
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
    $full.GetEnumerator().ForEach({ $psi.EnvironmentVariables[$_.Key] = [string]$_.Value })
    $p = [System.Diagnostics.Process]::Start($psi)
    $so = $p.StandardOutput.ReadToEnd(); $se = $p.StandardError.ReadToEnd(); $p.WaitForExit()
    "EXIT $($p.ExitCode)`n--- stdout ---`n$so`n--- stderr ---`n$se" | Set-Content -Path $out -Encoding utf8
    return @{ rc = $p.ExitCode; out = $out }
}

# ---- 2. THE-818 — ceiling binds? (plain PowerShell context) -----------------
Section "2. THE-818 — job-object ceiling probe (PowerShell context), CODECALC_WIN_JOB_AT_CREATION=1"
$env818 = @{
    CODECALC_WIN_JOB_AT_CREATION = "1"
    CODECALC_MAX_PROCESSES       = "$Limit"
    CODECALC_DIAG_JOB            = "$results\jobdiag-ps.txt"
}
$r = Invoke-Probe "the818-powershell" "scripts\diag_windows_job.py" $env818
switch ($r.rc) {
    0 { Write-Host "  -> EXIT 0: CEILING BOUND. THE-818 fix WORKS in the PowerShell context." -ForegroundColor Green }
    3 { Write-Host "  -> EXIT 3: ceiling did NOT bind (bug reproduced). See $($r.out)." -ForegroundColor Red }
    default { Write-Host "  -> EXIT $($r.rc): setup problem (not Windows / binary missing). See $($r.out)." -ForegroundColor Yellow }
}

# ---- 2b. THE-818 — portable probe in the test suite -------------------------
Section "2b. THE-818 — tests\test_security.py (portable process-limit probe + fork-bomb)"
$rsec = Invoke-Probe "the818-test_security" "tests\test_security.py" $env818
Write-Host "  test_security.py EXIT $($rsec.rc) (0 = pass). See $($rsec.out)." -ForegroundColor ($(if($rsec.rc -eq 0){"Green"}else{"Red"}))

# ---- 3. THE-818 — Task Scheduler context (the DECISIVE second launcher) ------
Section "3. THE-818 — same probe from Task Scheduler (no agent in the parent chain)"
Note "  Registers a one-shot task, runs it, reads its exit code + output, then removes it."
$taskName = "codecalc-jobprobe-$stamp"
$tsOut    = "$results\the818-taskscheduler.txt"
$tsRc     = "$results\the818-taskscheduler.rc"
# the task sets the env itself (Task Scheduler does not inherit this shell's env)
$inner = "set CODECALC_WIN_JOB_AT_CREATION=1&& set CODECALC_MAX_PROCESSES=$Limit&& set CODECALC_DIAG_JOB=$results\jobdiag-ts.txt&& cd /d `"$Repo`"&& `"$venvPy`" scripts\diag_windows_job.py > `"$tsOut`" 2>&1& echo %ERRORLEVEL% > `"$tsRc`""
try {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $inner"
    Register-ScheduledTask -TaskName $taskName -Action $action -Force -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Note "  waiting for the task to finish..."
    for ($i=0; $i -lt 120; $i++) {
        Start-Sleep -Milliseconds 500
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        if ($info.LastTaskResult -ne 267009 -and (Test-Path $tsRc)) { break }  # 267009 = still running
    }
    Start-Sleep -Milliseconds 500
    $rc = if (Test-Path $tsRc) { [int]((Get-Content $tsRc -Raw).Trim()) } else { 999 }
    switch ($rc) {
        0 { Write-Host "  -> Task Scheduler EXIT 0: CEILING BOUND here too. This is the decisive case." -ForegroundColor Green }
        3 { Write-Host "  -> Task Scheduler EXIT 3: ceiling did NOT bind from the Schedule service. See $tsOut." -ForegroundColor Red }
        default { Write-Host "  -> Task Scheduler EXIT $rc: could not capture a clean result. See $tsOut." -ForegroundColor Yellow }
    }
} finally {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Note "  task $taskName removed."
}

# ---- 4. THE-829 — AppContainer path runs + discloses (compile/run/disclose) --
Section "4. THE-829 — tests\test_appcontainer.py (path taken + discloses unverified OR fails closed)"
Note "  NOTE: this proves the AppContainer path RUNS and is honest; it does NOT yet"
Note "  prove the deep isolation (SID / can't-read-secrets / --no-net egress) — those"
Note "  are the box-measured criteria we add next once this is green."
$rac = Invoke-Probe "the829-test_appcontainer" "tests\test_appcontainer.py" @{}
Write-Host "  test_appcontainer.py EXIT $($rac.rc) (0 = contract holds). See $($rac.out)." -ForegroundColor ($(if($rac.rc -eq 0){"Green"}else{"Red"}))

# ---- summary ----------------------------------------------------------------
Section "SUMMARY"
Write-Host ("  THE-818 PowerShell probe : EXIT {0}  {1}" -f $r.rc,   $(if($r.rc -eq 0){"BOUND (good)"}elseif($r.rc -eq 3){"ESCAPED (bug)"}else{"setup"}))
Write-Host ("  THE-818 test_security    : EXIT {0}  {1}" -f $rsec.rc, $(if($rsec.rc -eq 0){"pass"}else{"fail"}))
Write-Host ("  THE-818 TaskScheduler    : EXIT {0}  {1}" -f $rc,     $(if($rc -eq 0){"BOUND (good)"}elseif($rc -eq 3){"ESCAPED (bug)"}else{"setup"}))
Write-Host ("  THE-829 test_appcontainer: EXIT {0}  {1}" -f $rac.rc, $(if($rac.rc -eq 0){"contract holds"}else{"fail"}))
Write-Host "`nAll outputs are in: $results" -ForegroundColor Green
Write-Host "Paste that folder's *.txt back (or point the Cave session at it) and I'll read the measurements." -ForegroundColor Green
