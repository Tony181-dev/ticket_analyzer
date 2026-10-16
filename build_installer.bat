@echo off
rem Build the Windows installer: dist\TicketAnalyzer-Setup-<version>.exe
rem   - installs per user (no admin), Start menu + optional Desktop shortcut
rem   - registers an uninstaller (Apps & features, Start menu "Uninstall Ticket Analyzer")
rem Usage: build_installer.bat [--skip-build]   (--skip-build reuses the existing dist\TicketAnalyzer)
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if /i not "%~1"=="--skip-build" (
  call "%~dp0build_exe.bat"
  if errorlevel 1 exit /b 1
)
if not exist "%~dp0dist\TicketAnalyzer\TicketAnalyzer.exe" (
  echo [!] Chua co dist\TicketAnalyzer. Chay build_exe.bat truoc.
  exit /b 1
)

call :findiscc
if not defined ISCC (
  echo [Installer] Chua co Inno Setup - cai qua winget ^(chi cho user hien tai^)...
  winget install -e --id JRSoftware.InnoSetup --scope user --silent --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo [!] Khong tim thay Inno Setup 6. Cai tu https://jrsoftware.org/isdl.php roi chay lai.
  exit /b 1
)

set "VER="
for /f "delims=" %%V in ('call "%~dp0.venv\Scripts\python.exe" -c "import app; print(app.__version__)"') do set "VER=%%V"
if not defined VER ( echo [!] Khong doc duoc phien ban app. & exit /b 1 )
echo [Installer] Dong goi bo cai %VER% bang "%ISCC%" ...
"%ISCC%" /Qp /DAppVersion=%VER% "%~dp0packaging\installer.iss"
if errorlevel 1 ( echo [!] Inno Setup loi. & exit /b 1 )
if not exist "%~dp0dist\TicketAnalyzer-Setup-%VER%.exe" ( echo [!] Khong thay file setup. & exit /b 1 )
echo.
echo Xong: dist\TicketAnalyzer-Setup-%VER%.exe
echo  - Cai dat: chay file Setup (khong can quyen admin)
echo  - Go cai dat: Settings ^> Apps ^> Ticket Analyzer, hoac Start menu ^> Uninstall Ticket Analyzer
goto :eof

:findiscc
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%I in ('where iscc 2^>nul') do set "ISCC=%%I"
goto :eof
