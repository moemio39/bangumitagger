@echo off
setlocal
node "%~dp0launcher.cjs" %*
set "status=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %status%
