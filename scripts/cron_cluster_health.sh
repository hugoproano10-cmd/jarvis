#!/bin/bash
# Cron wrapper para el health check del cluster JARVIS.
# Se ejecuta cada 5 minutos, 24/7.

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_cluster_health.log"

mkdir -p "$PROYECTO/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/scripts/cluster_health_check.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
