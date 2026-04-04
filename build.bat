@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  Unshackle GUI — Standalone Builder
echo ============================================================
echo.

if not exist "gui.py" (
    echo ERROR: Run this script from the unshackle-gui folder.
    pause & exit /b 1
)

:: ── Detect Python / uv ───────────────────────────────────────────────────────
where uv >nul 2>&1
if %errorlevel% equ 0 (
    echo [1/4] Using uv...
    set PYTHON=uv run python
    set PIP=uv pip
) else (
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo ERROR: Neither 'uv' nor 'python' found in PATH.
        pause & exit /b 1
    )
    echo [1/4] Using system Python...
    set PYTHON=python
    set PIP=python -m pip
)

:: ── Install dependencies ─────────────────────────────────────────────────────
echo.
echo [2/4] Installing dependencies...
%PIP% install --quiet --upgrade customtkinter pywinpty pyte pyinstaller
if %errorlevel% neq 0 ( echo ERROR: pip install failed. & pause & exit /b 1 )

:: ── Locate customtkinter data dir ────────────────────────────────────────────
echo.
echo [3/4] Locating customtkinter assets...
for /f "delims=" %%i in ('%PYTHON% -c "import customtkinter,os; print(os.path.dirname(customtkinter.__file__))"') do set CTK_PATH=%%i
if not defined CTK_PATH ( echo ERROR: customtkinter not found. & pause & exit /b 1 )
echo        !CTK_PATH!

:: ── PyInstaller ──────────────────────────────────────────────────────────────
echo.
echo [4/4] Building exe (this takes a minute)...
echo.

%PYTHON% -m PyInstaller ^
    --name "unshackle-gui" ^
    --onefile ^
    --windowed ^
    --add-data "!CTK_PATH!;customtkinter/" ^
    --collect-submodules customtkinter ^
    --hidden-import customtkinter ^
    --hidden-import pyte ^
    --hidden-import winpty ^
    --hidden-import PIL ^
    --hidden-import PIL._imagingtk ^
    --noconfirm ^
    gui.py

if %errorlevel% neq 0 ( echo. & echo ERROR: PyInstaller failed. & pause & exit /b 1 )

echo.
echo ============================================================
echo  DONE  →  dist\unshackle-gui.exe
echo ============================================================
echo.
echo  NOTE: unshackle itself must still be installed and in PATH.
echo        The .exe only bundles the GUI.
echo.
pause
