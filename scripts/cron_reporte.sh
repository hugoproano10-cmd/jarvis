#!/bin/bash
# Cron wrapper para el reporte diario matutino de JARVIS.
# Se ejecuta L-V a las 7:30 AM hora Ecuador (UTC-5).

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_reporte.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/trading/reporte_diario.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
