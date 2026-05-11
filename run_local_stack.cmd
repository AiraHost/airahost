@echo off
setlocal

REM One-command local stack launcher for frontend, nightly worker, ML worker, and optional nightly scheduling.
REM Examples:
REM   run_local_stack.cmd
REM   run_local_stack.cmd -ForceNightly
REM   run_local_stack.cmd -ListingId <uuid> -ForceNightly
REM Default nightly target: Airbnb room 1305899249107196055.
REM Data quality runs automatically after a scheduled nightly report is ready.
REM Add -SkipDataQuality to disable it.

powershell.exe -ExecutionPolicy Bypass -File "%~dp0scripts\run-local-stack.ps1" %*
exit /b %ERRORLEVEL%
