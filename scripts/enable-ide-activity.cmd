@echo off
setlocal DisableDelayedExpansion
set "LANXI_IDE_DIR=%~dp0"
if not exist "%LANXI_IDE_DIR%Lanxi-IdeActivity.exe" (
  echo Extract the complete Lanxi package before enabling IDE activity.
  pause
  exit /b 1
)
powershell.exe -NoProfile -NonInteractive -Command "[Environment]::SetEnvironmentVariable('LANXI_INSTALL_DIR',$env:LANXI_IDE_DIR,'User')"
if errorlevel 1 exit /b 1
echo Lanxi location saved for this Windows user. Restart VS Code or PyCharm after installing its plugin.
pause
