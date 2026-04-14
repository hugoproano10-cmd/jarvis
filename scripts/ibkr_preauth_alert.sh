#!/bin/bash
# Alerta 10 min antes del reinicio del Gateway (6:20 AM)
curl -s -X POST http://localhost:8001/alerta \
  -H "Content-Type: application/json" \
  -d '{"mensaje": "⚠️ JARVIS: El Gateway de IBKR se reiniciará en 10 minutos (6:30 AM). Por favor aprueba la autenticación en IBKR Mobile cuando llegue la notificación al teléfono."}' \
  2>/dev/null
