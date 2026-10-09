@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem start.ps1: finds Python or downloads a portable one into python\
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
