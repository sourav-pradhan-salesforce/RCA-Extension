@echo off
:: ─────────────────────────────────────────────────────────
::  RCA Analysis — Windows Installer
::  Double-click this file to install everything
:: ─────────────────────────────────────────────────────────
title RCA Analysis Installer

echo.
echo ====================================================
echo   RCA Analysis -- Salesforce Support Intelligence
echo   Windows Installer
echo ====================================================
echo.

:: Check PowerShell is available
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: PowerShell not found. Please install PowerShell 5.1+
    pause
    exit /b 1
)

:: Allow execution of the installer script (bypass policy for this session only)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1"

if %errorlevel% neq 0 (
    echo.
    echo Installer exited with errors. Check output above.
    pause
)
