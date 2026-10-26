@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install --quiet --disable-pip-version-check requests
%PY% vimeo_downloader.py
if errorlevel 1 pause
