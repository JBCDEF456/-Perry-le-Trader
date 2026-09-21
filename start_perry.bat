@echo off
title Perry Le Trader - Superviseur VPS
cd /d "C:\Projet forex bot"

echo ================================================
echo   Lancement de l'ecosysteme Perry Le Trader
echo ================================================

:: Lancement initial de MetaTrader 5
start "" "C:\Program Files\MetaTrader 5\terminal64.exe"
timeout /t 20 /nobreak > nul

:loop
echo [%date% %time%] Demarrage du script Python de Perry...
python "bot_2.py"

set exit_code=%errorlevel%
echo [%date% %time%] Perry s'est arrete avec le code : %exit_code%

:: Code 99 (Commande /pull via Telegram) -> Git Pull + Relance automatique
if %exit_code% EQU 99 (
    echo.
    echo [%date% %time%] --- MISE A JOUR GIT EN COURS ---
    git pull
    echo [%date% %time%] --- MISE A JOUR TERMINEE ---
    echo Redemarrage de Perry avec la nouvelle version dans 3 secondes...
    timeout /t 3 /nobreak > nul
    goto loop
)

:: Code 0 (Commande /stop via Telegram) -> On coupe la boucle définitivement
if %exit_code% EQU 0 (
    echo [%date% %time%] Arret volontaire de Perry detecte (/stop). Fermeture du superviseur.
    goto end
)

:: Crash ou coupure réseau inattendue -> Relance automatique de sécurité après 10 sec
echo [%date% %time%] Crash ou erreur detectee (Code %exit_code%). Relance automatique dans 10 secondes...
timeout /t 15 /nobreak > nul
goto loop

:end
echo Superviseur arrete avec succes.
pause