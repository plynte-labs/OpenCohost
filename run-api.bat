@echo off
title OpenCohost API
setlocal enabledelayedexpansion

REM ============================================================
REM  OpenCohost - levanta el backend FastAPI (control de Kira).
REM  Doble-click, o cre un acceso directo en el Escritorio.
REM  Ctrl+C para parar.
REM ============================================================

set "PYTHONPATH=E:\VoiceAI"
set "PY=E:\Miniconda\envs\flux_env\python.exe"
set "PORT=8765"

REM 8765 tambien lo usa WhisperLive STT (WS_URI). Si esta ocupado, uso 8770.
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul 2>&1
if !errorlevel! equ 0 (
  echo [!] Puerto 8765 ocupado ^(WhisperLive STT?^) -^> uso 8770.
  set "PORT=8770"
)

if not exist "%PY%" (
  echo [X] No encuentro el interprete de Python:
  echo     %PY%
  echo     Edita la linea  set "PY=..."  en este .bat con la ruta de tu env.
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
