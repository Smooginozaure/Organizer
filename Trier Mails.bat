@echo off
chcp 65001 >nul
title Trier Mails
if exist "%~dp0Programme\mailarchive\" (cd /d "%~dp0Programme") else (cd /d "%~dp0")
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%PY%" ("%PY%" -m mailarchive menu) else (py -3 -m mailarchive menu)
if errorlevel 1 pause
