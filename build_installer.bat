@echo off
rem Build the Windows installer package in dist\:
rem   - TicketAnalyzer-Setup-<version>.exe   (installer, per user, no admin)
rem   - TicketAnalyzer-Uninstall.bat          (finds and runs the installed uninstaller)
rem Usage: build_installer.bat [--skip-build]   (--skip-build reuses dist\TicketAnalyzer if it still exists)
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if /i not "%~1"=="--skip-build" (
  call "%~dp0build_exe.bat"
  if errorlevel 1 goto :fail
)
if not exist "%~dp0dist\TicketAnalyzer\TicketAnalyzer.exe" (
  echo [!] Chua co dist\TicketAnalyzer. Chay build_exe.bat truoc.
  goto :fail
)

call :findiscc
if not defined ISCC (
  echo [Installer] Chua co Inno Setup - cai qua winget ^(chi cho user hien tai^)...
  winget install -e --id JRSoftware.InnoSetup --scope user --silent --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo [!] Khong tim thay Inno Setup 6. Cai tu https://jrsoftware.org/isdl.php roi chay lai.
  goto :fail
)

set "VER="
for /f "delims=" %%V in ('call "%~dp0.venv\Scripts\python.exe" -c "import app; print(app.__version__)"') do set "VER=%%V"
if not defined VER ( echo [!] Khong doc duoc phien ban app. & goto :fail )
echo [Installer] Dong goi bo cai %VER% bang "%ISCC%" ...
"%ISCC%" /Qp /DAppVersion=%VER% "%~dp0packaging\installer.iss"
if errorlevel 1 ( echo [!] Inno Setup loi. & goto :fail )
set "SETUP=%~dp0dist\TicketAnalyzer-Setup-%VER%.exe"
if not exist "%SETUP%" ( echo [!] Khong thay file setup. & goto :fail )

echo [Installer] Don dep file cu / tam...
for %%F in ("%~dp0dist\TicketAnalyzer-Setup-*.exe") do if /i not "%%~nxF"=="TicketAnalyzer-Setup-%VER%.exe" del /q "%%~F"
for %%F in ("%~dp0dist\TicketAnalyzer-*-win64.zip") do del /q "%%~F"
if exist "%~dp0build" rd /s /q "%~dp0build"
if exist "%~dp0dist\TicketAnalyzer" rd /s /q "%~dp0dist\TicketAnalyzer"
copy /y "%~dp0packaging\uninstall.bat" "%~dp0dist\TicketAnalyzer-Uninstall.bat" >nul

echo.
echo Xong, thu muc dist chua:
echo  - TicketAnalyzer-Setup-%VER%.exe : bo cai (khong can quyen admin)
echo  - TicketAnalyzer-Uninstall.bat    : bo go ^(hoac Settings ^> Apps ^> Ticket Analyzer^)
explorer /select,"%SETUP%"
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1

:findiscc
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%I in ('where iscc 2^>nul') do set "ISCC=%%I"
goto :eof
