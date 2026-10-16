@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PIP_DISABLE_PIP_VERSION_CHECK=1

if exist ".venv\Scripts\python.exe" goto deps

call :findpy
if not defined PY goto nopy
echo [Ticket Analyzer] Tao moi truong Python (.venv) lan dau...
%PY% -m venv .venv
if errorlevel 1 goto fail

:deps
echo [Ticket Analyzer] Kiem tra thu vien Python...
set "VPY=.venv\Scripts\python.exe"
set "QUICK=--retries 1 --timeout 20"
%VPY% -m pip install -q %QUICK% -r requirements.txt >nul 2>&1 && goto run

rem Corporate proxies re-sign TLS. A fresh venv ships an old pip that ignores the
rem Windows certificate store, so upgrade it with the base Python's pip first.
if not defined PY call :findpy
if defined PY (
  echo  - Nang cap pip trong .venv bang pip cua Python goc...
  %PY% -m pip --python "%VPY%" install -q %QUICK% --upgrade pip >nul 2>&1
  %VPY% -m pip install -q %QUICK% -r requirements.txt && goto run
)
echo  - Thu lai voi --trusted-host (mang chan SSL)...
%VPY% -m pip install -q %QUICK% --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt && goto run
echo [!] Chua cai duoc thu vien. Mo "Cai dat moi truong" trong giao dien de thu lai.

:run
%VPY% -m app
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

:nopy
echo.
echo [!] Khong tim thay Python 3.10+.
echo     Cai Python tu https://www.python.org/downloads/ (tick "Add python.exe to PATH") roi chay lai start.bat.
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo [!] Khong tao duoc moi truong Python.
pause
exit /b 1
