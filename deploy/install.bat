@echo off
chcp 65001 >nul
cd /d "%~dp0.."
setlocal

echo ============================================
echo  ANPR OS Fayz — установка на рабочий ПК
echo ============================================
echo.

python --version
python -c "import sys; assert sys.version_info[:2] == (3, 14), 'Нужен Python 3.14'"
if errorlevel 1 goto :fail

if not exist "deploy\wheelhouse" (
  echo [install] Нет папки deploy\wheelhouse.
  echo [install] Запустите scripts\make_wheelhouse.py на исходном ПК.
  goto :fail
)

echo [install] Установка из колёс без интернета...
python -m pip install --no-index --find-links=deploy\wheelhouse -r requirements-lock.txt
if errorlevel 1 goto :fail

python -m pip check
if errorlevel 1 goto :fail

python -c "import requests, fastapi, uvicorn, PIL, customtkinter, pystray, cv2, tkinter, tzdata; print('[install] импорты OK')"
if errorlevel 1 goto :fail

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [install] Создан .env — запустите settings.bat и заполните данные.
)

echo.
echo [install] УСТАНОВКА ЗАВЕРШЕНА
echo [install] Далее: settings.bat, затем run.bat
endlocal
exit /b 0

:fail
echo.
echo [install] ОШИБКА УСТАНОВКИ
endlocal
exit /b 1
