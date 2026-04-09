#!/home/hproano/asistente_env/bin/python
"""
Fuentes de datos adicionales para JARVIS.
Integra: FRED (macro), Finnhub (noticias/sentimiento), Alpaca WebSocket (precios RT).
Combina todo con contexto_mercado.py en get_contexto_enriquecido().
"""

import os
import sys
import json
import time
import threading
import importlib.util
from datetime import datetime, timedelta, date
from collections import defaultdict

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Importar config dinámicamente ──────────────────────────────
_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

# ── API Keys ───────────────────────────────────────────────────
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_PREMIUM_KEY = os.getenv("FINNHUB_PREMIUM_KEY", "")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_PREMIUM_KEY", "")
TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "")
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")


# ================================================================
#  1. FRED API — Datos macroeconómicos de la Reserva Federal
# ================================================================

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

def _fred_ultima_obs(serie_id):
    """Obtiene la observación más reciente de una serie FRED."""
    if not FRED_API_KEY:
        return {"valor": None, "fecha": None, "error": "FRED_API_KEY no configurada"}
    params = {
        "series_id": serie_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,
    }
    try:
        resp = requests.get(FRED_BASE, params=params, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        for o in obs:
            if o["value"] != ".":
                return {"valor": float(o["value"]), "fecha": o["date"]}
        return {"valor": None, "fecha": None, "error": "Sin datos recientes"}
    except Exception as e:
        return {"valor": None, "fecha": None, "error": str(e)}


def get_datos_macro_fed():
    """
    Obtiene indicadores macro de la Reserva Federal vía FRED API:
      - FEDFUNDS: tasa de interés federal funds rate
      - CPIAUCSL: índice de precios al consumidor (inflación)
      - UNRATE: tasa de desempleo
    Retorna dict con los 3 indicadores + interpretación.
    """
    series = {
        "fed_funds_rate": {"id": "FEDFUNDS", "nombre": "Tasa Fed Funds"},
        "cpi": {"id": "CPIAUCSL", "nombre": "CPI (Inflación)"},
        "desempleo": {"id": "UNRATE", "nombre": "Tasa Desempleo"},
    }

    resultado = {}
    for clave, info in series.items():
        obs = _fred_ultima_obs(info["id"])
        resultado[clave] = {
            "nombre": info["nombre"],
            "serie": info["id"],
            "valor": obs.get("valor"),
            "fecha": obs.get("fecha"),
            "error": obs.get("error"),
        }

    # Interpretación
    ff = resultado["fed_funds_rate"]["valor"]
    cpi = resultado["cpi"]["valor"]
    desemp = resultado["desempleo"]["valor"]

    notas = []
    if ff is not None:
        if ff >= 5.0:
            notas.append(f"Fed Funds en {ff}%: política monetaria restrictiva, presión bajista sobre acciones.")
        elif ff >= 3.0:
            notas.append(f"Fed Funds en {ff}%: tasas moderadamente altas.")
        else:
            notas.append(f"Fed Funds en {ff}%: política acomodaticia, favorable para renta variable.")

    if desemp is not None:
        if desemp >= 6.0:
            notas.append(f"Desempleo {desemp}%: mercado laboral débil, posible recesión.")
        elif desemp <= 4.0:
            notas.append(f"Desempleo {desemp}%: mercado laboral fuerte.")
        else:
            notas.append(f"Desempleo {desemp}%: nivel moderado.")

    resultado["interpretacion"] = " ".join(notas) if notas else "Datos macro no disponibles."
    return resultado


# ================================================================
#  2. FINNHUB API — Noticias, sentimiento y earnings
# ================================================================

FINNHUB_BASE = "https://finnhub.io/api/v1"

def _finnhub_get(endpoint, params=None):
    """Helper para llamadas a Finnhub."""
    if not FINNHUB_API_KEY:
        return {"error": "FINNHUB_API_KEY no configurada"}
    if params is None:
        params = {}
    params["token"] = FINNHUB_API_KEY
    try:
        resp = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def get_finnhub_datos(simbolo):
    """
    Obtiene datos de Finnhub para un símbolo:
      - Noticias recientes (últimos 3 días)
      - Sentimiento de analistas (recomendaciones buy/hold/sell)
      - Earnings próximos
    """
    hoy = date.today()
    hace_3d = hoy - timedelta(days=3)

    # Noticias
    noticias_raw = _finnhub_get("company-news", {
        "symbol": simbolo,
        "from": hace_3d.strftime("%Y-%m-%d"),
        "to": hoy.strftime("%Y-%m-%d"),
    })
    noticias = []
    if isinstance(noticias_raw, list):
        for art in noticias_raw[:5]:
            noticias.append({
                "titulo": art.get("headline", ""),
                "resumen": (art.get("summary") or "")[:180].strip(),
                "fuente": art.get("source", ""),
                "fecha": datetime.fromtimestamp(art.get("datetime", 0)).strftime("%Y-%m-%d %H:%M"),
                "sentimiento": art.get("sentiment"),
            })

    # Recomendaciones de analistas
    recs_raw = _finnhub_get("stock/recommendation", {"symbol": simbolo})
    recomendacion = {}
    if isinstance(recs_raw, list) and recs_raw:
        r = recs_raw[0]  # Más reciente
        total = r.get("buy", 0) + r.get("hold", 0) + r.get("sell", 0) + r.get("strongBuy", 0) + r.get("strongSell", 0)
        recomendacion = {
            "periodo": r.get("period", ""),
            "strong_buy": r.get("strongBuy", 0),
            "buy": r.get("buy", 0),
            "hold": r.get("hold", 0),
            "sell": r.get("sell", 0),
            "strong_sell": r.get("strongSell", 0),
            "total_analistas": total,
        }
        if total > 0:
            bullish = r.get("strongBuy", 0) + r.get("buy", 0)
            bearish = r.get("sell", 0) + r.get("strongSell", 0)
            if bullish > total * 0.6:
                recomendacion["sesgo"] = "Alcista"
            elif bearish > total * 0.4:
                recomendacion["sesgo"] = "Bajista"
            else:
                recomendacion["sesgo"] = "Neutral"

    # Earnings próximos
    earnings_raw = _finnhub_get("stock/earnings", {"symbol": simbolo, "limit": 1})
    proximo_earnings = {}
    if isinstance(earnings_raw, list) and earnings_raw:
        e = earnings_raw[0]
        proximo_earnings = {
            "periodo": e.get("period", ""),
            "eps_actual": e.get("actual"),
            "eps_estimado": e.get("estimate"),
            "sorpresa": e.get("surprise"),
            "sorpresa_pct": e.get("surprisePercent"),
        }

    return {
        "simbolo": simbolo,
        "noticias": noticias,
        "recomendacion": recomendacion,
        "ultimo_earnings": proximo_earnings,
    }


def get_finnhub_earnings_calendario(dias=7):
    """Calendario de earnings de Finnhub para los próximos N días."""
    hoy = date.today()
    limite = hoy + timedelta(days=dias)
    data = _finnhub_get("calendar/earnings", {
        "from": hoy.strftime("%Y-%m-%d"),
        "to": limite.strftime("%Y-%m-%d"),
    })
    if isinstance(data, dict) and "error" not in data:
        earnings = data.get("earningsCalendar", [])
        # Filtrar solo los que nos interesan
        relevantes = [e for e in earnings if e.get("symbol") in ACTIVOS_OPERABLES]
        return relevantes
    return data if isinstance(data, dict) and "error" in data else []


# ================================================================
#  2b. FINNHUB PREMIUM — Insider, opciones, institucional
# ================================================================

def _finnhub_premium_get(endpoint, params=None):
    """Helper para llamadas con la key premium de Finnhub."""
    key = FINNHUB_PREMIUM_KEY or FINNHUB_API_KEY
    if not key:
        return {"error": "FINNHUB_PREMIUM_KEY no configurada"}
    if params is None:
        params = {}
    params["token"] = key
    try:
        resp = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def get_insider_transactions(simbolo, meses=3):
    """
    Transacciones insider: quién compra/vende acciones dentro de la empresa.
    Retorna resumen con compras/ventas recientes y señal.
    """
    hoy = date.today()
    desde = hoy - timedelta(days=meses * 30)
    raw = _finnhub_premium_get("stock/insider-transactions", {
        "symbol": simbolo,
        "from": desde.strftime("%Y-%m-%d"),
        "to": hoy.strftime("%Y-%m-%d"),
    })

    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "transacciones": [], "senal": "N/D"}

    datos = raw.get("data", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []

    compras_total = 0
    ventas_total = 0
    transacciones = []
    for t in datos[:20]:
        nombre = t.get("name", "")
        tipo = t.get("transactionType", "")
        shares = t.get("share", 0) or 0
        precio = t.get("transactionPrice") or 0
        fecha = t.get("filingDate", "")

        es_compra = tipo in ("P - Purchase", "P-Purchase", "A-Award") or "purchase" in tipo.lower()
        es_venta = tipo in ("S - Sale", "S-Sale", "S - Sale+OE") or "sale" in tipo.lower()

        if es_compra:
            compras_total += abs(shares)
        elif es_venta:
            ventas_total += abs(shares)

        transacciones.append({
            "nombre": nombre,
            "tipo": tipo,
            "acciones": shares,
            "precio": precio,
            "fecha": fecha,
            "es_compra": es_compra,
        })

    # Señal
    if compras_total > 0 and compras_total > ventas_total * 0.5:
        senal = "ALCISTA"
        nota = f"Insiders comprando: {compras_total:,.0f} acc compradas vs {ventas_total:,.0f} vendidas"
    elif ventas_total > compras_total * 3:
        senal = "BAJISTA"
        nota = f"Insiders vendiendo fuerte: {ventas_total:,.0f} acc vendidas vs {compras_total:,.0f} compradas"
    else:
        senal = "NEUTRAL"
        nota = f"Actividad insider normal: {compras_total:,.0f} compras, {ventas_total:,.0f} ventas"

    return {
        "transacciones": transacciones[:10],
        "compras_total": compras_total,
        "ventas_total": ventas_total,
        "senal": senal,
        "nota": nota,
    }


def get_opciones_inusuales(simbolo):
    """
    Detecta flujo inusual de opciones analizando la cadena de opciones.
    Compara volumen de calls vs puts y volumen vs open interest.
    """
    raw = _finnhub_premium_get("stock/option-chain", {"symbol": simbolo})

    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "senal": "N/D"}

    cadena = raw.get("data", []) if isinstance(raw, dict) else []
    if not cadena:
        return {"senal": "N/D", "nota": "Sin datos de opciones"}

    total_call_vol = 0
    total_put_vol = 0
    total_call_oi = 0
    total_put_oi = 0
    inusuales = []

    for expiry in cadena[:3]:  # Primeras 3 expiraciones
        opciones = expiry.get("options", {})
        for tipo_key, tipo_label in [("CALL", "CALL"), ("PUT", "PUT")]:
            for opt in opciones.get(tipo_key, []):
                vol = opt.get("volume", 0) or 0
                oi = opt.get("openInterest", 0) or 0
                strike = opt.get("strike", 0)

                if tipo_key == "CALL":
                    total_call_vol += vol
                    total_call_oi += oi
                else:
                    total_put_vol += vol
                    total_put_oi += oi

                # Actividad inusual: volumen > 3x open interest
                if oi > 0 and vol > oi * 3 and vol > 100:
                    inusuales.append({
                        "tipo": tipo_label,
                        "strike": strike,
                        "volumen": vol,
                        "open_interest": oi,
                        "ratio": round(vol / oi, 1),
                        "expiracion": expiry.get("expirationDate", ""),
                    })

    total_vol = total_call_vol + total_put_vol
    put_call_ratio = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else 0

    if inusuales:
        calls_inusuales = sum(1 for i in inusuales if i["tipo"] == "CALL")
        puts_inusuales = sum(1 for i in inusuales if i["tipo"] == "PUT")
        if calls_inusuales > puts_inusuales:
            senal = "ALCISTA"
            nota = f"Flujo inusual en {len(inusuales)} opciones (mayoría CALLS). P/C ratio: {put_call_ratio}"
        elif puts_inusuales > calls_inusuales:
            senal = "BAJISTA"
            nota = f"Flujo inusual en {len(inusuales)} opciones (mayoría PUTS). P/C ratio: {put_call_ratio}"
        else:
            senal = "ALERTA"
            nota = f"Flujo inusual mixto en {len(inusuales)} opciones. P/C ratio: {put_call_ratio}"
    elif put_call_ratio > 1.5:
        senal = "BAJISTA"
        nota = f"Put/Call ratio alto: {put_call_ratio} — sesgo bajista en opciones"
    elif put_call_ratio < 0.5 and total_vol > 0:
        senal = "ALCISTA"
        nota = f"Put/Call ratio bajo: {put_call_ratio} — sesgo alcista en opciones"
    else:
        senal = "NEUTRAL"
        nota = f"Flujo de opciones normal. P/C ratio: {put_call_ratio}"

    return {
        "call_volume": total_call_vol,
        "put_volume": total_put_vol,
        "put_call_ratio": put_call_ratio,
        "inusuales": inusuales[:5],
        "senal": senal,
        "nota": nota,
    }


def get_ownership_institucional(simbolo):
    """
    Ownership institucional: qué fondos tienen cada acción y cambios recientes.
    """
    raw = _finnhub_premium_get("institutional-ownership", {
        "symbol": simbolo,
        "limit": 10,
    })

    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "senal": "N/D"}

    ownership = raw.get("ownership", []) if isinstance(raw, dict) else []
    if not ownership:
        # Intentar endpoint alternativo
        raw2 = _finnhub_premium_get("stock/ownership", {
            "symbol": simbolo,
            "limit": 10,
        })
        if isinstance(raw2, list):
            ownership = raw2
        elif isinstance(raw2, dict):
            ownership = raw2.get("ownership", [])

    if not ownership:
        return {"fondos": [], "senal": "N/D", "nota": "Sin datos de ownership"}

    fondos = []
    total_cambio = 0
    for o in ownership[:10]:
        nombre = o.get("name", o.get("ownerName", ""))
        acciones = o.get("share", o.get("noShares", 0)) or 0
        cambio = o.get("change", o.get("shareChange", 0)) or 0
        pct = o.get("percentage", o.get("putCallShare", 0)) or 0
        total_cambio += cambio

        fondos.append({
            "nombre": nombre,
            "acciones": acciones,
            "cambio": cambio,
            "porcentaje": round(pct * 100, 2) if pct < 1 else round(pct, 2),
        })

    if total_cambio > 0:
        senal = "ALCISTA"
        nota = f"Institucionales aumentando posición (cambio neto: +{total_cambio:,.0f} acc)"
    elif total_cambio < 0:
        senal = "BAJISTA"
        nota = f"Institucionales reduciendo posición (cambio neto: {total_cambio:,.0f} acc)"
    else:
        senal = "NEUTRAL"
        nota = "Sin cambios significativos en ownership institucional"

    return {
        "fondos": fondos,
        "cambio_neto": total_cambio,
        "senal": senal,
        "nota": nota,
    }


def get_senales_institucionales(simbolo):
    """
    Combina insider + opciones + ownership en un resumen de señales institucionales.
    """
    insider = get_insider_transactions(simbolo)
    opciones = get_opciones_inusuales(simbolo)
    ownership = get_ownership_institucional(simbolo)

    senales_alcistas = 0
    senales_bajistas = 0
    detalles = []

    for fuente, datos in [("Insider", insider), ("Opciones", opciones), ("Institucional", ownership)]:
        senal = datos.get("senal", "N/D")
        nota = datos.get("nota", datos.get("error", ""))
        if senal == "ALCISTA":
            senales_alcistas += 1
        elif senal in ("BAJISTA",):
            senales_bajistas += 1
        detalles.append(f"{fuente}: {senal} — {nota}")

    if senales_alcistas >= 2:
        senal_general = "ALCISTA FUERTE"
    elif senales_alcistas > senales_bajistas:
        senal_general = "ALCISTA"
    elif senales_bajistas >= 2:
        senal_general = "BAJISTA FUERTE"
    elif senales_bajistas > senales_alcistas:
        senal_general = "BAJISTA"
    else:
        senal_general = "NEUTRAL"

    return {
        "simbolo": simbolo,
        "senal_general": senal_general,
        "insider": insider,
        "opciones": opciones,
        "ownership": ownership,
        "detalles": detalles,
    }


# ================================================================
#  2c. ALPHA VANTAGE PREMIUM — Earnings, sentimiento, económicos
# ================================================================

AV_BASE = "https://www.alphavantage.co/query"


def _av_get(function, params=None):
    """Helper para llamadas a Alpha Vantage."""
    if not ALPHA_VANTAGE_KEY:
        return {"error": "ALPHA_VANTAGE_PREMIUM_KEY no configurada"}
    if params is None:
        params = {}
    params["function"] = function
    params["apikey"] = ALPHA_VANTAGE_KEY
    try:
        resp = requests.get(AV_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "Error Message" in data:
            return {"error": data["Error Message"]}
        if "Note" in data:
            return {"error": f"Rate limit: {data['Note'][:80]}"}
        return data
    except Exception as e:
        return {"error": str(e)}


def get_av_earnings(simbolo):
    """
    Earnings con sorpresas históricas: EPS actual vs estimado (últimos 4 trimestres).
    """
    raw = _av_get("EARNINGS", {"symbol": simbolo})
    if "error" in raw:
        return {"error": raw["error"], "trimestres": []}

    quarterly = raw.get("quarterlyEarnings", [])[:4]
    trimestres = []
    sorpresas_pct = []

    for q in quarterly:
        reported = q.get("reportedEPS")
        estimated = q.get("estimatedEPS")
        surprise = q.get("surprise")
        surprise_pct = q.get("surprisePercentage")

        try:
            reported_f = float(reported) if reported and reported != "None" else None
        except (ValueError, TypeError):
            reported_f = None
        try:
            estimated_f = float(estimated) if estimated and estimated != "None" else None
        except (ValueError, TypeError):
            estimated_f = None
        try:
            surprise_pct_f = float(surprise_pct) if surprise_pct and surprise_pct != "None" else None
        except (ValueError, TypeError):
            surprise_pct_f = None

        if surprise_pct_f is not None:
            sorpresas_pct.append(surprise_pct_f)

        trimestres.append({
            "fecha": q.get("fiscalDateEnding", ""),
            "reportado": q.get("reportedDate", ""),
            "eps_actual": reported_f,
            "eps_estimado": estimated_f,
            "sorpresa": surprise,
            "sorpresa_pct": surprise_pct_f,
        })

    # Tendencia de sorpresas
    beats = sum(1 for s in sorpresas_pct if s and s > 0)
    misses = sum(1 for s in sorpresas_pct if s and s < 0)
    avg_surprise = sum(s for s in sorpresas_pct if s) / len(sorpresas_pct) if sorpresas_pct else 0

    if beats >= 3:
        tendencia = f"Supera estimados consistentemente ({beats}/4 trimestres, promedio +{avg_surprise:.1f}%)"
    elif misses >= 3:
        tendencia = f"Decepciona consistentemente ({misses}/4 misses, promedio {avg_surprise:.1f}%)"
    else:
        tendencia = f"Mixto: {beats} beats, {misses} misses en 4 trimestres"

    return {
        "trimestres": trimestres,
        "tendencia": tendencia,
        "beats": beats,
        "misses": misses,
        "promedio_sorpresa_pct": round(avg_surprise, 2),
    }


def get_av_sentimiento_noticias(simbolo, limite=10):
    """
    Sentimiento de noticias con score IA (-1 a +1) de Alpha Vantage.
    """
    raw = _av_get("NEWS_SENTIMENT", {"tickers": simbolo, "limit": str(limite)})
    if "error" in raw:
        return {"error": raw["error"], "noticias": [], "score_promedio": 0}

    feed = raw.get("feed", [])
    noticias = []
    scores = []

    for art in feed[:limite]:
        # Buscar el score específico del ticker
        ticker_sentiment = {}
        for ts in art.get("ticker_sentiment", []):
            if ts.get("ticker", "").upper() == simbolo.upper():
                ticker_sentiment = ts
                break

        score = None
        label = ""
        if ticker_sentiment:
            try:
                score = float(ticker_sentiment.get("ticker_sentiment_score", 0))
            except (ValueError, TypeError):
                score = None
            label = ticker_sentiment.get("ticker_sentiment_label", "")

        if score is not None:
            scores.append(score)

        # Sentimiento general del artículo
        overall_score = None
        try:
            overall_score = float(art.get("overall_sentiment_score", 0))
        except (ValueError, TypeError):
            pass

        noticias.append({
            "titulo": art.get("title", "")[:120],
            "fuente": art.get("source", ""),
            "fecha": art.get("time_published", "")[:16],
            "score_ticker": score,
            "label_ticker": label,
            "score_general": overall_score,
        })

    score_promedio = round(sum(scores) / len(scores), 4) if scores else 0

    if score_promedio >= 0.25:
        sentimiento = "Positivo"
    elif score_promedio >= 0.05:
        sentimiento = "Ligeramente positivo"
    elif score_promedio <= -0.25:
        sentimiento = "Negativo"
    elif score_promedio <= -0.05:
        sentimiento = "Ligeramente negativo"
    else:
        sentimiento = "Neutral"

    return {
        "noticias": noticias,
        "score_promedio": score_promedio,
        "sentimiento": sentimiento,
        "total_analizadas": len(scores),
    }


def get_av_indicadores_economicos():
    """
    Indicadores económicos de Alpha Vantage: GDP, inflación, desempleo.
    Más completo que FRED con datos trimestrales/anuales.
    """
    resultado = {}

    # GDP real
    gdp_raw = _av_get("REAL_GDP", {"interval": "quarterly"})
    if "error" not in gdp_raw:
        datos = gdp_raw.get("data", [])[:4]
        resultado["gdp"] = {
            "nombre": "PIB Real (trimestral)",
            "datos": [{"fecha": d["date"], "valor": d["value"]} for d in datos],
        }
        if len(datos) >= 2:
            try:
                actual = float(datos[0]["value"])
                anterior = float(datos[1]["value"])
                cambio_pct = ((actual - anterior) / anterior) * 100
                resultado["gdp"]["cambio_pct"] = round(cambio_pct, 2)
                resultado["gdp"]["valor_actual"] = actual
            except (ValueError, TypeError, ZeroDivisionError):
                pass
    else:
        resultado["gdp"] = {"error": gdp_raw["error"]}

    # Inflación (CPI anual)
    inf_raw = _av_get("INFLATION")
    if "error" not in inf_raw:
        datos = inf_raw.get("data", [])[:3]
        resultado["inflacion"] = {
            "nombre": "Inflación anual (%)",
            "datos": [{"fecha": d["date"], "valor": d["value"]} for d in datos],
        }
        if datos:
            try:
                resultado["inflacion"]["valor_actual"] = float(datos[0]["value"])
            except (ValueError, TypeError):
                pass
    else:
        resultado["inflacion"] = {"error": inf_raw["error"]}

    # Desempleo
    desemp_raw = _av_get("UNEMPLOYMENT")
    if "error" not in desemp_raw:
        datos = desemp_raw.get("data", [])[:3]
        resultado["desempleo"] = {
            "nombre": "Tasa de desempleo (%)",
            "datos": [{"fecha": d["date"], "valor": d["value"]} for d in datos],
        }
        if datos:
            try:
                resultado["desempleo"]["valor_actual"] = float(datos[0]["value"])
            except (ValueError, TypeError):
                pass
    else:
        resultado["desempleo"] = {"error": desemp_raw["error"]}

    # Interpretación
    notas = []
    gdp_cambio = resultado.get("gdp", {}).get("cambio_pct")
    if gdp_cambio is not None:
        if gdp_cambio > 2:
            notas.append(f"PIB creciendo {gdp_cambio:+.1f}%: expansión económica.")
        elif gdp_cambio > 0:
            notas.append(f"PIB creciendo {gdp_cambio:+.1f}%: crecimiento moderado.")
        else:
            notas.append(f"PIB {gdp_cambio:+.1f}%: contracción, riesgo de recesión.")

    inf_val = resultado.get("inflacion", {}).get("valor_actual")
    if inf_val is not None:
        if inf_val > 4:
            notas.append(f"Inflación {inf_val:.1f}%: alta, presión sobre tasas.")
        elif inf_val > 2:
            notas.append(f"Inflación {inf_val:.1f}%: moderada.")
        else:
            notas.append(f"Inflación {inf_val:.1f}%: controlada.")

    resultado["interpretacion"] = " ".join(notas) if notas else "Datos económicos no disponibles."
    return resultado


# ================================================================
#  2d. TIINGO PREMIUM — Históricos, intraday, noticias
# ================================================================

TIINGO_BASE = "https://api.tiingo.com"
TIINGO_IEX_BASE = "https://api.tiingo.com/iex"
TIINGO_NEWS_BASE = "https://api.tiingo.com/tiingo/news"

TIINGO_HEADERS = {}


def _tiingo_headers():
    """Construye headers con token de Tiingo (lazy init)."""
    if not TIINGO_HEADERS:
        TIINGO_HEADERS["Authorization"] = f"Token {TIINGO_API_KEY}"
        TIINGO_HEADERS["Content-Type"] = "application/json"
    return TIINGO_HEADERS


def _tiingo_get(url, params=None):
    """Helper para llamadas a Tiingo."""
    if not TIINGO_API_KEY:
        return {"error": "TIINGO_API_KEY no configurada"}
    try:
        resp = requests.get(url, headers=_tiingo_headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def get_tiingo_historico(simbolo, anios=30):
    """
    Precios históricos de alta calidad (hasta 30+ años) para backtesting.
    Retorna datos diarios: fecha, open, high, low, close, volume, adjClose.
    """
    hoy = date.today()
    inicio = date(hoy.year - anios, hoy.month, hoy.day)
    url = f"{TIINGO_BASE}/tiingo/daily/{simbolo}/prices"
    raw = _tiingo_get(url, {
        "startDate": inicio.strftime("%Y-%m-%d"),
        "endDate": hoy.strftime("%Y-%m-%d"),
    })
    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "datos": [], "total_dias": 0}
    if not isinstance(raw, list):
        return {"error": "Respuesta inesperada", "datos": [], "total_dias": 0}

    datos = []
    for d in raw:
        datos.append({
            "fecha": d.get("date", "")[:10],
            "open": d.get("adjOpen") or d.get("open"),
            "high": d.get("adjHigh") or d.get("high"),
            "low": d.get("adjLow") or d.get("low"),
            "close": d.get("adjClose") or d.get("close"),
            "volume": d.get("adjVolume") or d.get("volume"),
        })

    # Estadísticas
    total = len(datos)
    primer_fecha = datos[0]["fecha"] if datos else "N/D"
    ultima_fecha = datos[-1]["fecha"] if datos else "N/D"
    anios_reales = total / 252 if total > 0 else 0

    return {
        "datos": datos,
        "total_dias": total,
        "primer_fecha": primer_fecha,
        "ultima_fecha": ultima_fecha,
        "anios_aprox": round(anios_reales, 1),
    }


def get_tiingo_intraday(simbolo, intervalo="1min"):
    """
    Datos intraday de Tiingo IEX para señales más precisas.
    Intervalos: 1min, 5min, 15min, 30min, 1hour.
    Retorna las últimas barras del día.
    """
    url = f"{TIINGO_IEX_BASE}/{simbolo}/prices"
    params = {
        "resampleFreq": intervalo,
        "columns": "open,high,low,close,volume",
    }
    raw = _tiingo_get(url, params)

    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "barras": []}
    if not isinstance(raw, list):
        return {"error": "Sin datos intraday", "barras": []}

    barras = []
    for b in raw:
        barras.append({
            "fecha": b.get("date", "")[:19],
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
            "volume": b.get("volume"),
        })

    # Calcular variación intraday
    var_pct = 0
    if len(barras) >= 2:
        primer_open = barras[0].get("open") or 0
        ultimo_close = barras[-1].get("close") or 0
        if primer_open > 0:
            var_pct = ((ultimo_close - primer_open) / primer_open) * 100

    return {
        "barras": barras,
        "total_barras": len(barras),
        "intervalo": intervalo,
        "variacion_intraday_pct": round(var_pct, 2),
        "ultimo_precio": barras[-1].get("close") if barras else None,
    }


def get_tiingo_noticias(simbolo, limite=5):
    """
    Noticias con sentimiento de Tiingo para un símbolo.
    """
    raw = _tiingo_get(TIINGO_NEWS_BASE, {
        "tickers": simbolo,
        "limit": limite,
        "sortBy": "crawlDate",
    })

    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"], "noticias": []}
    if not isinstance(raw, list):
        return {"error": "Sin noticias", "noticias": []}

    noticias = []
    for art in raw[:limite]:
        noticias.append({
            "titulo": art.get("title", "")[:120],
            "fuente": art.get("source", ""),
            "fecha": art.get("publishedDate", "")[:16],
            "url": art.get("url", ""),
            "tickers": art.get("tickers", []),
            "tags": art.get("tags", []),
        })

    return {
        "noticias": noticias,
        "total": len(noticias),
    }


# ================================================================
#  3. ALPACA WEBSOCKET — Monitor de precios en tiempo real
# ================================================================

# Almacena último precio y timestamp por símbolo
_precios_rt = {}
_precios_lock = threading.Lock()
_ws_callbacks = []
_ws_thread = None
_ws_running = False


def _on_precio_cambio(simbolo, precio_anterior, precio_actual, pct_cambio, segundos):
    """Callback interno cuando se detecta cambio > 1% en < 5 min."""
    for cb in _ws_callbacks:
        try:
            cb(simbolo, precio_anterior, precio_actual, pct_cambio, segundos)
        except Exception as e:
            print(f"  [WS] Error en callback: {e}")


def _default_callback(simbolo, precio_ant, precio_act, pct, segs):
    """Callback por defecto: imprime alerta en consola."""
    direccion = "SUBE" if pct > 0 else "BAJA"
    print(f"  [ALERTA RT] {simbolo} {direccion} {abs(pct):.2f}% en {segs:.0f}s "
          f"(${precio_ant:.2f} -> ${precio_act:.2f})")


def start_websocket_monitor(callback=None, umbral_pct=1.0, ventana_seg=300):
    """
    Inicia monitor de precios en tiempo real vía Alpaca WebSocket.
    - callback(simbolo, precio_ant, precio_act, pct_cambio, segundos)
    - umbral_pct: porcentaje mínimo de cambio para disparar alerta (default 1%)
    - ventana_seg: ventana de tiempo en segundos (default 300 = 5 min)

    Requiere: pip install websocket-client
    """
    global _ws_thread, _ws_running

    if callback:
        _ws_callbacks.append(callback)
    elif not _ws_callbacks:
        _ws_callbacks.append(_default_callback)

    if not ALPACA_KEY or not ALPACA_SECRET:
        print("  [WS] Error: ALPACA_API_KEY / ALPACA_SECRET_KEY no configuradas")
        return False

    if _ws_running:
        print("  [WS] Monitor ya está corriendo")
        return True

    try:
        import websocket
    except ImportError:
        print("  [WS] Instalando websocket-client...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client", "-q"])
        import websocket

    def _ws_worker():
        global _ws_running
        _ws_running = True
        url = "wss://stream.data.alpaca.markets/v2/iex"

        def on_open(ws):
            auth = {"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}
            ws.send(json.dumps(auth))
            subs = {"action": "subscribe", "trades": ACTIVOS_OPERABLES}
            ws.send(json.dumps(subs))
            print(f"  [WS] Conectado. Monitoreando {len(ACTIVOS_OPERABLES)} activos...")

        def on_message(ws, message):
            datos = json.loads(message)
            if not isinstance(datos, list):
                return
            for msg in datos:
                if msg.get("T") != "t":  # Solo trades
                    continue
                sym = msg.get("S")
                precio = msg.get("p")
                ts = time.time()

                if not sym or not precio:
                    continue

                with _precios_lock:
                    if sym in _precios_rt:
                        anterior = _precios_rt[sym]
                        elapsed = ts - anterior["ts"]
                        if elapsed <= ventana_seg and anterior["precio"] > 0:
                            pct = ((precio - anterior["precio"]) / anterior["precio"]) * 100
                            if abs(pct) >= umbral_pct:
                                _on_precio_cambio(sym, anterior["precio"], precio, pct, elapsed)
                                _precios_rt[sym] = {"precio": precio, "ts": ts}
                        elif elapsed > ventana_seg:
                            _precios_rt[sym] = {"precio": precio, "ts": ts}
                    else:
                        _precios_rt[sym] = {"precio": precio, "ts": ts}

        def on_error(ws, error):
            print(f"  [WS] Error: {error}")

        def on_close(ws, close_code, close_msg):
            global _ws_running
            _ws_running = False
            print(f"  [WS] Desconectado (code={close_code})")

        ws = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever()
        _ws_running = False

    _ws_thread = threading.Thread(target=_ws_worker, daemon=True)
    _ws_thread.start()
    return True


def get_precios_realtime():
    """Retorna snapshot de precios en tiempo real del WebSocket."""
    with _precios_lock:
        return {s: {"precio": d["precio"], "hace_seg": time.time() - d["ts"]}
                for s, d in _precios_rt.items()}


def stop_websocket_monitor():
    """Detiene el monitor WebSocket."""
    global _ws_running
    _ws_running = False


# ================================================================
#  4. INTEGRACIÓN — Contexto enriquecido
# ================================================================

def get_contexto_enriquecido():
    """
    Combina todas las fuentes con contexto_mercado.py existente.
    Retorna (texto, datos) compatible con el formato de get_contexto_completo().
    """
    # Importar contexto_mercado existente
    _ctx_spec = importlib.util.spec_from_file_location(
        "contexto_mercado", os.path.join(PROYECTO, "datos", "contexto_mercado.py"))
    _ctx = importlib.util.module_from_spec(_ctx_spec)
    _ctx_spec.loader.exec_module(_ctx)

    # Obtener contexto base
    texto_base, datos_base = _ctx.get_contexto_completo()

    # 1. FRED macro
    macro = get_datos_macro_fed()

    # 2. Finnhub — sentimiento de los top 5 activos por prioridad
    top_activos = ACTIVOS_OPERABLES[:5]
    finnhub_datos = {}
    for sym in top_activos:
        finnhub_datos[sym] = get_finnhub_datos(sym)

    # Calendario de earnings Finnhub
    earnings_fh = get_finnhub_earnings_calendario()

    # 3. Señales institucionales (top 5 activos)
    senales_inst = {}
    for sym in top_activos:
        senales_inst[sym] = get_senales_institucionales(sym)

    # 4. Alpha Vantage — sentimiento IA y earnings (top 3 para no agotar rate limit)
    av_sentimiento = {}
    av_earnings = {}
    for sym in top_activos[:3]:
        av_sentimiento[sym] = get_av_sentimiento_noticias(sym, limite=5)
        av_earnings[sym] = get_av_earnings(sym)

    # 5. Alpha Vantage — indicadores económicos
    av_economicos = get_av_indicadores_economicos()

    # 6. Precios en tiempo real (si WS está activo)
    precios_rt = get_precios_realtime()

    # 7. Régimen de mercado
    try:
        from datos.regimen_mercado import get_regimen_actual
        regimen = get_regimen_actual()
    except Exception:
        regimen = {"regimen": "LATERAL", "confianza": 0, "razon": "No disponible",
                   "nota": "", "activos_permitidos": list(ACTIVOS_OPERABLES),
                   "max_posiciones": 8, "umbral_compra": 3}

    # 8. SEC EDGAR — posiciones institucionales (top 5)
    sec_datos = {}
    try:
        from datos.sec_edgar import get_posiciones_institucionales
        for sym in top_activos:
            sec_datos[sym] = get_posiciones_institucionales(sym)
    except Exception:
        pass

    # 9. Google Trends — señal contrarian (con cache 2h para evitar 429)
    google_trends = {}
    _gt_cache_path = "/tmp/google_trends_cache.json"
    _gt_cache_ttl = 7200  # 2 horas
    try:
        from datos.google_trends import get_tendencias_mercado
        google_trends = get_tendencias_mercado()
        # Guardar cache exitoso
        import json as _json_gt
        with open(_gt_cache_path, "w") as _f:
            _json_gt.dump({"ts": __import__("time").time(), "data": google_trends}, _f)
    except Exception:
        # Intentar cache anterior
        try:
            import json as _json_gt
            with open(_gt_cache_path) as _f:
                _cached = _json_gt.load(_f)
            if __import__("time").time() - _cached["ts"] < _gt_cache_ttl:
                google_trends = _cached["data"]
            else:
                google_trends = {"senal": "NEUTRAL", "nota": "Cache expirado", "panico_score": 0, "euforia_score": 0}
        except Exception:
            google_trends = {"senal": "NEUTRAL", "nota": "No disponible", "panico_score": 0, "euforia_score": 0}

    # 10. Señales quant (Momentum 12-1, RSI semanal, Golden/Death Cross)
    senales_quant = {}
    try:
        from datos.quantconnect_estrategias import get_senales_quant
        senales_quant = get_senales_quant(top_activos)
    except Exception:
        pass

    # 11. Tono ejecutivos (earnings calls NLP)
    tono_ejecutivos = {}
    try:
        from datos.earnings_calls_nlp import get_tono_ejecutivos
        tono_ejecutivos = get_tono_ejecutivos(top_activos[:3])
    except Exception:
        pass

    # 12. Wikipedia signals (atención inusual)
    wiki_signals = {}
    try:
        from datos.wikipedia_signals import get_wikipedia_signals
        wiki_signals = get_wikipedia_signals(top_activos)
    except Exception:
        pass

    # 13. Señales sociales (Reddit + Earnings estimates + SEC actividad)
    senales_sociales = {}
    try:
        from datos.senales_sociales import get_senales_sociales_batch
        senales_sociales = get_senales_sociales_batch(top_activos[:3])
    except Exception:
        pass

    # 14. AIS marítimo — señales de tráfico marítimo
    ais_signal = {}
    try:
        from datos.ais_maritimo import get_ais_signal
        ais_signal = get_ais_signal()
    except Exception:
        ais_signal = {"trafico_petroleo": "N/D", "trafico_carga": "N/D",
                      "canal_panama": "N/D", "confianza": 0}

    # 15. Multi-timeframe — 5 horizontes (top 5 activos)
    multi_tf = {}
    try:
        from datos.multi_timeframe import get_señal_multitimeframe
        for sym in top_activos:
            multi_tf[sym] = get_señal_multitimeframe(sym)
    except Exception:
        pass

    # 19. NASDAQ Data + Fundamentales
    fundamentales = {}
    try:
        from datos.nasdaq_data import get_fundamentals_batch
        fundamentales = get_fundamentals_batch(top_activos[:3])
    except Exception:
        pass

    # 20. FMP Valuation (DCF + Price Target)
    fmp_valuation = {}
    try:
        from datos.fmp_data import get_fmp_signals_batch
        fmp_valuation = get_fmp_signals_batch(top_activos[:5])
    except Exception:
        pass

    # 21. EODHD Global (Sentimiento + Noticias)
    eodhd_data = {}
    try:
        from datos.eodhd_data import get_eodhd_signals_batch
        eodhd_data = get_eodhd_signals_batch(top_activos[:5])
    except Exception:
        pass

    # ── Construir texto enriquecido ──
    L = [texto_base.rstrip("=\n ")]

    # Macro FRED
    L.append(f"\n\n  --- DATOS MACRO (FRED / Reserva Federal) ---")
    for clave in ["fed_funds_rate", "cpi", "desempleo"]:
        d = macro[clave]
        if d["valor"] is not None:
            L.append(f"  {d['nombre']}: {d['valor']} (dato: {d['fecha']})")
        else:
            err = d.get("error", "N/D")
            L.append(f"  {d['nombre']}: No disponible ({err})")
    L.append(f"  >> {macro['interpretacion']}")

    # Alpha Vantage económicos
    if av_economicos.get("interpretacion") and "no disponibles" not in av_economicos["interpretacion"]:
        L.append(f"\n  --- INDICADORES ECONÓMICOS (Alpha Vantage) ---")
        for clave in ["gdp", "inflacion", "desempleo"]:
            d = av_economicos.get(clave, {})
            if "error" not in d and d.get("valor_actual") is not None:
                L.append(f"  {d.get('nombre', clave)}: {d['valor_actual']}")
        L.append(f"  >> {av_economicos['interpretacion']}")

    # Finnhub sentimiento analistas
    L.append(f"\n  --- SENTIMIENTO ANALISTAS (Finnhub, top 5) ---")
    for sym in top_activos:
        fd = finnhub_datos.get(sym, {})
        rec = fd.get("recomendacion", {})
        if rec and rec.get("total_analistas", 0) > 0:
            L.append(f"  {sym}: {rec.get('sesgo', 'N/D')} "
                     f"(Buy:{rec.get('strong_buy',0)+rec.get('buy',0)} "
                     f"Hold:{rec.get('hold',0)} "
                     f"Sell:{rec.get('sell',0)+rec.get('strong_sell',0)} "
                     f"| {rec.get('total_analistas',0)} analistas)")
        else:
            L.append(f"  {sym}: Sin datos de analistas")

    # Sentimiento IA (Alpha Vantage)
    tiene_av_sent = any(s.get("total_analizadas", 0) > 0 for s in av_sentimiento.values())
    if tiene_av_sent:
        L.append(f"\n  --- SENTIMIENTO NOTICIAS IA (Alpha Vantage) ---")
        for sym in top_activos[:3]:
            s = av_sentimiento.get(sym, {})
            if s.get("total_analizadas", 0) > 0:
                L.append(f"  {sym}: {s['sentimiento']} (score: {s['score_promedio']:+.3f}, "
                         f"{s['total_analizadas']} noticias)")

    # Earnings sorpresas (Alpha Vantage)
    tiene_av_earn = any(e.get("trimestres") for e in av_earnings.values())
    if tiene_av_earn:
        L.append(f"\n  --- EARNINGS SORPRESAS (Alpha Vantage, últimos 4Q) ---")
        for sym in top_activos[:3]:
            e = av_earnings.get(sym, {})
            if e.get("tendencia"):
                L.append(f"  {sym}: {e['tendencia']}")

    # SEÑALES INSTITUCIONALES
    L.append(f"\n  --- SEÑALES INSTITUCIONALES (Finnhub Premium) ---")
    for sym in top_activos:
        si = senales_inst.get(sym, {})
        senal_gen = si.get("senal_general", "N/D")
        L.append(f"  {sym}: {senal_gen}")
        for detalle in si.get("detalles", []):
            L.append(f"    {detalle}")

    # Noticias Finnhub (solo si hay)
    tiene_noticias = any(fd.get("noticias") for fd in finnhub_datos.values())
    if tiene_noticias:
        L.append(f"\n  --- NOTICIAS FINNHUB (complementarias) ---")
        for sym in top_activos:
            arts = finnhub_datos.get(sym, {}).get("noticias", [])
            if arts:
                L.append(f"  {sym}:")
                for a in arts[:2]:
                    L.append(f"    - [{a['fecha']}] {a['titulo'][:100]}")

    # Régimen de mercado
    reg_icono = {"BULL": ">>", "BEAR": "<<", "LATERAL": "=="}.get(regimen["regimen"], "??")
    L.append(f"\n  --- RÉGIMEN DE MERCADO ---")
    L.append(f"  {reg_icono} {regimen['regimen']} (confianza {regimen.get('confianza', 0)}/3)")
    L.append(f"  {regimen.get('razon', '')}")
    L.append(f"  {regimen.get('nota', '')}")
    if regimen["regimen"] == "BEAR":
        L.append(f"  Activos permitidos: {', '.join(regimen.get('activos_permitidos', []))}")

    # SEC EDGAR
    if sec_datos:
        L.append(f"\n  --- POSICIONES INSTITUCIONALES (SEC 13F) ---")
        for sym in top_activos:
            sd = sec_datos.get(sym, {})
            n = sd.get("fondos_con_posicion", 0)
            total = sd.get("total_fondos", 3)
            if n > 0:
                L.append(f"  {sym}: {sd.get('senal', '?')} ({n}/{total} fondos)")
            else:
                L.append(f"  {sym}: Sin posición en los fondos rastreados")

    # Google Trends contrarian
    if google_trends.get("senal") and google_trends["senal"] != "N/D":
        L.append(f"\n  --- GOOGLE TRENDS (señal contrarian) ---")
        L.append(f"  Señal: {google_trends['senal']} — {google_trends.get('nota', '')}")
        L.append(f"  Pánico retail: {google_trends.get('panico_score', 0)}/100 | "
                 f"Euforia retail: {google_trends.get('euforia_score', 0)}/100 | "
                 f"Cripto interés: {google_trends.get('cripto_score', 0)}/100")

    # Señales quant
    if senales_quant:
        L.append(f"\n  --- SEÑALES QUANT (Momentum + RSI semanal + Cross) ---")
        for sym in top_activos:
            sq = senales_quant.get(sym, {})
            comb = sq.get("combinada", "N/D")
            mom = sq.get("momentum", {}).get("retorno_pct")
            rsi = sq.get("rsi_semanal", {}).get("rsi")
            cross = sq.get("cross", {}).get("tipo_cruce", "")
            mom_s = f"Mom:{mom:+.0f}%" if mom is not None else ""
            rsi_s = f"RSI:{rsi:.0f}" if rsi is not None else ""
            L.append(f"  {sym}: {comb} ({mom_s} {rsi_s} {cross})")

    # Tono ejecutivos
    if tono_ejecutivos:
        L.append(f"\n  --- TONO EJECUTIVOS (Earnings Calls NLP) ---")
        for sym in top_activos[:3]:
            te = tono_ejecutivos.get(sym, {})
            if te.get("error"):
                continue
            score = te.get("score", 0)
            senal = te.get("senal", "N/D")
            tend = te.get("tendencia", "")
            L.append(f"  {sym}: score {score:+d} → {senal} | {tend}")

    # Wikipedia traffic
    if wiki_signals:
        alertas_wiki = {s: d for s, d in wiki_signals.items() if d.get("senal") in ("ALERTA", "FUERTE")}
        if alertas_wiki:
            L.append(f"\n  --- WIKIPEDIA TRAFFIC (atención inusual) ---")
            for sym, ws in alertas_wiki.items():
                L.append(f"  {sym}: {ws['senal']} — {ws['vistas_hoy']:,} vistas "
                         f"({ws['ratio_hoy']:.1f}x promedio, {ws['vs_promedio']})")
        else:
            L.append(f"\n  --- WIKIPEDIA TRAFFIC ---")
            L.append(f"  Sin atención inusual. Todos los activos en niveles normales.")

    # Reddit sentiment + Earnings estimates + SEC actividad
    if senales_sociales:
        L.append(f"\n  --- REDDIT SENTIMENT + EARNINGS + SEC ---")
        for sym in top_activos[:3]:
            ss = senales_sociales.get(sym, {})
            if not ss:
                continue
            rd = ss.get("reddit", {})
            ee = ss.get("earnings_est", {})
            sec = ss.get("sec_actividad", {})
            partes = [f"  {sym}:"]
            rd_senal = rd.get("senal", "SIN_DATOS")
            partes.append(f"Reddit {rd_senal} ({rd.get('menciones', 0)} menciones)")
            if ee.get("senal") not in (None, "N/D"):
                partes.append(f"Earnings {ee['senal']} ({ee.get('beats',0)}/{ee.get('total',0)} beats)")
            if sec.get("senal") not in (None, "N/D"):
                partes.append(f"SEC {sec['senal']} ({sec.get('filings_90d',0)} filings)")
            L.append(" | ".join(partes))

    # Fundamentales NASDAQ (trimestral)
    if fundamentales:
        L.append(f"\n  --- FUNDAMENTALES TRIMESTRALES (NASDAQ + Alpha Vantage) ---")
        for sym in top_activos[:3]:
            fd = fundamentales.get(sym, {})
            if fd.get("error") or fd.get("senal") == "N/D":
                continue
            pe_s = f"P/E:{fd['pe']:.0f}" if fd.get("pe") else ""
            rg_s = f"RevGr:{fd['revenue_growth_yoy']:+.0f}%" if fd.get("revenue_growth_yoy") is not None else ""
            roe_s = f"ROE:{fd['roe']:.0f}%" if fd.get("roe") is not None else ""
            t = fd.get("tendencia", {})
            trend = ""
            if t.get("rev_creciendo_consecutivo", 0) >= 2:
                trend += f" RevMom:{t['rev_creciendo_consecutivo']}Q"
            if t.get("pe_comprimiendo_consecutivo", 0) >= 2:
                trend += f" PEcomp:{t['pe_comprimiendo_consecutivo']}Q"
            L.append(f"  {sym}: {fd['senal']} ({pe_s} {rg_s} {roe_s}{trend})")

    # FMP Valuation
    if fmp_valuation:
        L.append(f"\n  --- FMP VALUATION (DCF + Price Target) ---")
        for sym in top_activos[:5]:
            fv = fmp_valuation.get(sym, {})
            if fv.get("senal") and fv["senal"] != "N/D":
                dcf_d = fv.get("dcf", {})
                dcf_s = f"DCF:${dcf_d['dcf']:.0f}" if dcf_d.get("dcf") else ""
                up_s = f"upside:{dcf_d['upside_pct']:+.0f}%" if dcf_d.get("upside_pct") is not None else ""
                L.append(f"  {sym}: {fv['senal']} (score {fv['score']:+d}) {dcf_s} {up_s}")

    # EODHD Global
    eodhd_alertas = {s: d for s, d in eodhd_data.items()
                     if d.get("senal") and d["senal"] not in ("NEUTRAL", "N/D")}
    if eodhd_alertas:
        L.append(f"\n  --- EODHD SENTIMIENTO GLOBAL ---")
        for sym, ed in eodhd_alertas.items():
            sent = ed.get("sentimiento", {})
            L.append(f"  {sym}: {ed['senal']} (sent:{sent.get('score_promedio',0):.2f} "
                     f"tend:{sent.get('tendencia','?')})")

    # AIS Marítimo
    if ais_signal.get("trafico_petroleo") and ais_signal["trafico_petroleo"] != "N/D":
        L.append(f"\n  --- TRÁFICO MARÍTIMO AIS (señal commodities) ---")
        L.append(f"  Petróleo: {ais_signal['trafico_petroleo']} | "
                 f"Carga: {ais_signal['trafico_carga']} | "
                 f"Canal Panamá: {ais_signal['canal_panama']}")
        L.append(f"  Confianza: {ais_signal.get('confianza', 0)}/3 "
                 f"(fuentes: {', '.join(ais_signal.get('fuentes', []))})")
        for activo in ["xom", "xle", "gld", "spy"]:
            clave = f"señal_{activo}"
            if clave in ais_signal:
                L.append(f"  {activo.upper()}: {ais_signal[clave]}")

    # Multi-Timeframe
    if multi_tf:
        L.append(f"\n  --- MULTI-TIMEFRAME (5 horizontes, top 5) ---")
        L.append(f"  {'SYM':<6} {'1h':>3} {'1d':>3} {'1w':>3} {'1m':>3} {'6m':>3}  CONSENSO")
        for sym in top_activos:
            mt = multi_tf.get(sym, {})
            if mt and mt.get("consenso") != "N/D":
                marca = " **" if mt.get("señal_fuerte") else ""
                L.append(f"  {sym:<6} {mt.get('1h','?'):>3} {mt.get('1d','?'):>3} "
                         f"{mt.get('1w','?'):>3} {mt.get('1m','?'):>3} {mt.get('6m','?'):>3}  "
                         f"{mt['consenso']} ({mt.get('fuerza',0)}/5){marca}")

    # Precios RT
    if precios_rt:
        L.append(f"\n  --- PRECIOS TIEMPO REAL (WebSocket) ---")
        for sym in ACTIVOS_OPERABLES:
            if sym in precios_rt:
                p = precios_rt[sym]
                L.append(f"  {sym}: ${p['precio']:.2f} (hace {p['hace_seg']:.0f}s)")

    L.append(f"\n{'=' * 60}")

    texto = "\n".join(L)

    # ── Datos combinados ──
    datos_base["macro_fed"] = macro
    datos_base["finnhub_sentimiento"] = finnhub_datos
    datos_base["finnhub_earnings_calendario"] = earnings_fh
    datos_base["senales_institucionales"] = senales_inst
    datos_base["av_sentimiento"] = av_sentimiento
    datos_base["av_earnings"] = av_earnings
    datos_base["av_economicos"] = av_economicos
    datos_base["precios_realtime"] = precios_rt
    datos_base["regimen_mercado"] = regimen
    datos_base["sec_13f"] = sec_datos
    datos_base["google_trends"] = google_trends
    datos_base["senales_quant"] = senales_quant
    datos_base["tono_ejecutivos"] = tono_ejecutivos
    datos_base["wikipedia_signals"] = wiki_signals
    datos_base["senales_sociales"] = senales_sociales
    datos_base["ais_maritimo"] = ais_signal
    datos_base["multi_timeframe"] = multi_tf
    datos_base["fundamentales"] = fundamentales
    datos_base["fmp_valuation"] = fmp_valuation
    datos_base["eodhd"] = eodhd_data

    return texto, datos_base


# ================================================================
#  5. CLI — Prueba con datos reales
# ================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  FUENTES DE MERCADO — Test completo (XOM, META)")
    print("=" * 60)

    TEST_SYMBOLS = ["XOM", "META"]

    # ── Test 1: FRED ──
    print("\n" + "-" * 50)
    print("  [1/6] FRED API — Datos macroeconómicos")
    print("-" * 50)
    macro = get_datos_macro_fed()
    for clave in ["fed_funds_rate", "cpi", "desempleo"]:
        d = macro[clave]
        if d["valor"] is not None:
            print(f"  {d['nombre']}: {d['valor']} (fecha: {d['fecha']})")
        else:
            print(f"  {d['nombre']}: ERROR — {d.get('error', 'N/D')}")
    print(f"  >> {macro['interpretacion']}")

    # ── Test 2: Finnhub base ──
    print("\n" + "-" * 50)
    print("  [2/6] FINNHUB — Noticias y analistas")
    print("-" * 50)
    for sym in TEST_SYMBOLS:
        print(f"\n  --- {sym} ---")
        fd = get_finnhub_datos(sym)
        rec = fd.get("recomendacion", {})
        if rec and rec.get("total_analistas", 0) > 0:
            print(f"  Analistas ({rec['total_analistas']}): "
                  f"SBuy={rec['strong_buy']} Buy={rec['buy']} "
                  f"Hold={rec['hold']} Sell={rec['sell']} SSell={rec['strong_sell']} "
                  f"=> {rec.get('sesgo', 'N/D')}")
        noticias = fd.get("noticias", [])
        if noticias:
            for n in noticias[:2]:
                print(f"  [{n['fecha']}] {n['titulo'][:80]}")

    # ── Test 3: Finnhub Premium — Señales institucionales ──
    print("\n" + "-" * 50)
    print("  [3/6] FINNHUB PREMIUM — Insider + Opciones + Institucional")
    print("-" * 50)
    for sym in TEST_SYMBOLS:
        print(f"\n  === {sym} ===")
        si = get_senales_institucionales(sym)
        print(f"  SEÑAL GENERAL: {si['senal_general']}")
        for detalle in si.get("detalles", []):
            print(f"    {detalle}")

        # Detalle insider
        ins = si.get("insider", {})
        txs = ins.get("transacciones", [])
        if txs:
            print(f"  Insider transacciones recientes ({len(txs)}):")
            for t in txs[:3]:
                tipo = "COMPRA" if t.get("es_compra") else "VENTA"
                print(f"    {t['fecha']} {t['nombre'][:30]}: {tipo} {abs(t['acciones']):,.0f} acc"
                      f" @ ${t['precio']:,.2f}" if t['precio'] else "")

        # Detalle opciones
        opc = si.get("opciones", {})
        if opc.get("put_call_ratio"):
            print(f"  Opciones: P/C ratio={opc['put_call_ratio']} "
                  f"Call vol={opc.get('call_volume',0):,} Put vol={opc.get('put_volume',0):,}")
        inusuales = opc.get("inusuales", [])
        if inusuales:
            print(f"  Flujo inusual ({len(inusuales)}):")
            for i in inusuales[:3]:
                print(f"    {i['tipo']} strike={i['strike']} vol={i['volumen']:,} "
                      f"OI={i['open_interest']:,} ratio={i['ratio']}x exp={i['expiracion']}")

        # Detalle ownership
        own = si.get("ownership", {})
        fondos = own.get("fondos", [])
        if fondos:
            print(f"  Top fondos institucionales:")
            for f in fondos[:3]:
                cambio = f"cambio: {f['cambio']:+,.0f}" if f.get("cambio") else ""
                print(f"    {f['nombre'][:35]}: {f['acciones']:,.0f} acc ({f['porcentaje']:.1f}%) {cambio}")

    # ── Test 4: Alpha Vantage Premium ──
    print("\n" + "-" * 50)
    print("  [4/6] ALPHA VANTAGE — Earnings + Sentimiento IA + Económicos")
    print("-" * 50)
    for sym in TEST_SYMBOLS:
        print(f"\n  === {sym} ===")

        # Earnings sorpresas
        earn = get_av_earnings(sym)
        if earn.get("trimestres"):
            print(f"  Earnings (últimos 4Q): {earn['tendencia']}")
            for q in earn["trimestres"][:2]:
                sorpr = f" sorpresa: {q['sorpresa_pct']:+.1f}%" if q.get("sorpresa_pct") else ""
                print(f"    {q['fecha']}: EPS actual={q['eps_actual']} est={q['eps_estimado']}{sorpr}")
        elif earn.get("error"):
            print(f"  Earnings: {earn['error']}")

        # Sentimiento IA
        sent = get_av_sentimiento_noticias(sym, limite=5)
        if sent.get("total_analizadas", 0) > 0:
            print(f"  Sentimiento IA: {sent['sentimiento']} (score: {sent['score_promedio']:+.3f}, "
                  f"{sent['total_analizadas']} noticias)")
            for n in sent.get("noticias", [])[:2]:
                score = f" [{n['score_ticker']:+.3f}]" if n.get("score_ticker") is not None else ""
                print(f"    {n['titulo'][:70]}{score}")
        elif sent.get("error"):
            print(f"  Sentimiento: {sent['error']}")

    # Indicadores económicos
    print(f"\n  --- Indicadores económicos ---")
    av_eco = get_av_indicadores_economicos()
    for clave in ["gdp", "inflacion", "desempleo"]:
        d = av_eco.get(clave, {})
        if "error" not in d and d.get("valor_actual") is not None:
            extra = f" (cambio: {d['cambio_pct']:+.1f}%)" if d.get("cambio_pct") else ""
            print(f"  {d.get('nombre', clave)}: {d['valor_actual']}{extra}")
        elif "error" in d:
            print(f"  {clave}: {d['error'][:60]}")
    print(f"  >> {av_eco.get('interpretacion', 'N/D')}")

    # ── Test 5: Tiingo Premium ──
    print("\n" + "-" * 50)
    print("  [5/7] TIINGO PREMIUM — Históricos + Intraday + Noticias")
    print("-" * 50)
    for sym in TEST_SYMBOLS:
        print(f"\n  === {sym} ===")

        # Históricos
        hist = get_tiingo_historico(sym, anios=30)
        if hist.get("total_dias"):
            print(f"  Histórico: {hist['total_dias']:,} días ({hist['anios_aprox']} años) "
                  f"desde {hist['primer_fecha']} hasta {hist['ultima_fecha']}")
            if hist["datos"]:
                ultimo = hist["datos"][-1]
                print(f"  Último: {ultimo['fecha']} close=${ultimo['close']:.2f} vol={ultimo['volume']:,.0f}")
        else:
            print(f"  Histórico: {hist.get('error', 'N/D')}")

        # Intraday
        intra = get_tiingo_intraday(sym, intervalo="1min")
        if intra.get("total_barras", 0) > 0:
            print(f"  Intraday 1min: {intra['total_barras']} barras, "
                  f"var={intra['variacion_intraday_pct']:+.2f}%, "
                  f"último=${intra['ultimo_precio']:.2f}")
        else:
            print(f"  Intraday: {intra.get('error', 'sin datos (mercado cerrado)')}")

        # Noticias
        news = get_tiingo_noticias(sym, limite=3)
        if news.get("noticias"):
            print(f"  Noticias Tiingo ({news['total']}):")
            for n in news["noticias"][:2]:
                print(f"    [{n['fecha']}] {n['titulo'][:70]}")
        else:
            print(f"  Noticias: {news.get('error', 'ninguna')}")

    # ── Test 6: WebSocket ──
    print("\n" + "-" * 50)
    print("  [6/7] ALPACA WEBSOCKET")
    print("-" * 50)
    if ALPACA_KEY and ALPACA_SECRET:
        ok = start_websocket_monitor()
        if ok:
            print(f"  Escuchando 10 segundos...")
            time.sleep(10)
            precios = get_precios_realtime()
            if precios:
                for sym in TEST_SYMBOLS:
                    if sym in precios:
                        p = precios[sym]
                        print(f"  {sym}: ${p['precio']:.2f} (hace {p['hace_seg']:.0f}s)")
            else:
                print("  Sin precios (mercado cerrado).")
            stop_websocket_monitor()
    else:
        print("  Saltando (keys no configuradas).")

    # ── Test 7: Contexto enriquecido completo ──
    print("\n" + "=" * 60)
    print("  [7/7] CONTEXTO ENRIQUECIDO COMPLETO")
    print("=" * 60)
    texto, datos = get_contexto_enriquecido()
    print(texto)

    # Guardar JSON
    dir_datos = os.path.join(PROYECTO, "datos")
    ruta = os.path.join(dir_datos, f"fuentes_{datetime.now().strftime('%Y-%m-%d')}.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  JSON guardado en: {ruta}")
