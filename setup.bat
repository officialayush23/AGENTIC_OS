@echo off
setlocal
cd /d "%~dp0"
echo === Agentic OS setup ===

where python >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ & exit /b 1)
where npm >nul 2>nul || (echo Node.js 20+ is required: https://nodejs.org/ & exit /b 1)

if not exist backend\.venv (
  echo Creating backend\.venv ...
  python -m venv backend\.venv || exit /b 1
)
call backend\.venv\Scripts\python.exe -m pip install --upgrade pip >nul
call backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt -r backend\requirements-dev.txt || exit /b 1

pushd frontend
call npm install --no-audit --no-fund || (popd & exit /b 1)
popd

where cargo >nul 2>nul || echo NOTE: Rust is not installed. Browser mode works now; for the desktop app install Rust from https://rustup.rs
echo.
echo Done. Next:  run.bat (browser)   or   dev-desktop.bat (desktop app)
