param(
  [string]$AppName = 'Lanxi',
  [string]$OutputDirectory,
  [string]$BuildDirectory
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$dist = if ($OutputDirectory) { $OutputDirectory } else { Join-Path $project 'release' }
$work = if ($BuildDirectory) { $BuildDirectory } else { Join-Path $project 'build-exe' }
$specDir = Join-Path $work 'specs'
$helpers = Join-Path $work 'helpers'
New-Item -ItemType Directory -Force -Path $specDir | Out-Null

if (-not (Test-Path $python)) { throw "找不到虚拟环境 Python：$python" }
& $python -m PyInstaller --noconfirm --clean --onefile --console `
  --name Lanxi-Verify --paths $project `
  --distpath $helpers --workpath $work --specpath $specDir `
  (Join-Path $project 'tests\smoke_personal_exe.py')
if ($LASTEXITCODE -ne 0) { throw "Lanxi-Verify 构建失败，退出码：$LASTEXITCODE" }

& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name $AppName `
  --paths $project `
  --icon "$(Join-Path $project 'app.ico')" `
  --add-data "$(Join-Path $project 'windows_notify.ps1');." `
  --add-data "$(Join-Path $project 'app.ico');." `
  --add-data "$(Join-Path $helpers 'Lanxi-Verify.exe');." `
  --add-data "$(Join-Path $project 'docs\assets\os-x-mavericks-wave.jpg');docs/assets" `
  --add-data "$(Join-Path $project 'docs\assets\macos-big-sur-day.jpg');docs/assets" `
  --add-data "$(Join-Path $project 'docs\assets\macos-catalina-night.jpg');docs/assets" `
  --exclude-module tkinter --exclude-module _tkinter `
  --hidden-import dingtalk_commands --hidden-import desktop_win32 --hidden-import desktop_qt `
  --distpath $dist --workpath $work --specpath $specDir `
  (Join-Path $project 'desktop_app.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败，退出码：$LASTEXITCODE" }

# A configuration next to the EXE can seed the first profile migration.
if (-not $OutputDirectory) {
  $source = Join-Path $project 'local-config.json'
  if ((Test-Path $source) -and -not (Test-Path (Join-Path $dist 'local-config.json'))) {
    Copy-Item $source (Join-Path $dist 'local-config.json')
  }
}
Write-Host "Built: $(Join-Path $dist ($AppName + '.exe'))"
