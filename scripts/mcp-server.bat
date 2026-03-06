@echo off
REM -------------------------------------------------------------------
REM skcapstone MCP server launcher for Windows (cmd.exe)
REM Task: e5f81637
REM
REM Auto-detects the Python virtualenv and launches the MCP server
REM on stdio transport. Works with any MCP client that speaks stdio.
REM
REM Usage:
REM   scripts\mcp-server.bat
REM
REM Environment overrides:
REM   set SKCAPSTONE_VENV=C:\path\to\venv
REM   set SKMEMORY_HOME=C:\path\to\memory
REM -------------------------------------------------------------------

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Navigate up one level to get skcapstone project dir
for %%I in ("%SCRIPT_DIR%\..") do set "SKCAPSTONE_DIR=%%~fI"

set "PYTHON="

REM --- 1. Explicit SKCAPSTONE_VENV override ---
if defined SKCAPSTONE_VENV (
    if exist "%SKCAPSTONE_VENV%\Scripts\python.exe" (
        set "PYTHON=%SKCAPSTONE_VENV%\Scripts\python.exe"
        goto :found
    )
    if exist "%SKCAPSTONE_VENV%\bin\python" (
        set "PYTHON=%SKCAPSTONE_VENV%\bin\python"
        goto :found
    )
    echo WARNING: SKCAPSTONE_VENV=%SKCAPSTONE_VENV% set but python not found, falling back. >&2
)

REM --- 2. Standard skenv on Windows (%LOCALAPPDATA%\skenv) ---
if exist "%LOCALAPPDATA%\skenv\Scripts\python.exe" (
    "%LOCALAPPDATA%\skenv\Scripts\python.exe" -c "import skcapstone" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=%LOCALAPPDATA%\skenv\Scripts\python.exe"
        goto :found
    )
)

REM --- 3. Standard skenv Unix-style (%USERPROFILE%\.skenv) ---
if exist "%USERPROFILE%\.skenv\Scripts\python.exe" (
    "%USERPROFILE%\.skenv\Scripts\python.exe" -c "import skcapstone" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=%USERPROFILE%\.skenv\Scripts\python.exe"
        goto :found
    )
)

REM --- 4. Project-local .venv ---
if exist "%SKCAPSTONE_DIR%\.venv\Scripts\python.exe" (
    "%SKCAPSTONE_DIR%\.venv\Scripts\python.exe" -c "import skcapstone" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=%SKCAPSTONE_DIR%\.venv\Scripts\python.exe"
        goto :found
    )
)

REM --- 5. System Python ---
where python >nul 2>&1
if %errorlevel% equ 0 (
    python -c "import skcapstone" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=python"
        goto :found
    )
)

where python3 >nul 2>&1
if %errorlevel% equ 0 (
    python3 -c "import skcapstone" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=python3"
        goto :found
    )
)

REM --- Not found ---
echo ERROR: Could not find a Python interpreter with skcapstone installed. >&2
echo. >&2
echo Install with: >&2
echo   scripts\install.bat >&2
echo. >&2
echo Or point to an existing venv: >&2
echo   set SKCAPSTONE_VENV=C:\path\to\venv >&2
echo   scripts\mcp-server.bat >&2
exit /b 1

:found

REM --- Set environment variables ---
if not defined SKMEMORY_HOME set "SKMEMORY_HOME=%USERPROFILE%\.skcapstone\memory"
if not defined SKCAPSTONE_HOME set "SKCAPSTONE_HOME=%USERPROFILE%\.skcapstone"

REM Ensure skcapstone is importable
if defined PYTHONPATH (
    set "PYTHONPATH=%SKCAPSTONE_DIR%\src;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%SKCAPSTONE_DIR%\src"
)

REM --- Launch MCP server on stdio ---
%PYTHON% -m skcapstone.mcp_server %*
exit /b %errorlevel%
