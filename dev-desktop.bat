@echo off
rem Desktop app in development: Vite dev server + Tauri window; the shell starts the Python backend
rem from backend\.venv automatically and passes it a fresh token over stdin.
setlocal
cd /d "%~dp0frontend"
call npm run tauri dev
