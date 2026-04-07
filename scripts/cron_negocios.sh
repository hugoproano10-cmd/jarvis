#!/bin/bash
# Cron wrapper para JARVIS Negocios — investigación de oportunidades.
# Se ejecuta cada noche a las 11PM.

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_negocios.log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/agentes/jarvis_negocios.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
