@echo off
rem Double-click: asks for a username and how many posts, then downloads them.
rem Uses the venv next to this file; creates it on the first run if missing.
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating venv and installing requirements...
    python -m venv venv || goto :fail
    venv\Scripts\python.exe -m pip install -r requirements.txt || goto :fail
)

venv\Scripts\python.exe fb_post.py %*
echo.
pause
exit /b

:fail
echo.
echo Setup failed. Make sure Python is installed and on PATH.
pause
