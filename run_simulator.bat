@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" tiger_etf_simulator.py) else (python tiger_etf_simulator.py)
