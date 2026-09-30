@echo off
rem ============================================================
rem  stable-diffusion.cpp Web UI  -  Windows launcher
rem
rem  NOTE: messages here are intentionally ASCII-only, because
rem  cmd.exe renders non-ASCII batch files inconsistently across
rem  code pages. All the friendly (Chinese) output is produced by
rem  webui\server.py, which handles UTF-8 properly.
rem
rem  For a fully localized launcher use start.ps1 instead.
rem ============================================================
setlocal EnableExtensions
title stable-diffusion.cpp Web UI

pushd "%~dp0"
set "ROOT=%CD%"

echo.
echo ==============================================================
echo    stable-diffusion.cpp  -  Web Image Generation Service
echo --------------------------------------------------------------
echo    Project : %ROOT%
echo ==============================================================
echo.

rem ---------- 1. check the inference engine ----------
if not exist "%ROOT%\bin\sd-server.exe" goto NO_ENGINE

rem ---------- 2. model search paths come from model_path.json ----------
rem  (no local models\ check: models may live in any configured directory)
if exist "%ROOT%\model_path.json" (
    echo [INFO] Model paths : model_path.json
) else (
    echo [WARN] model_path.json not found, falling back to webui\config.json
)

rem ---------- 3. locate Python 3 ----------
set "PYEXE="
set "PYARG="

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARG=-3"
)

if not defined PYEXE (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    if exist "%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" (
        set "PYEXE=%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
    )
)

if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    )
)

if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    )
)

if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    )
)

if not defined PYEXE goto NO_PYTHON

echo [INFO] Python  : "%PYEXE%" %PYARG%
echo [INFO] Starting service, the browser will open automatically.
echo [INFO] Press Ctrl+C to stop.
echo.

rem ---------- 4. run the service ----------
"%PYEXE%" %PYARG% "%ROOT%\webui\server.py"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [ERROR] Service exited with code %RC%
    echo         Check the log: %ROOT%\logs\sd-server.log
) else (
    echo [INFO] Service stopped.
)

popd
pause
exit /b %RC%


rem ============================================================
:NO_ENGINE
echo [ERROR] Inference engine not found: bin\sd-server.exe
echo.
echo         Run the setup script first:
echo             python "%ROOT%\scripts\setup.py"
echo.
popd
pause
exit /b 1

:NO_PYTHON
echo [ERROR] Python 3 was not found on this system.
echo.
echo         Please install Python 3.10+ and tick
echo         "Add python.exe to PATH" during installation:
echo             https://www.python.org/downloads/
echo.
popd
pause
exit /b 1
