#!/home/hproano/asistente_env/bin/python
"""
EODHD — Sentimiento de noticias, precios real-time, noticias con polarity.

Endpoints disponibles (plan gratuito):
  - /sentiments: score de sentimiento diario por activo
  - /real-time: precio actual con 52w high/low
  - /news: noticias con sentiment polarity score
"""

import os
import logging
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

EODHD_KEY = os.getenv("EODHD_API_KEY", "")
EODHD_BASE = "https://eodhd.com/api"

log = logging.getLogger("eodhd")


def _eodhd_get(endpoint, params=None):
    if not EODHD_KEY:
        return {"error": "EODHD_API_KEY no configurada"}
    if params is None:
        params = {}
    params["api_token"] = EODHD_KEY
    params["fmt"] = "json"
    try:
        resp = requests.get(f"{EODHD_BASE}/{endpoint}", params=params, timeout=15)
        if "Forbidden" in resp.text:
            return {"error": "Endpoint premium"}
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)[:80]}


# ══════════════════════════════════════════════════════════════
#  1. SENTIMIENTO DIARIO
# ══════════════════════════════════════════════════════════════

def get_sentiment(simbolo, dias=14):
    """Sentimiento diario de noticias. Score normalizado 0-1 (>0.5 = positivo)."""
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    data = _eodhd_get("sentiments", {"s": f"{simbolo}.US", "from": desde})

    if isinstance(data, dict) and "error" in data:
        return {"senal": "N/D", "error": data["error"]}

    key = f"{simbolo}.US"
    entries = data.get(key, []) if isinstance(data, dict) else []
    if not entries:
        return {"senal": "N/D", "score": 0, "dias_datos": 0}

    scores = [e["normalized"] for e in entries if "normalized" in e]
    counts = [e.get("count", 0) for e in entries]

    if not scores:
        return {"senal": "N/D", "score": 0}

    avg = sum(scores) / len(scores)
    ultimo = scores[0] if scores else 0
    total_noticias = sum(counts)

    # Tendencia: comparar última semana vs anterior
    reciente = scores[:7]
    anterior = scores[7:]
    avg_reciente = sum(reciente) / len(reciente) if reciente else 0.5
    avg_anterior = sum(anterior) / len(anterior) if anterior else 0.5
    cambio = avg_reciente - avg_anterior

    if avg > 0.65:
        senal = "POSITIVO"
    elif avg < 0.35:
        senal = "NEGATIVO"
    else:
        senal = "NEUTRAL"

    return {
        "senal": senal,
        "score_promedio": round(avg, 4),
        "score_hoy": round(ultimo, 4),
        "total_noticias": total_noticias,
        "dias_datos": len(scores),
        "cambio_semanal": round(cambio, 4),
        "tendencia": "MEJORANDO" if cambio > 0.05 else "EMPEORANDO" if cambio < -0.05 else "ESTABLE",
    }


# ══════════════════════════════════════════════════════════════
#  2. PRECIO REAL-TIME + SEÑAL 52W
# ══════════════════════════════════════════════════════════════

def get_realtime_52w(simbolo):
    """Precio actual vs 52-week high/low. Si cerca de mínimos → oportunidad."""
    data = _eodhd_get(f"real-time/{simbolo}.US")

    if isinstance(data, dict) and "error" in data:
        return {"senal": "N/D", "error": data["error"]}

    precio = data.get("close", 0)
    high_52 = data.get("high", 0)
    low_52 = data.get("low", 0)
    prev = data.get("previousClose", 0)
    vol = data.get("volume", 0)

    if not precio or precio <= 0:
        return {"senal": "N/D"}

    # Posición en rango 52w (necesitamos datos diarios para 52w real)
    # Usamos open/close del día como proxy
    var_dia = ((precio / prev) - 1) * 100 if prev > 0 else 0

    return {
        "precio": round(precio, 2),
        "variacion_dia": round(var_dia, 2),
        "volumen": vol,
        "high_dia": round(high_52, 2),
        "low_dia": round(low_52, 2),
    }


# ══════════════════════════════════════════════════════════════
#  3. NOTICIAS CON SENTIMIENTO
# ══════════════════════════════════════════════════════════════

def get_news_sentiment(simbolo, limite=5):
    """Noticias recientes con score de sentimiento por artículo."""
    data = _eodhd_get("news", {"s": f"{simbolo}.US", "limit": str(limite)})

    if isinstance(data, dict) and "error" in data:
        return {"noticias": [], "error": data["error"]}
    if not isinstance(data, list):
        return {"noticias": []}

    noticias = []
    scores = []
    for n in data[:limite]:
        sent = n.get("sentiment", {})
        polarity = sent.get("polarity", 0)
        if polarity:
            try:
                polarity = float(polarity)
                scores.append(polarity)
            except (ValueError, TypeError):
                polarity = 0

        noticias.append({
            "titulo": n.get("title", "")[:100],
            "fecha": n.get("date", "")[:16],
            "polarity": round(polarity, 3) if polarity else 0,
            "fuente": n.get("link", "")[:50],
        })

    avg_polarity = sum(scores) / len(scores) if scores else 0

    return {
        "noticias": noticias,
        "polarity_promedio": round(avg_polarity, 3),
        "total": len(noticias),
    }


# ══════════════════════════════════════════════════════════════
#  4. SEÑAL COMBINADA EODHD
# ══════════════════════════════════════════════════════════════

def get_eodhd_signal(simbolo):
    """Combina sentimiento + noticias + precio en una señal."""
    sent = get_sentiment(simbolo)
    news = get_news_sentiment(simbolo, limite=5)
    rt = get_realtime_52w(simbolo)

    score = 0
    notas = []

    # Sentimiento
    if sent.get("senal") == "POSITIVO":
        score += 1
        notas.append(f"Sent: {sent['senal']} ({sent['score_promedio']:.2f})")
    elif sent.get("senal") == "NEGATIVO":
        score -= 1
        notas.append(f"Sent: {sent['senal']} ({sent['score_promedio']:.2f})")

    # Tendencia semanal
    if sent.get("tendencia") == "MEJORANDO":
        score += 1
        notas.append(f"Tendencia mejorando ({sent['cambio_semanal']:+.3f})")
    elif sent.get("tendencia") == "EMPEORANDO":
        score -= 1
        notas.append(f"Tendencia empeorando ({sent['cambio_semanal']:+.3f})")

    # News polarity
    pol = news.get("polarity_promedio", 0)
    if pol > 0.7:
        notas.append(f"Noticias muy positivas ({pol:.2f})")
    elif pol < 0.3 and pol > 0:
        notas.append(f"Noticias negativas ({pol:.2f})")

    if score >= 2:
        senal = "ALCISTA"
    elif score <= -2:
        senal = "BAJISTA"
    elif score >= 1:
        senal = "ALCISTA_LEVE"
    elif score <= -1:
        senal = "BAJISTA_LEVE"
    else:
        senal = "NEUTRAL"

    return {
        "simbolo": simbolo,
        "senal": senal,
        "score": score,
        "notas": notas,
        "sentimiento": sent,
        "noticias": news,
        "precio_rt": rt,
    }


def get_eodhd_signals_batch(simbolos):
    """Evalúa múltiples activos."""
    return {sym: get_eodhd_signal(sym) for sym in simbolos}


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 80)
    print(f"  EODHD GLOBAL — Sentimiento + Noticias + Real-time")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    test = ["EEM", "EFA", "XOM", "META", "IBM", "JNJ"]

    hdr = f"  {'Sym':<6} {'Señal':<14} {'Sent':>6} {'Hoy':>6} {'Cambio':>7} {'Tend':<12} {'News':>5} {'Polarity':>8}"
    print(hdr)
    print(f"  {'─' * 70}")

    for sym in test:
        r = get_eodhd_signal(sym)
        s = r["sentimiento"]
        n = r["noticias"]

        sent_s = f"{s.get('score_promedio',0):.3f}" if s.get("score_promedio") else "N/D"
        hoy_s = f"{s.get('score_hoy',0):.3f}" if s.get("score_hoy") else "N/D"
        cambio_s = f"{s.get('cambio_semanal',0):+.3f}" if s.get("cambio_semanal") is not None else "N/D"
        tend = s.get("tendencia", "N/D")
        news_n = f"{n.get('total',0)}"
        pol = f"{n.get('polarity_promedio',0):.3f}"

        print(f"  {sym:<6} {r['senal']:<14} {sent_s:>6} {hoy_s:>6} {cambio_s:>7} {tend:<12} {news_n:>5} {pol:>8}")

        # Top noticias
        for art in n.get("noticias", [])[:2]:
            print(f"    [{art['polarity']:.2f}] {art['titulo'][:65]}")

    print(f"\n{'=' * 80}")
