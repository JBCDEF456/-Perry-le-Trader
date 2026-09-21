@echo off
title Perry Le Trader - Superviseur VPS
cd /d "C:\Projet forex bot"

if not exist "logs" mkdir logs

tasklist /FI "IMAGENAME eq terminal64.exe" 2>NUL | find /I "terminal64.exe" >NUL
if errorlevel 1 (
    echo Lancement de MetaTrader 5...
    start "" "C:\Program Files\MetaTrader 5\terminal64.exe"
    timeout /t 20 /nobreak > nul
) else (
    echo MT5 deja en cours.
)

:loop
set LOGFILE=logs\perry_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log
echo [%date% %time%] Demarrage bot.py >> "%LOGFILE%"
python "bot.py" >> "%LOGFILE%" 2>&1

set exit_code=%errorlevel%
echo [%date% %time%] Arret code : %exit_code% >> "%LOGFILE%"

if %exit_code% EQU 99 (
    git pull >> "%LOGFILE%" 2>&1
    timeout /t 3 /nobreak > nul
    goto loop
)
if %exit_code% EQU 0 (
    echo Arret volontaire. >> "%LOGFILE%"
    goto end
)

echo CRASH code %exit_code%. Restart 30s... >> "%LOGFILE%"
timeout /t 30 /nobreak > nul
goto loop

:end
echo Superviseur arrete.