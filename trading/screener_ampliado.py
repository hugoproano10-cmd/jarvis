#!/home/hproano/asistente_env/bin/python
"""
Screener ampliado con datos Tiingo (30 años disponibles, backtest 2 años).
Evalúa candidatos nuevos para agregar a config.py.
Estrategia: Score >= 3 | SL: -10% | TP: +20%
Criterio: Sharpe ratio > 0.5
"""

import os
import sys
import math
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from datos.fuentes_mercado import get_tiingo_historico

# ── Parámetros ─────────────────────────────────────────────────
PERIODO_ANOS = 2
CAPITAL_INICIAL = 10000.0
STOP_LOSS_PCT = 0.10
TAKE_PROFIT_PCT = 0.20
UMBRAL_COMPRA = 3
SHARPE_MINIMO = 0.5

# ── Candidatos ─────────────────────────────────────────────────
CANDIDATOS = {
    "ETF Sectorial": [
        ("XLF", "Financiero"),
        ("XLV", "Salud"),
        ("XLI", "Industrial"),
        ("XLB", "Materiales"),
        ("XLU", "Utilities"),
        ("XLC", "Comunicaciones"),
        ("XLY", "Consumo Discr."),
        ("XLK", "Tecnología"),
    ],
    "Dividendos": [
        ("PG",  "Procter & Gamble"),
        ("MMM", "3M"),
        ("T",   "AT&T"),
        ("IBM", "IBM"),
        ("CVX", "Chevron"),
        ("PFE", "Pfizer"),
        ("ABT", "Abbott Labs"),
        ("D",   "Dominion Energy"),
    ],
    "Internacional": [
        ("EEM", "Emerg. Markets"),
        ("EFA", "EAFE Developed"),
        ("VWO", "Vanguard EM"),
        ("FXI", "China Large Cap"),
    ],
    "Bonos/Cobertura": [
        ("TLT", "Bonos 20+ años"),
        ("IEF", "Bonos 7-10 años"),
        ("AGG", "Aggregate Bond"),
        ("HYG", "High Yield Corp"),
    ],
}

# Activos actuales en config.py (para evitar duplicados)
ACTUALES = {"XOM", "JNJ", "GLD", "VZ", "META", "SOXX", "MCD", "KO", "XLE", "SPY", "TSLA", "AAPL"}


# ── Tiingo → DataFrame ────────────────────────────────────────

def descargar_tiingo(simbolo):
    """Descarga datos de Tiingo y convierte a DataFrame compatible."""
    hist = get_tiingo_historico(simbolo, anios=3)  # 3 años para tener 200 SMA desde el día 1
    if hist.get("error") or not hist.get("datos"):
        return None

    datos = hist["datos"]
    df = pd.DataFrame(datos)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df = df.set_index("fecha")
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    # Filtrar a últimos 2 años + margen para SMA200
    corte = datetime.now() - timedelta(days=PERIODO_ANOS * 365 + 250)
    df = df[df.index >= corte.strftime("%Y-%m-%d")]

    if len(df) < 220:
        return None
    return df


# ── Indicadores y score (misma lógica que screener_activos.py) ─

def indicadores(df):
    close, volume = df["Close"], df["Volume"]
    df["RSI_14"] = ta.rsi(close, length=14)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    df["MACD"] = macd.iloc[:, 0]
    df["MACD_signal"] = macd.iloc[:, 2]
    df["MACD_hist"] = macd.iloc[:, 1]
    bbands = ta.bbands(close, length=20, std=2)
    df["BB_upper"] = bbands.iloc[:, 2]
    df["BB_mid"] = bbands.iloc[:, 1]
    df["BB_lower"] = bbands.iloc[:, 0]
    df["SMA_50"] = ta.sma(close, length=50)
    df["SMA_200"] = ta.sma(close, length=200)
    df["Vol_avg_20"] = volume.rolling(window=20).mean()
    return df


def score(row, prev):
    precio = row["Close"]
    pts = 0
    rsi = row["RSI_14"] if pd.notna(row["RSI_14"]) else 50.0
    mv = row["MACD"] if pd.notna(row["MACD"]) else 0.0
    ms = row["MACD_signal"] if pd.notna(row["MACD_signal"]) else 0.0
    mh = row["MACD_hist"] if pd.notna(row["MACD_hist"]) else 0.0
    mhp = prev["MACD_hist"] if pd.notna(prev["MACD_hist"]) else 0.0
    bbu = row["BB_upper"] if pd.notna(row["BB_upper"]) else precio * 1.05
    bbl = row["BB_lower"] if pd.notna(row["BB_lower"]) else precio * 0.95
    bbm = row["BB_mid"] if pd.notna(row["BB_mid"]) else precio
    s50 = row["SMA_50"] if pd.notna(row["SMA_50"]) else precio
    s200 = row["SMA_200"] if pd.notna(row["SMA_200"]) else precio

    if rsi < 30: pts += 2
    elif rsi < 40: pts += 1
    elif rsi > 70: pts -= 2
    elif rsi > 60: pts -= 1

    if mv > ms and mh > mhp: pts += 2
    elif mv > ms: pts += 1
    elif mv < ms and mh < mhp: pts -= 2
    elif mv < ms: pts -= 1

    if precio <= bbl: pts += 2
    elif precio >= bbu: pts -= 2
    elif precio < bbm: pts += 1

    if s50 > s200: pts += 1
    elif s50 < s200: pts -= 1

    if precio > s50: pts += 1
    else: pts -= 1

    return pts


def backtest(df):
    idx0 = df["SMA_200"].first_valid_index()
    if idx0 is None:
        return None
    d = df.loc[idx0:].copy()
    if len(d) < 20:
        return None

    capital = CAPITAL_INICIAL
    pos = None
    trades = []
    equity = []

    for i in range(1, len(d)):
        row, prev = d.iloc[i], d.iloc[i - 1]
        precio = float(row["Close"])
        hi, lo = float(row["High"]), float(row["Low"])

        if pos is not None:
            hs = lo <= pos["sl"]
            ht = hi >= pos["tp"]
            px = mot = None
            if hs and ht:
                px, mot = pos["sl"], "stop-loss"
            elif hs:
                px, mot = pos["sl"], "stop-loss"
            elif ht:
                px, mot = pos["tp"], "take-profit"
            if px:
                pnl = (px - pos["pe"]) * pos["q"]
                pnl_p = (px / pos["pe"] - 1) * 100
                capital += pos["q"] * px
                trades.append({"pnl": pnl, "pnl_p": pnl_p, "mot": mot})
                pos = None

        if pos is None:
            s = score(row, prev)
            if s >= UMBRAL_COMPRA:
                q = math.floor(capital / precio)
                if q > 0:
                    capital -= q * precio
                    pos = {
                        "pe": precio, "q": q,
                        "sl": round(precio * (1 - STOP_LOSS_PCT), 2),
                        "tp": round(precio * (1 + TAKE_PROFIT_PCT), 2),
                    }

        vp = pos["q"] * precio if pos else 0
        equity.append(capital + vp)

    if pos:
        pf = float(d.iloc[-1]["Close"])
        pnl = (pf - pos["pe"]) * pos["q"]
        pnl_p = (pf / pos["pe"] - 1) * 100
        capital += pos["q"] * pf
        trades.append({"pnl": pnl, "pnl_p": pnl_p, "mot": "cierre"})

    ef = capital
    ea = np.array(equity) if equity else np.array([CAPITAL_INICIAL])
    ret = ((ef / CAPITAL_INICIAL) - 1) * 100

    p0 = float(d.iloc[0]["Close"])
    pfin = float(d.iloc[-1]["Close"])
    bh = ((pfin / p0) - 1) * 100

    gan = [t for t in trades if t["pnl"] > 0]
    per = [t for t in trades if t["pnl"] < 0]
    n = len(trades)
    wr = (len(gan) / n * 100) if n > 0 else 0

    pk = np.maximum.accumulate(ea)
    dd = ((ea - pk) / pk).min() * 100

    if len(ea) > 1:
        rets = np.diff(ea) / ea[:-1]
        sharpe = (rets.mean() / rets.std()) * math.sqrt(252) if rets.std() > 0 else 0
    else:
        sharpe = 0

    gb = sum(t["pnl"] for t in gan)
    pb = abs(sum(t["pnl"] for t in per))
    pf_r = (gb / pb) if pb > 0 else (float("inf") if gb > 0 else 0)

    vol_usd_avg = (d["Close"] * d["Volume"]).tail(60).mean()

    return {
        "ret": round(ret, 2),
        "bh": round(bh, 2),
        "alfa": round(ret - bh, 2),
        "trades": n,
        "wins": len(gan),
        "losses": len(per),
        "wr": round(wr, 1),
        "sharpe": round(sharpe, 2),
        "dd": round(dd, 2),
        "pf": round(pf_r, 2),
        "vol_usd_avg": round(vol_usd_avg / 1e6, 1),
        "dias": len(d),
    }


# ── Main ───────────────────────────────────────────────────────

def main():
    print("=" * 95)
    print(f"  SCREENER AMPLIADO (Tiingo) — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Estrategia: Score >= {UMBRAL_COMPRA} | SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}% | {PERIODO_ANOS} años | Sharpe mín: {SHARPE_MINIMO}")
    print("=" * 95)

    todos = []

    for cat, activos in CANDIDATOS.items():
        print(f"\n  --- {cat} ---")
        for ticker, nombre in activos:
            print(f"    {ticker:>5} ({nombre:<20})...", end=" ", flush=True)
            df = descargar_tiingo(ticker)
            if df is None:
                print("SKIP (sin datos Tiingo)")
                continue
            df = indicadores(df)
            r = backtest(df)
            if r is None:
                print("SKIP (datos insuficientes)")
                continue
            r["ticker"] = ticker
            r["nombre"] = nombre
            r["categoria"] = cat
            todos.append(r)
            marca = " <<<" if r["sharpe"] >= SHARPE_MINIMO else ""
            print(f"ret {r['ret']:+7.2f}% | sharpe {r['sharpe']:+.2f} | "
                  f"wr {r['wr']:5.1f}% | {r['trades']}T | dd {r['dd']:.1f}% | "
                  f"vol ${r['vol_usd_avg']:.0f}M/d{marca}")

    todos.sort(key=lambda x: x["sharpe"], reverse=True)

    # ── Ranking completo ──
    print(f"\n{'=' * 95}")
    print(f"  RANKING COMPLETO POR SHARPE RATIO (24 candidatos, datos Tiingo)")
    print(f"{'=' * 95}")
    hdr = (f"  {'#':>2}  {'Ticker':<6} {'Nombre':<20} {'Categoría':<16} "
           f"{'Ret%':>7} {'B&H%':>7} {'Alfa':>7} {'T':>3} {'WR%':>6} "
           f"{'Sharpe':>7} {'DD%':>7} {'PF':>5} {'Vol$M':>6}")
    print(hdr)
    print(f"  {'─' * 91}")

    for i, r in enumerate(todos, 1):
        pasa = r["sharpe"] >= SHARPE_MINIMO
        mark = " *" if pasa else "  "
        print(
            f"{mark}{i:>2}  {r['ticker']:<6} {r['nombre']:<20} {r['categoria']:<16} "
            f"{r['ret']:>+7.2f} {r['bh']:>+7.2f} {r['alfa']:>+7.2f} "
            f"{r['trades']:>3} {r['wr']:>5.1f}% {r['sharpe']:>+7.2f} "
            f"{r['dd']:>6.1f}% {r['pf']:>5.2f} {r['vol_usd_avg']:>6.1f}"
        )

    # ── Filtrar Sharpe > 0.5 ──
    aprobados = [r for r in todos if r["sharpe"] >= SHARPE_MINIMO]

    print(f"\n{'=' * 95}")
    print(f"  ACTIVOS CON SHARPE > {SHARPE_MINIMO} ({len(aprobados)} de {len(todos)})")
    print(f"{'=' * 95}")

    if not aprobados:
        print(f"  Ningún candidato supera Sharpe {SHARPE_MINIMO}.")
        print(f"  Top 5 más cercanos:")
        for r in todos[:5]:
            print(f"    {r['ticker']:<6} sharpe {r['sharpe']:+.2f} | ret {r['ret']:+.2f}% | {r['categoria']}")
    else:
        for i, r in enumerate(aprobados, 1):
            ya_existe = " (YA EN CONFIG)" if r["ticker"] in ACTUALES else ""
            print(f"\n  {i:>2}. {r['ticker']} — {r['nombre']} ({r['categoria']}){ya_existe}")
            print(f"      Retorno: {r['ret']:+.2f}% | Buy&Hold: {r['bh']:+.2f}% | Alfa: {r['alfa']:+.2f}pp")
            print(f"      Trades: {r['trades']} ({r['wins']}W/{r['losses']}L) | Win rate: {r['wr']:.1f}%")
            print(f"      Sharpe: {r['sharpe']:.2f} | Max DD: {r['dd']:.2f}% | Profit factor: {r['pf']:.2f}")
            print(f"      Liquidez: ${r['vol_usd_avg']:.0f}M/día promedio")

    # ── Recomendación top 10 ──
    nuevos = [r for r in aprobados if r["ticker"] not in ACTUALES][:10]

    print(f"\n{'=' * 95}")
    print(f"  RECOMENDACIÓN: TOP 10 NUEVOS ACTIVOS PARA config.py")
    print(f"{'=' * 95}")

    if not nuevos:
        print(f"  Sin candidatos nuevos con Sharpe > {SHARPE_MINIMO}.")
        # Mostrar los mejores de todos modos
        todos_nuevos = [r for r in todos if r["ticker"] not in ACTUALES][:10]
        print(f"  Top 10 por Sharpe (sin filtro):")
        for i, r in enumerate(todos_nuevos, 1):
            print(f"    {i:>2}. {r['ticker']:<6} sharpe {r['sharpe']:+.2f} | ret {r['ret']:+.2f}% | {r['categoria']}")
        nuevos = todos_nuevos  # Para la línea de config
    else:
        for i, r in enumerate(nuevos, 1):
            print(f"  {i:>2}. {r['ticker']:<6} — {r['nombre']:<20} ({r['categoria']:<16}) "
                  f"Sharpe: {r['sharpe']:+.2f} | Ret: {r['ret']:+.2f}% | WR: {r['wr']:.0f}%")

    # Resumen por categoría
    print(f"\n  Distribución por categoría:")
    cats = {}
    for r in nuevos:
        cats.setdefault(r["categoria"], []).append(r["ticker"])
    for cat, tickers in sorted(cats.items()):
        print(f"    {cat:<20}: {', '.join(tickers)}")

    # Línea para config.py
    actuales_lista = ["XOM", "JNJ", "GLD", "VZ", "META", "SOXX", "MCD", "KO", "XLE", "SPY", "TSLA", "AAPL"]
    nuevos_tickers = [r["ticker"] for r in nuevos]

    print(f"\n  Para agregar a config.py (12 actuales + {len(nuevos_tickers)} nuevos = {12 + len(nuevos_tickers)}):")
    print(f"  ACTIVOS_OPERABLES = {actuales_lista + nuevos_tickers}")

    sharpe_prom = np.mean([r["sharpe"] for r in nuevos]) if nuevos else 0
    ret_prom = np.mean([r["ret"] for r in nuevos]) if nuevos else 0
    print(f"\n  Sharpe promedio nuevos: {sharpe_prom:.2f} | Retorno promedio: {ret_prom:+.2f}%")
    print(f"{'=' * 95}")

    # Guardar
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_datos, f"screener_ampliado_{fecha}.txt")

    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"Screener ampliado Tiingo — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        f.write(f"Estrategia: Score >= {UMBRAL_COMPRA} | SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}% | Sharpe mín: {SHARPE_MINIMO}\n\n")
        f.write(f"{'Ticker':<8} {'Cat':<18} {'Ret%':>8} {'B&H%':>8} {'Sharpe':>8} {'WR%':>7} {'T':>4} {'DD%':>8} {'PF':>7} {'Vol$M':>7}\n")
        f.write("-" * 90 + "\n")
        for r in todos:
            pasa = "*" if r["sharpe"] >= SHARPE_MINIMO else " "
            f.write(f"{pasa}{r['ticker']:<7} {r['categoria']:<18} {r['ret']:>+8.2f} {r['bh']:>+8.2f} "
                    f"{r['sharpe']:>+8.2f} {r['wr']:>6.1f}% {r['trades']:>4} {r['dd']:>7.2f}% {r['pf']:>7.2f} {r['vol_usd_avg']:>7.1f}\n")

    print(f"\n  Datos guardados en: {ruta}")


if __name__ == "__main__":
    main()
