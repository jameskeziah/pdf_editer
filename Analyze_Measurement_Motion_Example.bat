@echo off
cd /d "%~dp0"
echo Edit the two paths below if your folders are elsewhere.
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output" --class-filter 6 --subject-filter physics --chapter-filter "Measurement and Motion" --analyze-only --preview-plans
pause
