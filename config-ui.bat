@echo off
chcp 65001 >nul 2>&1
title llama.cpp config UI
cd /d "%~dp0"

:: llama.cpp 설정 UI - 창 없이 서버를 띄우고 브라우저로 연다 (이미 떠 있으면 브라우저만)
:: 주소 http://127.0.0.1:8092  /  로그 config-ui.log  /  설계 docs\config-ui-design.md

where pythonw >nul 2>&1
if errorlevel 1 goto :NO_PYTHONW
start "" pythonw "%~dp0config-ui\server.py" --open
exit /b 0

:NO_PYTHONW
echo [WARN] pythonw not found - running with python in this window
python "%~dp0config-ui\server.py" --open
pause
exit /b 1
