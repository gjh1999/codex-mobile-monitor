param([string]$AppName = 'CodexMobileMonitor-0.7.3')
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$dist = Join-Path $project 'release'
$work = Join-Path $project 'build-exe'
$specDir = Join-Path $project 'build-exe\specs'
New-Item -ItemType Directory -Force -Path $specDir | Out-Null

if (-not (Test-Path $python)) { throw "找不到虚拟环境 Python：$python" }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name $AppName `
  --paths $project `
  --icon "$(Join-Path $project 'app.ico')" `
  --add-data "$(Join-Path $project 'windows_notify.ps1');." `
  --add-data "$(Join-Path $project 'app.ico');." `
  --exclude-module tkinter --exclude-module _tkinter `
  --hidden-import dingtalk_commands --hidden-import desktop_win32 --hidden-import desktop_qt `
  --distpath $dist --workpath $work --specpath $specDir `
  (Join-Path $project 'desktop_app.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败，退出码：$LASTEXITCODE" }

# The EXE keeps user state next to itself. Copy the optional local configuration
# only for the first packaged run, preserving any existing release configuration.
foreach ($name in @('local-config.json')) {
  $source = Join-Path $project $name
  if ((Test-Path $source) -and -not (Test-Path (Join-Path $dist $name))) { Copy-Item $source (Join-Path $dist $name) }
}
Write-Host "Built: $(Join-Path $dist ($AppName + '.exe'))"
