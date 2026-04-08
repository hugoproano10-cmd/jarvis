#!/home/hproano/asistente_env/bin/python
"""
JARVIS Performance — Análisis de performance del día con DeepSeek 671B.
Se ejecuta a las 5 PM ECT (L-V).

Flujo:
  1. Leer trades y decisiones del día
  2. Leer estado actual del portafolio
  3. Llamar al 671B para análisis
  4. Guardar en logs/performance_hoy.json
  5. Enviar resumen por WhatsApp
"""

import os
import sys
import re
import json
from datetime import datetime

import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Config ──────────────────────────────────────────────────
BRAIN_URL = "http://192.168.202.53:11436/v1/chat/completions"
BRAIN_TIMEOUT = 600

_TEST_MODE = "--test" in sys.argv

import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_cfg = _load("trading_config", os.path.join(PROYECTO, "trading", "config.py"))
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES


def _notificar_whatsapp(mensaje):
    """Envía mensaje a WhatsApp via servidor alertas."""
    if _TEST_MODE:
        print(f"  [WA] (suprimido en test)")
        return
    try:
        requests.post("http://localhost:8001/alerta",
                      json={"mensaje": mensaje}, timeout=10)
    except Exception as e:
        print(f"  WhatsApp error: {e}")


def _limpiar_think(texto):
    return re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()


# ── 1. Recopilar datos del día ─────────────────────────────

def obtener_datos_dia():
    """Lee todos los datos de trading del día actual."""
    hoy = datetime.now().strftime("%Y-%m-%d")

    # Decisiones del día
    log_path = os.path.join(PROYECTO, "logs", "trading_decisiones.log")
    decisiones = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            for linea in f:
                if linea.strip() and linea.startswith(hoy):
                    decisiones.append(linea.strip())

    # Log detallado de sesión
    log_sesion = os.path.join(PROYECTO, "logs", f"jarvis_trading_{hoy}.txt")
    sesion_texto = ""
    if os.path.exists(log_sesion):
        with open(log_sesion, "r", encoding="utf-8") as f:
            sesion_texto = f.read()[-3000:]  # últimos 3000 chars

    # Estado del portafolio
    estado_path = os.path.join(PROYECTO, "logs", "ultimo_estado.json")
    estado = {}
    if os.path.exists(estado_path):
        try:
            with open(estado_path, "r") as f:
                estado = json.load(f)
        except Exception:
            pass

    # Briefing de la mañana (si existe)
    briefing_path = os.path.join(PROYECTO, "datos", "briefing_hoy.json")
    briefing = {}
    if os.path.exists(briefing_path):
        try:
            with open(briefing_path, "r") as f:
                briefing = json.load(f)
        except Exception:
            pass

    return hoy, decisiones, sesion_texto, estado, briefing


# ── 2. Llamar al 671B ──────────────────────────────────────

SYSTEM_PERFORMANCE = """\
Eres JARVIS, agente de trading autónomo. Analiza el performance del día.
Responde SIEMPRE en español. Tu respuesta DEBE ser un JSON válido y nada más.

El JSON debe tener exactamente esta estructura:
{
  "fecha": "YYYY-MM-DD",
  "trades_ejecutados": 0,
  "trades_exitosos": 0,
  "pnl_estimado": 0.0,
  "errores_detectados": ["descripción de error 1"],
  "ajustes_recomendados": [
    {"parametro": "STOP_LOSS_PCT", "valor_actual": 0.03, "valor_sugerido": 0.05, "razon": "..."},
  ],
  "activos_revisar": ["SYM1"],
  "calificacion_dia": "BUENO|NEUTRAL|MALO",
  "resumen": "texto corto para WhatsApp (máximo 300 chars)"
}

Analiza:
- Si los trades fueron rentables o no
- Si los stop-loss y take-profit fueron adecuados
- Si el briefing matutino acertó en sus predicciones
- Qué ajustes de parámetros mejorarían el rendimiento
- Si hubo errores operativos (órdenes fallidas, timeouts, etc.)\
"""


def llamar_671b(hoy, decisiones, sesion, estado, briefing):
    """Llama al 671B para análisis de performance."""
    prompt = (
        f"FECHA: {hoy}\n\n"
        f"DECISIONES DE TRADING HOY:\n"
        f"{chr(10).join(decisiones[-30:]) if decisiones else 'Sin trades hoy'}\n\n"
        f"LOG DE SESIÓN (últimas entradas):\n{sesion[-1500:] if sesion else 'Sin log'}\n\n"
        f"ESTADO DEL PORTAFOLIO:\n"
        f"{json.dumps(estado, indent=2, ensure_ascii=False)[:800] if estado else 'No disponible'}\n\n"
        f"BRIEFING MATUTINO:\n"
        f"{json.dumps(briefing, indent=2, ensure_ascii=False)[:500] if briefing else 'No hubo briefing'}\n\n"
        f"Activos operables: {', '.join(ACTIVOS_OPERABLES)}\n"
        f"Parámetros actuales: SL={_cfg.STOP_LOSS_PCT*100}%, TP={_cfg.TAKE_PROFIT_PCT*100}%\n\n"
        f"Genera el análisis de performance JSON."
    )

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PERFORMANCE},
            {"role": "user", "content": prompt[:3500]},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "stream": False,
    }

    print("  Llamando al 671B (puede tardar 5-10 min)...")
    resp = requests.post(BRAIN_URL, json=payload, timeout=BRAIN_TIMEOUT)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    content = _limpiar_think(msg.get("content", ""))
    reasoning = _limpiar_think(msg.get("reasoning_content", ""))
    for candidate in [content, f"{reasoning}\n{content}"]:
        if re.search(r'\{[\s\S]*"fecha"[\s\S]*\}', candidate):
            return candidate
    return content or reasoning


# ── 3. Parsear y guardar ───────────────────────────────────

def parsear_performance(texto_llm):
    """Extrae JSON del texto del LLM."""
    bloques = list(re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', texto_llm))
    for match in reversed(bloques):
        candidate = match.group()
        if '"fecha"' in candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    match = re.search(r'\{[\s\S]*\}', texto_llm)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def generar_performance_basico(hoy, decisiones):
    """Fallback sin LLM."""
    ejecutadas = [d for d in decisiones if "exec=SI" in d]
    buys = [d for d in decisiones if "BUY" in d]
    sells = [d for d in decisiones if "SELL" in d]

    return {
        "fecha": hoy,
        "trades_ejecutados": len(ejecutadas),
        "trades_exitosos": 0,
        "pnl_estimado": 0.0,
        "errores_detectados": [],
        "ajustes_recomendados": [],
        "activos_revisar": [],
        "calificacion_dia": "NEUTRAL",
        "resumen": (f"Día {hoy}: {len(ejecutadas)} trades ejecutados "
                    f"({len(buys)} compras, {len(sells)} ventas). "
                    f"671B no disponible para análisis profundo."),
    }


def main():
    print(f"{'='*60}")
    print(f"  JARVIS PERFORMANCE — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}\n")

    # 1) Datos del día
    print("  Recopilando datos del día...")
    hoy, decisiones, sesion, estado, briefing = obtener_datos_dia()
    print(f"  Decisiones: {len(decisiones)} | Log sesión: {len(sesion)} chars")

    # 2) Llamar al 671B
    performance = None
    try:
        texto_llm = llamar_671b(hoy, decisiones, sesion, estado, briefing)
        print(f"  671B respondió ({len(texto_llm)} chars)")
        performance = parsear_performance(texto_llm)
        if performance:
            print(f"  JSON parseado OK")
        else:
            print(f"  Error parseando JSON, usando fallback")
            print(f"  Respuesta 671B: {texto_llm[:300]}")
    except Exception as e:
        print(f"  671B no disponible ({e}), usando análisis básico")

    if not performance:
        performance = generar_performance_basico(hoy, decisiones)

    performance["fecha"] = hoy

    # 3) Guardar JSON
    ruta_json = os.path.join(PROYECTO, "logs", "performance_hoy.json")
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(performance, f, indent=2, ensure_ascii=False)
    print(f"  Guardado: {ruta_json}")

    # 4) Enviar WhatsApp
    resumen = performance.get("resumen", "Análisis disponible")
    calif = performance.get("calificacion_dia", "?")
    trades = performance.get("trades_ejecutados", 0)
    errores = performance.get("errores_detectados", [])
    ajustes = performance.get("ajustes_recomendados", [])

    mensaje_wa = (
        f"JARVIS PERFORMANCE {hoy}\n"
        f"Calificación: {calif}\n"
        f"Trades ejecutados: {trades}\n"
    )
    if errores:
        mensaje_wa += f"Errores: {len(errores)}\n"
    if ajustes:
        mensaje_wa += f"Ajustes sugeridos: {len(ajustes)}\n"
    mensaje_wa += f"\n{resumen}"

    print(f"\n  Mensaje WhatsApp:\n  {mensaje_wa[:200]}...")
    _notificar_whatsapp(mensaje_wa)

    # Mostrar ajustes
    if ajustes:
        print(f"\n  Ajustes recomendados:")
        for a in ajustes:
            print(f"    {a.get('parametro', '?')}: {a.get('valor_actual', '?')} → {a.get('valor_sugerido', '?')}")
            print(f"      Razón: {a.get('razon', '?')}")

    print(f"\n{'='*60}")
    print(f"  Performance completado.")
    print(f"{'='*60}")


if __name__ == "__main__":
    if _TEST_MODE:
        print("=== TEST MODE ===\n")
    main()
