#!/home/hproano/asistente_env/bin/python
"""
Señales quant clásicas para JARVIS — 3 estrategias con track record documentado.

A) Momentum 12-1 (Jegadeesh & Titman 1993)
B) Mean Reversion RSI semanal extremo
C) Golden/Death Cross institucional (MA50 x MA200 + volumen)

Señal combinada: si 2 de 3 coinciden → señal fuerte.
"""

import os
import sys
import warnings
import importlib.util
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS = _cfg.ACTIVOS_OPERABLES


# ── Datos ────────────────────────────────────────────────────

def _descargar(simbolo):
    """Descarga 14 meses de datos diarios via yfinance (rápido, sin API key)."""
    import yfinance as yf
    fin = datetime.now()
    inicio = fin - timedelta(days=420)  # ~14 meses
    df = yf.download(simbolo, start=inicio.strftime("%Y-%m-%d"),
                     end=fin.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ══════════════════════════════════════════════════════════════
#  A) MOMENTUM 12-1 (Jegadeesh & Titman)
# ══════════════════════════════════════════════════════════════

def momentum_12_1(df):
    """
    Retorno de los últimos 12 meses excluyendo el último mes.
    >+10% → alcista, <-10% → bajista, entre → neutral.
    """
    if df is None or len(df) < 252:
        return {"senal": "N/D", "retorno_pct": None, "error": "datos insuficientes"}

    precio_actual = float(df["Close"].iloc[-1])
    # Hace 12 meses
    idx_12m = max(0, len(df) - 252)
    precio_12m = float(df["Close"].iloc[idx_12m])
    # Hace 1 mes (excluir)
    idx_1m = max(0, len(df) - 21)
    precio_1m = float(df["Close"].iloc[idx_1m])

    # Retorno 12m excluyendo último mes
    if precio_12m <= 0:
        return {"senal": "N/D", "retorno_pct": None}
    ret = ((precio_1m / precio_12m) - 1) * 100

    if ret > 10:
        senal = "ALCISTA"
    elif ret < -10:
        senal = "BAJISTA"
    else:
        senal = "NEUTRAL"

    return {"senal": senal, "retorno_pct": round(ret, 2)}


# ══════════════════════════════════════════════════════════════
#  B) MEAN REVERSION — RSI semanal extremo
# ══════════════════════════════════════════════════════════════

def rsi_semanal(df):
    """
    RSI(14) en timeframe semanal.
    <25 → compra fuerte, >75 → venta fuerte.
    """
    if df is None or len(df) < 100:
        return {"senal": "N/D", "rsi": None, "error": "datos insuficientes"}

    # Resample a semanal
    weekly = df["Close"].resample("W").last().dropna()
    if len(weekly) < 20:
        return {"senal": "N/D", "rsi": None, "error": "pocas semanas"}

    # RSI(14) manual
    delta = weekly.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_val = float(rsi.iloc[-1]) if not rsi.empty and pd.notna(rsi.iloc[-1]) else 50.0

    if rsi_val < 25:
        senal = "ALCISTA"
    elif rsi_val > 75:
        senal = "BAJISTA"
    else:
        senal = "NEUTRAL"

    return {"senal": senal, "rsi": round(rsi_val, 1)}


# ══════════════════════════════════════════════════════════════
#  C) GOLDEN/DEATH CROSS institucional
# ══════════════════════════════════════════════════════════════

def golden_death_cross(df):
    """
    MA50 cruza MA200. Confirmado con volumen > 1.5x promedio 20d.
    Golden cross → alcista fuerte. Death cross → bajista fuerte.
    """
    if df is None or len(df) < 210:
        return {"senal": "N/D", "tipo_cruce": None, "error": "datos insuficientes"}

    close = df["Close"]
    vol = df["Volume"]

    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    vol_avg = vol.rolling(20).mean()

    # Valores actuales y de ayer
    ma50_hoy = float(ma50.iloc[-1])
    ma200_hoy = float(ma200.iloc[-1])
    ma50_ayer = float(ma50.iloc[-2])
    ma200_ayer = float(ma200.iloc[-2])
    vol_hoy = float(vol.iloc[-1]) if pd.notna(vol.iloc[-1]) else 0
    vol_prom = float(vol_avg.iloc[-1]) if pd.notna(vol_avg.iloc[-1]) else 1

    vol_confirmado = vol_hoy > vol_prom * 1.5

    # Detectar cruce reciente (últimos 5 días)
    tipo_cruce = None
    for i in range(-5, 0):
        if i - 1 < -len(ma50):
            continue
        m50_prev = float(ma50.iloc[i - 1]) if pd.notna(ma50.iloc[i - 1]) else 0
        m200_prev = float(ma200.iloc[i - 1]) if pd.notna(ma200.iloc[i - 1]) else 0
        m50_curr = float(ma50.iloc[i]) if pd.notna(ma50.iloc[i]) else 0
        m200_curr = float(ma200.iloc[i]) if pd.notna(ma200.iloc[i]) else 0

        if m50_prev <= m200_prev and m50_curr > m200_curr:
            tipo_cruce = "GOLDEN"
            break
        elif m50_prev >= m200_prev and m50_curr < m200_curr:
            tipo_cruce = "DEATH"
            break

    # Señal
    if tipo_cruce == "GOLDEN":
        senal = "ALCISTA" if vol_confirmado else "ALCISTA_DEBIL"
    elif tipo_cruce == "DEATH":
        senal = "BAJISTA" if vol_confirmado else "BAJISTA_DEBIL"
    elif ma50_hoy > ma200_hoy:
        senal = "NEUTRAL"  # Por encima pero sin cruce reciente
        tipo_cruce = "MA50>MA200"
    else:
        senal = "NEUTRAL"
        tipo_cruce = "MA50<MA200"

    pct_gap = ((ma50_hoy / ma200_hoy) - 1) * 100 if ma200_hoy > 0 else 0

    return {
        "senal": senal,
        "tipo_cruce": tipo_cruce,
        "ma50": round(ma50_hoy, 2),
        "ma200": round(ma200_hoy, 2),
        "gap_pct": round(pct_gap, 2),
        "vol_confirmado": vol_confirmado,
    }


# ══════════════════════════════════════════════════════════════
#  SEÑAL COMBINADA
# ══════════════════════════════════════════════════════════════

def evaluar_activo(simbolo):
    """Evalúa las 3 estrategias y genera señal combinada."""
    df = _descargar(simbolo)

    mom = momentum_12_1(df)
    rsi = rsi_semanal(df)
    cross = golden_death_cross(df)

    alcistas = sum(1 for s in [mom, rsi, cross] if s["senal"] in ("ALCISTA", "ALCISTA_DEBIL"))
    bajistas = sum(1 for s in [mom, rsi, cross] if s["senal"] in ("BAJISTA", "BAJISTA_DEBIL"))

    if alcistas >= 2:
        combinada = "COMPRA_FUERTE"
    elif bajistas >= 2:
        combinada = "VENTA_FUERTE"
    elif alcistas == 1 and bajistas == 0:
        combinada = "COMPRA_LEVE"
    elif bajistas == 1 and alcistas == 0:
        combinada = "VENTA_LEVE"
    else:
        combinada = "NEUTRAL"

    return {
        "simbolo": simbolo,
        "combinada": combinada,
        "momentum": mom,
        "rsi_semanal": rsi,
        "cross": cross,
        "alcistas": alcistas,
        "bajistas": bajistas,
    }


def get_senales_quant(activos=None):
    """Evalúa los 22 activos y retorna dict con señales quant."""
    if activos is None:
        activos = ACTIVOS
    resultados = {}
    for sym in activos:
        resultados[sym] = evaluar_activo(sym)
    return resultados


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 90)
    print(f"  SEÑALES QUANT — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Estrategias: Momentum 12-1 | RSI semanal | Golden/Death Cross")
    print("=" * 90)

    hdr = (f"  {'Sym':<6} {'Combinada':<14} {'Mom12-1':>8} {'RSIsem':>7} "
           f"{'Cross':<14} {'Gap%':>6} {'Detalles'}")
    print(hdr)
    print(f"  {'─' * 84}")

    compra_fuerte = []
    venta_fuerte = []

    for sym in ACTIVOS:
        print(f"  {sym:<6}", end="", flush=True)
        r = evaluar_activo(sym)
        comb = r["combinada"]
        mom = r["momentum"]
        rsi = r["rsi_semanal"]
        cross = r["cross"]

        mom_str = f"{mom['retorno_pct']:+.1f}%" if mom.get("retorno_pct") is not None else "N/D"
        rsi_str = f"{rsi['rsi']:.0f}" if rsi.get("rsi") is not None else "N/D"
        cross_str = cross.get("tipo_cruce", "N/D")
        gap_str = f"{cross['gap_pct']:+.1f}%" if cross.get("gap_pct") is not None else ""

        detalles = []
        if mom["senal"] != "NEUTRAL":
            detalles.append(f"Mom:{mom['senal']}")
        if rsi["senal"] != "NEUTRAL":
            detalles.append(f"RSI:{rsi['senal']}")
        if cross["senal"] not in ("NEUTRAL", "N/D"):
            v = "+vol" if cross.get("vol_confirmado") else ""
            detalles.append(f"Cross:{cross['senal']}{v}")

        marca = ""
        if comb == "COMPRA_FUERTE":
            marca = " <<"
            compra_fuerte.append(sym)
        elif comb == "VENTA_FUERTE":
            marca = " !!"
            venta_fuerte.append(sym)

        print(f" {comb:<14} {mom_str:>8} {rsi_str:>7} "
              f"{cross_str:<14} {gap_str:>6} {' | '.join(detalles)}{marca}")

    print(f"\n{'=' * 90}")
    print(f"  RESUMEN:")
    if compra_fuerte:
        print(f"  COMPRA FUERTE (2+ estrategias alcistas): {', '.join(compra_fuerte)}")
    if venta_fuerte:
        print(f"  VENTA FUERTE (2+ estrategias bajistas): {', '.join(venta_fuerte)}")
    if not compra_fuerte and not venta_fuerte:
        print(f"  Sin señales fuertes. Mercado sin consenso quant.")
    print(f"{'=' * 90}")
