#!/home/hproano/asistente_env/bin/python
"""
Backtest de 3 estrategias cripto para BTC-USD y ETH-USD.
  1) Mean Reversion: comprar si cae >5% en 24h, TP +8%, SL -7%
  2) Momentum + Volumen: comprar si vol >2x avg Y precio sube >2%, TP +10%, SL -6%
  3) Fear & Greed + Precio: comprar si F&G < 25 Y caída >3%, TP +12%, SL -8%
"""

import os
import sys
import math
import warnings
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
import requests

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SIMBOLOS = ["BTC-USD", "ETH-USD"]
PERIODO_ANOS = 2
CAPITAL = 10000.0
FNG_URL = "https://api.alternative.me/fng/?limit=800&format=json"


# ── Datos ────────────────────────────────────────────────────

def descargar_cripto(simbolo, intentos=3):
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
    raise ValueError(f"Sin datos para {simbolo}")


def descargar_fng():
    """Descarga histórico de Fear & Greed Index y retorna Series indexada por fecha."""
    resp = requests.get(FNG_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()["data"]
    records = []
    for d in data:
        ts = int(d["timestamp"])
        fecha = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        records.append({"date": fecha, "fng": int(d["value"])})
    fng_df = pd.DataFrame(records)
    fng_df["date"] = pd.to_datetime(fng_df["date"])
    fng_df = fng_df.drop_duplicates(subset="date").set_index("date").sort_index()
    return fng_df["fng"]


def preparar_datos(df, fng_series):
    """Agrega columnas de variación diaria, volumen promedio, y F&G al DataFrame."""
    df = df.copy()
    df["ret_1d"] = df["Close"].pct_change() * 100  # variación % diaria
    df["vol_avg_20"] = df["Volume"].rolling(20).mean()
    df["vol_ratio"] = df["Volume"] / df["vol_avg_20"]

    # Unir F&G por fecha
    df["fng"] = np.nan
    for idx in df.index:
        fecha = idx.normalize()
        if fecha in fng_series.index:
            df.loc[idx, "fng"] = fng_series.loc[fecha]
    df["fng"] = df["fng"].ffill()

    return df


# ── Motor de backtest genérico ───────────────────────────────

def backtest(df, signal_fn, tp_pct, sl_pct, nombre):
    """
    Corre backtest genérico.
    signal_fn(row, prev_row) -> True/False para señal de entrada.
    tp_pct: take profit decimal (0.08 = +8%)
    sl_pct: stop loss decimal (0.07 = -7%, se pasa positivo)
    """
    capital = CAPITAL
    pos = None
    trades = []
    equity = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        precio = float(row["Close"])
        hi = float(row["High"])
        lo = float(row["Low"])

        # Verificar salida
        if pos is not None:
            hit_sl = lo <= pos["sl"]
            hit_tp = hi >= pos["tp"]
            px = mot = None
            if hit_sl and hit_tp:
                px, mot = pos["sl"], "stop-loss"
            elif hit_sl:
                px, mot = pos["sl"], "stop-loss"
            elif hit_tp:
                px, mot = pos["tp"], "take-profit"
            if px is not None:
                pnl = (px - pos["pe"]) * pos["q"]
                pnl_pct = (px / pos["pe"] - 1) * 100
                capital += pos["q"] * px
                trades.append({
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "motivo": mot,
                    "fecha": df.index[i].strftime("%Y-%m-%d") if hasattr(df.index[i], "strftime") else str(df.index[i])[:10],
                })
                pos = None

        # Evaluar entrada
        if pos is None:
            if signal_fn(row, prev):
                q = capital / precio  # fraccional OK para cripto
                if q > 0:
                    capital -= q * precio
                    pos = {
                        "pe": precio,
                        "q": q,
                        "sl": round(precio * (1 - sl_pct), 2),
                        "tp": round(precio * (1 + tp_pct), 2),
                    }

        val_pos = pos["q"] * precio if pos else 0
        equity.append(capital + val_pos)

    # Cerrar posición abierta al final
    if pos is not None:
        pf = float(df.iloc[-1]["Close"])
        pnl = (pf - pos["pe"]) * pos["q"]
        capital += pos["q"] * pf
        trades.append({
            "pnl": round(pnl, 2),
            "pnl_pct": round((pf / pos["pe"] - 1) * 100, 2),
            "motivo": "cierre",
            "fecha": str(df.index[-1])[:10],
        })

    eq_final = capital
    eq_arr = np.array(equity) if equity else np.array([CAPITAL])
    ret_pct = ((eq_final / CAPITAL) - 1) * 100

    # Buy & hold
    p0 = float(df.iloc[0]["Close"])
    pf = float(df.iloc[-1]["Close"])
    bh_pct = ((pf / p0) - 1) * 100

    gan = [t for t in trades if t["pnl"] > 0]
    per = [t for t in trades if t["pnl"] < 0]
    n = len(trades)
    wr = (len(gan) / n * 100) if n > 0 else 0

    # Drawdown
    pk = np.maximum.accumulate(eq_arr)
    dd = ((eq_arr - pk) / pk).min() * 100 if len(eq_arr) > 0 else 0

    # Sharpe
    if len(eq_arr) > 1:
        rets = np.diff(eq_arr) / eq_arr[:-1]
        sharpe = (rets.mean() / rets.std()) * math.sqrt(365) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    g = sum(t["pnl"] for t in gan)
    p = abs(sum(t["pnl"] for t in per))
    pf_r = (g / p) if p > 0 else (float("inf") if g > 0 else 0)

    # Promedio ganancia/pérdida
    avg_win = np.mean([t["pnl_pct"] for t in gan]) if gan else 0
    avg_loss = np.mean([t["pnl_pct"] for t in per]) if per else 0

    return {
        "estrategia": nombre,
        "ret": round(ret_pct, 2),
        "bh": round(bh_pct, 2),
        "alfa": round(ret_pct - bh_pct, 2),
        "trades": n,
        "wins": len(gan),
        "losses": len(per),
        "wr": round(wr, 1),
        "sharpe": round(sharpe, 2),
        "dd": round(dd, 2),
        "pf": round(pf_r, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "equity_final": round(eq_final, 2),
    }


# ── Estrategias ──────────────────────────────────────────────

def signal_mean_reversion(row, prev):
    """Comprar si cae >5% en 24h."""
    ret = row["ret_1d"]
    return pd.notna(ret) and ret <= -5.0


def signal_momentum_volumen(row, prev):
    """Comprar si volumen >2x promedio Y precio sube >2%."""
    ret = row["ret_1d"]
    vol_ratio = row["vol_ratio"]
    return (pd.notna(ret) and pd.notna(vol_ratio)
            and ret >= 2.0 and vol_ratio >= 2.0)


def signal_fng_precio(row, prev):
    """Comprar si F&G < 25 Y caída >3% en 24h."""
    ret = row["ret_1d"]
    fng = row["fng"]
    return (pd.notna(ret) and pd.notna(fng)
            and fng < 25 and ret <= -3.0)


ESTRATEGIAS = [
    ("Mean Reversion",     signal_mean_reversion,    0.08, 0.07),
    ("Momentum + Volumen", signal_momentum_volumen,  0.10, 0.06),
    ("F&G + Precio",       signal_fng_precio,        0.12, 0.08),
]


# ── Reporte ──────────────────────────────────────────────────

def main():
    print("=" * 95)
    print(f"  BACKTEST ESTRATEGIAS CRIPTO — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Período: {PERIODO_ANOS} años | Capital: ${CAPITAL:,.0f} | Activos: {', '.join(SIMBOLOS)}")
    print("=" * 95)

    # Descargar datos
    print("\nDescargando datos...")
    datos = {}
    for s in SIMBOLOS:
        print(f"  {s}...", end=" ", flush=True)
        datos[s] = descargar_cripto(s)
        print(f"OK ({len(datos[s])} velas)")

    print("  Fear & Greed Index...", end=" ", flush=True)
    fng = descargar_fng()
    print(f"OK ({len(fng)} días)")

    # Preparar datos
    for s in SIMBOLOS:
        datos[s] = preparar_datos(datos[s], fng)

    # Correr backtests
    print(f"\nEjecutando {len(ESTRATEGIAS)} estrategias × {len(SIMBOLOS)} activos...\n")
    todos = []

    for s in SIMBOLOS:
        df = datos[s]
        for nombre, signal_fn, tp, sl in ESTRATEGIAS:
            etiqueta = f"{nombre} | {s}"
            print(f"  {etiqueta}...", end=" ", flush=True)
            r = backtest(df, signal_fn, tp, sl, etiqueta)
            r["simbolo"] = s
            r["nombre_corto"] = nombre
            r["tp"] = f"+{tp*100:.0f}%"
            r["sl"] = f"-{sl*100:.0f}%"
            todos.append(r)
            print(f"ret {r['ret']:+.2f}% | {r['trades']}T | wr {r['wr']:.1f}% | sharpe {r['sharpe']:.2f}")

    # ── Tabla por activo ──
    for s in SIMBOLOS:
        subset = [r for r in todos if r["simbolo"] == s]
        bh = subset[0]["bh"] if subset else 0

        print(f"\n{'=' * 95}")
        print(f"  {s}  |  Buy & Hold: {bh:+.2f}%")
        print(f"{'=' * 95}")
        print(f"  {'Estrategia':<22} {'SL/TP':>8} {'Ret%':>8} {'Alfa':>8} {'Trades':>7} {'Win%':>7} {'Sharpe':>8} {'MaxDD':>8} {'PF':>7} {'AvgW':>7} {'AvgL':>7}")
        print(f"  {'─' * 91}")

        for r in subset:
            sltp = f"{r['sl']}/{r['tp']}"
            print(
                f"  {r['nombre_corto']:<22} {sltp:>8} {r['ret']:>+8.2f} {r['alfa']:>+8.2f} "
                f"{r['trades']:>7} {r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['dd']:>7.2f}% "
                f"{r['pf']:>7.2f} {r['avg_win']:>+6.1f}% {r['avg_loss']:>+6.1f}%"
            )

    # ── Tabla global rankeada por Sharpe ──
    todos.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n{'=' * 95}")
    print(f"  RANKING GLOBAL POR SHARPE RATIO")
    print(f"{'=' * 95}")
    print(f"  {'#':>2} {'Estrategia':<35} {'Ret%':>8} {'B&H%':>8} {'Alfa':>8} {'Trades':>6} {'Win%':>7} {'Sharpe':>8} {'DD%':>8} {'PF':>7}")
    print(f"  {'─' * 91}")

    for i, r in enumerate(todos, 1):
        marca = " *" if r["sharpe"] > 0 and r["ret"] > 0 else "  "
        print(
            f"{marca}{i:>2} {r['estrategia']:<35} {r['ret']:>+8.2f} {r['bh']:>+8.2f} {r['alfa']:>+8.2f} "
            f"{r['trades']:>6} {r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['dd']:>7.2f}% {r['pf']:>7.2f}"
        )

    # ── Conclusión ──
    rentables = [r for r in todos if r["ret"] > 0 and r["sharpe"] > 0]

    print(f"\n{'=' * 95}")
    print(f"  CONCLUSIÓN")
    print(f"{'=' * 95}")

    if rentables:
        mejor = max(rentables, key=lambda x: x["sharpe"])
        print(f"\n  MEJOR ESTRATEGIA: {mejor['estrategia']}")
        print(f"    Retorno: {mejor['ret']:+.2f}% | Sharpe: {mejor['sharpe']:.2f} | Win rate: {mejor['wr']:.1f}%")
        print(f"    Profit factor: {mejor['pf']:.2f} | Max DD: {mejor['dd']:.2f}%")
        print(f"    Promedio ganador: {mejor['avg_win']:+.1f}% | Promedio perdedor: {mejor['avg_loss']:+.1f}%")
        print(f"    vs Buy & Hold: {mejor['alfa']:+.2f}pp de alfa")

        print(f"\n  Estrategias rentables (Sharpe > 0 y retorno > 0):")
        for r in sorted(rentables, key=lambda x: x["sharpe"], reverse=True):
            print(f"    {r['estrategia']}: ret {r['ret']:+.2f}%, sharpe {r['sharpe']:.2f}")
    else:
        menos_malo = todos[0]
        print(f"\n  Ninguna estrategia logra retorno positivo con Sharpe > 0.")
        print(f"  Menos mala: {menos_malo['estrategia']}")
        print(f"    Retorno: {menos_malo['ret']:+.2f}% | Sharpe: {menos_malo['sharpe']:.2f}")

    # Comparar familias
    print(f"\n  Comparación de estrategias (promedio ambos activos):")
    for nombre, _, _, _ in ESTRATEGIAS:
        subset = [r for r in todos if r["nombre_corto"] == nombre]
        avg_ret = np.mean([r["ret"] for r in subset])
        avg_sharpe = np.mean([r["sharpe"] for r in subset])
        avg_wr = np.mean([r["wr"] for r in subset])
        print(f"    {nombre:<22}: ret {avg_ret:+.2f}%, sharpe {avg_sharpe:.2f}, wr {avg_wr:.1f}%")

    print(f"\n{'=' * 95}")

    # Guardar
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    ruta = os.path.join(dir_datos, f"backtest_cripto_{datetime.now().strftime('%Y-%m-%d')}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"Backtest cripto — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        f.write(f"Período: {PERIODO_ANOS} años | Capital: ${CAPITAL:,.0f}\n\n")
        f.write(f"{'Estrategia':<36} {'Ret%':>8} {'Sharpe':>8} {'WR%':>7} {'Trades':>7} {'DD%':>8} {'PF':>7}\n")
        f.write("-" * 85 + "\n")
        for r in todos:
            f.write(f"{r['estrategia']:<36} {r['ret']:>+8.2f} {r['sharpe']:>+8.2f} {r['wr']:>6.1f}% {r['trades']:>7} {r['dd']:>7.2f}% {r['pf']:>7.2f}\n")
    print(f"  Reporte guardado en: {ruta}")


if __name__ == "__main__":
    main()
