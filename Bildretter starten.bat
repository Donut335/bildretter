@echo off
rem Startet den Bildretter. Beim ersten Start wird die Python-Umgebung eingerichtet.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Erster Start: richte Python-Umgebung ein, das dauert einen Moment ...
    python -m venv .venv || (echo Python wurde nicht gefunden. Bitte Python 3.10+ installieren. & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo Installation fehlgeschlagen. & pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" -m bildretter
