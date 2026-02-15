@echo off
title Ledger Dashboard
echo Starting Ledger Dashboard...
start http://127.0.0.1:5000
python -m scripts.dashboard
