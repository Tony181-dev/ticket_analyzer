@echo off
rem Build dist\TicketAnalyzer and its portable ZIP. Called by build_installer.bat.
rem Usage: build_exe.bat [--no-pause]
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "VPY=%~dp0.venv\Scripts\python.exe"

if exist "%VPY%" goto :deps
call :findpy
if not defined PY (
  echo [!] Khong tim thay Python 3.10+ 64-bit. Cai Python roi chay lai.
  goto :fail
)
echo [Build] Tao moi truong .venv...
%PY% -m venv --system-site-packages "%~dp0.venv"
if errorlevel 1 goto :fail

:deps
"%VPY%" -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 10) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
  echo [!] Build Windows can Python 3.10+ 64-bit trong .venv.
  goto :fail
)
echo [Build] Kiem tra thu vien dong goi...
"%VPY%" -c "import PyInstaller, playwright, uv, py7zr, multivolumefile" >nul 2>&1
if not errorlevel 1 goto :build
"%VPY%" -m pip install --retries 1 --timeout 30 -r "%~dp0requirements.txt" "pyinstaller>=6,<7"
if errorlevel 1 goto :fail

:build
rem PyInstaller replaces these outputs: reject links that could lead outside the checkout.
"%VPY%" -c "from pathlib import Path; r=Path.cwd().resolve(); targets=[r/'build', r/'dist', r/'dist'/'TicketAnalyzer']; assert all(p.resolve().is_relative_to(r) and p.resolve()!=r for p in targets), 'Build output points outside the checkout'"
if errorlevel 1 goto :fail
echo [Build] Dong goi TicketAnalyzer.exe...
"%VPY%" -m PyInstaller --noconfirm --clean --distpath "%~dp0dist" --workpath "%~dp0build" "%~dp0packaging\ticket_analyzer.spec"
if errorlevel 1 goto :fail
if not exist "%~dp0dist\TicketAnalyzer\TicketAnalyzer.exe" (
  echo [!] PyInstaller khong tao TicketAnalyzer.exe.
  goto :fail
)
echo [Build] Tao ZIP portable...
"%VPY%" -c "import shutil; from pathlib import Path; from app import __version__; print(shutil.make_archive(str(Path('dist')/('TicketAnalyzer-'+__version__+'-win64')), 'zip', 'dist', 'TicketAnalyzer'))"
if errorlevel 1 goto :fail
echo [Build] Xong. EXE va ZIP nam trong dist.
if /i not "%~1"=="--no-pause" pause
exit /b 0

:findpy
for %%C in ("py -3" "python" "python3") do (
  %%~C -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 10) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PY=%%~C"
    goto :eof
  )
)
goto :eof

:fail
echo [!] Build EXE that bai. Xem loi o tren.
if /i not "%~1"=="--no-pause" pause
exit /b 1
