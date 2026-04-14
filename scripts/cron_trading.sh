#!/bin/bash
# Cron wrapper para JARVIS Trading Agent.
# Se ejecuta cada 30 min, L-V, 8:30-16:00 hora Ecuador (UTC-5).
# Analiza mercado via JARVIS + ejecuta órdenes en IBKR (cuenta real, TWS en 192.168.202.37:7496).

PROYECTO="/home/hproano/asistente"
PYTHON="/home/hproano/asistente_env/bin/python"
LOG_CRON="$PROYECTO/logs/cron_trading.log"
LOCKFILE="/tmp/jarvis_trading.lock"

mkdir -p "$PROYECTO/logs"

# Evitar ejecución simultánea
if [ -f "$LOCKFILE" ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') SKIP: trading ya en progreso ===" >> "$LOG_CRON"
    exit 0
fi

trap "rm -f $LOCKFILE" EXIT
touch "$LOCKFILE"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_CRON"
$PYTHON "$PROYECTO/agentes/jarvis_trading.py" >> "$LOG_CRON" 2>&1
echo "" >> "$LOG_CRON"
