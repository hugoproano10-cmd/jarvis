#!/home/hproano/asistente_env/bin/python
"""
Earnings Calls NLP — Análisis del tono de ejecutivos en llamadas de resultados.
Fuente: Alpha Vantage (transcripciones completas con sentimiento por speaker).
Análisis profundo: Nemotron Super 120B (jarvis-core).
Historial: ChromaDB para comparar trimestre a trimestre.
"""

import os
import sys
import json
import time
import re
import logging
from datetime import datetime

import requests
import chromadb

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

AV_KEY = os.getenv("ALPHA_VANTAGE_PREMIUM_KEY", "")
OLLAMA_URL = "http://localhost:11434/api/chat"

log = logging.getLogger("earnings-nlp")

# ChromaDB
MEMORIA_DIR = os.path.join(PROYECTO, "datos", "memoria")
os.makedirs(MEMORIA_DIR, exist_ok=True)
_chroma = chromadb.PersistentClient(path=MEMORIA_DIR)
_col_earnings = _chroma.get_or_create_collection(
    name="earnings_tone",
    metadata={"hnsw:space": "cosine"},
)


# ══════════════════════════════════════════════════════════════
#  1. OBTENER TRANSCRIPCIÓN
# ══════════════════════════════════════════════════════════════

def _ultimo_trimestre():
    """Retorna el trimestre más reciente con earnings (el anterior al actual)."""
    now = datetime.now()
    y = now.year
    q = (now.month - 1) // 3  # trimestre actual (0-3), earnings aún no publicados
    # Retroceder 1 trimestre (el último con datos)
    if q <= 0:
        return f"{y-1}Q4"
    return f"{y}Q{q}"


def _trimestres_recientes(n=2):
    """Retorna los N trimestres más recientes con posibles earnings."""
    trimestres = []
    now = datetime.now()
    y = now.year
    q = (now.month - 1) // 3
    for _ in range(n):
        if q <= 0:
            y -= 1
            q = 4
        trimestres.append(f"{y}Q{q}")
        q -= 1
    return trimestres


def obtener_transcripcion(simbolo, quarter=None):
    """
    Obtiene la transcripción de earnings call via Alpha Vantage.
    Retorna dict con speakers, contenido y sentimiento pre-calculado.
    """
    if not AV_KEY:
        return {"error": "ALPHA_VANTAGE_PREMIUM_KEY no configurada"}

    if quarter is None:
        quarter = _ultimo_trimestre()

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "EARNINGS_CALL_TRANSCRIPT",
        "symbol": simbolo,
        "quarter": quarter,
        "apikey": AV_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "Error Message" in data or "Note" in data:
            return {"error": data.get("Error Message", data.get("Note", "API error"))}

        transcript = data.get("transcript", [])
        if not transcript:
            return {"error": f"Sin transcripción para {simbolo} {quarter}"}

        # Extraer speakers principales (CEO, CFO)
        speakers = []
        texto_completo = ""
        sentimientos_av = []

        for seg in transcript:
            title = seg.get("title", "").lower()
            content = seg.get("content", "")
            sent = seg.get("sentiment")

            texto_completo += content + " "

            if sent:
                try:
                    sentimientos_av.append(float(sent))
                except (ValueError, TypeError):
                    pass

            # Solo ejecutivos principales
            if any(role in title for role in ["ceo", "cfo", "chief", "president", "chairman"]):
                speakers.append({
                    "nombre": seg.get("speaker", ""),
                    "titulo": seg.get("title", ""),
                    "contenido": content[:500],
                    "sentimiento_av": sent,
                })

        sent_promedio_av = sum(sentimientos_av) / len(sentimientos_av) if sentimientos_av else 0

        return {
            "simbolo": simbolo,
            "quarter": quarter,
            "speakers": speakers,
            "texto_completo": texto_completo,
            "total_segmentos": len(transcript),
            "sentimiento_av_promedio": round(sent_promedio_av, 3),
        }

    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
#  2. ANÁLISIS NLP CON NEMOTRON SUPER 120B
# ══════════════════════════════════════════════════════════════

PROMPT_ANALISIS = """\
Eres un analista financiero experto en interpretar el tono de ejecutivos.
Responde SOLO en español. Responde SOLO con JSON válido, sin markdown.

Analiza estos fragmentos del CEO/CFO en la llamada de resultados de {simbolo} ({quarter}).
Identifica:
1) Palabras de incertidumbre usadas
2) Palabras de confianza usadas
3) Score de confianza ejecutiva: -100 (muy negativo) a +100 (muy positivo)
4) Señal de trading: ALCISTA/BAJISTA/NEUTRAL

Fragmentos:
{fragmentos}

JSON exacto:
{{"score":<número -100 a 100>,"senal":"ALCISTA|BAJISTA|NEUTRAL","palabras_confianza":["..."],"palabras_incertidumbre":["..."],"razon":"una línea"}}\
"""


def analizar_tono(transcripcion):
    """Envía fragmentos ejecutivos al Nemotron Super 120B para análisis de tono."""
    if transcripcion.get("error"):
        return {"error": transcripcion["error"], "score": 0, "senal": "N/D"}

    # Preparar fragmentos de ejecutivos
    fragmentos = []
    for sp in transcripcion.get("speakers", []):
        fragmentos.append(f"[{sp['titulo']}] {sp['contenido'][:300]}")

    if not fragmentos:
        # Usar primeros 500 chars del texto completo
        fragmentos = [transcripcion.get("texto_completo", "")[:500]]

    texto_fragmentos = "\n".join(fragmentos)[:1500]

    prompt = PROMPT_ANALISIS.format(
        simbolo=transcripcion["simbolo"],
        quarter=transcripcion["quarter"],
        fragmentos=texto_fragmentos,
    )

    try:
        payload = {
            "model": "nemotron-3-super",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        texto = resp.json()["message"]["content"]
        texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()

        # Parsear JSON
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio >= 0 and fin > inicio:
            resultado = json.loads(texto[inicio:fin])
            resultado["sentimiento_av"] = transcripcion.get("sentimiento_av_promedio", 0)
            return resultado

        return {"score": 0, "senal": "N/D", "error": "No se pudo parsear respuesta LLM"}

    except Exception as e:
        # Fallback: usar sentimiento de Alpha Vantage directamente
        av_sent = transcripcion.get("sentimiento_av_promedio", 0)
        score = int(av_sent * 100)
        if score > 30:
            senal = "ALCISTA"
        elif score < -30:
            senal = "BAJISTA"
        else:
            senal = "NEUTRAL"
        return {
            "score": score,
            "senal": senal,
            "razon": f"Sentimiento Alpha Vantage: {av_sent:.2f} (LLM no disponible: {e})",
            "sentimiento_av": av_sent,
        }


# ══════════════════════════════════════════════════════════════
#  3. HISTORIAL EN CHROMADB
# ══════════════════════════════════════════════════════════════

def guardar_score(simbolo, quarter, score, senal, razon=""):
    """Guarda score de earnings en ChromaDB."""
    doc_id = f"earn_{simbolo}_{quarter}"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Verificar si ya existe
    existente = _col_earnings.get(ids=[doc_id])
    if existente and existente["ids"]:
        _col_earnings.update(
            ids=[doc_id],
            documents=[f"{simbolo} {quarter}: score {score}, {senal}. {razon}"],
            metadatas=[{"simbolo": simbolo, "quarter": quarter, "score": str(score),
                        "senal": senal, "razon": razon[:200], "timestamp": ts}],
        )
    else:
        _col_earnings.add(
            ids=[doc_id],
            documents=[f"{simbolo} {quarter}: score {score}, {senal}. {razon}"],
            metadatas=[{"simbolo": simbolo, "quarter": quarter, "score": str(score),
                        "senal": senal, "razon": razon[:200], "timestamp": ts}],
        )


def obtener_score_anterior(simbolo, quarter_actual):
    """Busca el score del trimestre anterior en ChromaDB."""
    # Calcular trimestre anterior
    y = int(quarter_actual[:4])
    q = int(quarter_actual[-1])
    if q == 1:
        prev = f"{y-1}Q4"
    else:
        prev = f"{y}Q{q-1}"

    doc_id = f"earn_{simbolo}_{prev}"
    existente = _col_earnings.get(ids=[doc_id], include=["metadatas"])
    if existente and existente["ids"]:
        meta = existente["metadatas"][0]
        return {"quarter": prev, "score": int(meta.get("score", 0)), "senal": meta.get("senal", "N/D")}
    return None


# ══════════════════════════════════════════════════════════════
#  4. FUNCIÓN PRINCIPAL
# ══════════════════════════════════════════════════════════════

def analizar_earnings_call(simbolo, quarter=None):
    """
    Pipeline completo: obtener transcripción → analizar tono → comparar historial.
    Retorna dict con score, señal, comparación y detalles.
    """
    # Buscar el trimestre más reciente con transcripción disponible
    if quarter is None:
        trans = None
        for q in _trimestres_recientes(3):
            trans = obtener_transcripcion(simbolo, q)
            if not trans.get("error"):
                quarter = q
                break
        if trans is None or trans.get("error"):
            return {
                "simbolo": simbolo, "quarter": _ultimo_trimestre(),
                "score": 0, "senal": "N/D",
                "error": trans.get("error", "Sin transcripción") if trans else "Sin transcripción",
            }
    else:
        trans = obtener_transcripcion(simbolo, quarter)
        if trans.get("error"):
            return {
                "simbolo": simbolo, "quarter": quarter,
                "score": 0, "senal": "N/D",
                "error": trans["error"],
            }

    # 2. Analizar tono
    analisis = analizar_tono(trans)

    score = analisis.get("score", 0)
    senal = analisis.get("senal", "NEUTRAL")
    razon = analisis.get("razon", "")

    # 3. Comparar con trimestre anterior
    anterior = obtener_score_anterior(simbolo, quarter)
    tendencia = "N/D"
    if anterior:
        diff = score - anterior["score"]
        if diff > 10:
            tendencia = f"MEJORA (+{diff} vs {anterior['quarter']})"
        elif diff < -10:
            tendencia = f"DETERIORO ({diff} vs {anterior['quarter']})"
        else:
            tendencia = f"ESTABLE ({diff:+d} vs {anterior['quarter']})"

    # 4. Guardar en ChromaDB
    guardar_score(simbolo, quarter, score, senal, razon)

    return {
        "simbolo": simbolo,
        "quarter": quarter,
        "score": score,
        "senal": senal,
        "razon": razon,
        "tendencia": tendencia,
        "score_anterior": anterior,
        "palabras_confianza": analisis.get("palabras_confianza", []),
        "palabras_incertidumbre": analisis.get("palabras_incertidumbre", []),
        "sentimiento_av": analisis.get("sentimiento_av", 0),
        "total_speakers": len(trans.get("speakers", [])),
    }


def get_tono_ejecutivos(simbolos, quarter=None):
    """Analiza earnings calls de múltiples activos."""
    resultados = {}
    for sym in simbolos:
        resultados[sym] = analizar_earnings_call(sym, quarter)
    return resultados


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 70)
    print(f"  EARNINGS CALLS NLP — Análisis de tono ejecutivo")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 70)

    test_symbols = ["XOM", "JNJ", "META"]
    print(f"  Buscando trimestre más reciente con datos...")

    for sym in test_symbols:
        print(f"\n{'─' * 60}")

        resultado = analizar_earnings_call(sym)  # auto-detect quarter

        quarter = resultado.get("quarter", "?")
        print(f"  {sym} — Earnings Call {quarter}")
        print(f"{'─' * 60}")

        if resultado.get("error"):
            print(f"  Error: {resultado['error']}")
            continue

        score = resultado["score"]
        senal = resultado["senal"]
        barra = "+" * max(0, score // 5) if score >= 0 else "-" * max(0, abs(score) // 5)

        print(f"  Score: {score:+d}/100  [{barra}]")
        print(f"  Señal: {senal}")
        print(f"  Razón: {resultado.get('razon', 'N/D')}")
        print(f"  Tendencia: {resultado['tendencia']}")
        print(f"  Sentimiento AV: {resultado.get('sentimiento_av', 0):.3f}")

        if resultado.get("palabras_confianza"):
            print(f"  Confianza: {', '.join(resultado['palabras_confianza'][:5])}")
        if resultado.get("palabras_incertidumbre"):
            print(f"  Incertidumbre: {', '.join(resultado['palabras_incertidumbre'][:5])}")

        if resultado.get("score_anterior"):
            prev = resultado["score_anterior"]
            print(f"  Anterior ({prev['quarter']}): score {prev['score']:+d} → {prev['senal']}")

    print(f"\n{'=' * 70}")
