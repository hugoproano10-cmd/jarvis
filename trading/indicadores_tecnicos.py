#!/home/hproano/asistente_env/bin/python
"""
Indicadores técnicos para acciones monitoreadas.
Descarga datos históricos, calcula RSI, MACD, Bollinger Bands, SMAs y volumen,
genera señales de trading y exporta a JSON para JARVIS.
"""

import os
import sys
import json
import warnings
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore", category=FutureWarning)

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

import importlib.util as _ilu
_cfg_spec = _ilu.spec_from_file_location("trading_config",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
_cfg = _ilu.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
SIMBOLOS = _cfg.ACTIVOS_OPERABLES
PERIODO_MESES = _cfg.PERIODO_HISTORICO_MESES


# ── Descarga de datos ───────────────────────────────────────

def descargar_historico(simbolo, meses=PERIODO_MESES):
    """Descarga datos OHLCV históricos de yfinance."""
    fin = datetime.now()
    inicio = fin - timedelta(days=meses * 30)
    df = yf.download(
        simbolo,
        start=inicio.strftime("%Y-%m-%d"),
        end=fin.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        raise ValueError(f"Sin datos para {simbolo}")
    # Aplanar MultiIndex si existe (yfinance con un solo ticker a veces lo crea)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ── Cálculo de indicadores ──────────────────────────────────

def calcular_indicadores(df):
    """Calcula todos los indicadores técnicos sobre un DataFrame OHLCV."""
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # RSI(14)
    df["RSI_14"] = ta.rsi(close, length=14)

    # MACD(12, 26, 9)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    df["MACD"] = macd.iloc[:, 0]        # MACD line
    df["MACD_signal"] = macd.iloc[:, 2]  # Signal line
    df["MACD_hist"] = macd.iloc[:, 1]    # Histogram

    # Bollinger Bands(20, 2)
    bbands = ta.bbands(close, length=20, std=2)
    df["BB_upper"] = bbands.iloc[:, 2]   # Upper band
    df["BB_mid"] = bbands.iloc[:, 1]     # Middle band
    df["BB_lower"] = bbands.iloc[:, 0]   # Lower band

    # SMAs
    df["SMA_50"] = ta.sma(close, length=50)
    df["SMA_200"] = ta.sma(close, length=200)

    # Volumen promedio 20 días
    df["Vol_avg_20"] = volume.rolling(window=20).mean()

    return df


# ── Generación de señales ───────────────────────────────────

def generar_senal(df, simbolo):
    """Genera señal COMPRAR/VENDER/MANTENER basada en combinación de indicadores."""
    ultimo = df.iloc[-1]
    prev = df.iloc[-2]
    precio = float(ultimo["Close"])

    # Extraer valores actuales
    rsi = float(ultimo["RSI_14"]) if pd.notna(ultimo["RSI_14"]) else 50.0
    macd_val = float(ultimo["MACD"]) if pd.notna(ultimo["MACD"]) else 0.0
    macd_sig = float(ultimo["MACD_signal"]) if pd.notna(ultimo["MACD_signal"]) else 0.0
    macd_hist = float(ultimo["MACD_hist"]) if pd.notna(ultimo["MACD_hist"]) else 0.0
    macd_hist_prev = float(prev["MACD_hist"]) if pd.notna(prev["MACD_hist"]) else 0.0
    bb_upper = float(ultimo["BB_upper"]) if pd.notna(ultimo["BB_upper"]) else precio * 1.05
    bb_lower = float(ultimo["BB_lower"]) if pd.notna(ultimo["BB_lower"]) else precio * 0.95
    bb_mid = float(ultimo["BB_mid"]) if pd.notna(ultimo["BB_mid"]) else precio
    sma50 = float(ultimo["SMA_50"]) if pd.notna(ultimo["SMA_50"]) else precio
    sma200 = float(ultimo["SMA_200"]) if pd.notna(ultimo["SMA_200"]) else precio
    vol = float(ultimo["Volume"]) if pd.notna(ultimo["Volume"]) else 0
    vol_avg = float(ultimo["Vol_avg_20"]) if pd.notna(ultimo["Vol_avg_20"]) else vol

    # ── Sistema de puntuación ──
    # Rango: -10 (fuerte venta) a +10 (fuerte compra)
    puntos = 0
    razones = []

    # RSI
    if rsi < 30:
        puntos += 2
        razones.append(f"RSI en sobreventa ({rsi:.0f})")
    elif rsi < 40:
        puntos += 1
        razones.append(f"RSI bajo ({rsi:.0f})")
    elif rsi > 70:
        puntos -= 2
        razones.append(f"RSI en sobrecompra ({rsi:.0f})")
    elif rsi > 60:
        puntos -= 1
        razones.append(f"RSI elevado ({rsi:.0f})")

    # MACD: cruce y dirección del histograma
    if macd_val > macd_sig and macd_hist > macd_hist_prev:
        puntos += 2
        razones.append("MACD alcista con histograma creciente")
    elif macd_val > macd_sig:
        puntos += 1
        razones.append("MACD por encima de señal")
    elif macd_val < macd_sig and macd_hist < macd_hist_prev:
        puntos -= 2
        razones.append("MACD bajista con histograma decreciente")
    elif macd_val < macd_sig:
        puntos -= 1
        razones.append("MACD por debajo de señal")

    # Bollinger Bands
    if precio <= bb_lower:
        puntos += 2
        razones.append("Precio en banda inferior de Bollinger (soporte)")
    elif precio >= bb_upper:
        puntos -= 2
        razones.append("Precio en banda superior de Bollinger (resistencia)")
    elif precio < bb_mid:
        puntos += 1
        razones.append("Precio debajo de media de Bollinger")

    # SMAs: tendencia
    if sma50 > sma200:
        puntos += 1
        razones.append("Golden cross (SMA50 > SMA200)")
    elif sma50 < sma200:
        puntos -= 1
        razones.append("Death cross (SMA50 < SMA200)")

    if precio > sma50:
        puntos += 1
        razones.append("Precio sobre SMA50")
    else:
        puntos -= 1
        razones.append("Precio bajo SMA50")

    # Volumen
    if vol_avg > 0 and vol > vol_avg * 1.5:
        razones.append(f"Volumen alto ({vol/vol_avg:.1f}x promedio)")

    # ── Decisión final ──
    if puntos >= 3:
        senal = "COMPRAR"
    elif puntos <= -3:
        senal = "VENDER"
    else:
        senal = "MANTENER"

    return {
        "simbolo": simbolo,
        "precio": round(precio, 2),
        "senal": senal,
        "puntuacion": puntos,
        "razones": razones,
        "indicadores": {
            "rsi_14": round(rsi, 2),
            "macd": round(macd_val, 4),
            "macd_signal": round(macd_sig, 4),
            "macd_hist": round(macd_hist, 4),
            "bb_upper": round(bb_upper, 2),
            "bb_mid": round(bb_mid, 2),
            "bb_lower": round(bb_lower, 2),
            "sma_50": round(sma50, 2),
            "sma_200": round(sma200, 2),
            "volumen": int(vol),
            "vol_promedio_20d": int(vol_avg),
        },
    }


# ── Resumen en español ─────────────────────────────────────

def imprimir_resumen(senales):
    """Imprime resumen formateado de todas las señales."""
    print("=" * 60)
    print(f"  INDICADORES TÉCNICOS — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    for s in senales:
        icono = {"COMPRAR": "[+]", "VENDER": "[-]", "MANTENER": "[=]"}[s["senal"]]
        ind = s["indicadores"]

        print(f"\n  {icono} {s['simbolo']}  —  ${s['precio']:,.2f}  —  {s['senal']} (puntuación: {s['puntuacion']:+d})")
        print(f"      RSI: {ind['rsi_14']:.0f} | MACD: {ind['macd']:.4f} (señal: {ind['macd_signal']:.4f})")
        print(f"      BB: [{ind['bb_lower']:.2f} — {ind['bb_mid']:.2f} — {ind['bb_upper']:.2f}]")
        print(f"      SMA50: {ind['sma_50']:.2f} | SMA200: {ind['sma_200']:.2f}")
        print(f"      Volumen: {ind['volumen']:,} (prom 20d: {ind['vol_promedio_20d']:,})")
        print(f"      Razones:")
        for r in s["razones"]:
            print(f"        - {r}")

    print("\n" + "=" * 60)


# ── Exportar a JSON ─────────────────────────────────────────

def guardar_json(senales):
    """Guarda señales en datos/indicadores_FECHA.json."""
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)

    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_datos, f"indicadores_{fecha}.json")

    salida = {
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "activos": senales,
    }

    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    return os.path.abspath(ruta)


# ── Main ────────────────────────────────────────────────────

def main():
    senales = []

    for simbolo in SIMBOLOS:
        try:
            print(f"  Descargando {simbolo}...", end=" ", flush=True)
            df = descargar_historico(simbolo)
            df = calcular_indicadores(df)
            senal = generar_senal(df, simbolo)
            senales.append(senal)
            print(f"OK ({len(df)} velas)")
        except Exception as e:
            print(f"ERROR: {e}")
            senales.append({
                "simbolo": simbolo,
                "senal": "ERROR",
                "error": str(e),
            })

    imprimir_resumen([s for s in senales if "error" not in s])

    ruta = guardar_json(senales)
    print(f"  Datos guardados en: {ruta}")


if __name__ == "__main__":
    main()
