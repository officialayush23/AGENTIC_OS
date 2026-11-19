@echo off
rem Hot-reload UI in the browser: backend on :8765 (dev origin allowed) + Vite on :5173 proxying /api.
setlocal
cd /d "%~dp0"
start "Agentic OS backend" cmd /k "cd backend && .venv\Scripts\python.exe -m agentic_os --dev --no-browser"
cd frontend
call npm run dev
