param(
    [switch]$Remove,
    [ValidatePattern('^[^\\/:*?"<>|\x00-\x1f]+\.exe$')]
    [string]$ExecutableName
)
$ErrorActionPreference='Stop'
$taskName='CodexMobileMonitor'
if ($Remove) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false; exit }
$projectPath=Split-Path -Parent $PSScriptRoot
$releasePath = Join-Path $projectPath 'release'
$candidates = if ($ExecutableName) { $ExecutableName } else {
  'Lanxi.exe', 'CodexMobileMonitor-0.7.3.exe', 'CodexMobileMonitor-0.7.2.exe',
  'CodexMobileMonitor-0.7.1.exe', 'CodexMobileMonitor-0.7-final.exe', 'CodexMobileMonitor.exe'
}
$exePath = foreach ($name in $candidates) {
  $candidate = Join-Path $releasePath $name
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate; break }
}
if ($exePath) {
  $action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $releasePath
} elseif ($ExecutableName) {
  throw "Desktop EXE not found in release: $ExecutableName"
} else {
  $pythonPath=Join-Path $projectPath '.venv\Scripts\pythonw.exe'
  $scriptPath=Join-Path $projectPath 'desktop_app.py'
  if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Desktop EXE or project Python not found' }
  $action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"'+$scriptPath+'"') -WorkingDirectory $projectPath
}
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$existing=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne 'Local Codex monitor; only explicitly configured managed tasks can run.') { throw 'An unrelated task uses this name; refusing to overwrite' }
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local Codex monitor; only explicitly configured managed tasks can run.' -Force | Select-Object TaskName,State,@{Name='Executable';Expression={$_.Actions.Execute}}
