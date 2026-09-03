@echo off
title WhatsApp Sidecar Setup
echo ============================================
echo   WhatsApp Sidecar - One-Click Setup
echo ============================================
echo.

:: Check Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Node.js is not installed or not in PATH.
    echo         Download from https://nodejs.org/ (version 22 or higher)
    pause
    exit /b 1
)

:: Show Node version
for /f "tokens=*" %%v in ('node -v') do echo [OK] Node.js %%v found

:: Navigate to sidecar directory
cd /d "%~dp0"
echo [..] Working directory: %cd%
echo.

:: Step 1: Install dependencies
echo [1/3] Installing dependencies...
call npm install
if %ERRORLEVEL% neq 0 (
    echo [ERROR] npm install failed. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.
echo.

:: Step 2: Apply patches (runs automatically via postinstall, but run again to be sure)
echo [2/3] Applying patches...
call npx patch-package
if %ERRORLEVEL% neq 0 (
    echo [WARNING] Patch may have failed. Continuing anyway...
)
echo [OK] Patches applied.
echo.

:: Step 3: Build TypeScript
echo [3/3] Building TypeScript...
call npm run build
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)
echo [OK] Build complete.
echo.

echo ============================================
echo   Setup Complete!
echo ============================================
echo.
echo To start the sidecar, run:
echo   npm start
echo.
echo Or start it from Odoo:
echo   WhatsApp ^> Accounts ^> Start Sidecar
echo.
pause
