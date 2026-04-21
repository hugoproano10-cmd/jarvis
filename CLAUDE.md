# JARVIS — Instrucciones para Claude Code

## Seguridad (OBLIGATORIO)
- NUNCA escribir API keys, passwords, secrets o tokens en el código. Siempre leer de .env via os.getenv()
- NUNCA hacer print() ni log de API keys completas. Máximo los primeros 8 caracteres: key[:8] + "..."
- NUNCA commitear .env, secrets, ni archivos con credenciales a git
- NUNCA exponer puertos al exterior sin autenticación (el dashboard es solo red local)
- NUNCA usar eval(), exec(), ni subprocess.shell=True con input del usuario
- NUNCA almacenar passwords en texto plano en la DB
- Todas las queries SQL deben usar parámetros (?) nunca string formatting/concatenación
- Todas las llamadas HTTP deben tener timeout (máximo 30s para APIs, 120s para LLMs)
- Todos los archivos creados deben tener permisos 644 (no 777)
- Los lockfiles deben limpiarse con atexit.register()

## Resiliencia (OBLIGATORIO)
- NUNCA bloquear el trading por un error en componentes secundarios (DB, dashboard, logs, notificaciones)
- Usar try/except alrededor de toda escritura a DB, envío de notificaciones, y logging
- Si una fuente de datos falla, usar fallback o score 0 (neutro), nunca abortar el ciclo
- Las ventas protectivas (stop-loss, trailing-stop) NUNCA deben ser bloqueadas por ninguna condición
- Si IBKR no responde, usar datos cacheados y no ejecutar compras (pero sí alertar)
- Si Binance no responde, reintentar con mirrors (api1, api2, api3.binance.com)

## Arquitectura
- jarvis-core (192.168.208.79): trading acciones + cripto, WhatsApp, crons, IBKR Gateway
- jarvis-power (192.168.208.80): FinBERT :8002, DeepSeek 70B :11435
- jarvis-brain (192.168.202.53): DeepSeek 671B :11436, Dashboard :8050
- DB: SQLite en datos/jarvis_history.db (se sincroniza a brain cada 5 min)
- Config: .env en la raíz del proyecto
- Entorno Python: /home/hproano/asistente_env/bin/python

## Convenciones de código
- Idioma: comentarios y logs en español
- Formato de logs: "YYYY-MM-DD HH:MM:SS | ACCION | SIMBOLO | detalles"
- Notificaciones solo por WhatsApp (http://localhost:8001/alerta)
- Siempre usar dotenv para cargar .env
- Imports dinámicos con importlib.util para módulos del proyecto
- NO modificar archivos de trading sin confirmación explícita del usuario

## Trading (CRÍTICO)
- Las reglas de trading son sagradas: R-SL, R-TP, R-TRAILING nunca se desactivan
- El LLM es un filtro, no un decisor. Si falla, la operación procede
- MAX_POR_OPERACION = $1,500 (acciones), $1,000 (cripto)
- Stop-loss acciones: -3%, cripto: -6%
- Take-profit parcial: +8% (50%), total: +15% (acciones), +10% (cripto)
- Posiciones protegidas: AMD, NVDA (nunca tocar)
- Anti day-trading: no recomprar lo vendido el mismo día (acciones), 4h cooldown (cripto)
