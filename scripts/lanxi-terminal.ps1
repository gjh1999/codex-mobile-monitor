# Dot-source this file in the PowerShell profile used by the IDE terminal.
$script:LanxiHandoffScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'handoff.py'
$script:LanxiHandoffPython = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'

function global:codex {
    $native = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    if ($args.Count -gt 0 -or (Get-Location).Provider.Name -ne 'FileSystem') {
        & $native @args
        return
    }
    $reply = & $script:LanxiHandoffPython $script:LanxiHandoffScript prepare-resume --cwd (Get-Location).ProviderPath
    if ($LASTEXITCODE -ne 0) { return }
    if ($reply -ne 'null') {
        $binding = $reply | ConvertFrom-Json
        & $native resume --cd $binding.cwd $binding.thread
    } else {
        & $native
    }
}
