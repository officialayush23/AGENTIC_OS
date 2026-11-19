@echo off
rem ============================================================================
rem  Agentic OS - one command: install, test, build, run, open.
rem
rem    start.bat                 install (first time), run all tests, open the app
rem    start.bat --skip-tests    same, without the tests (faster on later runs)
rem    start.bat --desktop       open the Tauri desktop app instead (needs Rust)
rem
rem  Safe to run again: steps that are already done are skipped.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Agentic OS

set SKIP_TESTS=0
set DESKTOP=0
for %%A in (%*) do (
  if /i "%%~A"=="--skip-tests" set SKIP_TESTS=1
  if /i "%%~A"=="--desktop" set DESKTOP=1
)

echo.
echo  [1/5] Checking prerequisites
where python >nul 2>nul || (echo     Python 3.10+ is missing. Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^). & goto :fail)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || (echo     Python 3.10 or newer is required. & goto :fail)
where npm >nul 2>nul || (echo     Node.js 20+ is missing. Install it from https://nodejs.org/ & goto :fail)
if "%DESKTOP%"=="1" (
  where cargo >nul 2>nul || (echo     The desktop app needs Rust: https://rustup.rs  ^(or run without --desktop^) & goto :fail)
)
echo     ok

echo  [2/5] Installing the backend (Python)
if not exist backend\.venv\Scripts\python.exe (
  python -m venv backend\.venv || goto :fail
)
set PY=%~dp0backend\.venv\Scripts\python.exe
"%PY%" -m pip install --disable-pip-version-check -q --upgrade pip >nul
"%PY%" -m pip install --disable-pip-version-check -q -r backend\requirements.txt -r backend\requirements-dev.txt || goto :fail
echo     ok

echo  [3/5] Installing the frontend (npm)
pushd frontend
rem Always run npm install: it is quick when nothing changed, and it picks up new packages
call npm install --no-audit --no-fund --loglevel=error || (popd & goto :fail)
popd
echo     ok

if "%SKIP_TESTS%"=="1" (
  echo  [4/5] Tests skipped
) else (
  echo  [4/5] Running tests
  pushd backend
  "%PY%" -m pytest tests -q --timeout 120 -p no:cacheprovider || (popd & echo     Backend tests failed - see above. & goto :fail)
  popd
  pushd frontend
  call npm run --silent typecheck || (popd & echo     Frontend typecheck failed. & goto :fail)
  call npx vitest run --reporter=dot || (popd & echo     Frontend tests failed. & goto :fail)
  popd
  echo     all tests passed
)

if "%DESKTOP%"=="1" (
  echo  [5/5] Starting the desktop app ^(first build takes a few minutes^)
  pushd frontend
  call npm run tauri dev
  popd
  goto :eof
)

echo  [5/5] Building the UI and starting Agentic OS
pushd frontend
call npm run --silent build >nul || (popd & echo     UI build failed. & goto :fail)
popd
set AGENTIC_OS_UI_DIST=%~dp0frontend\dist
echo.
echo  Your browser will open and pair automatically.
echo  Keep this window open while you use the app. Press Ctrl+C to stop.
echo.
pushd backend
"%PY%" -m agentic_os
popd
goto :eof

:fail
echo.
echo  Setup stopped. Fix the message above and run start.bat again.
pause
exit /b 1
