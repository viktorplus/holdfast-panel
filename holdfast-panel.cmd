@echo off
rem Starts Holdfast Panel. The first run makes a .venv next to this file and
rem installs the panel into it; later runs only start it.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto :run

echo First run: setting up the environment, this takes a minute...
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -m venv .venv || goto :failed
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :failed

:run
rem pythonw has no console window; start returns at once.
start "" ".venv\Scripts\pythonw.exe" -m holdfast_panel
exit /b 0

:failed
echo.
echo Setup failed. Python 3.11.4 or newer is needed: https://www.python.org/downloads/
pause
exit /b 1
