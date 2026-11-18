# Dot-source this file in the PowerShell profile used by the IDE terminal.
function global:codex {
    $native = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    if ($args.Count -gt 0 -or (Get-Location).Provider.Name -ne 'FileSystem') {
        & $native @args
        return
    }
    $lanxiRoot = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $lanxiRoot '.venv\Scripts\python.exe'
    & $python (Join-Path $lanxiRoot 'coordination_client.py') terminal --cwd (Get-Location).ProviderPath --resume -- $native
}
