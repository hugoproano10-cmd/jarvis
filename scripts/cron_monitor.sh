#!/bin/bash
# Cron wrapper para el monitor diario de sistema JARVIS.
# Se ejecuta diariamente a las 9:00 AM ECT.

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_monitor.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/scripts/monitor_sistema.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
