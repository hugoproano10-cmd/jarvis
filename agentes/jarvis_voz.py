#!/home/hproano/asistente_env/bin/python
"""
JARVIS Voice — Transcripción (Whisper) y Síntesis de voz (gTTS + Fish Speech).

Funciones:
  - transcribir_audio(archivo) → texto en español
  - sintetizar_respuesta(texto, referencia) → archivo audio
  - procesar_mensaje_voz(audio_entrada) → audio_respuesta (pipeline completo)

TTS: gTTS (siempre disponible) + Fish Speech API (opcional, si el server corre)
STT: OpenAI Whisper medium en GPU
"""

import os
import sys
import time
import logging
from datetime import datetime

import nest_asyncio
nest_asyncio.apply()

log = logging.getLogger("jarvis-voz")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
AUDIOS_DIR = os.path.join(PROYECTO, "audios")
RESPUESTAS_DIR = os.path.join(AUDIOS_DIR, "respuestas")
REFERENCIA_WAV = os.path.join(AUDIOS_DIR, "jarvis_referencia.wav")
REFERENCIA_M4A = os.path.join(AUDIOS_DIR, "jarvis_referencia.m4a")

os.makedirs(RESPUESTAS_DIR, exist_ok=True)

# Fish Speech API server (fish_speech_server.py en puerto 8080)
FISH_API_URL = os.environ.get("FISH_SPEECH_API", "http://localhost:8080")

_whisper_model = None


# ══════════════════════════════════════════════════════════════
#  1. WHISPER — Transcripción de voz a texto
# ══════════════════════════════════════════════════════════════

def _get_whisper():
    """Carga Whisper medium (lazy, una sola vez)."""
    global _whisper_model
    if _whisper_model is None:
        import whisper
        log.info("Cargando Whisper medium en GPU...")
        _whisper_model = whisper.load_model("medium", device="cuda")
        log.info("Whisper medium cargado.")
    return _whisper_model


def transcribir_audio(archivo, idioma="es"):
    """
    Transcribe un archivo de audio a texto usando Whisper.

    Args:
        archivo: ruta al archivo de audio (mp3, wav, ogg, etc.)
        idioma: código de idioma (default: "es" para español)

    Returns:
        dict con: texto, idioma_detectado, duracion, segmentos
    """
    if not os.path.exists(archivo):
        return {"texto": "", "error": f"Archivo no encontrado: {archivo}"}

    inicio = time.time()
    model = _get_whisper()

    result = model.transcribe(archivo, language=idioma, fp16=True, verbose=False)

    elapsed = time.time() - inicio
    return {
        "texto": result["text"].strip(),
        "idioma_detectado": result.get("language", idioma),
        "duracion": round(elapsed, 2),
        "segmentos": len(result.get("segments", [])),
    }


# ══════════════════════════════════════════════════════════════
#  2. TTS — Síntesis de voz
#     Prioridad: Fish Speech API → Edge TTS → gTTS
# ══════════════════════════════════════════════════════════════

# Voz Edge TTS: US español masculina, ágil y natural (estilo JARVIS)
EDGE_VOICE = "es-US-AlonsoNeural"
EDGE_RATE = "+15%"    # Rápido, natural y ágil
EDGE_PITCH = "-10Hz"  # Grave, autoritario
EDGE_VOLUME = "+10%"  # Más volumen


def _fish_speech_disponible():
    return False  # Fish Speech eliminado del sistema


def _sintetizar_fish(texto, referencia=None):
    """Sintetiza con Fish Speech API server (voz clonada de la referencia)."""
    import requests

    try:
        resp = requests.post(f"{FISH_API_URL}/synthesize",
                             json={"text": texto, "temperature": 0.7, "top_p": 0.8},
                             timeout=120)
        resp.raise_for_status()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ruta = os.path.join(RESPUESTAS_DIR, f"jarvis_{ts}.wav")
        with open(ruta, "wb") as f:
            f.write(resp.content)
        return ruta
    except Exception as e:
        log.warning(f"Fish Speech API error: {e}")
        return None


def _sintetizar_edge(texto):
    """Sintetiza con Edge TTS (neural, voz masculina grave y pausada)."""
    import asyncio
    import edge_tts

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = os.path.join(RESPUESTAS_DIR, f"jarvis_{ts}.mp3")

    async def _gen():
        comm = edge_tts.Communicate(texto, EDGE_VOICE, rate=EDGE_RATE, pitch=EDGE_PITCH, volume=EDGE_VOLUME)
        await comm.save(ruta)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_gen())
    return ruta


def _sintetizar_gtts(texto):
    """Sintetiza con gTTS (fallback simple)."""
    from gtts import gTTS
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = os.path.join(RESPUESTAS_DIR, f"jarvis_{ts}.mp3")
    tts = gTTS(texto, lang="es", slow=False)
    tts.save(ruta)
    return ruta


def sintetizar_respuesta(texto, referencia=None, premium=False):
    """
    Sintetiza voz a partir de texto.
    Por defecto: Edge TTS (rápido, ~1.5s).
    Fish Speech (voz clonada, ~60s) solo si premium=True o texto empieza con "REPORTE:".

    Returns:
        dict con: archivo, motor, duracion_generacion, texto_procesado
    """
    inicio = time.time()
    usar_fish = premium

    # 1. Fish Speech (solo en modo premium / reportes)
    if usar_fish and _fish_speech_disponible():
        texto_limpio = texto.removeprefix("REPORTE:").strip() if texto.startswith("REPORTE:") else texto
        ruta = _sintetizar_fish(texto_limpio, referencia)
        if ruta:
            return {"archivo": ruta, "motor": "fish-speech",
                    "duracion_generacion": round(time.time() - inicio, 2),
                    "texto_procesado": texto_limpio}

    # 2. Edge TTS (default, rápido)
    try:
        ruta = _sintetizar_edge(texto)
        return {"archivo": ruta, "motor": f"edge-tts ({EDGE_VOICE})",
                "duracion_generacion": round(time.time() - inicio, 2),
                "texto_procesado": texto}
    except Exception as e:
        log.warning(f"Edge TTS error: {e}")

    # 3. gTTS (siempre funciona)
    ruta = _sintetizar_gtts(texto)
    return {"archivo": ruta, "motor": "gtts",
            "duracion_generacion": round(time.time() - inicio, 2),
            "texto_procesado": texto}


# ══════════════════════════════════════════════════════════════
#  3. PIPELINE COMPLETO — Voz entrada → Voz salida
# ══════════════════════════════════════════════════════════════

def procesar_mensaje_voz(audio_entrada, referencia_voz=None):
    """
    Pipeline completo:
      1. Transcribir audio de entrada con Whisper
      2. Procesar texto con el model router (JARVIS responde)
      3. Sintetizar respuesta con TTS

    Returns:
        dict con: texto_usuario, texto_respuesta, audio_respuesta, tiempos
    """
    tiempos = {}

    # 1. Transcribir
    t0 = time.time()
    transcripcion = transcribir_audio(audio_entrada)
    tiempos["transcripcion"] = round(time.time() - t0, 2)

    if transcripcion.get("error"):
        return {"error": f"Transcripción: {transcripcion['error']}"}

    texto_usuario = transcripcion["texto"]
    if not texto_usuario.strip():
        return {"error": "No se detectó habla en el audio"}

    # 2. Obtener respuesta de JARVIS
    t1 = time.time()
    sys.path.insert(0, PROYECTO)
    try:
        from agentes.model_router import route_message
        resultado = route_message(
            texto_usuario,
            system_prompt=(
                "INSTRUCCIÓN ABSOLUTA: Responde SIEMPRE en español. NUNCA en inglés. "
                "Eres JARVIS, asistente de Hugo Proaño. "
                "Responde en español, conciso, máximo 3 oraciones. "
                "Tu respuesta será leída en voz alta, así que sé natural."
            ),
        )
        texto_respuesta = resultado["respuesta"]
        modelo_usado = resultado.get("modelo", "")
    except Exception as e:
        texto_respuesta = f"Error obteniendo respuesta: {e}"
        modelo_usado = "error"
    tiempos["llm"] = round(time.time() - t1, 2)

    # 3. Sintetizar voz
    t2 = time.time()
    sintesis = sintetizar_respuesta(texto_respuesta, referencia=referencia_voz)
    tiempos["sintesis"] = round(time.time() - t2, 2)

    return {
        "texto_usuario": texto_usuario,
        "texto_respuesta": texto_respuesta,
        "modelo": modelo_usado,
        "audio_respuesta": sintesis.get("archivo"),
        "motor_tts": sintesis.get("motor"),
        "tiempos": tiempos,
        "tiempo_total": round(sum(tiempos.values()), 2),
    }


# ══════════════════════════════════════════════════════════════
#  CLI — Test
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 60)
    print("  JARVIS VOZ — Test completo")
    print("=" * 60)

    # 0. Referencia
    print("\n--- [0] Referencia de voz ---")
    if os.path.exists(REFERENCIA_M4A) and not os.path.exists(REFERENCIA_WAV):
        print(f"  Convirtiendo M4A → WAV 16kHz mono...")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-i", REFERENCIA_M4A,
                        "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
                        REFERENCIA_WAV], capture_output=True)
    if os.path.exists(REFERENCIA_WAV):
        print(f"  WAV: {REFERENCIA_WAV} ({os.path.getsize(REFERENCIA_WAV)/1024:.0f} KB)")
    if os.path.exists(REFERENCIA_M4A):
        print(f"  M4A: {REFERENCIA_M4A} ({os.path.getsize(REFERENCIA_M4A)/1024:.0f} KB)")

    # 1. Whisper
    print("\n--- [1] Whisper STT ---")
    try:
        model = _get_whisper()
        print(f"  Whisper medium en GPU: OK")
        if os.path.exists(REFERENCIA_WAV):
            print(f"  Transcribiendo referencia...")
            r = transcribir_audio(REFERENCIA_WAV)
            print(f"  Texto ({r['duracion']}s): \"{r['texto'][:150]}\"")
    except Exception as e:
        print(f"  Error: {e}")

    # 2. TTS
    print("\n--- [2] TTS ---")
    fish_ok = _fish_speech_disponible()
    print(f"  Fish Speech API: {'OK (voz clonada)' if fish_ok else 'No disponible'}")
    print(f"  Edge TTS ({EDGE_VOICE}): disponible")

    # 3. Generar audio
    print("\n--- [3] Audio de prueba ---")
    texto = "Buenos días Hugo, todos los sistemas están operativos y listos para comenzar."
    print(f"  Texto: \"{texto}\"")
    resultado = sintetizar_respuesta(texto)
    if resultado.get("archivo"):
        size_kb = os.path.getsize(resultado["archivo"]) / 1024
        print(f"  Audio: {resultado['archivo']}")
        print(f"  Motor: {resultado['motor']}")
        print(f"  Tamaño: {size_kb:.1f} KB | Tiempo: {resultado['duracion_generacion']}s")
        import shutil
        prueba = os.path.join(RESPUESTAS_DIR, "prueba_jarvis_voz.mp3")
        shutil.copy2(resultado["archivo"], prueba)
        print(f"  Copia: {prueba}")

    # 4. Archivos
    print("\n--- [4] Archivos ---")
    for f in sorted(os.listdir(RESPUESTAS_DIR))[-5:]:
        print(f"  {f} ({os.path.getsize(os.path.join(RESPUESTAS_DIR, f))/1024:.0f} KB)")

    print(f"\n{'=' * 60}")
