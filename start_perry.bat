@echo off
title Perry Le Trader - Superviseur VPS
cd /d "C:\Projet forex bot"

echo ================================================
echo   Lancement de l'ecosysteme Perry Le Trader
echo ================================================

:: Lancement initial de MetaTrader 5
start "" "C:\Program Files\MetaTrader 5\terminal64.exe"
echo Attente du chargement de MetaTrader 5 (20 secondes)...
timeout /t 20 /nobreak > nul

:loop
echo [%date% %time%] Demarrage du script Python de Perry...
python "bot.py"

set exit_code=%errorlevel%
echo [%date% %time%] Perry s'est arrete avec le code : %exit_code%

:: Code 99 (Commande /pull via Telegram) -> Git Pull + Relance automatique
if %exit_code% EQU 99 (
    echo.
    echo [%date% %time%] --- MISE A JOUR GIT EN COURS ---
    git pull
    echo [%date% %time%] --- MISE A JOUR TERMINEE ---
    echo Redemarrage de Perry dans 3 secondes...
    timeout /t 3 /nobreak > nul
    goto loop
)

:: Code 0 (Commande /stop via Telegram) -> On coupe la boucle définitivement
if %exit_code% EQU 0 (
    echo [%date% %time%] Arret volontaire de Perry detecte (/stop). Fermeture du superviseur.
    goto end
)

:: En cas de crash ou d'erreur, on fige l'écran pour lire l'erreur exacte au lieu de boucler en silence
echo [%date% %time%] Une erreur est survenue (Code %exit_code%).
echo Regardez le message ci-dessus, puis appuyez sur une touche pour relancer...
pause
goto loop

:end
echo Superviseur arrete avec succes.
pause