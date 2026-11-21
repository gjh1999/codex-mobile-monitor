@echo off
setlocal DisableDelayedExpansion
set "LANXI_DIAG_FILE=%~f0"
set "LANXI_DIAG_PROJECT=%~1"
set "LANXI_DIAG_DATA_DIR="
powershell.exe -NoLogo -NoProfile -Command "$s=[IO.File]::ReadAllText($env:LANXI_DIAG_FILE,[Text.Encoding]::UTF8) -split '(?m)^:LANXI_DIAG\r?$',2; & ([ScriptBlock]::Create($s[1]))"
set "result=%errorlevel%"
if /i not "%~1"=="--no-pause" pause
exit /b %result%
:LANXI_DIAG
$ErrorActionPreference = 'Stop'
$lines = [Collections.Generic.List[string]]::new()
function Note([string]$message) { $lines.Add($message); Write-Host $message }
function Read-Git([string[]]$arguments) {
    $ErrorActionPreference = 'Continue'
    & $git.Source -C $project @arguments 2>$null
}
function Path-Key([string]$path) {
    if ($path.StartsWith('\\?\UNC\')) { $path = '\\' + $path.Substring(8) }
    elseif ($path.StartsWith('\\?\')) { $path = $path.Substring(4) }
    if ($path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') { return }
    [IO.Path]::GetFullPath($path).TrimEnd('\')
}
try {
    $data = if ($env:LANXI_DIAG_DATA_DIR) { $env:LANXI_DIAG_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'Lanxi\data' }
    $root = $null
    $library = Join-Path $data 'project-library.json'
    if ([IO.File]::Exists($library)) {
        try { $root = Path-Key (([IO.File]::ReadAllText($library) | ConvertFrom-Json).root) }
        catch { Note '项目母目录配置不可读。' }
    }
    $native = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    Note "电脑：$env:COMPUTERNAME"
    Note "澜汐项目母目录：$root"
    $paths = [Collections.Generic.List[string]]::new()
    if ($env:LANXI_DIAG_PROJECT -and $env:LANXI_DIAG_PROJECT -ne '--no-pause') {
        $manual = Path-Key $env:LANXI_DIAG_PROJECT
        if (!$manual) { throw '请将项目文件夹直接拖到这个脚本上。' }
        $paths.Add($manual)
    }
    elseif ($root) {
        $paths.Add((Join-Path $root 'vscodePronject\Satisfactry'))
        foreach ($name in @('Satisfactry','VscodeProject\Satisfactry','AppData\Local\Temp\satisfactory-sbp-source')) {
            $candidate = Join-Path $root $name
            if ([IO.Directory]::Exists($candidate) -or $name.StartsWith('AppData')) { $paths.Add($candidate) }
        }
    }
    if (!$paths.Count) { throw '未读取到澜汐项目母目录。请将实际项目文件夹拖到脚本上再测。' }
    $projects = @($paths | Select-Object -Unique)
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $known = @{}
    foreach ($project in $projects) {
        Note ""
        Note "项目路径：$project"
        if (![IO.Directory]::Exists($project)) { Note '目录状态：未找到或不可访问'; continue }
        if (!(Test-Path -LiteralPath (Join-Path $project '.git'))) { Note '目录存在，根目录没有 .git'; continue }
        if (!$git) { Note '目录有 .git；未找到 git.exe，无法读取 origin 和接续索引。'; continue }
        $origin = Read-Git @('config','--get','remote.origin.url')
        if ($LASTEXITCODE -ne 0) { Note 'Git 未能读取 origin（可能未配置或仓库访问受限）。' }
        elseif ($origin -match '^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$') {
            $repo = ($Matches[1] + '/' + ($Matches[2] -replace '\.git$','')).ToLowerInvariant()
            Note "GitHub 仓库：$repo"
        } else { Note 'origin 不是澜汐支持的标准 GitHub 地址。' }
        $gitDir = Read-Git @('rev-parse','--absolute-git-dir')
        if ($LASTEXITCODE -ne 0) { Note 'Git 目录不可读。'; continue }
        $index = Join-Path $gitDir 'lanxi-personal-sessions.json'
        if (![IO.File]::Exists($index)) { Note '本机没有此仓库的澜汐会话接续索引。'; continue }
        try {
            $keys = @(([IO.File]::ReadAllText($index) | ConvertFrom-Json).PSObject.Properties | Where-Object { $_.Name -match '^[0-9a-f-]{36}$' } | ForEach-Object Name)
            Note "接续索引：$($keys.Count) 条"
            foreach ($id in $keys) { $known[$id] = $project; Note "  已登记 $id" }
        } catch { Note '接续索引存在，但读取失败。' }
    }
    Note ""
    Note '本机 Codex 会话（按原始工作目录或接续索引匹配）'
    $sessions = Join-Path $native 'sessions'
    if (![IO.Directory]::Exists($sessions)) { Note '未找到或无法访问 Codex sessions 目录。' }
    else {
        $matched = 0
        $failed = 0
        $scanErrors = @()
        $files = Get-ChildItem -LiteralPath $sessions -Filter 'rollout-*.jsonl' -Recurse -File -ErrorAction SilentlyContinue -ErrorVariable +scanErrors
        foreach ($file in $files) {
            try {
                $stream = [IO.File]::Open($file.FullName,'Open','Read',([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                try {
                    $reader = [IO.StreamReader]::new($stream)
                    try { $head = $reader.ReadLine() | ConvertFrom-Json } finally { $reader.Dispose() }
                } finally { $stream.Dispose() }
                $id = [string]$head.payload.id
                if ($head.type -ne 'session_meta' -or $id -notmatch '^[0-9a-f-]{36}$') { $failed++; continue }
                $cwd = Path-Key ([string]$head.payload.cwd)
                $found = $known.ContainsKey($id)
                foreach ($project in $projects) {
                    if ($cwd -and ($cwd -eq $project -or $cwd.StartsWith($project + '\',[StringComparison]::OrdinalIgnoreCase))) { $found = $true; break }
                }
                if (!$found) { continue }
                $matched++
                $size = if ($file.Length -ge 10MB) { '已达单条同步上限' } else { '原文件小于上限' }
                Note ("  {0} | {1:N2} MiB | {2} | 工作目录 {3}" -f $id,($file.Length / 1MB),$size,$cwd)
            } catch { $failed++ }
        }
        Note "本机匹配 $matched 条；无法读取的文件头 $failed 个；目录扫描错误 $($scanErrors.Count) 个。"
    }
    $report = Join-Path ([IO.Path]::GetTempPath()) ('Lanxi-project-check-' + [guid]::NewGuid().ToString('N') + '.txt')
    [IO.File]::WriteAllLines($report,$lines,[Text.UTF8Encoding]::new($true))
    Write-Host "报告已保存：$report"
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
exit 0
