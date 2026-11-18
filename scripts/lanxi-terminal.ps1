# Dot-source this file in the PowerShell profile used by the IDE terminal.
function global:codex {
    $native = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    if ($args.Count -gt 0 -or (Get-Location).Provider.Name -ne 'FileSystem') {
        & $native @args
        return
    }
    $lanxiRoot = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $lanxiRoot '.venv\Scripts\python.exe'
    $reply = & $python (Join-Path $lanxiRoot 'handoff.py') prepare-resume --cwd (Get-Location).ProviderPath
    if ($LASTEXITCODE -ne 0) { return }
    if ($reply -ne 'null') {
        $binding = $reply | ConvertFrom-Json
        & $native resume --cd $binding.cwd $binding.thread
    } else {
        & $native
    }
}
