@echo off
cd /d "%~dp0"
python -m pdf_branding.gui
if errorlevel 1 pause
