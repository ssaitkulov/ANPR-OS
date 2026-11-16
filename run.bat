@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import requests, fastapi, uvicorn, tzdata, tkinter, PIL" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю необходимые компоненты...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Установка не удалась. Проверьте интернет и установлен ли Python.
    pause
    exit /b 1
  )
)
start "" pythonw.exe "%~dp0control_panel.py"
