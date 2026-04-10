#!/home/hproano/asistente_env/bin/python
"""
Régimen de mercado — Clasifica BULL / BEAR / LATERAL.
Ajusta parámetros de trading según el régimen actual.
Integra con jarvis_trading.py para adaptar la estrategia dinámicamente.
"""

import os
import sys
import importlib.util
from datetime import datetime

import numpy as np
import yfinance as yf
import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES
MAX_POSICIONES_BASE = _cfg.MAX_POSICIONES
UMBRAL_COMPRA_BASE = 3

# ── Activos defensivos (permitidos en BEAR) ───────────────────
DEFENSIVOS = {"GLD", "AGG", "HYG", "IEF", "XLU", "D", "KO", "JNJ", "VZ", "T"}

# ── Umbrales de régimen ───────────────────────────────────────
# BULL: SPY sobre MA200 + VIX < 20 + F&G > 50
# BEAR: SPY bajo MA200 + VIX > 30 + F&G < 30
# LATERAL: todo lo demás

FNG_URL = "https://api.alternative.me/fng/?limit=1"


# ── Datos ─────────────────────────────────────────────────────

def obtener_spy_ma200():
    """Obtiene precio actual de SPY y su MA200."""
    try:
        ticker = yf.Ticker("SPY")
        hist = ticker.history(period="1y")
        if hist.empty or len(hist) < 200:
            return None, None, None

        precio_actual = float(hist["Close"].iloc[-1])
        ma200 = float(hist["Close"].rolling(200).mean().iloc[-1])
        pct_vs_ma200 = ((precio_actual / ma200) - 1) * 100

        return precio_actual, ma200, pct_vs_ma200
    except Exception as e:
        return None, None, None


def obtener_vix():
    """Obtiene VIX actual."""
    try:
        info = yf.Ticker("^VIX").fast_info
        return round(info["lastPrice"], 2)
    except Exception:
        return None


def obtener_fng():
    """Obtiene Fear & Greed Index actual."""
    try:
        resp = requests.get(FNG_URL, timeout=10)
        resp.raise_for_status()
        return int(resp.json()["data"][0]["value"])
    except Exception:
        return None


# ── Clasificación de régimen ──────────────────────────────────

def clasificar_regimen(spy_precio, spy_ma200, vix, fng):
    """
    Clasifica el régimen del mercado.
    Retorna: "BULL", "BEAR", o "LATERAL" con score de confianza.
    """
    if spy_precio is None or spy_ma200 is None or vix is None or fng is None:
        return "LATERAL", 0, "Datos insuficientes"

    spy_sobre_ma200 = spy_precio > spy_ma200
    pct_vs_ma200 = ((spy_precio / spy_ma200) - 1) * 100

    # Contar señales alcistas y bajistas
    bull_signals = 0
    bear_signals = 0
    detalles = []

    # SPY vs MA200
    if spy_sobre_ma200:
        bull_signals += 1
        detalles.append(f"SPY sobre MA200 ({pct_vs_ma200:+.1f}%)")
    else:
        bear_signals += 1
        detalles.append(f"SPY bajo MA200 ({pct_vs_ma200:+.1f}%)")

    # VIX
    if vix < 20:
        bull_signals += 1
        detalles.append(f"VIX bajo ({vix:.1f})")
    elif vix > 30:
        bear_signals += 1
        detalles.append(f"VIX alto ({vix:.1f})")
    else:
        detalles.append(f"VIX neutral ({vix:.1f})")

    # Fear & Greed
    if fng > 50:
        bull_signals += 1
        detalles.append(f"F&G alcista ({fng})")
    elif fng < 30:
        bear_signals += 1
        detalles.append(f"F&G temeroso ({fng})")
    else:
        detalles.append(f"F&G neutral ({fng})")

    # Clasificar
    if bull_signals == 3:
        regimen = "BULL"
        confianza = 3
    elif bear_signals == 3:
        regimen = "BEAR"
        confianza = 3
    elif bull_signals >= 2 and bear_signals == 0:
        regimen = "BULL"
        confianza = 2
    elif bear_signals >= 2 and bull_signals == 0:
        regimen = "BEAR"
        confianza = 2
    else:
        regimen = "LATERAL"
        confianza = 1

    razon = " | ".join(detalles)
    return regimen, confianza, razon


# ── Parámetros ajustados por régimen ──────────────────────────

def get_regimen_actual():
    """
    Determina el régimen actual del mercado y retorna parámetros ajustados.
    Retorna dict con toda la info necesaria para jarvis_trading.py.
    """
    spy_precio, spy_ma200, pct_vs_ma200 = obtener_spy_ma200()
    vix = obtener_vix()
    fng = obtener_fng()

    regimen, confianza, razon = clasificar_regimen(spy_precio, spy_ma200, vix, fng)

    # Ajustar parámetros según régimen
    if regimen == "BULL":
        activos_permitidos = list(ACTIVOS_OPERABLES)  # Todos
        max_posiciones = 10
        umbral_compra = 2
        nota = "Mercado alcista: universo completo, umbral bajo, más posiciones."
    elif regimen == "BEAR":
        activos_permitidos = [a for a in ACTIVOS_OPERABLES if a in DEFENSIVOS]
        max_posiciones = MAX_POSICIONES_BASE
        umbral_compra = UMBRAL_COMPRA_BASE
        nota = (f"Mercado bajista: solo defensivos "
                f"({', '.join(activos_permitidos)}), umbral estándar.")
    else:  # LATERAL
        activos_permitidos = list(ACTIVOS_OPERABLES)  # Todos pero conservador
        max_posiciones = MAX_POSICIONES_BASE
        umbral_compra = UMBRAL_COMPRA_BASE
        nota = "Mercado lateral: universo completo, parámetros estándar."

    return {
        "regimen": regimen,
        "tipo": regimen,  # alias para compatibilidad
        "confianza": confianza,
        "razon": razon,
        "nota": nota,
        # Datos crudos
        "spy_precio": spy_precio,
        "spy_ma200": spy_ma200,
        "spy_pct_vs_ma200": round(pct_vs_ma200, 2) if pct_vs_ma200 is not None else None,
        "vix": vix,
        "fng": fng,
        # Parámetros ajustados
        "activos_permitidos": activos_permitidos,
        "max_posiciones": max_posiciones,
        "umbral_compra": umbral_compra,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print(f"  RÉGIMEN DE MERCADO — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 70)

    print("\nObteniendo datos...")
    r = get_regimen_actual()

    print(f"\n  SPY:  ${r['spy_precio']:,.2f}" if r['spy_precio'] else "\n  SPY: N/D")
    if r['spy_ma200']:
        print(f"  MA200: ${r['spy_ma200']:,.2f} ({r['spy_pct_vs_ma200']:+.2f}%)")
    print(f"  VIX:  {r['vix']}" if r['vix'] else "  VIX: N/D")
    print(f"  F&G:  {r['fng']}/100" if r['fng'] else "  F&G: N/D")

    iconos = {"BULL": ">>", "BEAR": "<<", "LATERAL": "=="}
    print(f"\n  {iconos[r['regimen']]} RÉGIMEN: {r['regimen']} (confianza {r['confianza']}/3)")
    print(f"  {r['razon']}")
    print(f"  {r['nota']}")

    print(f"\n  Parámetros ajustados:")
    print(f"    Activos permitidos: {len(r['activos_permitidos'])}/{len(ACTIVOS_OPERABLES)}")
    print(f"    Max posiciones: {r['max_posiciones']}")
    print(f"    Umbral compra: score >= {r['umbral_compra']}")

    if r["regimen"] == "BEAR":
        print(f"\n  Activos defensivos permitidos:")
        for a in r["activos_permitidos"]:
            print(f"    {a}")

    print(f"\n{'=' * 70}")
