# THE-829 AppContainer isolation — real-Windows-11 acceptance harness.
#
# Builds the executor from the current checkout (must include the THE-818 alias
# fix, or the AppContainer child broker-escapes the same way), plants a secret in
# the user profile, then runs scripts\appcontainer_probe.py through codecalc TWICE
# -- a control arm (no flag) and an AppContainer arm (CODECALC_WIN_APPCONTAINER=1)
# -- and checks the two results against the ticket's acceptance criteria.
#
# PowerShell 5.1 safe: ASCII only, no '&&', explicit $LASTEXITCODE / Test-Path.
# The AppContainer grants its SID only the workdir and the RESOLVED interpreter's
# own directory. So AppContainer mode needs a SELF-CONTAINED interpreter -- exe,
# DLLs and stdlib all under one directory. A PyManager/pythoncore install whose
# stdlib lives in a separate redirected install tree, or a Store alias, cannot
# load under the AppContainer (fails closed, 0x0005 access denied). Pass
# -PythonDir <dir> at a self-contained interpreter (e.g. a uv-managed cpython
# dir); it is prepended to PATH so BOTH arms use the same interpreter.
#
# Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts\verify_appcontainer.ps1 -PythonDir C:\path\to\selfcontained-python
param([string]$PythonDir = "")

$ErrorActionPreference = "Continue"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location -LiteralPath $repo -ErrorAction Stop

if ($PythonDir -ne "") {
    if (-not (Test-Path -LiteralPath (Join-Path $PythonDir "python3.exe"))) {
        Write-Host "WARNING: no python3.exe under -PythonDir '$PythonDir'; AppContainer arm may fail closed"
    }
    $env:PATH = $PythonDir + ";" + $env:PATH
    Write-Host "PATH prepended with self-contained interpreter dir: $PythonDir"
}

$fails = New-Object System.Collections.Generic.List[string]
function Check($name, $cond, $detail) {
    $tag = if ($cond) { "PASS" } else { "FAIL" }
    Write-Host ("{0} {1} {2}" -f $tag, $name, $detail)
    if (-not $cond) { $fails.Add($name) | Out-Null }
}

# ---- 1. build the executor from this checkout ------------------------------
Write-Host "== building codecalc-exec.exe (release) =="
& cargo build --release --manifest-path executor\Cargo.toml 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 2 }
$exe = Join-Path $repo "executor\target\release\codecalc-exec.exe"
if (-not (Test-Path -LiteralPath $exe)) { Write-Host "no exe at $exe"; exit 2 }
Write-Host ("git HEAD: " + (& git rev-parse --short HEAD))

# ---- 2. plant a user-profile secret ----------------------------------------
$secret = Join-Path $env:USERPROFILE "codecalc-ac-secret.txt"
Set-Content -LiteralPath $secret -Value "TOP-SECRET-THE829" -Encoding ASCII
$probe = Join-Path $repo "scripts\appcontainer_probe.py"
$code = Get-Content -LiteralPath $probe -Raw

# ---- 3. run one arm, return the probe's parsed JSON ------------------------
function Run-Arm($appcontainer) {
    if ($appcontainer) { $env:CODECALC_WIN_APPCONTAINER = "1" }
    else { Remove-Item Env:\CODECALC_WIN_APPCONTAINER -ErrorAction SilentlyContinue }
    $raw = $code | & $exe --lang python3 --timeout 60 2>$null
    Remove-Item Env:\CODECALC_WIN_APPCONTAINER -ErrorAction SilentlyContinue
    $outer = $null; $inner = $null
    try { $outer = $raw | ConvertFrom-Json } catch {}
    # The executor carries the payload's stdout in the "stdout" field.
    if ($null -ne $outer -and $outer.stdout) {
        try { $inner = $outer.stdout | ConvertFrom-Json } catch {}
    }
    return [pscustomobject]@{ outer = $outer; inner = $inner; raw = $raw }
}

Write-Host "== control arm (no AppContainer) =="
$ctl = Run-Arm $false
Write-Host ("control probe: " + ($ctl.inner | ConvertTo-Json -Compress))
Write-Host "== AppContainer arm (CODECALC_WIN_APPCONTAINER=1) =="
$ac = Run-Arm $true
Write-Host ("appcontainer probe: " + ($ac.inner | ConvertTo-Json -Compress))
Write-Host ("appcontainer unenforced: " + ($ac.outer.unenforced -join ", "))

Remove-Item -LiteralPath $secret -ErrorAction SilentlyContinue

# Diagnostic: if the AppContainer arm could not execute the payload (0xC0000135
# STATUS_DLL_NOT_FOUND = the AC child could not read the interpreter's DLLs),
# show whether the AC-SID grant actually reached the interpreter's files. The
# grant is not reverted on teardown, so an orphaned S-1-15-2-* ACE here means the
# inheritable grant DID propagate to existing files (bug is elsewhere); its
# ABSENCE means propagation to existing files is the bug.
if ($PythonDir -ne "" -and $ac.outer.exit_code -eq 3221225781) {
    Write-Host "== AC arm exited 0xC0000135 (DLL_NOT_FOUND). ACL on interpreter files =="
    Write-Host "   (an S-1-15-2-* entry with (R) = an AppContainer read ACE reached the file)"
    foreach ($f in @("python3.exe", "python.exe", "python311.dll")) {
        $p = Join-Path $PythonDir $f
        if (Test-Path -LiteralPath $p) { & icacls $p 2>&1 | Out-Host }
    }
    Write-Host "== directory ACL (inheritance flags on the granted dir) =="
    & icacls $PythonDir 2>&1 | Out-Host
}

if ($null -eq $ctl.inner -or $null -eq $ac.inner) {
    Write-Host "ABORT: one arm produced no parseable probe JSON (see raw above)"
    Write-Host ("control raw:      " + $ctl.raw)
    Write-Host ("appcontainer raw: " + $ac.raw)
    exit 2
}

# ---- 4. acceptance checks --------------------------------------------------
# Control arm establishes the baseline: without the AppContainer the payload CAN
# reach these, so a blocked AC arm is the AppContainer's doing, not the box's.
Check "control: baseline can read the secret"   ($ctl.inner.can_read_secret -eq $true)  "-> secret is otherwise readable"
Check "control: baseline has network"           ($ctl.inner.can_network -eq $true)      "-> network otherwise works"
Check "control: not an AppContainer token"      ($ctl.inner.is_appcontainer -eq $false) "-> control is unconfined"

# Acceptance criterion 1: AppContainer SID + no ambient privileges.
Check "AC: token IS an AppContainer"            ($ac.inner.is_appcontainer -eq $true)   ("-> sid=" + $ac.inner.appcontainer_sid)
Check "AC: has an AppContainer SID"             ([string]::IsNullOrEmpty([string]$ac.inner.appcontainer_sid) -eq $false) ("-> " + $ac.inner.appcontainer_sid)
# "No ambient user token privileges" means no DANGEROUS/ambient privilege
# survives — asserted BY NAME so it is not flaky across boxes. An AppContainer
# legitimately retains two benign, unavoidable ones (SeChangeNotify: Everyone has
# it, bypass-traverse; SeIncreaseWorkingSet: grow its own working set), so a bare
# count bar is wrong. Also require the set to have shrunk vs the unconfined control.
$dangerous = @(
    "SeDebugPrivilege", "SeBackupPrivilege", "SeRestorePrivilege", "SeTakeOwnershipPrivilege",
    "SeTcbPrivilege", "SeLoadDriverPrivilege", "SeImpersonatePrivilege", "SeAssignPrimaryTokenPrivilege",
    "SeSecurityPrivilege", "SeCreateTokenPrivilege", "SeManageVolumePrivilege",
    "SeSystemEnvironmentPrivilege", "SeShutdownPrivilege", "SeUndockPrivilege", "SeTimeZonePrivilege"
)
$acPrivs = @($ac.inner.privilege_names)
$leaked = @($acPrivs | Where-Object { $dangerous -contains $_ })
Check "AC: token privileges enumerated" ($null -ne $ac.inner.privilege_names) "-> names readable, not a token error"
Check "AC: no dangerous/ambient privilege survives" ($leaked.Count -eq 0) ("-> leaked=[" + ($leaked -join ",") + "] AC=[" + ($acPrivs -join ",") + "]")
Check "AC: privilege set reduced vs control" ([int]$ac.inner.privilege_count -lt [int]$ctl.inner.privilege_count) ("-> AC=" + $ac.inner.privilege_count + " control=" + $ctl.inner.privilege_count)

# Acceptance criterion 2: cannot read user-profile secrets / write outside workdir.
Check "AC: CANNOT read the user-profile secret" ($ac.inner.can_read_secret -eq $false)  ("-> read_err=" + $ac.inner.read_err)
Check "AC: CANNOT write outside the workdir"    ($ac.inner.can_write_outside -eq $false) ("-> err=" + $ac.inner.write_outside_err)
Check "AC: CAN still write inside the workdir"  ($ac.inner.can_write_workdir -eq $true)  "-> sandbox stays usable"

# Acceptance criterion 3: no-net egress blocked (structurally: no network capability SID).
Check "AC: network egress is BLOCKED"           ($ac.inner.can_network -eq $false)      ("-> net_err=" + $ac.inner.net_err)

# The AppContainer path is taken and still discloses itself unverified (by design).
Check "AC: path was taken (disclosure present)" ($ac.outer.unenforced -contains "appcontainer_isolation_unverified_on_windows") "-> honesty token emitted"

if ($fails.Count -eq 0) {
    Write-Host "`n=== THE-829 AppContainer isolation VERIFIED on this box ==="
    exit 0
} else {
    Write-Host ("`n=== " + $fails.Count + " FAILURE(S): " + ($fails -join "; ") + " ===")
    exit 1
}
