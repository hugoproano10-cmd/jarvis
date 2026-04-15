#!/bin/bash
# Detecta cuando IB Gateway pide TOTP y lo ingresa automáticamente
# via xdotool en el display VNC del container Docker.

set -u

LOG="/home/hproano/asistente/logs/ibkr_totp.log"
CONTAINER="ibgateway-config-ib-gateway-1"
ENV_FILE="/home/hproano/asistente/.env"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
TOTP_SECRET="${IBKR_TOTP_SECRET:-}"
if [ -z "$TOTP_SECRET" ]; then
    log "ERROR: IBKR_TOTP_SECRET no definido en $ENV_FILE"
    exit 1
fi

NEEDS_TOTP=$(docker logs "$CONTAINER" --since=300s 2>/dev/null | grep -c "Second Factor Authentication initiated")
if [ "$NEEDS_TOTP" -eq 0 ]; then
    exit 0
fi

log "TOTP requerido - generando código (esperando ventana de >=15s de validez)..."

TOTP_CODE=$(/home/hproano/asistente_env/bin/python3 -c "
import pyotp, time
totp = pyotp.TOTP('$TOTP_SECRET')
remaining = 30 - int(time.time()) % 30
if remaining < 15:
    time.sleep(remaining + 1)
print(totp.now())
" 2>/dev/null)

if [ -z "$TOTP_CODE" ]; then
    log "ERROR: No se pudo generar código TOTP"
    exit 1
fi

log "Código generado - ingresando via xdotool..."

docker exec "$CONTAINER" which xdotool >/dev/null 2>&1 || \
    docker exec "$CONTAINER" apt-get install -y xdotool >/dev/null 2>&1

docker exec "$CONTAINER" bash -c "
    DISPLAY=:1 xdotool search --sync --name 'Second Factor' \
        windowactivate --sync windowfocus --sync 2>/dev/null || true
    sleep 1
    DISPLAY=:1 xdotool mousemove --sync 362 288 2>/dev/null
    DISPLAY=:1 xdotool click 1 2>/dev/null
    sleep 0.5
    DISPLAY=:1 xdotool key ctrl+a 2>/dev/null
    DISPLAY=:1 xdotool type --clearmodifiers --delay 100 '$TOTP_CODE' 2>/dev/null
    sleep 1
    DISPLAY=:1 xdotool key Return 2>/dev/null
" 2>/dev/null

log "Código ingresado - esperando 30s para verificar conexión..."

sleep 30
STATUS=$(cd /home/hproano/asistente && \
    /home/hproano/asistente_env/bin/python3 -c "
from agentes.ibkr_trading import get_balance
print(get_balance().get('status','UNKNOWN'))
" 2>/dev/null)

log "Estado post-TOTP: ${STATUS:-UNKNOWN}"
