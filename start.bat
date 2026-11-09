@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Оперативный журнал

if exist ".venv\Scripts\python.exe" goto install

set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto nopython

echo Создаю окружение Python...
%PY% -m venv .venv
if errorlevel 1 goto nopython

:install
if exist ".venv\installed.ok" goto run
echo Устанавливаю компоненты. Это нужно один раз и займёт несколько минут...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail
echo ok> ".venv\installed.ok"

:run
".venv\Scripts\python.exe" run.py
pause
goto :eof

:nopython
echo.
echo Не найден Python. Установите Python 3.12 с сайта python.org,
echo при установке отметьте "Add python.exe to PATH", и запустите start.bat снова.
pause
goto :eof

:pipfail
echo.
echo Не удалось установить компоненты. Проверьте интернет и запустите start.bat снова.
pause
