@echo off
setlocal

REM Long-running ML queue worker. Run this on the backend/ML machine.
python -m ml_sidecar.worker %*
exit /b %ERRORLEVEL%
