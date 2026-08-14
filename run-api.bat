@echo off
title OpenCohost API
setlocal enabledelayedexpansion

REM ============================================================
REM  OpenCohost - levanta el backend FastAPI (control de Kira).
REM  Doble-click, o cre un acceso directo en el Escritorio.
REM  Ctrl+C para parar.
REM ============================================================

set "PYTHONPATH=%~dp0"
if "%PYTHONPATH:~-1%"=="\" set "PYTHONPATH=%PYTHONPATH:~0,-1%"
set "PORT=8765"

REM Interprete: OPENCOHOST_PY tiene prioridad (para conda/venv especificos).
REM Si no esta seteada, busco "python" en el PATH.
if defined OPENCOHOST_PY (
  set "PY=%OPENCOHOST_PY%"
) else (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
)

REM 8765 tambien lo usa WhisperLive STT (WS_URI). Si esta ocupado, uso 8770.
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul 2>&1
if !errorlevel! equ 0 (
  echo [!] Puerto 8765 ocupado ^(WhisperLive STT?^) -^> uso 8770.
  set "PORT=8770"
)

if not defined PY (
  echo [X] No encuentro un interprete de Python.
  echo     No hay "python" en el PATH, y OPENCOHOST_PY no esta seteada.
  echo     Seteala con la ruta a tu interprete, por ejemplo:
  echo       set OPENCOHOST_PY=ruta\a\tu\entorno\python.exe
  echo.
  pause
  exit /b 1
)

if not exist "%PY%" (
  echo [X] No encuentro el interprete de Python:
  echo     %PY%
  echo     Revisa la variable de entorno OPENCOHOST_PY, o instala Python
  echo     y agregalo al PATH.
  echo.
  pause
  exit /b 1
)

echo.
echo   OpenCohost API  -^>  http://127.0.0.1:!PORT!
echo   En el front:     VITE_API_BASE_URL=http://127.0.0.1:!PORT!
echo   (loopback only - no exponer a la red)
echo.

"%PY%" -m uvicorn opencohost.api.main:app --host 127.0.0.1 --port !PORT! --workers 1

echo.
echo (servidor detenido)
pause
