@echo off
title Remote Job Search - Mobile Web App
echo ============================================
echo   Remote Job Search - Mobile Web App
echo ============================================
echo.
echo Installing/checking requirements...
pip install flask requests pdfminer.six python-docx -q
echo.
echo Starting server...
python app.py
pause
