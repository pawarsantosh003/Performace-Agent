@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHONPATH=%PROJECT_DIR%src"
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PY%" (
  set "PY_EXE=%BUNDLED_PY%"
) else (
  set "PY_EXE=python"
)

echo Starting Performance Testing AI Agent UI...
echo.
echo Open this URL in your browser:
echo http://127.0.0.1:8765
echo.
echo Keep this window open while using the UI.
echo Press Ctrl+C to stop the server.
echo.

"%PY_EXE%" -m perf_agent.web --host 127.0.0.1 --port 8765

endlocal

