@echo off
setlocal
cd /d "%~dp0"

if not defined PRINTOPS_PYTHON set "PRINTOPS_PYTHON=py"
if not defined PRINTOPS_PORT set "PRINTOPS_PORT=4173"
echo PrintOps is starting at http://localhost:%PRINTOPS_PORT%
"%PRINTOPS_PYTHON%" server.py
endlocal
