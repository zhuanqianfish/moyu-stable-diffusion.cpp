@echo off
REM ===========================================================================
REM  Convert Ultralytics YOLOv8 detectors for sd.cpp ADetailer
REM  sd.cpp can NOT read .pt directly (unsupported torch pickle opcode),
REM  the checkpoint must be converted to .safetensors first.
REM
REM  This script is safe to re-run: it skips models already converted.
REM ===========================================================================
setlocal
cd /d "%~dp0.."

set SRC=sd-src\scripts\convert_yolov8_to_safetensors.py
set INDIR=models\adetailer
set VENV=.cache\convert-env
set PY=%VENV%\Scripts\python.exe

if not exist "%SRC%" (
  echo [ERROR] converter not found: %SRC%
  pause & exit /b 1
)
if not exist "%INDIR%" (
  echo [ERROR] detector dir not found: %INDIR%
  pause & exit /b 1
)

echo.
echo === [1/3] Preparing python environment ===
if not exist "%PY%" (
  echo Creating venv at %VENV% ...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] failed to create venv. Is python on PATH?
    pause & exit /b 1
  )
)

"%PY%" -c "import torch, ultralytics, safetensors" 2>nul
if errorlevel 1 (
  echo Installing torch ^(CPU^) + ultralytics ...
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install --index-url https://download.pytorch.org/whl/cpu torch
  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple safetensors ultralytics
  if errorlevel 1 (
    echo [ERROR] dependency install failed.
    pause & exit /b 1
  )
)

echo.
echo === [2/3] Converting detectors ===
set COUNT=0
for %%F in ("%INDIR%\*.pt") do call :convert "%%~fF" "%%~nF"
goto done

:convert
set "IN=%~1"
set "NAME=%~2"
set "OUT=%INDIR%\%NAME%.safetensors"
if exist "%OUT%" (
  echo   [skip] %NAME%.safetensors already exists
  goto :eof
)
echo   [conv] %NAME%.pt
"%PY%" "%SRC%" "%IN%" "%OUT%" 2>&1 | findstr /V /C:"Downloading" /C:"it/s"
if errorlevel 1 (
  echo   [FAIL] %NAME%  ^(only YOLOv8 *detection* models are supported; -seg / worldv2 are not^)
  if exist "%OUT%" del /q "%OUT%"
) else (
  set /a COUNT+=1
)
goto :eof

:done
echo.
echo === [3/3] Done ===
echo Converted detectors are in: %INDIR%
echo Open the web UI -^> Model panel -^> ADetailer -^> pick a detector.
echo.
dir /b "%INDIR%\*.safetensors" 2>nul
pause
endlocal
