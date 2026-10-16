@echo off
rem Build a shareable Windows package: dist\TicketAnalyzer-<version>-win64.zip
rem Recipients unzip it and double-click TicketAnalyzer.exe (no Python needed).
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PIP_DISABLE_PIP_VERSION_CHECK=1
set "VPY=.venv\Scripts\python.exe"
set "QUICK=--retries 1 --timeout 20"

if not exist "%VPY%" (
  call :findpy
  if not defined PY ( echo [!] Can Python 3.10+ de build. & exit /b 1 )
  %PY% -m venv --system-site-packages .venv || exit /b 1
)

echo [1/3] Chuan bi cong cu build (PyInstaller)...
%VPY% -c "import playwright, uv, PyInstaller" >nul 2>&1 && goto build
%VPY% -m pip install -q %QUICK% -r requirements.txt pyinstaller && goto build
if not defined PY call :findpy
if defined PY %PY% -m pip --python "%VPY%" install -q %QUICK% --upgrade pip >nul 2>&1
%VPY% -m pip install -q %QUICK% -r requirements.txt pyinstaller && goto build
%VPY% -m pip install -q %QUICK% --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt pyinstaller && goto build
echo [!] Khong cai duoc PyInstaller.
exit /b 1

:build
echo [2/3] Dong goi TicketAnalyzer.exe...
%VPY% -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build packaging\ticket_analyzer.spec
if errorlevel 1 ( echo [!] PyInstaller loi. & exit /b 1 )
copy /y README.md dist\TicketAnalyzer\README.md >nul

for /f %%V in ('%VPY% -c "import app; print(app.__version__)"') do set VER=%%V
set "ZIP=dist\TicketAnalyzer-%VER%-win64.zip"
echo [3/3] Nen %ZIP% ...
if exist "%ZIP%" del /q "%ZIP%"
%VPY% -c "import shutil, sys; shutil.make_archive(sys.argv[1][:-4], 'zip', 'dist', 'TicketAnalyzer')" "%ZIP%"
if not exist "%ZIP%" ( echo [!] Khong nen duoc zip. & exit /b 1 )
echo.
echo Xong: %ZIP%
echo Gui file zip nay cho nguoi dung: giai nen, chay TicketAnalyzer\TicketAnalyzer.exe
goto :eof

:findpy
for %%C in ("py -3" "python" "python3") do (
  %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PY=%%~C"
    goto :eof
  )
)
goto :eof
