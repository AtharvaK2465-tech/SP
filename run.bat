@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1 && set PYTHON=py || set PYTHON=python
%PYTHON% -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py %1
