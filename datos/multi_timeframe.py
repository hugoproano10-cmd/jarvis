#!/home/hproano/asistente_env/bin/python
"""
Análisis multi-timeframe para JARVIS — 5 horizontes temporales.

Usa Tiingo para datos históricos e intraday.
Para cada activo calcula señal en:
  - 1 hora:   momentum intradía (Tiingo IEX)
  - 1 día:    tendencia corto plazo
  - 1 semana: momentum medio plazo (RSI semanal)
  - 1 mes:    régimen de precio
  - 6 meses:  macro momentum

Señal combinada: si 4 de 5 timeframes coinciden → señal fuerte.

Función principal: get_señal_multitimeframe(simbolo)
"""

import os
import sys
import time
import warnings
import importlib.util
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Config ────────────────────────────────────────────────────
_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "")
TIINGO_BASE = "https://api.tiingo.com"
TIINGO_IEX_BASE = "https://api.tiingo.com/iex"

# ── Caché para no repetir llamadas ────────────────────────────
_cache = {}
_CACHE_TTL_INTRA = 300    # 5 min para intraday
_CACHE_TTL_DAILY = 3600   # 1 hora para diario


def _cache_get(key, ttl=None):
    if key in _cache:
        ts, val = _cache[key]
        max_ttl = ttl or _CACHE_TTL_DAILY
        if time.time() - ts < max_ttl:
            return val
    return None


def _cache_set(key, val):
    _cache[key] = (time.time(), val)


# ── Helpers Tiingo ────────────────────────────────────────────

def _tiingo_headers():
    return {
        "Authorization": f"Token {TIINGO_API_KEY}",
        "Content-Type": "application/json",
    }


def _tiingo_get(url, params=None):
    if not TIINGO_API_KEY:
        return {"error": "TIINGO_API_KEY no configurada"}
    try:
        resp = requests.get(url, headers=_tiingo_headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _get_daily_data(simbolo, dias=180):
    """Obtiene datos diarios de Tiingo (últimos N días)."""
    cached = _cache_get(f"daily_{simbolo}_{dias}")
    if cached is not None:
        return cached

    hoy = date.today()
    inicio = hoy - timedelta(days=dias)
    url = f"{TIINGO_BASE}/tiingo/daily/{simbolo}/prices"
    raw = _tiingo_get(url, {
        "startDate": inicio.strftime("%Y-%m-%d"),
        "endDate": hoy.strftime("%Y-%m-%d"),
    })

    if isinstance(raw, dict) and "error" in raw:
        return None
    if not isinstance(raw, list) or len(raw) == 0:
        return None

    df = pd.DataFrame(raw)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)

    # Usar precios ajustados
    for col in ["adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume"]:
        base = col.replace("adj", "").lower()
        if col in df.columns:
            df[base] = df[col]

    _cache_set(f"daily_{simbolo}_{dias}", df)
    return df


def _get_intraday_data(simbolo):
    """Obtiene datos intraday de Tiingo IEX (barras de 5 min del día)."""
    cached = _cache_get(f"intra_{simbolo}", ttl=_CACHE_TTL_INTRA)
    if cached is not None:
        return cached

    url = f"{TIINGO_IEX_BASE}/{simbolo}/prices"
    raw = _tiingo_get(url, {
        "resampleFreq": "5min",
        "columns": "open,high,low,close,volume",
    })

    if isinstance(raw, dict) and "error" in raw:
        return None
    if not isinstance(raw, list) or len(raw) == 0:
        return None

    df = pd.DataFrame(raw)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)

    _cache_set(f"intra_{simbolo}", df)
    return df


# ================================================================
#  Cálculo de RSI
# ================================================================

def _rsi(series, period=14):
    """Calcula RSI sobre una serie de precios."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ================================================================
#  Señales por timeframe
# ================================================================

def _señal_1h(simbolo):
    """
    1 HORA — Momentum intradía.
    Usa barras de 5 min de Tiingo IEX (últimas ~12 barras = 1 hora).
    RSI rápido (5 periodos) + dirección del precio.
    """
    df = _get_intraday_data(simbolo)
    if df is None or len(df) < 12:
        return {"direccion": "→", "detalle": "Sin datos intraday", "rsi": None}

    # Últimas 12 barras (~1 hora en intervalos de 5 min)
    reciente = df.tail(12)
    precio_inicio = float(reciente["close"].iloc[0])
    precio_fin = float(reciente["close"].iloc[-1])
    var_pct = ((precio_fin / precio_inicio) - 1) * 100 if precio_inicio > 0 else 0

    # RSI rápido sobre intraday
    rsi_val = None
    if len(df) >= 14:
        rsi_series = _rsi(df["close"], period=5)
        rsi_val = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None

    # Clasificar
    score = 0
    if var_pct > 0.3:
        score += 1
    elif var_pct < -0.3:
        score -= 1

    if rsi_val is not None:
        if rsi_val > 70:
            score -= 1  # Sobrecompra intraday
        elif rsi_val < 30:
            score += 1  # Sobreventa intraday

    if score >= 1:
        direccion = "↑"
    elif score <= -1:
        direccion = "↓"
    else:
        direccion = "→"

    return {
        "direccion": direccion,
        "var_1h_pct": round(var_pct, 3),
        "rsi_5": round(rsi_val, 1) if rsi_val else None,
        "detalle": f"Var:{var_pct:+.3f}% RSI5:{rsi_val:.0f}" if rsi_val else f"Var:{var_pct:+.3f}%",
    }


def _señal_1d(simbolo):
    """
    1 DIA — Tendencia corto plazo.
    RSI(14) diario + posición vs SMA(10) + variación del día.
    """
    df = _get_daily_data(simbolo, dias=30)
    if df is None or len(df) < 14:
        return {"direccion": "→", "detalle": "Sin datos diarios", "rsi": None}

    close = df["close"] if "close" in df.columns else df["adjClose"]
    precio = float(close.iloc[-1])
    precio_ayer = float(close.iloc[-2]) if len(close) >= 2 else precio
    var_dia = ((precio / precio_ayer) - 1) * 100 if precio_ayer > 0 else 0

    # RSI(14) diario
    rsi_val = None
    rsi_series = _rsi(close, period=14)
    if not pd.isna(rsi_series.iloc[-1]):
        rsi_val = float(rsi_series.iloc[-1])

    # SMA(10)
    sma10 = float(close.rolling(10).mean().iloc[-1]) if len(close) >= 10 else None

    score = 0
    if var_dia > 0.5:
        score += 1
    elif var_dia < -0.5:
        score -= 1

    if rsi_val is not None:
        if rsi_val > 70:
            score -= 1
        elif rsi_val < 30:
            score += 1

    if sma10 is not None:
        if precio > sma10:
            score += 1
        else:
            score -= 1

    if score >= 1:
        direccion = "↑"
    elif score <= -1:
        direccion = "↓"
    else:
        direccion = "→"

    return {
        "direccion": direccion,
        "var_dia_pct": round(var_dia, 2),
        "rsi_14": round(rsi_val, 1) if rsi_val else None,
        "vs_sma10": "ARRIBA" if sma10 and precio > sma10 else "ABAJO",
        "detalle": f"Var:{var_dia:+.2f}% RSI14:{rsi_val:.0f}" if rsi_val else f"Var:{var_dia:+.2f}%",
    }


def _señal_1w(simbolo):
    """
    1 SEMANA — Momentum medio plazo.
    RSI semanal (14 periodos sobre datos semanales) + variación semanal.
    """
    df = _get_daily_data(simbolo, dias=120)
    if df is None or len(df) < 20:
        return {"direccion": "→", "detalle": "Sin datos semanales", "rsi": None}

    close = df["close"] if "close" in df.columns else df["adjClose"]

    # Resamplear a semanal
    weekly = close.resample("W").last().dropna()
    if len(weekly) < 14:
        return {"direccion": "→", "detalle": "Datos semanales insuficientes", "rsi": None}

    precio_actual = float(weekly.iloc[-1])
    precio_semana_ant = float(weekly.iloc[-2])
    var_sem = ((precio_actual / precio_semana_ant) - 1) * 100 if precio_semana_ant > 0 else 0

    # RSI semanal
    rsi_val = None
    rsi_series = _rsi(weekly, period=14)
    if not pd.isna(rsi_series.iloc[-1]):
        rsi_val = float(rsi_series.iloc[-1])

    # SMA(4) semanal = 1 mes
    sma4w = float(weekly.rolling(4).mean().iloc[-1]) if len(weekly) >= 4 else None

    score = 0
    if var_sem > 1.5:
        score += 1
    elif var_sem < -1.5:
        score -= 1

    if rsi_val is not None:
        if rsi_val > 70:
            score -= 1
        elif rsi_val < 30:
            score += 1
        elif rsi_val > 55:
            score += 1
        elif rsi_val < 45:
            score -= 1

    if sma4w and precio_actual > sma4w:
        score += 1
    elif sma4w:
        score -= 1

    if score >= 2:
        direccion = "↑"
    elif score <= -2:
        direccion = "↓"
    else:
        direccion = "→"

    return {
        "direccion": direccion,
        "var_semanal_pct": round(var_sem, 2),
        "rsi_semanal": round(rsi_val, 1) if rsi_val else None,
        "vs_sma4w": "ARRIBA" if sma4w and precio_actual > sma4w else "ABAJO",
        "detalle": f"Var:{var_sem:+.2f}% RSIw:{rsi_val:.0f}" if rsi_val else f"Var:{var_sem:+.2f}%",
    }


def _señal_1m(simbolo):
    """
    1 MES — Régimen de precio.
    Variación mensual + posición vs SMA(20) + Bollinger Bands.
    """
    df = _get_daily_data(simbolo, dias=60)
    if df is None or len(df) < 21:
        return {"direccion": "→", "detalle": "Sin datos mensuales", "rsi": None}

    close = df["close"] if "close" in df.columns else df["adjClose"]
    precio = float(close.iloc[-1])
    precio_1m = float(close.iloc[-21]) if len(close) >= 21 else float(close.iloc[0])
    var_mes = ((precio / precio_1m) - 1) * 100 if precio_1m > 0 else 0

    # SMA(20) y Bollinger
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    std20 = float(close.rolling(20).std().iloc[-1]) if len(close) >= 20 else None

    bb_upper = sma20 + 2 * std20 if sma20 and std20 else None
    bb_lower = sma20 - 2 * std20 if sma20 and std20 else None

    score = 0
    if var_mes > 5:
        score += 2
    elif var_mes > 2:
        score += 1
    elif var_mes < -5:
        score -= 2
    elif var_mes < -2:
        score -= 1

    if sma20 and precio > sma20:
        score += 1
    elif sma20:
        score -= 1

    # Bollinger: cerca de banda inferior → posible rebote
    if bb_lower and precio <= bb_lower:
        score += 1
    elif bb_upper and precio >= bb_upper:
        score -= 1

    if score >= 2:
        direccion = "↑"
    elif score <= -2:
        direccion = "↓"
    else:
        direccion = "→"

    # Régimen
    if var_mes > 5:
        regimen = "TENDENCIA_ALCISTA"
    elif var_mes < -5:
        regimen = "TENDENCIA_BAJISTA"
    else:
        regimen = "CONSOLIDACION"

    return {
        "direccion": direccion,
        "var_mensual_pct": round(var_mes, 2),
        "regimen": regimen,
        "vs_sma20": "ARRIBA" if sma20 and precio > sma20 else "ABAJO",
        "bb_posicion": "SUPERIOR" if bb_upper and precio >= bb_upper else
                       "INFERIOR" if bb_lower and precio <= bb_lower else "MEDIO",
        "detalle": f"Var:{var_mes:+.2f}% {regimen}",
    }


def _señal_6m(simbolo):
    """
    6 MESES — Macro momentum.
    Variación 6 meses + SMA(50) vs SMA(200) (Golden/Death Cross) + RSI mensual.
    """
    df = _get_daily_data(simbolo, dias=252)
    if df is None or len(df) < 126:
        return {"direccion": "→", "detalle": "Sin datos 6m", "rsi": None}

    close = df["close"] if "close" in df.columns else df["adjClose"]
    precio = float(close.iloc[-1])
    precio_6m = float(close.iloc[-126]) if len(close) >= 126 else float(close.iloc[0])
    var_6m = ((precio / precio_6m) - 1) * 100 if precio_6m > 0 else 0

    # Golden/Death Cross
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    cross = "N/D"
    if sma50 and sma200:
        cross = "GOLDEN" if sma50 > sma200 else "DEATH"

    # RSI mensual (resamplear a mensual)
    monthly = close.resample("ME").last().dropna()
    rsi_val = None
    if len(monthly) >= 6:
        rsi_series = _rsi(monthly, period=6)
        if not pd.isna(rsi_series.iloc[-1]):
            rsi_val = float(rsi_series.iloc[-1])

    score = 0
    if var_6m > 15:
        score += 2
    elif var_6m > 5:
        score += 1
    elif var_6m < -15:
        score -= 2
    elif var_6m < -5:
        score -= 1

    if cross == "GOLDEN":
        score += 1
    elif cross == "DEATH":
        score -= 1

    if precio > (sma50 or 0):
        score += 1
    elif sma50:
        score -= 1

    if score >= 2:
        direccion = "↑"
    elif score <= -2:
        direccion = "↓"
    else:
        direccion = "→"

    return {
        "direccion": direccion,
        "var_6m_pct": round(var_6m, 2),
        "cross": cross,
        "vs_sma50": "ARRIBA" if sma50 and precio > sma50 else "ABAJO",
        "rsi_mensual": round(rsi_val, 1) if rsi_val else None,
        "detalle": f"Var6m:{var_6m:+.1f}% {cross}" + (f" RSIm:{rsi_val:.0f}" if rsi_val else ""),
    }


# ================================================================
#  Señal combinada multi-timeframe
# ================================================================

def get_señal_multitimeframe(simbolo):
    """
    Calcula señal en 5 horizontes y genera consenso.

    Retorna:
    {
        "simbolo": "XOM",
        "1h": "↑/↓/→",
        "1d": "↑/↓/→",
        "1w": "↑/↓/→",
        "1m": "↑/↓/→",
        "6m": "↑/↓/→",
        "consenso": "ALCISTA/BAJISTA/MIXTO",
        "fuerza": 0-5,
        "detalle": {...},
        "timestamp": "...",
    }
    """
    tf_1h = _señal_1h(simbolo)
    tf_1d = _señal_1d(simbolo)
    tf_1w = _señal_1w(simbolo)
    tf_1m = _señal_1m(simbolo)
    tf_6m = _señal_6m(simbolo)

    direcciones = [
        tf_1h["direccion"],
        tf_1d["direccion"],
        tf_1w["direccion"],
        tf_1m["direccion"],
        tf_6m["direccion"],
    ]

    alcistas = sum(1 for d in direcciones if d == "↑")
    bajistas = sum(1 for d in direcciones if d == "↓")

    # Consenso: 4+ en misma dirección = señal fuerte
    if alcistas >= 4:
        consenso = "ALCISTA"
        fuerza = alcistas
    elif bajistas >= 4:
        consenso = "BAJISTA"
        fuerza = bajistas
    elif alcistas >= 3:
        consenso = "ALCISTA"
        fuerza = alcistas
    elif bajistas >= 3:
        consenso = "BAJISTA"
        fuerza = bajistas
    else:
        consenso = "MIXTO"
        fuerza = max(alcistas, bajistas)

    return {
        "simbolo": simbolo,
        "1h": tf_1h["direccion"],
        "1d": tf_1d["direccion"],
        "1w": tf_1w["direccion"],
        "1m": tf_1m["direccion"],
        "6m": tf_6m["direccion"],
        "consenso": consenso,
        "fuerza": fuerza,
        "señal_fuerte": alcistas >= 4 or bajistas >= 4,
        "detalle": {
            "1h": tf_1h,
            "1d": tf_1d,
            "1w": tf_1w,
            "1m": tf_1m,
            "6m": tf_6m,
        },
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_multitimeframe_todos(simbolos=None):
    """
    Calcula multi-timeframe para una lista de símbolos.
    Si no se pasa lista, usa ACTIVOS_OPERABLES.
    """
    if simbolos is None:
        simbolos = ACTIVOS_OPERABLES

    resultados = {}
    for sym in simbolos:
        try:
            resultados[sym] = get_señal_multitimeframe(sym)
        except Exception as e:
            resultados[sym] = {
                "simbolo": sym,
                "1h": "→", "1d": "→", "1w": "→", "1m": "→", "6m": "→",
                "consenso": "N/D", "fuerza": 0, "señal_fuerte": False,
                "error": str(e),
            }

    return resultados


# ================================================================
#  CLI — Test
# ================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multi-Timeframe JARVIS")
    parser.add_argument("simbolos", nargs="*", default=["XOM"],
                        help="Símbolos a analizar (default: XOM)")
    parser.add_argument("--todos", action="store_true",
                        help="Analizar los 22 activos operables")
    args = parser.parse_args()

    if args.todos:
        simbolos = ACTIVOS_OPERABLES
    else:
        simbolos = args.simbolos

    print("=" * 70)
    print("  MULTI-TIMEFRAME ANALYSIS — JARVIS")
    print("=" * 70)

    for sym in simbolos:
        print(f"\n  --- {sym} ---")
        result = get_señal_multitimeframe(sym)

        # Visual compacto
        tf_line = (f"  1h:{result['1h']}  1d:{result['1d']}  "
                   f"1w:{result['1w']}  1m:{result['1m']}  6m:{result['6m']}")
        print(tf_line)
        print(f"  Consenso: {result['consenso']} (fuerza {result['fuerza']}/5"
              f"{' FUERTE' if result.get('señal_fuerte') else ''})")

        # Detalle por timeframe
        for tf_key in ["1h", "1d", "1w", "1m", "6m"]:
            det = result["detalle"][tf_key]
            print(f"    {tf_key}: {det.get('detalle', 'N/D')}")

    # Resumen si hay varios
    if len(simbolos) > 1:
        print(f"\n{'=' * 70}")
        print("  RESUMEN")
        print("-" * 70)
        print(f"  {'SYM':<6} {'1h':>3} {'1d':>3} {'1w':>3} {'1m':>3} {'6m':>3}  {'CONSENSO':<10} {'F':>1}")
        print(f"  {'-'*6} {'-'*3} {'-'*3} {'-'*3} {'-'*3} {'-'*3}  {'-'*10} {'-'*1}")
        for sym in simbolos:
            r = get_señal_multitimeframe(sym)
            marca = "*" if r.get("señal_fuerte") else " "
            print(f"  {sym:<6} {r['1h']:>3} {r['1d']:>3} {r['1w']:>3} "
                  f"{r['1m']:>3} {r['6m']:>3}  {r['consenso']:<10} {r['fuerza']}{marca}")
    print(f"\n{'=' * 70}")
