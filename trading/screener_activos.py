#!/home/hproano/asistente_env/bin/python
"""
Screener de activos para la estrategia JARVIS.
Evalúa candidatos con backtest de 2 años usando SL-10%/TP+20%
y los rankea por Sharpe ratio.
"""

import os
import sys
import time
import math
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
import pandas_ta as ta

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PERIODO_ANOS = 2
CAPITAL_INICIAL = 10000.0

# Parámetros optimizados
STOP_LOSS_PCT = 0.10
TAKE_PROFIT_PCT = 0.20
UMBRAL_COMPRA = 3

# ── Candidatos por categoría ────────────────────────────────

CANDIDATOS = {
    "ETF Sectorial": [
        ("QQQ",  "Nasdaq 100"),
        ("XLK",  "Tecnología"),
        ("XLF",  "Financiero"),
        ("XLE",  "Energía"),
        ("XLV",  "Salud"),
        ("SOXX", "Semiconductores"),
        ("IWM",  "Small Caps"),
        ("GLD",  "Oro"),
    ],
    "Tech": [
        ("MSFT", "Microsoft"),
        ("GOOGL","Alphabet"),
        ("META", "Meta"),
        ("AMZN", "Amazon"),
        ("AMD",  "AMD"),
        ("NFLX", "Netflix"),
        ("CRM",  "Salesforce"),
    ],
    "Dividendos": [
        ("JNJ",  "Johnson & Johnson"),
        ("KO",   "Coca-Cola"),
        ("PEP",  "PepsiCo"),
        ("PG",   "Procter & Gamble"),
        ("XOM",  "Exxon Mobil"),
        ("MCD",  "McDonald's"),
        ("VZ",   "Verizon"),
    ],
    "Cripto": [
        ("BTC-USD", "Bitcoin"),
        ("ETH-USD", "Ethereum"),
    ],
}


# ── Funciones de backtest (misma lógica que backtesting.py) ──

def descargar(simbolo, intentos=3):
    fin = datetime.now()
    inicio = fin - timedelta(days=PERIODO_ANOS * 365)
    for i in range(intentos):
        df = yf.download(simbolo, start=inicio.strftime("%Y-%m-%d"),
                         end=fin.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        if i < intentos - 1:
            time.sleep(2)
    return None


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

    # Cerrar posición abierta
    if pos:
        pf = float(d.iloc[-1]["Close"])
        pnl = (pf - pos["pe"]) * pos["q"]
        pnl_p = (pf / pos["pe"] - 1) * 100
        capital += pos["q"] * pf
        trades.append({"pnl": pnl, "pnl_p": pnl_p, "mot": "cierre"})

    ef = capital
    ea = np.array(equity) if equity else np.array([CAPITAL_INICIAL])
    ret = ((ef / CAPITAL_INICIAL) - 1) * 100

    # Buy & hold
    p0 = float(d.iloc[0]["Close"])
    pf = float(d.iloc[-1]["Close"])
    bh = ((pf / p0) - 1) * 100

    gan = [t for t in trades if t["pnl"] > 0]
    per = [t for t in trades if t["pnl"] < 0]
    n = len(trades)
    wr = (len(gan) / n * 100) if n > 0 else 0

    # Drawdown
    pk = np.maximum.accumulate(ea)
    dd = ((ea - pk) / pk).min() * 100

    # Sharpe
    if len(ea) > 1:
        rets = np.diff(ea) / ea[:-1]
        sharpe = (rets.mean() / rets.std()) * math.sqrt(252) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    gb = sum(t["pnl"] for t in gan)
    pb = abs(sum(t["pnl"] for t in per))
    pf_r = (gb / pb) if pb > 0 else (float("inf") if gb > 0 else 0)

    # Liquidez promedio (vol diario en USD)
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
        "vol_usd_avg": round(vol_usd_avg / 1e6, 1),  # en millones
        "dias": len(d),
    }


# ── Main ────────────────────────────────────────────────────

def main():
    print("=" * 90)
    print(f"  SCREENER DE ACTIVOS JARVIS — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Estrategia: Score >= {UMBRAL_COMPRA} | SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}% | {PERIODO_ANOS} años")
    print("=" * 90)

    todos = []

    for cat, activos in CANDIDATOS.items():
        print(f"\n  --- {cat} ---")
        for ticker, nombre in activos:
            print(f"    {ticker:>8} ({nombre})...", end=" ", flush=True)
            df = descargar(ticker)
            if df is None or len(df) < 220:
                print("SKIP (sin datos)")
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
            print(f"ret {r['ret']:+7.2f}% | sharpe {r['sharpe']:+.2f} | wr {r['wr']:5.1f}% | {r['trades']}T | vol ${r['vol_usd_avg']:.0f}M/d")

    # Ordenar por Sharpe
    todos.sort(key=lambda x: x["sharpe"], reverse=True)

    # ── Tabla completa ──
    print(f"\n{'=' * 90}")
    print(f"  RANKING COMPLETO POR SHARPE RATIO")
    print(f"{'=' * 90}")
    hdr = (f"  {'#':>2}  {'Ticker':<8} {'Nombre':<20} {'Cat':<14} "
           f"{'Ret%':>7} {'B&H%':>7} {'Alfa':>7} {'T':>3} {'WR%':>6} "
           f"{'Sharpe':>7} {'DD%':>7} {'PF':>5} {'Vol$M':>6}")
    print(hdr)
    print(f"  {'─' * 86}")

    for i, r in enumerate(todos, 1):
        mark = " *" if i <= 10 else "  "
        print(
            f"{mark}{i:>2}  {r['ticker']:<8} {r['nombre']:<20} {r['categoria']:<14} "
            f"{r['ret']:>+7.2f} {r['bh']:>+7.2f} {r['alfa']:>+7.2f} "
            f"{r['trades']:>3} {r['wr']:>5.1f}% {r['sharpe']:>+7.2f} "
            f"{r['dd']:>6.2f}% {r['pf']:>5.2f} {r['vol_usd_avg']:>6.1f}"
        )

    # ── Top 10 recomendados ──
    top10 = todos[:10]

    print(f"\n{'=' * 90}")
    print(f"  TOP 10 RECOMENDADOS PARA AGREGAR A JARVIS")
    print(f"{'=' * 90}")

    for i, r in enumerate(top10, 1):
        print(f"\n  {i:>2}. {r['ticker']} — {r['nombre']} ({r['categoria']})")
        print(f"      Retorno: {r['ret']:+.2f}% | Buy&Hold: {r['bh']:+.2f}% | Alfa: {r['alfa']:+.2f}pp")
        print(f"      Trades: {r['trades']} ({r['wins']}W/{r['losses']}L) | Win rate: {r['wr']:.1f}%")
        print(f"      Sharpe: {r['sharpe']:.2f} | Max DD: {r['dd']:.2f}% | Profit factor: {r['pf']:.2f}")
        print(f"      Liquidez: ${r['vol_usd_avg']:.0f}M/día promedio")

    # ── Resumen por categoría ──
    print(f"\n{'=' * 90}")
    print(f"  RESUMEN POR CATEGORÍA EN EL TOP 10")
    print(f"{'=' * 90}")
    cats = {}
    for r in top10:
        cats.setdefault(r["categoria"], []).append(r["ticker"])
    for cat, tickers in cats.items():
        print(f"    {cat:<16}: {', '.join(tickers)}")

    # ── Recomendación final ──
    rentables = [r for r in top10 if r["ret"] > 0 and r["sharpe"] > 0]
    print(f"\n  {'─' * 86}")
    print(f"  RECOMENDACIÓN FINAL:")
    print(f"  {'─' * 86}")
    if rentables:
        tickers_rec = [r["ticker"] for r in rentables]
        print(f"  Activos rentables con Sharpe > 0: {', '.join(tickers_rec)}")
        sharpe_prom = np.mean([r["sharpe"] for r in rentables])
        ret_prom = np.mean([r["ret"] for r in rentables])
        print(f"  Sharpe promedio: {sharpe_prom:.2f} | Retorno promedio: {ret_prom:+.2f}%")
        print(f"\n  Agregar a config.py:")
        actuales = ["SPY", "AAPL", "TSLA"]
        nuevos = [t for t in tickers_rec if t not in actuales]
        combo = actuales + nuevos
        print(f"    ACTIVOS_OPERABLES = {combo}")
    else:
        print(f"  Ningún candidato logra Sharpe > 0 con retorno positivo.")
        print(f"  Top 3 por Sharpe (menos malos):")
        for r in top10[:3]:
            print(f"    {r['ticker']}: sharpe {r['sharpe']:.2f}, ret {r['ret']:+.2f}%")

    print(f"\n{'=' * 90}")

    # Guardar
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_datos, f"screener_{fecha}.txt")

    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"Screener de activos — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        f.write(f"Estrategia: Score >= {UMBRAL_COMPRA} | SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}%\n\n")
        f.write(f"{'Ticker':<10} {'Cat':<16} {'Ret%':>8} {'Sharpe':>8} {'WR%':>7} {'Trades':>7} {'DD%':>8} {'PF':>7}\n")
        f.write("-" * 75 + "\n")
        for r in todos:
            f.write(f"{r['ticker']:<10} {r['categoria']:<16} {r['ret']:>+8.2f} {r['sharpe']:>+8.2f} {r['wr']:>6.1f}% {r['trades']:>7} {r['dd']:>7.2f}% {r['pf']:>7.2f}\n")

    print(f"  Datos guardados en: {ruta}")


if __name__ == "__main__":
    main()
