@echo off
setlocal

REM Validate the latest nightly ingestion and ML feature matrix before retraining.
REM Extra CLI args are forwarded to ml_sidecar.data_quality.
REM Example:
REM   run_ml_data_quality.cmd --listing-id <uuid> --since-hours 30

python -m ml_sidecar.data_quality %*
exit /b %ERRORLEVEL%
