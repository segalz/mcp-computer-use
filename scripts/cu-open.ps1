<#
.SYNOPSIS
  Launch an app and return the window name to use with `agent-cu -a`, as one JSON line.

.DESCRIPTION
  `agent-cu open <x> --wait` waits for an app literally named <x>. That fails for UWP/packaged apps
  (hosted by ApplicationFrameHost, localized titles such as 'מחשבון') and for single-instance apps.
  This script snapshots the open windows, launches with Start-Process, polls every 500 ms until a new
  window (new title / new app / new pid) appears or an existing one comes to the front, then prints:
    {"ok":true,"app":"<value for agent-cu -a>","title":"...","process":"...","pid":123,"seconds":1.5,"hint":"..."}
  On timeout: {"ok":false,"error":"...","seen":[...],"frontmost":{...},"seconds":15}
  It never closes or moves other windows.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File cu-open.ps1 calc.exe
  powershell -ExecutionPolicy Bypass -File cu-open.ps1 notepad.exe -Args C:\temp\a.txt
  powershell -ExecutionPolicy Bypass -File cu-open.ps1 ms-settings:
  powershell -ExecutionPolicy Bypass -File cu-open.ps1 cmd.exe -Args '/k echo hi'   # several args: one quoted string with -File
#>
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Target,
  [Alias('Args')][string[]]$ArgumentList = @(),
  [double]$TimeoutSec = 15,
  [switch]$NoVerify,
  [string]$AgentCu = 'agent-cu'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$sw = [Diagnostics.Stopwatch]::StartNew()
$HostApps = @('ApplicationFrameHost')

function Emit($obj) {
  $obj['seconds'] = [math]::Round($sw.Elapsed.TotalSeconds, 2)
  [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5))
  if ($obj['ok']) { exit 0 } else { exit 1 }
}

function Get-Windows {
  # Primary: agent-cu windows (all apps). Fallback: Get-Process MainWindowTitle.
  $list = @()
  try {
    $raw = & $AgentCu windows --compact 2>$null
    if ($LASTEXITCODE -eq 0 -and $raw) {
      foreach ($w in (($raw -join "`n") | ConvertFrom-Json)) {
        if ($w.title) {
          $list += [pscustomobject]@{ app = [string]$w.app; title = [string]$w.title; pid = [int]$w.pid
                                      frontmost = [bool]$w.frontmost; minimized = [bool]$w.minimized; source = 'agent-cu' }
        }
      }
    }
  } catch { }
  if (-not $list) {
    $fg = 0
    try {
      if (-not ('CuOpen.Win' -as [type])) {
        Add-Type -Namespace CuOpen -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
'@
      }
      [uint32]$p = 0; [void][CuOpen.Win]::GetWindowThreadProcessId([CuOpen.Win]::GetForegroundWindow(), [ref]$p); $fg = [int]$p
    } catch { }
    foreach ($p in (Get-Process | Where-Object { $_.MainWindowTitle })) {
      $list += [pscustomobject]@{ app = $p.ProcessName; title = $p.MainWindowTitle; pid = $p.Id
                                  frontmost = ($p.Id -eq $fg); minimized = $false; source = 'process' }
    }
  }
  return ,$list
}

function Key($w) { "$($w.pid)|$($w.title)" }

function Stem([string]$t) {
  $s = $t -replace '^.*[\\/]', '' -replace '\.(exe|lnk|appref-ms)$', '' -replace ':.*$', ''
  return $s.ToLowerInvariant()
}

function Test-Target([string]$name) {
  if ($NoVerify -or -not $name) { return $null }
  try {
    $null = & $AgentCu snapshot -a $name -i -c --timeout 5 2>$null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
}

function Result($w, [string]$how, $proc) {
  $isHost = $HostApps -contains $w.app
  # UWP windows live in ApplicationFrameHost: agent-cu targets them by window title.
  $app = if ($isHost) { $w.title } else { $w.app }
  $verified = Test-Target $app
  if ($verified -eq $false -and $app -ne $w.title) {
    if (Test-Target $w.title) { $app = $w.title; $verified = $true }
  }
  $hint = switch ($how) {
    'new'      { if ($isHost) { "UWP app hosted by ApplicationFrameHost: use -a '$app' (the window title)." } else { "Use -a '$app'." } }
    'front'    { "No new window: an existing window came to the front (single-instance app?). Use -a '$app'." }
  }
  if ($verified -eq $false) { $hint += " Warning: agent-cu snapshot -a '$app' failed; try -a with the title." }
  $procName = (Get-Process -Id $w.pid -ErrorAction SilentlyContinue).ProcessName
  if (-not $procName -and $proc) { try { $procName = $proc.ProcessName } catch { } }
  Emit ([ordered]@{ ok = $true; app = $app; title = $w.title; process = $procName; pid = $w.pid
                    seconds = 0; hint = $hint.Trim(); verified = $verified; source = $w.source })
}

# ---------------------------------------------------------------- before
$before = Get-Windows
$beforeKeys = @{}; foreach ($w in $before) { $beforeKeys[(Key $w)] = $true }
$beforePids = @{}; foreach ($w in $before) { $beforePids[$w.pid] = $true }
$beforeFront = $before | Where-Object frontmost | Select-Object -First 1

# ---------------------------------------------------------------- launch
$proc = $null
try {
  $sp = @{ FilePath = $Target; PassThru = $true }
  if ($ArgumentList.Count -gt 0) { $sp.ArgumentList = $ArgumentList }
  $proc = Start-Process @sp
} catch {
  Emit ([ordered]@{ ok = $false; error = "Start-Process failed: $($_.Exception.Message)"; target = $Target; seconds = 0 })
}
$launchPid = if ($proc) { try { $proc.Id } catch { $null } } else { $null }
$stem = Stem $Target

# ---------------------------------------------------------------- poll
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
$last = @()
while ([DateTime]::UtcNow -lt $deadline) {
  Start-Sleep -Milliseconds 500
  $now = Get-Windows
  $last = $now
  $new = @($now | Where-Object { -not $beforeKeys.ContainsKey((Key $_)) })
  if ($new.Count -gt 0) {
    # prefer: launched pid > name match > new process > frontmost > first
    $pick = $new | Where-Object { $launchPid -and $_.pid -eq $launchPid } | Select-Object -First 1
    if (-not $pick) { $pick = $new | Where-Object { $stem -and ($_.app.ToLowerInvariant() -like "*$stem*" -or $_.title.ToLowerInvariant() -like "*$stem*") } | Select-Object -First 1 }
    if (-not $pick) { $pick = $new | Where-Object { -not $beforePids.ContainsKey($_.pid) } | Select-Object -First 1 }
    if (-not $pick) { $pick = $new | Where-Object frontmost | Select-Object -First 1 }
    if (-not $pick) { $pick = $new[0] }
    # UWP hosts sometimes show a generic title first; give it one more tick to settle
    if ($HostApps -contains $pick.app) {
      Start-Sleep -Milliseconds 500
      $cur = Get-Windows
      $again = $cur | Where-Object { $_.pid -eq $pick.pid -and -not $beforeKeys.ContainsKey((Key $_)) } | Select-Object -First 1
      if ($again) { $pick = $again }
    }
    Result $pick 'new' $proc
  }
  # single-instance app: no new window, but a matching existing window became frontmost
  $front = $now | Where-Object frontmost | Select-Object -First 1
  if ($front -and (-not $beforeFront -or (Key $front) -ne (Key $beforeFront))) {
    $match = ($launchPid -and $front.pid -eq $launchPid) -or
             ($stem -and ($front.app.ToLowerInvariant() -like "*$stem*" -or $front.title.ToLowerInvariant() -like "*$stem*"))
    $elapsed = $sw.Elapsed.TotalSeconds
    if ($match -or $elapsed -gt [math]::Min(4, $TimeoutSec / 2)) { Result $front 'front' $proc }
  }
}

$seen = @($last | Where-Object { -not $beforeKeys.ContainsKey((Key $_)) } | ForEach-Object { "$($_.app): $($_.title)" })
$frontNow = $last | Where-Object frontmost | Select-Object -First 1
Emit ([ordered]@{
  ok = $false
  error = "No new window appeared within $TimeoutSec s after launching '$Target'. The app may still be starting, may have opened in the background, or reused an existing window."
  target = $Target; launched_pid = $launchPid
  new_windows_seen = $seen
  frontmost = if ($frontNow) { [ordered]@{ app = $frontNow.app; title = $frontNow.title; pid = $frontNow.pid } } else { $null }
  hint = "Run 'agent-cu windows' (or 'agent-cu windows -a ApplicationFrameHost' for UWP apps) and use the window title with -a."
  seconds = 0
})
