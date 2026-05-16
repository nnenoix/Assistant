@echo off
REM Launches Workspace Agent as a desktop window (uvicorn + pywebview).
REM Double-click this file, or pin a shortcut to it on your taskbar / desktop.
cd /d "%~dp0"
uv run python -m src.desktop %*
