#!/bin/bash
# JARVIS IBKR Watchdog — verifica conexión y reconecta si es necesario
LOG="/home/hproano/asistente/logs/ibkr_watchdog.log"
COMPOSE_DIR="/home/hproano/ibgateway-config"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

# Verificar si IBKR responde
STATUS=$(cd /home/hproano/asistente && \
  /home/hproano/asistente_env/bin/python3 -c "
from agentes.ibkr_trading import get_balance
bal = get_balance()
print(bal.get('status','UNKNOWN'))
" 2>/dev/null)

if [ "$STATUS" = "ACTIVE" ]; then
    log "OK - IBKR ACTIVE"
    exit 0
fi

log "ALERTA - IBKR status: $STATUS - Reconectando Gateway..."

# Enviar alerta a WhatsApp
curl -s -X POST http://localhost:8001/alerta \
  -H "Content-Type: application/json" \
  -d "{\"mensaje\": \"⚠️ JARVIS: IBKR desconectado ($STATUS). Reconectando...\"}" \
  2>/dev/null

# Reconectar
cd "$COMPOSE_DIR"
docker compose restart
sleep 180

# Verificar resultado
STATUS2=$(cd /home/hproano/asistente && \
  /home/hproano/asistente_env/bin/python3 -c "
from agentes.ibkr_trading import get_balance
bal = get_balance()
print(bal.get('status','UNKNOWN'))
" 2>/dev/null)

if [ "$STATUS2" = "ACTIVE" ]; then
    log "RECONECTADO OK - IBKR ACTIVE"
    curl -s -X POST http://localhost:8001/alerta \
      -H "Content-Type: application/json" \
      -d "{\"mensaje\": \"✅ JARVIS: IBKR reconectado exitosamente\"}" \
      2>/dev/null
else
    log "ERROR - No se pudo reconectar. Status: $STATUS2"
    curl -s -X POST http://localhost:8001/alerta \
      -H "Content-Type: application/json" \
      -d "{\"mensaje\": \"🚨 JARVIS CRÍTICO: IBKR no reconecta ($STATUS2). Intervención manual requerida.\"}" \
      2>/dev/null
fi
