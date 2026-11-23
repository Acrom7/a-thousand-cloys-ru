@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-RU.ps1" -Mode Install
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
