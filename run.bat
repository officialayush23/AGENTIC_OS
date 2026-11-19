@echo off
rem Browser mode: builds the UI once, then the backend serves it and prints a pairing code.
setlocal
cd /d "%~dp0"
if not exist frontend\dist\index.html (
  pushd frontend & call npm run build || (popd & exit /b 1) & popd
)
set AGENTIC_OS_UI_DIST=%~dp0frontend\dist
cd backend
call .venv\Scripts\python.exe -m agentic_os %*
