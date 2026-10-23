@echo off
rem Double-click to start the Expense Tracker. Close this window to stop it.
cd /d "%~dp0"
title Expense Tracker
py -m pip install --quiet --disable-pip-version-check --no-warn-script-location -r requirements.txt
py -m app
if errorlevel 1 pause
