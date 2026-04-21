#!/home/hproano/asistente_env/bin/python
"""
Model Router — Routing inteligente entre 3 nodos Ollama.

Nodos:
  - jarvis-core  (localhost:11434):       nemotron-3-super   → trading tiempo real (~44s)
  - jarvis-power (192.168.208.80:11435):  nemotron-3-nano:30b → chat rápido (~15s)
                                          deepseek-r1:70b     → análisis medio (~60s)
  - jarvis-brain (192.168.202.53:11436):  deepseek-r1:671b   → análisis profundo (5-10 min)

Routing:
  - Saludos/comandos simples → Nano 30B en power
  - Trading/mercado/portafolio/cripto → Super 120B en core
  - Comparaciones/investigación/explícame → DeepSeek 70B en power
  - "analiza en detalle"/"investiga a fondo" → 671B en brain
  - Fallbacks cruzados si nodo no disponible
"""

import re
import time
import logging
from collections import deque

import requests

log = logging.getLogger("model-router")

# ── Balanceo por latencia ──────────────────────────────────
# Historial móvil de tiempos de respuesta por nodo (últimas N llamadas).
# Se resetea al reiniciar el proceso — está bien, es un predictor corto.
_MAX_HISTORIAL = 10
_MIN_MUESTRAS_SATURACION = 3  # no declarar saturado con < 3 muestras

# Umbral de saturación por nodo (segundos). Avg móvil > umbral → saturado.
# Normales esperados: core ~15s, power-nano ~10s, power-deep70 ~30s, brain ~120s.
_UMBRAL_SATURACION = {
    "core":         30.0,
    "power-nano":   20.0,
    "power-deep70": 60.0,
    "brain":        300.0,
}

_LATENCIAS = {}       # nodo_id -> deque(maxlen=_MAX_HISTORIAL)
_CONTADOR_REQUESTS = {}  # nodo_id -> int


def _registrar_latencia(nodo_id, segundos):
    hist = _LATENCIAS.setdefault(nodo_id, deque(maxlen=_MAX_HISTORIAL))
    hist.append(float(segundos))
    _CONTADOR_REQUESTS[nodo_id] = _CONTADOR_REQUESTS.get(nodo_id, 0) + 1


def _promedio_latencia(nodo_id):
    hist = _LATENCIAS.get(nodo_id)
    if not hist:
        return None
    return sum(hist) / len(hist)


def _esta_saturado(nodo_id):
    hist = _LATENCIAS.get(nodo_id)
    if not hist or len(hist) < _MIN_MUESTRAS_SATURACION:
        return False
    avg = sum(hist) / len(hist)
    umbral = _UMBRAL_SATURACION.get(nodo_id, float("inf"))
    return avg > umbral

# ── Configuración de nodos ─────────────────────────────────

NODOS = {
    "core": {
        "nombre": "jarvis-core",
        "url": "http://localhost:11434",
        "modelo": "nemotron-3-super",
        "descripcion": "Trading tiempo real (~44s)",
        "timeout": 120,
    },
    "power-nano": {
        "nombre": "jarvis-power",
        "url": "http://192.168.208.80:11435",
        "modelo": "deepseek-r1:70b",
        "descripcion": "Chat general (~60s)",
        "timeout": 60,
    },
    "power-deep70": {
        "nombre": "jarvis-power",
        "url": "http://192.168.208.80:11435",
        "modelo": "deepseek-r1:70b",
        "descripcion": "Análisis medio (~60s)",
        "timeout": 300,
    },
    "brain": {
        "nombre": "jarvis-brain",
        "url": "http://192.168.202.53:11436",
        "modelo": "deepseek-r1:671b",
        "descripcion": "Análisis profundo (5-10 min)",
        "timeout": 900,
        "api": "llama.cpp",  # OpenAI-compatible, no Ollama
    },
}

# ── Palabras clave por nivel ──────────────────────────────

PALABRAS_FINANCIERAS = {
    "trading", "trade", "trades", "operar", "operacion", "operaciones",
    "comprar", "vender", "orden", "ordenes", "stop loss", "take profit",
    "sl", "tp", "entrada", "salida", "long", "short", "posicion", "posiciones",
    "mercado", "mercados", "bolsa", "wall street", "nasdaq", "s&p",
    "sp500", "dow jones", "nyse", "indice", "indices",
    "bull", "bear", "bullish", "bearish", "rally", "crash",
    "soporte", "resistencia", "tendencia", "rango",
    "analisis", "análisis", "tecnico", "técnico", "fundamental",
    "rsi", "macd", "ema", "sma", "volumen", "vela", "velas",
    "patron", "divergencia", "fibonacci", "media movil",
    "portafolio", "portfolio", "inversion", "inversión", "rendimiento",
    "retorno", "roi", "pnl", "ganancia", "perdida", "pérdida",
    "equity", "balance", "capital", "dividendo",
    "cripto", "crypto", "bitcoin", "btc", "ethereum", "eth",
    "altcoin", "defi", "blockchain", "binance", "wallet",
    "token", "nft", "staking", "yield",
    "accion", "acciones", "stock", "stocks", "etf",
    "futures", "futuros", "opciones", "options",
    "forex", "dolar", "dólar", "euro", "yen",
    "fed", "inflacion", "inflación", "tasa", "tasas", "cpi",
    "gdp", "pib", "recesion", "recesión", "empleo",
}

PALABRAS_INVESTIGACION = {
    "compara", "comparar", "comparación", "diferencia", "diferencias",
    "vs", "versus", "contra",
    "explica", "explicame", "explícame", "explicar", "cómo funciona",
    "por qué", "porque", "por que", "investigar", "investiga",
    "profundiza", "detalla", "detallar", "pros y contras",
    "ventajas", "desventajas", "contexto", "historia",
}

PALABRAS_PROFUNDO = {
    "analiza en detalle", "análisis profundo", "analisis profundo",
    "investiga a fondo", "investigación completa", "investigacion completa",
    "estrategia completa", "plan completo", "plan detallado",
    "evalúa a fondo", "evalua a fondo", "análisis exhaustivo", "analisis exhaustivo",
    "deep analysis", "full analysis", "reporte completo",
    "dame todo", "quiero todo", "análisis largo",
    "análisis completo", "analisis completo",
    "completo de",
    "todas las señales", "todas las senales",
    "dime todo sobre", "cuéntame todo", "cuentame todo",
    "análisis integral", "analisis integral",
    "visión completa", "vision completa",
    "panorama completo",
}


def _clasificar(mensaje: str):
    """
    Clasifica el mensaje y retorna (nodo_preferido, fallbacks, razon).
    """
    texto = mensaje.lower()

    # Nivel 4: Análisis profundo (frases completas)
    for frase in PALABRAS_PROFUNDO:
        if frase in texto:
            return "brain", ["power-deep70", "core"], f"Análisis profundo → deepseek-r1:671b"

    # Nivel 3: Investigación/comparación
    for palabra in PALABRAS_INVESTIGACION:
        if re.search(r'(?:^|\s)' + re.escape(palabra) + r'(?:\s|$|[?.,!])', texto):
            return "power-deep70", ["core", "power-nano"], f"Investigación → deepseek-r1:70b"

    # Nivel 2: Trading/finanzas
    for palabra in PALABRAS_FINANCIERAS:
        if re.search(r'(?:^|\s|¿|/)' + re.escape(palabra) + r'(?:\s|$|[?.,!])', texto):
            return "core", ["power-deep70", "power-nano"], f"Trading/finanzas → nemotron-3-super"

    # Nivel 1: Chat general/saludos
    return "power-nano", ["core", "power-deep70"], f"Chat general → nemotron-3-nano:30b"


def _check_nodo(nodo_id: str, timeout: float = 3.0) -> bool:
    """Verifica si un nodo está disponible (Ollama o llama.cpp)."""
    nodo = NODOS[nodo_id]
    try:
        if nodo.get("api") == "llama.cpp":
            resp = requests.get(f"{nodo['url']}/health", timeout=timeout)
        else:
            resp = requests.get(f"{nodo['url']}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


def _llamar_nodo(nodo_id, messages, temperature=0.5):
    """Llama a un nodo (Ollama o llama.cpp OpenAI-compatible)."""
    nodo = NODOS[nodo_id]

    if nodo.get("api") == "llama.cpp":
        # llama.cpp: contexto limitado a 4096 tokens (~2000 chars por mensaje)
        truncated = []
        for m in messages:
            truncated.append({"role": m["role"], "content": m["content"][:2000]})
        payload = {
            "messages": truncated,
            "temperature": temperature,
            "max_tokens": 1000,
            "stream": False,
        }
        resp = requests.post(f"{nodo['url']}/v1/chat/completions",
                             json=payload, timeout=nodo["timeout"])
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        # DeepSeek R1 pone el razonamiento en reasoning_content y la respuesta en content
        texto = msg.get("content") or msg.get("reasoning_content") or ""
    else:
        # Ollama: POST /api/chat
        payload = {
            "model": nodo["modelo"],
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        resp = requests.post(f"{nodo['url']}/api/chat",
                             json=payload, timeout=nodo["timeout"])
        resp.raise_for_status()
        texto = resp.json()["message"]["content"]

    return re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()


# ── API pública ────────────────────────────────────────────

def health_check() -> dict:
    """Verifica disponibilidad de todos los nodos."""
    resultado = {}
    # Agrupar por URL+api para no hacer pings duplicados al mismo servidor
    cache_key_checked = {}
    for nodo_id, nodo in NODOS.items():
        key = f"{nodo['url']}|{nodo.get('api', 'ollama')}"
        if key not in cache_key_checked:
            cache_key_checked[key] = _check_nodo(nodo_id)
        resultado[nodo_id] = {
            "nombre": nodo["nombre"],
            "url": nodo["url"],
            "modelo": nodo["modelo"],
            "descripcion": nodo["descripcion"],
            "api": nodo.get("api", "ollama"),
            "disponible": cache_key_checked[key],
        }
    return resultado


def get_router_stats() -> dict:
    """Métricas de routing para diagnóstico / dashboard.

    Estado por nodo:
      - offline:  no responde al health check
      - saturado: promedio móvil supera el umbral configurado
      - OK:       disponible y latencia normal (o sin muestras suficientes)
    """
    stats = {}
    for nodo_id, nodo in NODOS.items():
        hist = _LATENCIAS.get(nodo_id) or deque()
        disponible = _check_nodo(nodo_id)
        saturado = _esta_saturado(nodo_id)
        if not disponible:
            estado = "offline"
        elif saturado:
            estado = "saturado"
        else:
            estado = "OK"
        stats[nodo_id] = {
            "nombre": nodo["nombre"],
            "modelo": nodo["modelo"],
            "estado": estado,
            "latencia_avg_s": round(sum(hist) / len(hist), 2) if hist else None,
            "latencia_ultima_s": round(hist[-1], 2) if hist else None,
            "requests_atendidos": _CONTADOR_REQUESTS.get(nodo_id, 0),
            "muestras": len(hist),
            "umbral_saturacion_s": _UMBRAL_SATURACION.get(nodo_id),
        }
    return stats


def route_message(mensaje: str, contexto: str = "", system_prompt: str = "") -> dict:
    """
    Enruta un mensaje al modelo más adecuado y devuelve la respuesta.
    Intenta fallbacks cruzados si el nodo preferido no está disponible.

    Returns:
        dict con claves: respuesta, modelo, nodo, tiempo, fallback, error
    """
    inicio = time.time()
    preferido, fallbacks, razon = _clasificar(mensaje)

    # Instrucción de idioma obligatoria
    _IDIOMA = ("INSTRUCCIÓN ABSOLUTA: Responde SIEMPRE en español. "
               "Sin excepciones. Traduce cualquier dato en inglés al español.")

    # Construir mensajes
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": f"{_IDIOMA}\n\n{system_prompt}"})
    else:
        messages.append({"role": "system", "content": _IDIOMA})
    if contexto:
        messages.append({"role": "system", "content": f"Contexto adicional: {contexto}"})
    messages.append({"role": "user", "content": mensaje})

    # Construir cadena de intentos. Si el preferido está saturado (no caído,
    # solo lento), intentar primero un fallback no-saturado. Si todos están
    # saturados, usar el preferido igual (mejor lento que nada).
    cadena = [preferido] + fallbacks
    desvio_saturacion = False

    if _esta_saturado(preferido):
        avg = _promedio_latencia(preferido) or 0
        umbral = _UMBRAL_SATURACION.get(preferido, 0)
        alternativa = next(
            (fb for fb in fallbacks
             if not _esta_saturado(fb) and _check_nodo(fb)),
            None,
        )
        if alternativa:
            log.info(f"ROUTER: {preferido} saturado (avg {avg:.1f}s > {umbral:.0f}s), "
                     f"redirigiendo a {alternativa}")
            desvio_saturacion = True
            cadena = [alternativa, preferido] + [f for f in fallbacks if f != alternativa]
        else:
            log.info(f"ROUTER: {preferido} saturado (avg {avg:.1f}s) pero todos los "
                     f"fallbacks también — se usa el preferido")

    for i, nodo_id in enumerate(cadena):
        if not _check_nodo(nodo_id):
            log.warning(f"{NODOS[nodo_id]['nombre']}({NODOS[nodo_id]['modelo']}) no disponible")
            continue

        try:
            t_call = time.time()
            texto = _llamar_nodo(nodo_id, messages)
            call_elapsed = time.time() - t_call
            _registrar_latencia(nodo_id, call_elapsed)

            elapsed = time.time() - inicio
            nodo = NODOS[nodo_id]
            uso_fallback = (nodo_id != preferido)

            saturacion_flag = "si" if desvio_saturacion and nodo_id != preferido else "no"
            log.info(f"ROUTER: {mensaje[:30]} → {nodo['nombre']} ({nodo['modelo']}) | "
                     f"{elapsed:.1f}s | saturación: {saturacion_flag}")
            if uso_fallback and not desvio_saturacion:
                log.info(f"[FALLBACK por error] {nodo['nombre']} {nodo['modelo']}")
            elif not uso_fallback:
                log.info(f"[{nodo['nombre']}] {nodo['modelo']} | {razon}")

            return {
                "respuesta": texto,
                "modelo": nodo["modelo"],
                "nodo": nodo["nombre"],
                "tiempo": round(elapsed, 2),
                "fallback": uso_fallback,
                "error": False,
            }
        except Exception as e:
            # Registrar latencia aunque falle — ayuda a detectar nodo lento-y-erroneo
            try:
                _registrar_latencia(nodo_id, time.time() - t_call)
            except Exception:
                pass
            log.warning(f"Error en {NODOS[nodo_id]['nombre']}({NODOS[nodo_id]['modelo']}): {e}")
            continue

    elapsed = time.time() - inicio
    return {
        "respuesta": "Ningún nodo Ollama está disponible. Verifica los servidores.",
        "modelo": None,
        "nodo": None,
        "tiempo": round(elapsed, 2),
        "fallback": False,
        "error": True,
    }


# ── Test ───────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 70)
    print("MODEL ROUTER — 3 nodos")
    print("=" * 70)

    print("\n--- Health Check ---")
    estado = health_check()
    for nodo_id, info in estado.items():
        status = "OK" if info["disponible"] else "OFFLINE"
        print(f"  {info['nombre']:14} {info['modelo']:25} {status:8} {info['url']}")

    print("\n--- Test de clasificación ---")
    tests = [
        ("Hola, cómo estás?", "power-nano"),
        ("Qué hora es?", "power-nano"),
        ("Analiza el mercado hoy", "core"),
        ("Cómo va mi portafolio?", "core"),
        ("Precio de Bitcoin?", "core"),
        ("Explícame qué es el RSI", "power-deep70"),
        ("Compara AAPL vs MSFT", "power-deep70"),
        ("Analiza en detalle la estrategia completa", "brain"),
        ("Investiga a fondo el sector energético", "brain"),
        ("Buenos días", "power-nano"),
    ]

    for msg, esperado in tests:
        nodo, _, razon = _clasificar(msg)
        ok = "OK" if nodo == esperado else "!!"
        modelo = NODOS[nodo]["modelo"]
        print(f"  [{ok}] \"{msg}\" → {modelo}")

    print("\n" + "=" * 70)
