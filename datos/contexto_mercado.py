#!/home/hproano/asistente_env/bin/python
"""
Contexto de mercado en tiempo real para JARVIS.
Fuentes: Alpaca News, Fear & Greed Index, VIX, Calendario de Earnings.
"""

import os
import sys
import json
import importlib.util
from datetime import datetime, timedelta, date

import requests
import yfinance as yf
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

HEADERS_ALPACA = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


# ── 1. Noticias (Alpaca News API) ───────────────────────────

def obtener_noticias(simbolos=None, limite_por_activo=3):
    """Últimas noticias de Alpaca para los activos operables."""
    if simbolos is None:
        simbolos = ACTIVOS_OPERABLES

    tickers = [s for s in simbolos if "-" not in s]
    noticias = {}
    vistas = set()

    batch_size = 6
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        params = {
            "symbols": ",".join(batch),
            "limit": limite_por_activo * len(batch),
            "sort": "desc",
        }
        try:
            resp = requests.get(ALPACA_NEWS_URL, headers=HEADERS_ALPACA,
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            for s in batch:
                noticias[s] = [{"error": str(e)}]
            continue

        for art in data.get("news", []):
            aid = art["id"]
            if aid in vistas:
                continue
            vistas.add(aid)
            for sym in art.get("symbols", []):
                if sym in tickers:
                    noticias.setdefault(sym, [])
                    if len(noticias[sym]) < limite_por_activo:
                        noticias[sym].append({
                            "titulo": art["headline"],
                            "resumen": (art.get("summary") or "")[:180].strip(),
                            "fuente": art.get("source", ""),
                            "fecha": art["created_at"][:16].replace("T", " "),
                        })

    return noticias


# ── 2. Fear & Greed Index ────────────────────────────────────

def obtener_fear_greed():
    """Fear & Greed Index de Alternative.me (cripto-basado, proxy de sentimiento)."""
    try:
        resp = requests.get(FNG_URL, timeout=10)
        resp.raise_for_status()
        d = resp.json()["data"][0]
        valor = int(d["value"])
        clas_en = d["value_classification"]

        trad = {
            "Extreme Fear": "Miedo Extremo", "Fear": "Miedo",
            "Neutral": "Neutral", "Greed": "Codicia",
            "Extreme Greed": "Codicia Extrema",
        }
        clas = trad.get(clas_en, clas_en)

        if valor <= 20:
            nota = "Mercado en pánico. Históricamente, zona de oportunidad de compra."
        elif valor <= 40:
            nota = "Sentimiento temeroso. Cautela pero posibles oportunidades."
        elif valor <= 60:
            nota = "Sentimiento neutral. Sin sesgo claro."
        elif valor <= 80:
            nota = "Sentimiento codicioso. Precaución con compras nuevas."
        else:
            nota = "Euforia extrema. Alto riesgo de corrección."

        return {"valor": valor, "clasificacion": clas, "nota": nota}
    except Exception as e:
        return {"valor": None, "clasificacion": "N/D", "nota": str(e)}


# ── 3. VIX ───────────────────────────────────────────────────

def obtener_vix():
    """VIX actual de yfinance con clasificación."""
    try:
        info = yf.Ticker("^VIX").fast_info
        precio = info["lastPrice"]
        prev = info["previousClose"]
        var = ((precio / prev) - 1) * 100 if prev else 0

        if precio < 15:
            nivel, nota = "Bajo", "Mercado tranquilo, baja volatilidad esperada."
        elif precio < 20:
            nivel, nota = "Normal", "Volatilidad normal. Condiciones estándar."
        elif precio < 25:
            nivel, nota = "Elevado", "Volatilidad alta. Precaución con posiciones grandes."
        elif precio < 30:
            nivel, nota = "Alto", "Mercado nervioso. Stops más amplios recomendados."
        else:
            nivel, nota = "Muy Alto", "Volatilidad extrema. Considerar reducir exposición."

        return {"precio": round(precio, 2), "variacion": round(var, 2),
                "nivel": nivel, "nota": nota}
    except Exception as e:
        return {"precio": None, "variacion": 0, "nivel": "N/D", "nota": str(e)}


# ── 4. Calendario de Earnings ────────────────────────────────

def obtener_earnings(simbolos=None, dias_adelante=7):
    """
    Revisa si algún activo del portafolio reporta earnings en los próximos días.
    Retorna lista de {simbolo, fecha, dias_faltan, estimado_eps}.
    """
    if simbolos is None:
        simbolos = ACTIVOS_OPERABLES

    hoy = date.today()
    limite = hoy + timedelta(days=dias_adelante)
    proximos = []

    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    for s in simbolos:
        try:
            cal = yf.Ticker(s).calendar
            if not cal:
                continue
        except Exception:
            continue

        fechas_er = cal.get("Earnings Date", [])
        if not fechas_er:
            continue

        for f in fechas_er:
            if isinstance(f, datetime):
                f = f.date()
            if hoy <= f <= limite:
                eps_est = cal.get("Earnings Average")
                rev_est = cal.get("Revenue Average")
                proximos.append({
                    "simbolo": s,
                    "fecha": f.strftime("%Y-%m-%d"),
                    "dias_faltan": (f - hoy).days,
                    "eps_estimado": round(eps_est, 2) if eps_est else None,
                    "revenue_est_M": round(rev_est / 1e6) if rev_est else None,
                })

    proximos.sort(key=lambda x: x["dias_faltan"])
    return proximos


# ── Contexto completo ───────────────────────────────────────

def get_contexto_completo():
    """
    Recopila las 4 fuentes y devuelve:
      - texto: resumen en español listo para JARVIS
      - datos: dict con datos crudos para JSON
    """
    noticias = obtener_noticias()
    fng = obtener_fear_greed()
    vix = obtener_vix()
    earnings = obtener_earnings()

    L = []
    L.append("=" * 60)
    L.append(f"  CONTEXTO DE MERCADO — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    L.append("=" * 60)

    # Fear & Greed
    L.append(f"\n  --- SENTIMIENTO: FEAR & GREED INDEX ---")
    L.append(f"  Valor: {fng['valor']}/100 — {fng['clasificacion']}")
    L.append(f"  {fng['nota']}")

    # VIX
    L.append(f"\n  --- VOLATILIDAD: VIX ---")
    if vix["precio"]:
        L.append(f"  VIX: {vix['precio']} ({vix['variacion']:+.2f}%) — {vix['nivel']}")
        L.append(f"  {vix['nota']}")
    else:
        L.append(f"  VIX: No disponible")

    # Earnings
    L.append(f"\n  --- EARNINGS ESTA SEMANA ---")
    if earnings:
        for e in earnings:
            dias = "HOY" if e["dias_faltan"] == 0 else f"en {e['dias_faltan']} día(s)"
            eps = f"EPS est: ${e['eps_estimado']}" if e["eps_estimado"] else ""
            rev = f"Rev est: ${e['revenue_est_M']:,}M" if e["revenue_est_M"] else ""
            detalle = " | ".join(filter(None, [eps, rev]))
            L.append(f"  {e['simbolo']}: reporta {e['fecha']} ({dias})")
            if detalle:
                L.append(f"    {detalle}")
        L.append(f"  PRECAUCIÓN: Evitar abrir posiciones nuevas en activos que reportan pronto.")
    else:
        L.append(f"  Ningún activo del portafolio reporta esta semana.")

    # Noticias
    L.append(f"\n  --- NOTICIAS RELEVANTES (3 por activo) ---")
    for activo in ACTIVOS_OPERABLES:
        arts = noticias.get(activo, [])
        if not arts:
            continue
        L.append(f"\n  {activo}:")
        for a in arts:
            if "error" in a:
                L.append(f"    (error: {a['error']})")
                break
            L.append(f"    • [{a['fecha']}] {a['titulo']}")

    L.append(f"\n{'=' * 60}")

    texto = "\n".join(L)
    datos = {
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fear_greed": fng,
        "vix": vix,
        "earnings": earnings,
        "noticias": noticias,
    }
    return texto, datos


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    texto, datos = get_contexto_completo()
    print(texto)

    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    ruta = os.path.join(dir_datos, f"contexto_{datetime.now().strftime('%Y-%m-%d')}.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"\n  JSON guardado en: {ruta}")
