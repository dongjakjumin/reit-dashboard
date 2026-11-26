@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist %PY% set PY=python
echo ==== %date% %time% ====>> logs\run.log
rem 12차 검토: 오늘 스냅숏이 이미 있으면(GitHub Actions가 수집한 날) 원문에 다시 묻지 않는다. 다시 받으려면 --force
if /i not "%~1"=="--force" (
  %PY% -c "import datetime,pathlib,sys;sys.exit(1 if pathlib.Path('data/snapshots/'+datetime.date.today().isoformat()+'.json').exists() else 0)"
  if errorlevel 1 (
    echo 오늘 스냅숏이 이미 있어 수집을 건너뜀>> logs\run.log
    exit /b 0
  )
)
%PY% collect.py >> logs\run.log 2>&1 || exit /b 1
%PY% build.py >> logs\run.log 2>&1 || exit /b 1
%PY% verify.py >> logs\run.log 2>&1
