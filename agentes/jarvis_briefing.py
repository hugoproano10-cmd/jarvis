#!/home/hproano/asistente_env/bin/python
"""
JARVIS Briefing Matutino — Análisis profundo con DeepSeek 671B.
Se ejecuta a las 6:30 AM ECT (L-V).

Flujo:
  1. Leer contexto de mercado (F&G, VIX, noticias, FRED)
  2. Leer historial de trades recientes
  3. Llamar al 671B (jarvis-brain) con todo el contexto
  4. Generar JSON con scores por activo y ajustes
  5. Guardar en datos/briefing_hoy.json
  6. Enviar resumen por WhatsApp
"""

import os
import sys
import re
import json
from datetime import datetime, timedelta

import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Config ──────────────────────────────────────────────────
BRAIN_URL = "http://192.168.202.53:11436/v1/chat/completions"
BRAIN_TAGS_URL = "http://192.168.202.53:11436/api/tags"
BRAIN_TIMEOUT = 600  # 10 min para 671B
# Fallback: el modelo está registrado como hash sin nombre en jarvis-brain.
MODEL_671B_FALLBACK = "sha256-439dd1a5e05286918f54941f49f9b56118c757440f6333f67f1cd5cbb5c8520b"


def _descubrir_modelo_671b():
    """Consulta /api/tags y retorna el nombre del primer modelo disponible."""
    try:
        r = requests.get(BRAIN_TAGS_URL, timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        if models and models[0].get("name"):
            return models[0]["name"]
    except Exception:
        pass
    return MODEL_671B_FALLBACK

_TEST_MODE = "--test" in sys.argv

# Importar módulos con carga explícita (evitar colisión con config/)
import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_ctx = _load("contexto_mercado", os.path.join(PROYECTO, "datos", "contexto_mercado.py"))
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
    """Elimina bloques <think>...</think> de DeepSeek-R1."""
    return re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()


# ── 1. Recopilar contexto ──────────────────────────────────

def obtener_contexto():
    """Recopila todo el contexto disponible para el briefing."""
    print("  1/4 Contexto de mercado...")
    texto_ctx, datos_ctx = _ctx.get_contexto_completo()

    # Trades recientes (últimos 3 días)
    print("  2/4 Historial de trades...")
    log_path = os.path.join(PROYECTO, "logs", "trading_decisiones.log")
    trades_recientes = []
    if os.path.exists(log_path):
        hace_3d = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        with open(log_path, "r", encoding="utf-8") as f:
            for linea in f:
                if linea.strip() and linea[:10] >= hace_3d:
                    trades_recientes.append(linea.strip())

    # Estado del portafolio (último estado guardado)
    estado_path = os.path.join(PROYECTO, "logs", "ultimo_estado.json")
    estado = {}
    if os.path.exists(estado_path):
        try:
            with open(estado_path, "r") as f:
                estado = json.load(f)
        except Exception:
            pass

    # Datos macro FRED
    print("  3/4 Datos macro FRED...")
    datos_fred = ""
    try:
        _fuentes = _load("fuentes_mercado", os.path.join(PROYECTO, "datos", "fuentes_mercado.py"))
        macro = _fuentes.get_datos_macro_fed()
        if macro and not macro.get("error"):
            datos_fred = json.dumps(macro, indent=2, ensure_ascii=False)[:1500]
    except Exception as e:
        datos_fred = f"FRED no disponible: {e}"

    return texto_ctx, datos_ctx, trades_recientes, estado, datos_fred


# ── 2. Llamar al 671B ──────────────────────────────────────

SYSTEM_BRIEFING = """\
Eres JARVIS, agente de trading autónomo. Genera un briefing matutino estructurado.
Responde SIEMPRE en español. Tu respuesta DEBE ser un JSON válido y nada más.

El JSON debe tener exactamente esta estructura:
{
  "fecha": "YYYY-MM-DD",
  "regimen": "BULL|BEAR|LATERAL",
  "scores_ajuste": {"SIMBOLO": N},
  "activos_vigilar": ["SYM1", "SYM2"],
  "riesgos_del_dia": "texto corto",
  "recomendacion_umbral": N,
  "resumen": "texto corto para WhatsApp (máximo 300 chars)"
}

Reglas para scores_ajuste:
- Valores entre -2 y +2
- +2: noticia muy positiva o macro favorable al activo
- +1: sentimiento ligeramente positivo
- 0: neutral o sin datos
- -1: riesgo moderado
- -2: riesgo alto, evitar comprar

Reglas para recomendacion_umbral:
- 1: mercado favorable, ser agresivo
- 2: condiciones normales
- 3: mercado adverso, ser conservador

Solo incluye activos operables en scores_ajuste.\
"""


def llamar_671b(contexto_texto, trades_texto, estado_texto, fred_texto):
    """Llama al 671B y retorna el JSON del briefing."""
    prompt = (
        f"CONTEXTO DE MERCADO:\n{contexto_texto[:1800]}\n\n"
        f"DATOS MACRO:\n{fred_texto[:800]}\n\n"
        f"TRADES RECIENTES (últimos 3 días):\n"
        f"{chr(10).join(trades_texto[-20:]) if trades_texto else 'Sin trades recientes'}\n\n"
        f"ESTADO PORTAFOLIO:\n{estado_texto[:500]}\n\n"
        f"Fecha de hoy: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Activos operables: {', '.join(ACTIVOS_OPERABLES)}\n\n"
        f"Genera el briefing JSON."
    )

    payload = {
        "model": _descubrir_modelo_671b(),
        "messages": [
            {"role": "system", "content": SYSTEM_BRIEFING},
            {"role": "user", "content": prompt[:3500]},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "stream": False,
    }

    print("  4/4 Llamando al 671B (puede tardar 5-10 min)...")
    resp = requests.post(BRAIN_URL, json=payload, timeout=BRAIN_TIMEOUT)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    content = _limpiar_think(msg.get("content", ""))
    reasoning = _limpiar_think(msg.get("reasoning_content", ""))
    # Buscar JSON en content primero, luego en reasoning+content
    for candidate in [content, f"{reasoning}\n{content}"]:
        if re.search(r'\{[\s\S]*"fecha"[\s\S]*\}', candidate):
            return candidate
    return content or reasoning


# ── 3. Parsear y guardar ───────────────────────────────────

def parsear_briefing(texto_llm):
    """Extrae JSON del texto del LLM (puede venir con markdown o reasoning)."""
    # Intentar extraer bloque JSON con "fecha" dentro
    # Buscar el último bloque { ... } que contenga "fecha" (el JSON final, no intermedios)
    bloques = list(re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', texto_llm))
    for match in reversed(bloques):
        candidate = match.group()
        if '"fecha"' in candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    # Fallback: greedy match
    match = re.search(r'\{[\s\S]*\}', texto_llm)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def generar_briefing_basico(datos_ctx):
    """Fallback: briefing sin LLM, usando solo datos de APIs."""
    fng = datos_ctx.get("fear_greed", {})
    vix = datos_ctx.get("vix", {})
    fng_valor = fng.get("valor", 50) or 50
    vix_precio = vix.get("precio", 0) or 0

    if fng_valor < 20:
        regimen = "BEAR"
        umbral = 1
        riesgos = f"Miedo extremo (F&G={fng_valor}). Oportunidad en defensivos."
    elif fng_valor > 70:
        regimen = "BULL"
        umbral = 2
        riesgos = f"Optimismo alto (F&G={fng_valor}). Vigilar sobrecompra."
    else:
        regimen = "LATERAL"
        umbral = 2
        riesgos = f"Mercado neutral (F&G={fng_valor}). Operar con cautela."

    if vix_precio > 30:
        riesgos += f" VIX elevado ({vix_precio})."
        umbral = min(umbral + 1, 3)

    return {
        "fecha": datetime.now().strftime("%Y-%m-%d"),
        "regimen": regimen,
        "scores_ajuste": {},
        "activos_vigilar": ["GLD", "IEF"] if regimen == "BEAR" else [],
        "riesgos_del_dia": riesgos,
        "recomendacion_umbral": umbral,
        "resumen": f"Briefing básico (sin 671B). {riesgos}",
    }


def main():
    print(f"{'='*60}")
    print(f"  JARVIS BRIEFING — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}\n")

    # 1-3) Recopilar contexto
    texto_ctx, datos_ctx, trades, estado, fred = obtener_contexto()

    estado_texto = json.dumps(estado, indent=2, ensure_ascii=False)[:500] if estado else "Sin estado"

    # 4) Llamar al 671B
    briefing = None
    try:
        texto_llm = llamar_671b(texto_ctx, trades, estado_texto, fred)
        print(f"  671B respondió ({len(texto_llm)} chars)")
        briefing = parsear_briefing(texto_llm)
        if briefing:
            print(f"  JSON parseado OK")
        else:
            print(f"  Error parseando JSON, usando fallback")
            print(f"  Respuesta 671B: {texto_llm[:300]}")
    except Exception as e:
        print(f"  671B no disponible ({e}), usando briefing básico")

    if not briefing:
        briefing = generar_briefing_basico(datos_ctx)

    # Asegurar fecha correcta
    briefing["fecha"] = datetime.now().strftime("%Y-%m-%d")

    # 5) Guardar JSON
    ruta_json = os.path.join(PROYECTO, "datos", "briefing_hoy.json")
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(briefing, f, indent=2, ensure_ascii=False)
    print(f"  Guardado: {ruta_json}")

    # 6) Enviar WhatsApp
    resumen = briefing.get("resumen", "Briefing disponible")
    vigilar = ", ".join(briefing.get("activos_vigilar", []))
    riesgos = briefing.get("riesgos_del_dia", "")
    umbral = briefing.get("recomendacion_umbral", 2)

    mensaje_wa = (
        f"JARVIS BRIEFING {briefing['fecha']}\n"
        f"Régimen: {briefing.get('regimen', '?')}\n"
        f"Umbral recomendado: {umbral}\n"
        f"Vigilar: {vigilar or 'ninguno'}\n"
        f"Riesgos: {riesgos}\n\n"
        f"{resumen}"
    )
    print(f"\n  Mensaje WhatsApp:\n  {mensaje_wa[:200]}...")
    _notificar_whatsapp(mensaje_wa)

    # Mostrar scores
    scores = briefing.get("scores_ajuste", {})
    if scores:
        print(f"\n  Scores de ajuste:")
        for sym, sc in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            print(f"    {sym:<5}: {sc:+d}")

    print(f"\n{'='*60}")
    print(f"  Briefing completado.")
    print(f"{'='*60}")


if __name__ == "__main__":
    if _TEST_MODE:
        print("=== TEST MODE ===\n")
    main()
