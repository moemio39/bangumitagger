@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 scraper.py gui
goto finished
:usepython
python scraper.py gui
:finished
if errorlevel 1 pause
