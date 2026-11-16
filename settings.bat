@echo off
cd /d "%~dp0"
python setup_anpr.py
if errorlevel 1 pause
