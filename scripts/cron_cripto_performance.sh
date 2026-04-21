#!/bin/bash
# Cron wrapper para el reporte diario de performance del bot cripto.
# Se ejecuta todos los días a las 8:00 PM (mercado cripto 24/7).

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_cripto_performance.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/agentes/jarvis_cripto_performance.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
