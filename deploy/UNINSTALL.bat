@echo off
chcp 65001 >nul
cd /d "%~dp0.."
setlocal

echo ============================================
echo  ANPR OS Fayz — удаление
echo ============================================
echo.
echo ВНИМАНИЕ: проезды, состояние и токены лежат в папке data/.
echo Если они нужны — скопируйте data/ в безопасное место ДО удаления.
echo.
set /p ANSWER=Продолжить? (д/н): 
if /i not "%ANSWER%"=="д" goto :cancel

echo.
echo [1/3] Останавливаю сервис...
python -c "from app.services.control import stop; stop()"
if errorlevel 1 echo [внимание] не удалось остановить сервис — проверьте вручную

echo [2/3] Удаляю автозапуск из реестра...
python -c "from app.core.preferences import set_autostart; set_autostart(False)"
if errorlevel 1 echo [внимание] не удалось удалить автозапуск — проверьте вручную

echo [3/3] Готово.
echo.
echo Сервис остановлен, запись автозапуска удалена.
echo Закройте окно панели «Эврика», затем удалите папку проекта вручную:
echo   %CD%
echo.
echo Пакеты Python остались в системе. Если Python используется ТОЛЬКО
echo для ANPR OS, их можно убрать командой:
echo   python -m pip uninstall -y -r requirements.txt
echo (внимание: requests/Pillow/uvicorn могут использоваться другими программами)
endlocal
exit /b 0

:cancel
echo Удаление отменено, ничего не изменено.
endlocal
exit /b 0
