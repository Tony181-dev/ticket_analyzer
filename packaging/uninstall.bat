@echo off
rem Standalone uninstaller launcher: finds the registered Ticket Analyzer uninstaller and runs it.
setlocal
set "KEY=Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F2C4E7A-3B1D-4C8E-9A57-1D2E8B4F0C93}_is1"
set "UNINS="
for %%H in (HKCU HKLM) do if not defined UNINS for /f "tokens=2,*" %%A in ('reg query "%%H\%KEY%" /v UninstallString 2^>nul ^| findstr /i UninstallString') do set "UNINS=%%B"
if not defined UNINS (
  echo Ticket Analyzer is not installed on this account.
  pause
  exit /b 1
)
start "" %UNINS%
