@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist %PY% set PY=python
echo ==== %date% %time% ====>> logs\run.log
%PY% collect.py >> logs\run.log 2>&1 || exit /b 1
%PY% build.py >> logs\run.log 2>&1 || exit /b 1
%PY% verify.py >> logs\run.log 2>&1
