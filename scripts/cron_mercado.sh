#!/bin/bash
# Cron wrapper para el monitor de mercado con alertas Telegram.
# Se ejecuta cada 30 min, L-V, 8:30-17:00 hora Ecuador.

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_alertas.log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/config/alertas.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
