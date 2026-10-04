@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" self_test.py
) else (
  py -3 self_test.py
)
pause
