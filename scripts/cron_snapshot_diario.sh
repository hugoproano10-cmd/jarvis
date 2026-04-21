#!/bin/bash
# Cron wrapper para el snapshot diario del portafolio en jarvis_history.db.
# Se ejecuta todos los días a las 9:00 PM (tras el reporte cripto de 8 PM).

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_snapshot_diario.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/scripts/snapshot_diario.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
