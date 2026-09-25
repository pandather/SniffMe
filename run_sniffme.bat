@echo off
rem SniffMe live: requires bridge.py already running in another window (run.bat).
cd /d "%~dp0"
"C:\Users\Mark\AppData\Local\Programs\Python\Python313\python.exe" sniffme.py %*
