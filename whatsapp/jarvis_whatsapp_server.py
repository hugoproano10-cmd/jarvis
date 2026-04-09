#!/home/hproano/asistente_env/bin/python
"""
JARVIS WhatsApp Server — FastAPI backend para jarvis_whatsapp.js.
Procesa mensajes de texto y audio igual que el bot de Telegram.
Puerto: 8000
"""

import os
import sys
import base64
import tempfile
import logging
import importlib.util as ilu
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import uvicorn

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)
log = logging.getLogger("jarvis-whatsapp")


def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Módulos JARVIS
fuentes = _load("fuentes_mercado", os.path.join(PROYECTO, "datos", "fuentes_mercado.py"))
memoria = _load("memoria_jarvis", os.path.join(PROYECTO, "datos", "memoria_jarvis.py"))
_router = _load("model_router", os.path.join(PROYECTO, "agentes", "model_router.py"))
route_message = _router.route_message
voz = _load("jarvis_voz", os.path.join(PROYECTO, "agentes", "jarvis_voz.py"))

try:
    alpaca = _load("alpaca_client",
                   os.path.join(os.path.expanduser("~"), "trading", "alpaca_client.py"))
except Exception:
    alpaca = None

try:
    cripto = _load("jarvis_cripto", os.path.join(PROYECTO, "cripto", "jarvis_cripto.py"))
except Exception:
    cripto = None

app = FastAPI(title="JARVIS WhatsApp Server")

SYSTEM_PROMPT = """\
INSTRUCCIÓN ABSOLUTA E INAMOVIBLE: Responde SIEMPRE en español. \
Sin excepciones. Traduce cualquier dato en inglés al español. \
NUNCA respondas en inglés.

Eres JARVIS, asistente personal de Hugo Proaño, trader e ingeniero en Ecuador.
Respondes en español, con tono profesional pero cercano.
Tienes conocimiento de mercados financieros, criptomonedas, tecnología y programación.
Sé conciso: máximo 3-4 párrafos por respuesta.
Cuando Hugo pregunte sobre su portafolio, mercado o cripto, USA LOS DATOS REALES del contexto. NO inventes datos.
Hugo usa paper trading en Alpaca (acciones) y Binance Testnet (cripto).\
"""


# ── Contexto ─────────────────────────────────────────────────

def obtener_contexto():
    """Contexto de mercado para inyectar en las respuestas."""
    partes = [f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}"]

    # Portafolio
    if alpaca:
        try:
            bal = alpaca.get_balance()
            partes.append(f"Portafolio: equity=${float(bal['equity']):,.2f}, cash=${float(bal['cash']):,.2f}")
            pos = alpaca.get_positions()
            if pos:
                lineas = [f"{p['symbol']} {float(p['unrealized_plpc'])*100:+.1f}%" for p in pos]
                partes.append(f"Posiciones: {', '.join(lineas)}")
        except Exception:
            pass

    # Cripto
    if cripto:
        try:
            for par in cripto.PARES[:2]:
                precio = cripto.obtener_precio(par)
                nombre = cripto.NOMBRES.get(par, par)
                partes.append(f"{nombre}: ${precio:,.2f}")
        except Exception:
            pass

    # Contexto rápido (F&G + VIX + Fed Funds, sin las 21 APIs)
    try:
        from datos.contexto_mercado import obtener_fear_greed, obtener_vix
        fng = obtener_fear_greed()
        partes.append(f"Fear&Greed: {fng.get('valor','?')}/100 — {fng.get('clasificacion','?')}")
        vix = obtener_vix()
        partes.append(f"VIX: {vix.get('precio','?')} — {vix.get('nivel','?')}")
    except Exception:
        pass
    try:
        macro = fuentes.get_datos_macro_fed()
        ff = macro.get("fed_funds_rate", {}).get("valor")
        if ff:
            partes.append(f"Fed Funds Rate: {ff}%")
    except Exception:
        pass

    return "\n".join(partes)


# ── Modelos ──────────────────────────────────────────────────

class MensajeRequest(BaseModel):
    mensaje: str = ""
    tipo: str = "texto"  # "texto" o "audio"
    audio_base64: Optional[str] = None


class MensajeResponse(BaseModel):
    respuesta: str
    modelo: Optional[str] = None
    nodo: Optional[str] = None
    tiempo: Optional[float] = None
    audio_base64: Optional[str] = None
    transcripcion: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────

@app.post("/mensaje", response_model=MensajeResponse)
def procesar_mensaje(req: MensajeRequest):
    texto_usuario = req.mensaje
    transcripcion = None

    # Si es audio, transcribir primero
    if req.tipo == "audio" and req.audio_base64:
        try:
            audio_bytes = base64.b64decode(req.audio_base64)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False,
                                             dir=voz.RESPUESTAS_DIR) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            result = voz.transcribir_audio(tmp_path)
            os.unlink(tmp_path)

            if result.get("error"):
                return MensajeResponse(respuesta=f"Error transcribiendo: {result['error']}")

            texto_usuario = result["texto"]
            transcripcion = texto_usuario
            log.info(f"Transcripción ({result['duracion']}s): {texto_usuario[:80]}")

            if not texto_usuario.strip():
                return MensajeResponse(respuesta="No pude entender el audio. Intenta de nuevo.")
        except Exception as e:
            log.error(f"Error procesando audio: {e}")
            return MensajeResponse(respuesta=f"Error con audio: {e}")

    if not texto_usuario.strip():
        return MensajeResponse(respuesta="Mensaje vacío.")

    log.info(f"Mensaje: {texto_usuario[:80]}")

    # Buscar memoria
    ctx_memoria = ""
    try:
        ctx_memoria = memoria.obtener_contexto_memoria(texto_usuario, n=3)
    except Exception:
        pass

    # Contexto de mercado
    contexto = obtener_contexto()
    contexto_completo = f"Datos reales ahora mismo:\n{contexto}"
    if ctx_memoria:
        contexto_completo += f"\n\n{ctx_memoria}"

    log.info(f"Contexto: {len(contexto_completo)} chars")

    # Procesar con model_router
    resultado = route_message(
        texto_usuario,
        contexto=contexto_completo,
        system_prompt=SYSTEM_PROMPT,
    )

    if resultado["error"]:
        return MensajeResponse(respuesta=resultado["respuesta"])

    respuesta = resultado["respuesta"]

    # Guardar en memoria
    try:
        memoria.guardar_conversacion(
            pregunta=texto_usuario,
            respuesta=respuesta,
            modelo=resultado.get("modelo", ""),
            nodo=resultado.get("nodo", ""),
            tiempo=resultado.get("tiempo", 0),
        )
    except Exception:
        pass

    return MensajeResponse(
        respuesta=respuesta,
        modelo=resultado.get("modelo"),
        nodo=resultado.get("nodo"),
        tiempo=resultado.get("tiempo"),
        audio_base64=None,
        transcripcion=transcripcion,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "memoria": memoria.stats(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
