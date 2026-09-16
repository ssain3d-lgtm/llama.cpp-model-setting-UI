@echo off
chcp 65001 >nul 2>&1
title llama.cpp router (LM Studio models + MCP)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-router.ps1" %*
pause
