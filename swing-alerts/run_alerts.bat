@echo off
rem Swing alerts (alerts/scanner.py). Telegram alerts only, never places orders.
rem Schedule at 15:40 ET on weekdays: preview at 15:50, decision alerts at 15:55, exits by itself.
rem Usage: run_alerts.bat            normal run
rem        run_alerts.bat --dry-run  print instead of Telegram, separate state file
rem        run_alerts.bat --status   show paper positions and recent trades
cd /d "%~dp0"
python -m alerts.scanner %*
exit
