@echo off
rem Builds the Windows installer: PyInstaller sidecar + Tauri bundle (NSIS .exe and .msi).
rem Needs Rust (rustup, MSVC build tools) and roughly 5 GB of free disk for the Rust build cache.
setlocal
cd /d "%~dp0"

for /f "tokens=2" %%t in ('rustc -vV ^| findstr host') do set TRIPLE=%%t
if "%TRIPLE%"=="" (echo Rust is required: https://rustup.rs & exit /b 1)

echo === 1/2 Packaging the backend (%TRIPLE%) ===
pushd backend
call .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --console ^
  --name agentic-os-backend --paths . ^
  --collect-submodules agentic_os --collect-submodules uvicorn ^
  --collect-submodules keyring --copy-metadata keyring --hidden-import win32ctypes.core ^
  --exclude-module pkg_resources --exclude-module setuptools ^
  --collect-submodules openai --collect-submodules anthropic --collect-submodules google.genai ^
  run_backend.py || (popd & exit /b 1)
popd
if not exist frontend\src-tauri\binaries mkdir frontend\src-tauri\binaries
copy /y backend\dist\agentic-os-backend.exe frontend\src-tauri\binaries\agentic-os-backend-%TRIPLE%.exe >nul

echo === 2/2 Building the desktop app ===
pushd frontend
call npm run tauri build -- --config src-tauri/tauri.bundle.conf.json || (popd & exit /b 1)
popd
echo.
echo Installers: frontend\src-tauri\target\release\bundle\
