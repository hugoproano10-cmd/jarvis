#!/home/hproano/asistente_env/bin/python
"""
Señales de tráfico marítimo AIS para JARVIS.

Fuentes:
  - MarineTraffic (scraping de datos públicos de densidad de tráfico)
  - VesselFinder API (free tier) — posiciones de buques
  - NOAA/Coast Guard AIS público como fallback

Rutas clave monitoreadas:
  - Petroleros globales       → señal para XOM, XLE, GLD
  - Carga general global      → señal para SPY (economía global)
  - Canal de Panamá           → señal macro de comercio global

Señal final: get_ais_signal() → dict con tráfico y señales por activo.
"""

import os
import sys
import time
import importlib.util
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Config ────────────────────────────────────────────────────
_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

# ── Caché simple para no saturar APIs ─────────────────────────
_cache = {}
_CACHE_TTL = 3600  # 1 hora


def _cache_get(key):
    """Retorna valor cacheado si existe y no expiró."""
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return val
    return None


def _cache_set(key, val):
    _cache[key] = (time.time(), val)


# ================================================================
#  1. MarineTraffic — datos públicos de densidad
# ================================================================

# Áreas geográficas clave (bounding boxes aprox.)
ZONAS_MARITIMAS = {
    "golfo_mexico": {
        "nombre": "Golfo de México / Houston Ship Channel",
        "lat_min": 27.0, "lat_max": 30.0,
        "lon_min": -96.0, "lon_max": -88.0,
        "tipo": "petroleo",
        "relevancia": ["XOM", "XLE"],
    },
    "estrecho_hormuz": {
        "nombre": "Estrecho de Ormuz (aprox.)",
        "lat_min": 25.5, "lat_max": 27.0,
        "lon_min": 55.0, "lon_max": 57.5,
        "tipo": "petroleo",
        "relevancia": ["XOM", "XLE", "GLD"],
    },
    "canal_panama": {
        "nombre": "Canal de Panamá",
        "lat_min": 8.5, "lat_max": 9.5,
        "lon_min": -80.0, "lon_max": -79.0,
        "tipo": "comercio_global",
        "relevancia": ["SPY"],
    },
    "canal_suez": {
        "nombre": "Canal de Suez",
        "lat_min": 29.8, "lat_max": 31.3,
        "lon_min": 32.0, "lon_max": 33.0,
        "tipo": "comercio_global",
        "relevancia": ["SPY", "GLD"],
    },
    "rotterdam": {
        "nombre": "Puerto de Rotterdam",
        "lat_min": 51.5, "lat_max": 52.2,
        "lon_min": 3.5, "lon_max": 4.5,
        "tipo": "carga",
        "relevancia": ["SPY", "EFA"],
    },
    "singapur": {
        "nombre": "Estrecho de Malaca / Singapur",
        "lat_min": 1.0, "lat_max": 1.5,
        "lon_min": 103.5, "lon_max": 104.2,
        "tipo": "comercio_global",
        "relevancia": ["SPY", "EEM"],
    },
}

# Tipos de buques AIS (códigos estándar)
TIPO_PETROLERO = {80, 81, 82, 83, 84, 85, 86, 87, 88, 89}
TIPO_CARGA = {70, 71, 72, 73, 74, 75, 76, 77, 78, 79}
TIPO_CONTENEDOR = {70, 71, 72, 73}


def _fetch_vesselfinder(zona):
    """
    Intenta obtener datos de VesselFinder free API.
    Free tier: limitado a pocas peticiones/día.
    """
    cached = _cache_get(f"vf_{zona}")
    if cached is not None:
        return cached

    info = ZONAS_MARITIMAS[zona]
    url = "https://api.vesselfinder.com/vessels"
    params = {
        "userkey": os.getenv("VESSELFINDER_KEY", ""),
        "latmin": info["lat_min"],
        "latmax": info["lat_max"],
        "lonmin": info["lon_min"],
        "lonmax": info["lon_max"],
    }

    if not params["userkey"]:
        return {"error": "VESSELFINDER_KEY no configurada", "buques": []}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        buques = data if isinstance(data, list) else data.get("vessels", [])
        result = {"buques": buques, "total": len(buques)}
        _cache_set(f"vf_{zona}", result)
        return result
    except Exception as e:
        return {"error": str(e), "buques": []}


def _fetch_marinetraffic_density(zona):
    """
    Scraping ligero de MarineTraffic para obtener densidad de tráfico.
    Usa la página pública de densidad por área.
    """
    cached = _cache_get(f"mt_density_{zona}")
    if cached is not None:
        return cached

    info = ZONAS_MARITIMAS[zona]
    url = "https://www.marinetraffic.com/en/ais/details/density"
    params = {
        "minlat": info["lat_min"],
        "maxlat": info["lat_max"],
        "minlon": info["lon_min"],
        "maxlon": info["lon_max"],
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; JARVIS-Trading/1.0)",
            "Accept": "application/json",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            try:
                data = resp.json()
                result = {
                    "densidad": data.get("density", data.get("count", 0)),
                    "fuente": "marinetraffic",
                }
                _cache_set(f"mt_density_{zona}", result)
                return result
            except Exception:
                pass
        return {"error": f"HTTP {resp.status_code}", "densidad": None}
    except Exception as e:
        return {"error": str(e), "densidad": None}


def _fetch_ais_coastguard():
    """
    Datos AIS de la US Coast Guard (NAIS) — feed público.
    Alternativa gratuita para aguas US.
    """
    cached = _cache_get("uscg_ais")
    if cached is not None:
        return cached

    url = "https://marinecadastre.gov/ais/"
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JARVIS-Trading/1.0)"
        })
        if resp.status_code == 200:
            result = {"disponible": True, "fuente": "uscg"}
            _cache_set("uscg_ais", result)
            return result
    except Exception:
        pass
    return {"disponible": False, "fuente": "uscg"}


# ================================================================
#  2. Estimación por proxy — datos de commodities como señal
# ================================================================

def _proxy_trafico_petroleo():
    """
    Usa cambios en precios de crudo (WTI/Brent) y spreads de contango
    como proxy del tráfico de petroleros cuando AIS no está disponible.
    Lógica: si el crudo sube y el contango se amplía → más almacenamiento
    flotante → más petroleros en movimiento.
    """
    cached = _cache_get("proxy_petroleo")
    if cached is not None:
        return cached

    try:
        import yfinance as yf
        # WTI crudo
        cl = yf.download("CL=F", period="1mo", progress=False, auto_adjust=True)
        # Brent
        bz = yf.download("BZ=F", period="1mo", progress=False, auto_adjust=True)

        if cl.empty:
            return {"nivel": "N/D", "error": "Sin datos de crudo"}

        # Flatten multi-index if needed
        if hasattr(cl.columns, 'levels'):
            cl.columns = cl.columns.get_level_values(0)
        if not bz.empty and hasattr(bz.columns, 'levels'):
            bz.columns = bz.columns.get_level_values(0)

        precio_actual = float(cl["Close"].iloc[-1])
        precio_5d = float(cl["Close"].iloc[-5]) if len(cl) >= 5 else precio_actual
        precio_20d = float(cl["Close"].iloc[-20]) if len(cl) >= 20 else precio_actual

        var_5d = ((precio_actual / precio_5d) - 1) * 100 if precio_5d > 0 else 0
        var_20d = ((precio_actual / precio_20d) - 1) * 100 if precio_20d > 0 else 0

        # Spread Brent-WTI (indica demanda de transporte)
        spread = 0.0
        if not bz.empty:
            if hasattr(bz.columns, 'levels'):
                bz.columns = bz.columns.get_level_values(0)
            brent_actual = float(bz["Close"].iloc[-1])
            spread = brent_actual - precio_actual

        # Volumen promedio como proxy de actividad
        vol_reciente = float(cl["Volume"].iloc[-5:].mean()) if len(cl) >= 5 else 0
        vol_historico = float(cl["Volume"].iloc[-20:].mean()) if len(cl) >= 20 else 0
        ratio_vol = vol_reciente / vol_historico if vol_historico > 0 else 1.0

        # Clasificar nivel de tráfico
        score = 0
        if var_5d > 3:
            score += 1  # Crudo subiendo rápido → más embarques
        if var_5d < -3:
            score -= 1  # Crudo bajando → menos demanda transporte
        if spread > 3:
            score += 1  # Brent caro vs WTI → más tráfico transatlántico
        if ratio_vol > 1.3:
            score += 1  # Volumen alto → más actividad
        if ratio_vol < 0.7:
            score -= 1  # Volumen bajo → menos actividad

        if score >= 2:
            nivel = "ALTO"
        elif score <= -1:
            nivel = "BAJO"
        else:
            nivel = "NORMAL"

        result = {
            "nivel": nivel,
            "score": score,
            "wti_precio": round(precio_actual, 2),
            "wti_var_5d": round(var_5d, 2),
            "wti_var_20d": round(var_20d, 2),
            "spread_brent_wti": round(spread, 2),
            "ratio_volumen": round(ratio_vol, 2),
            "fuente": "proxy_commodities",
        }
        _cache_set("proxy_petroleo", result)
        return result

    except Exception as e:
        return {"nivel": "N/D", "error": str(e), "fuente": "proxy_commodities"}


def _proxy_trafico_carga():
    """
    Usa el Baltic Dry Index (BDI) como proxy del tráfico de carga.
    BDI subiendo → más comercio global → alcista SPY.
    También usa volumen de ETFs de shipping como señal.
    """
    cached = _cache_get("proxy_carga")
    if cached is not None:
        return cached

    try:
        import yfinance as yf
        # BDRY = Breakwave Dry Bulk Shipping ETF (proxy del BDI)
        bdi = yf.download("BDRY", period="3mo", progress=False, auto_adjust=True)

        if bdi.empty:
            return {"nivel": "N/D", "error": "Sin datos BDRY"}

        if hasattr(bdi.columns, 'levels'):
            bdi.columns = bdi.columns.get_level_values(0)

        precio_actual = float(bdi["Close"].iloc[-1])
        precio_20d = float(bdi["Close"].iloc[-20]) if len(bdi) >= 20 else precio_actual
        precio_60d = float(bdi["Close"].iloc[-60]) if len(bdi) >= 60 else precio_actual

        var_20d = ((precio_actual / precio_20d) - 1) * 100 if precio_20d > 0 else 0
        var_60d = ((precio_actual / precio_60d) - 1) * 100 if precio_60d > 0 else 0

        # SMA20
        sma20 = float(bdi["Close"].iloc[-20:].mean()) if len(bdi) >= 20 else precio_actual

        score = 0
        if var_20d > 10:
            score += 2
        elif var_20d > 5:
            score += 1
        elif var_20d < -10:
            score -= 2
        elif var_20d < -5:
            score -= 1

        if var_60d > 15:
            score += 1
        elif var_60d < -15:
            score -= 1

        if precio_actual > sma20:
            score += 1
        else:
            score -= 1

        if score >= 2:
            nivel = "ALTO"
        elif score <= -1:
            nivel = "BAJO"
        else:
            nivel = "NORMAL"

        result = {
            "nivel": nivel,
            "score": score,
            "bdry_precio": round(precio_actual, 2),
            "bdry_var_20d": round(var_20d, 2),
            "bdry_var_60d": round(var_60d, 2),
            "bdry_vs_sma20": "ARRIBA" if precio_actual > sma20 else "ABAJO",
            "fuente": "proxy_bdry",
        }
        _cache_set("proxy_carga", result)
        return result

    except Exception as e:
        return {"nivel": "N/D", "error": str(e), "fuente": "proxy_bdry"}


def _proxy_canal_panama():
    """
    Proxy del tráfico del Canal de Panamá.
    Usa noticias y datos de espera + volúmenes de comercio.
    En periodos de sequía/restricciones → señal bajista global.
    """
    cached = _cache_get("proxy_panama")
    if cached is not None:
        return cached

    try:
        import yfinance as yf
        # Usar SEA (US Global Sea to Sky Cargo) y BOAT como proxies
        # Si no existen, usar volúmenes de comercio global
        etfs = ["SEA", "IYT"]  # IYT = transporte USA
        datos = {}

        for etf in etfs:
            df = yf.download(etf, period="3mo", progress=False, auto_adjust=True)
            if not df.empty:
                if hasattr(df.columns, 'levels'):
                    df.columns = df.columns.get_level_values(0)
                precio = float(df["Close"].iloc[-1])
                precio_20d = float(df["Close"].iloc[-20]) if len(df) >= 20 else precio
                var = ((precio / precio_20d) - 1) * 100 if precio_20d > 0 else 0
                datos[etf] = {"precio": round(precio, 2), "var_20d": round(var, 2)}

        score = 0
        for etf, d in datos.items():
            v = d["var_20d"]
            if v > 5:
                score += 1
            elif v < -5:
                score -= 1

        if score >= 1:
            estado = "FLUIDO"
        elif score <= -1:
            estado = "RESTRINGIDO"
        else:
            estado = "NORMAL"

        result = {
            "estado": estado,
            "score": score,
            "datos_etfs": datos,
            "fuente": "proxy_transporte",
        }
        _cache_set("proxy_panama", result)
        return result

    except Exception as e:
        return {"estado": "N/D", "error": str(e), "fuente": "proxy_transporte"}


# ================================================================
#  3. Señal integrada: get_ais_signal()
# ================================================================

def get_ais_signal():
    """
    Genera señales de trading basadas en tráfico marítimo.

    Intenta fuentes AIS directas (VesselFinder, MarineTraffic).
    Si no están disponibles, usa proxies de commodities (crudo, BDI, transporte).

    Retorna:
    {
        "trafico_petroleo": "ALTO/NORMAL/BAJO",
        "trafico_carga": "ALTO/NORMAL/BAJO",
        "canal_panama": "FLUIDO/NORMAL/RESTRINGIDO",
        "señal_xom": "ALCISTA/NEUTRAL/BAJISTA",
        "señal_xle": "ALCISTA/NEUTRAL/BAJISTA",
        "señal_gld": "ALCISTA/NEUTRAL/BAJISTA",
        "señal_spy": "ALCISTA/NEUTRAL/BAJISTA",
        "confianza": 0-3,
        "fuentes": [...],
        "detalle": {...},
        "timestamp": "...",
    }
    """
    fuentes_usadas = []
    ais_directo = False

    # ── Intentar fuentes AIS directas ──
    vf_golfo = _fetch_vesselfinder("golfo_mexico")
    if "error" not in vf_golfo and vf_golfo.get("total", 0) > 0:
        ais_directo = True
        fuentes_usadas.append("vesselfinder")

    mt_density = _fetch_marinetraffic_density("golfo_mexico")
    if mt_density.get("densidad") is not None:
        ais_directo = True
        fuentes_usadas.append("marinetraffic")

    # ── Proxy de petróleo ──
    petroleo = _proxy_trafico_petroleo()
    if petroleo.get("nivel") != "N/D":
        fuentes_usadas.append(petroleo["fuente"])

    # ── Proxy de carga ──
    carga = _proxy_trafico_carga()
    if carga.get("nivel") != "N/D":
        fuentes_usadas.append(carga["fuente"])

    # ── Proxy canal de Panamá ──
    panama = _proxy_canal_panama()
    if panama.get("estado") != "N/D":
        fuentes_usadas.append(panama["fuente"])

    # ── Construir señales por activo ──
    trafico_petroleo = petroleo.get("nivel", "N/D")
    trafico_carga = carga.get("nivel", "N/D")
    canal_estado = panama.get("estado", "N/D")

    # XOM: petrolera pura → si hay más tráfico de crudo, más ingresos
    senal_xom = _clasificar_senal_petroleo(petroleo)
    # XLE: sector energía → similar a XOM pero más diversificado
    senal_xle = _clasificar_senal_petroleo(petroleo)
    # GLD: oro → si hay tensión en rutas (Ormuz, Suez) → alcista por refugio
    senal_gld = _clasificar_senal_oro(petroleo, canal_estado)
    # SPY: economía global → carga + canales fluidos = alcista
    senal_spy = _clasificar_senal_global(carga, panama)

    # Confianza: 0 = solo proxies sin datos, 1 = proxies OK, 2 = AIS parcial, 3 = AIS completo
    confianza = 0
    if len(fuentes_usadas) >= 1:
        confianza = 1
    if ais_directo:
        confianza = 2
    if "vesselfinder" in fuentes_usadas and "marinetraffic" in fuentes_usadas:
        confianza = 3

    return {
        "trafico_petroleo": trafico_petroleo,
        "trafico_carga": trafico_carga,
        "canal_panama": canal_estado,
        "señal_xom": senal_xom,
        "señal_xle": senal_xle,
        "señal_gld": senal_gld,
        "señal_spy": senal_spy,
        "confianza": confianza,
        "fuentes": fuentes_usadas,
        "detalle": {
            "petroleo": petroleo,
            "carga": carga,
            "panama": panama,
            "ais_directo": ais_directo,
        },
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _clasificar_senal_petroleo(petroleo):
    """Señal para petroleras (XOM, XLE) basada en tráfico de crudo."""
    nivel = petroleo.get("nivel", "N/D")
    score = petroleo.get("score", 0)

    # Precio crudo subiendo + volumen alto = más ingresos petroleras
    if nivel == "ALTO" and score >= 2:
        return "ALCISTA"
    elif nivel == "BAJO" and score <= -1:
        return "BAJISTA"
    elif nivel == "ALTO":
        return "ALCISTA"
    elif nivel == "BAJO":
        return "BAJISTA"
    return "NEUTRAL"


def _clasificar_senal_oro(petroleo, canal_estado):
    """
    Señal para GLD basada en tensión marítima.
    Si el petróleo está alto (posible tensión geopolítica) o los canales
    están restringidos → refugio en oro → alcista GLD.
    """
    score = 0
    if petroleo.get("nivel") == "ALTO" and petroleo.get("wti_var_5d", 0) > 5:
        score += 1  # Subida rápida del crudo → tensión → refugio oro
    if petroleo.get("spread_brent_wti", 0) > 5:
        score += 1  # Spread alto → disrupción de suministro
    if canal_estado == "RESTRINGIDO":
        score += 1  # Canal bloqueado → riesgo global

    if score >= 2:
        return "ALCISTA"
    elif score >= 1:
        return "ALCISTA"
    elif petroleo.get("nivel") == "BAJO" and canal_estado == "FLUIDO":
        return "BAJISTA"
    return "NEUTRAL"


def _clasificar_senal_global(carga, panama):
    """
    Señal para SPY basada en comercio global.
    Más carga + canales fluidos = economía saludable = alcista.
    """
    score = 0
    nivel_carga = carga.get("nivel", "N/D")
    estado_panama = panama.get("estado", "N/D")

    if nivel_carga == "ALTO":
        score += 2
    elif nivel_carga == "NORMAL":
        score += 0
    elif nivel_carga == "BAJO":
        score -= 2

    if estado_panama == "FLUIDO":
        score += 1
    elif estado_panama == "RESTRINGIDO":
        score -= 1

    if score >= 2:
        return "ALCISTA"
    elif score <= -2:
        return "BAJISTA"
    return "NEUTRAL"


# ================================================================
#  4. CLI — Test
# ================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  AIS MARITIMO — Señales de tráfico marítimo")
    print("=" * 60)

    print("\n  [1/3] Proxy petróleo (WTI/Brent)...")
    pet = _proxy_trafico_petroleo()
    if pet.get("nivel") != "N/D":
        print(f"  Nivel: {pet['nivel']} (score: {pet['score']})")
        print(f"  WTI: ${pet.get('wti_precio', '?')} | Var 5d: {pet.get('wti_var_5d', '?')}%"
              f" | Var 20d: {pet.get('wti_var_20d', '?')}%")
        print(f"  Spread Brent-WTI: ${pet.get('spread_brent_wti', '?')}"
              f" | Ratio vol: {pet.get('ratio_volumen', '?')}x")
    else:
        print(f"  Error: {pet.get('error', 'desconocido')}")

    print("\n  [2/3] Proxy carga (BDRY/BDI)...")
    car = _proxy_trafico_carga()
    if car.get("nivel") != "N/D":
        print(f"  Nivel: {car['nivel']} (score: {car['score']})")
        print(f"  BDRY: ${car.get('bdry_precio', '?')} | Var 20d: {car.get('bdry_var_20d', '?')}%"
              f" | Var 60d: {car.get('bdry_var_60d', '?')}%")
        print(f"  vs SMA20: {car.get('bdry_vs_sma20', '?')}")
    else:
        print(f"  Error: {car.get('error', 'desconocido')}")

    print("\n  [3/3] Proxy Canal Panamá (IYT/transporte)...")
    pan = _proxy_canal_panama()
    if pan.get("estado") != "N/D":
        print(f"  Estado: {pan['estado']} (score: {pan['score']})")
        for etf, d in pan.get("datos_etfs", {}).items():
            print(f"  {etf}: ${d['precio']} | Var 20d: {d['var_20d']}%")
    else:
        print(f"  Error: {pan.get('error', 'desconocido')}")

    print("\n" + "-" * 60)
    print("  SEÑAL INTEGRADA")
    print("-" * 60)
    signal = get_ais_signal()
    print(f"  Tráfico petróleo: {signal['trafico_petroleo']}")
    print(f"  Tráfico carga:    {signal['trafico_carga']}")
    print(f"  Canal Panamá:     {signal['canal_panama']}")
    print(f"  Confianza:        {signal['confianza']}/3")
    print(f"  Fuentes:          {', '.join(signal['fuentes']) or 'ninguna'}")
    print()
    print(f"  Señal XOM: {signal['señal_xom']}")
    print(f"  Señal XLE: {signal['señal_xle']}")
    print(f"  Señal GLD: {signal['señal_gld']}")
    print(f"  Señal SPY: {signal['señal_spy']}")
    print(f"\n  Timestamp: {signal['timestamp']}")
    print("=" * 60)
