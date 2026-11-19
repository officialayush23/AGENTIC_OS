@echo off
setlocal
cd /d "%~dp0"
echo === Backend tests ===
pushd backend
call .venv\Scripts\python.exe -m pytest tests -q --timeout 120 || (popd & exit /b 1)
popd
echo === Frontend typecheck + tests ===
pushd frontend
call npm run typecheck || (popd & exit /b 1)
call npm test || (popd & exit /b 1)
popd
echo All tests passed.
