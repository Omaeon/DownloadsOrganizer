@echo off

:: Try py launcher first, then python
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
echo Install Python from https://python.org and make sure to tick "Add to PATH"
pause
exit /b 1

:found
echo Checking customtkinter...
%PYTHON% -c "import customtkinter" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing customtkinter...
    %PYTHON% -m pip install customtkinter
)
echo Starting File Organizer...
%PYTHON% "%~dp0file_organizer.py"
