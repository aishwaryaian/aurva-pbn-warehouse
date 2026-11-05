@echo off
cd /d "%~dp0"
title Aurva World - PBN Warehouse
where py >nul 2>nul
if errorlevel 1 goto USE_PYTHON
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8765"
py -3 app.py
goto END
:USE_PYTHON
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8765"
python app.py
goto END
:NO_PYTHON
echo Python is not installed or is not on PATH.
echo Install Python from https://www.python.org/downloads/
:END
echo.
echo The warehouse has stopped. Press any key to close.
pause >nul
