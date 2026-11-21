# Notify the local desktop and let Codex open its original approval prompt.
$ErrorActionPreference = 'Stop'
try {
    [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
    $text = [Console]::In.ReadToEnd()
    if ($text.Length -gt 1048576) { exit 0 }
    $event = ConvertFrom-Json -InputObject $text
    if ($event.hook_event_name -ne 'PermissionRequest') { exit 0 }

    $project = [IO.Path]::GetFileName(([string]$event.cwd).TrimEnd('\', '/'))
    $tool = [string]$event.tool_name
    $thread = [string]$event.session_id
    $row = @{
        project = $project.Substring(0, [Math]::Min(160, $project.Length))
        tool = $tool.Substring(0, [Math]::Min(120, $tool.Length))
        thread = $thread.Substring(0, [Math]::Min(80, $thread.Length))
    }
    $inbox = Join-Path $PSScriptRoot 'codex-approval-alerts'
    [void][IO.Directory]::CreateDirectory($inbox)
    $name = Join-Path $inbox ([Guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($name + '.tmp', (ConvertTo-Json -InputObject $row -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($name + '.tmp', $name + '.json')
} catch {
    # The reminder is optional. An unavailable inbox must leave native approval available.
}
exit 0
