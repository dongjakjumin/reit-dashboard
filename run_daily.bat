@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist %PY% set PY=python
echo ==== %date% %time% ====>> logs\run.log
rem 12차 검토: 오늘 스냅숏이 이미 있으면(GitHub Actions가 수집한 날) 원문에 다시 묻지 않는다. 다시 받으려면 --force
rem 15차 검토: 괄호 블록 안의 exit는 cmd /c 호출에서 종료 코드를 잃고, errorlevel 1은 파이썬 없음(9009)도 잡으므로 goto로 나눈다
if /i "%~1"=="--force" goto collect
%PY% -c "import datetime,pathlib,sys;sys.exit(3 if pathlib.Path('data/snapshots/'+datetime.date.today().isoformat()+'.json').exists() else 0)"
if errorlevel 4 exit /b 1
if errorlevel 3 goto skip
if errorlevel 1 exit /b 1
:collect
%PY% collect.py >> logs\run.log 2>&1 || exit /b 1
%PY% build.py >> logs\run.log 2>&1 || exit /b 1
%PY% verify.py >> logs\run.log 2>&1
exit /b %errorlevel%
:skip
echo 오늘 스냅숏이 이미 있어 수집을 건너뜀>> logs\run.log
exit /b 0
