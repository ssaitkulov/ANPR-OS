@echo off
chcp 65001 >nul
cd /d "%~dp0.."
setlocal

echo ============================================
echo  LOCAL CI - полная проверка перед релизом
echo ============================================
echo.

set "STEP=pip check"
python -m pip check
if errorlevel 1 goto :fail

set "STEP=согласованность зависимостей"
python scripts\check_lock.py
if errorlevel 1 goto :fail

set "STEP=документация"
python scripts\check_docs.py
if errorlevel 1 goto :fail

set "STEP=сверка JSON и SQLite"
python scripts\verify_state.py
if errorlevel 1 goto :fail

python -c "import pytest" >nul 2>nul
if errorlevel 1 goto :tests_unittest
set "STEP=тесты (pytest)"
python -m pytest -q
if errorlevel 1 goto :fail
goto :tests_done

:tests_unittest
set "STEP=тесты (unittest)"
python -m unittest discover
if errorlevel 1 goto :fail

:tests_done
set "STEP=тесты (совместимость со stdlib)"
python -m unittest tests.test_safe_mode tests.test_control_auth tests.test_paths
if errorlevel 1 goto :fail

set "STEP=приёмочная проверка"
python scripts\acceptance_check.py --skip-service
if errorlevel 1 goto :fail

echo.
echo [local-ci] ВСЁ ЗЕЛЁНОЕ - можно релизить
endlocal
exit /b 0

:fail
echo.
echo [local-ci] ПРОВАЛ: %STEP%
echo [local-ci] Исправьте ошибку и запустите снова.
endlocal
exit /b 1
