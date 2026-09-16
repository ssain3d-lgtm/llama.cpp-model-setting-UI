@echo off
chcp 65001 >nul 2>&1
title llama.cpp WebUI launcher
cd /d "%~dp0"

:: 라우터가 이미 떠 있으면 브라우저만 열고, 아니면 라우터 창을 따로 띄운 뒤 준비되면 WebUI 를 연다.
:: 라우터 끄기: "llama.cpp router" 창에서 Ctrl+C

set "URL=http://127.0.0.1:8080/"
set "HEALTH=http://127.0.0.1:8080/health"
set /a TRIES=0

curl.exe -sf -m 2 "%HEALTH%" >nul 2>&1
if not errorlevel 1 goto :OPEN

echo [START] llama.cpp router - LM Studio sync + model watch + MCP gateway
start "llama.cpp router" "%~dp0start-router.bat"
echo [WAIT] waiting for router on port 8080 ...

:WAIT
ping -n 2 127.0.0.1 >nul
curl.exe -sf -m 2 "%HEALTH%" >nul 2>&1
if not errorlevel 1 goto :OPEN
set /a TRIES+=1
if %TRIES% GEQ 180 goto :TIMEOUT
goto :WAIT

:OPEN
echo [OPEN] %URL%
start "" "%URL%"
exit /b 0

:TIMEOUT
echo [ERROR] Router did not respond within 180 seconds.
echo         Check the "llama.cpp router" window for errors.
pause
exit /b 1
