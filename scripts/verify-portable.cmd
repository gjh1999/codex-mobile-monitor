@echo off
setlocal
chcp 65001 >nul
"%~dp0Lanxi-Verify.exe" --exe "%~dp0Lanxi-Glass-R28-Portable.exe" %*
set "CHECK_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %CHECK_EXIT%
