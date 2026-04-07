#!/bin/bash
# Cron wrapper para JARVIS Cripto.
# Se ejecuta cada 15 minutos, 24/7, todos los días.

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_cripto.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/cripto/jarvis_cripto.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
