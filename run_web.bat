@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat 2>nul
echo Waffles Priv Service - Web App starting...
echo Open http://localhost:5000 in your browser
python web_app.py
pause
