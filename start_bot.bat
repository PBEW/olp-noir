@echo off
title OLP-Noir Bot
cd /d "%~dp0"

echo ==========================================
echo   OLP-Noir Bot - starting...
echo.
echo   Keep this window OPEN while the bot runs.
echo   Press Ctrl+C to stop the bot.
echo ==========================================
echo.

".venv\Scripts\python.exe" bot.py

echo.
echo ==========================================
echo   Bot stopped. Read any error above.
echo ==========================================
pause
