@echo off
setlocal
set "SCRIPT=%~dp0..\mcp\wb_bridge.py"
set "PY=python"
if not "%WORKBUDDY_PYTHON%"=="" set "PY=%WORKBUDDY_PYTHON%"
:run
"%PY%" "%SCRIPT%" ensure >nul 2>&1
exit /b 0
