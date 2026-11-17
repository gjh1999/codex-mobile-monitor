param([switch]$Remove)
$ErrorActionPreference='Stop'
$taskName='CodexMobileMonitor'
if ($Remove) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false; exit }
$projectPath=Split-Path -Parent $PSScriptRoot
$exePath=Join-Path $projectPath 'release\CodexMobileMonitor-0.7.3.exe'
if (-not (Test-Path -LiteralPath $exePath)) { $exePath=Join-Path $projectPath 'release\CodexMobileMonitor-0.7.2.exe' }
if (-not (Test-Path -LiteralPath $exePath)) { $exePath=Join-Path $projectPath 'release\CodexMobileMonitor-0.7.1.exe' }
if (-not (Test-Path -LiteralPath $exePath)) { $exePath=Join-Path $projectPath 'release\CodexMobileMonitor-0.7-final.exe' }
if (-not (Test-Path -LiteralPath $exePath)) { $exePath=Join-Path $projectPath 'release\CodexMobileMonitor.exe' }
$pythonPath=Join-Path $projectPath '.venv\Scripts\pythonw.exe'
$scriptPath=Join-Path $projectPath 'desktop_app.py'
if (Test-Path -LiteralPath $exePath) {
  $executePath=$exePath
  $arguments=$null
  $workingDirectory=Join-Path $projectPath 'release'
} else {
  if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Desktop EXE or project Python not found' }
  $executePath=$pythonPath
  $arguments='"'+$scriptPath+'"'
  $workingDirectory=$projectPath
}
$action=New-ScheduledTaskAction -Execute $executePath -Argument $arguments -WorkingDirectory $workingDirectory
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$existing=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne 'Local Codex monitor; only explicitly configured managed tasks can run.') { throw 'An unrelated task uses this name; refusing to overwrite' }
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local Codex monitor; only explicitly configured managed tasks can run.' -Force | Select-Object TaskName,State,@{Name='Executable';Expression={$_.Actions.Execute}}
