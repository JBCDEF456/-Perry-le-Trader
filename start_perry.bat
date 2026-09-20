@echo off
timeout /t 15
cd /d "C:\Projet forex bot"
start "" "C:\Program Files\MetaTrader 5\terminal64.exe"
timeout /t 20
python bot.py