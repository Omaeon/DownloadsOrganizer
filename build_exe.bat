@echo off

:: Detect python
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set PYTHON=py
    goto found
)
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set PYTHON=python
    goto found
)
echo ERROR: Python not found.
pause
exit /b 1

:found
echo [1/3] Installing dependencies...
%PYTHON% -m pip install customtkinter pyinstaller

echo [2/3] Building EXE (this may take 1-2 minutes)...
%PYTHON% -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "FileOrganizer" ^
    --collect-all customtkinter ^
    "%~dp0file_organizer.py"

echo [3/3] Done!
echo.
echo Your EXE is here:
echo %~dp0dist\FileOrganizer.exe
echo.
pause
