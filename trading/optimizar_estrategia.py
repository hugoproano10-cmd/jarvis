#!/home/hproano/asistente_env/bin/python
"""
Optimizador de estrategia JARVIS.
Prueba múltiples combinaciones de SL/TP, filtros de entrada y universo de activos.
Compara resultados y recomienda la configuración óptima.
"""

import os
import sys
import warnings
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
import pandas_ta as ta

warnings.filterwarnings("ignore", category=FutureWarning)

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TODOS_SIMBOLOS = ["AAPL", "TSLA", "SPY", "NVDA"]
PERIODO_ANOS = 2
CAPITAL_INICIAL = 10000.0


# ── Datos e indicadores (reutilizables) ─────────────────────

def descargar_historico(simbolo, intentos=3):
    import time
    fin = datetime.now()
    inicio = fin - timedelta(days=PERIODO_ANOS * 365)
    for intento in range(intentos):
        df = yf.download(simbolo, start=inicio.strftime("%Y-%m-%d"),
                         end=fin.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        if intento < intentos - 1:
            time.sleep(2)
    raise ValueError(f"Sin datos para {simbolo} tras {intentos} intentos")


def calcular_indicadores(df):
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


# ── Scoring (igual que indicadores_tecnicos.py) ─────────────

def calcular_puntuacion(row, prev_row):
    precio = row["Close"]
    puntos = 0
    rsi = row["RSI_14"] if pd.notna(row["RSI_14"]) else 50.0
    macd_val = row["MACD"] if pd.notna(row["MACD"]) else 0.0
    macd_sig = row["MACD_signal"] if pd.notna(row["MACD_signal"]) else 0.0
    macd_hist = row["MACD_hist"] if pd.notna(row["MACD_hist"]) else 0.0
    macd_hist_prev = prev_row["MACD_hist"] if pd.notna(prev_row["MACD_hist"]) else 0.0
    bb_upper = row["BB_upper"] if pd.notna(row["BB_upper"]) else precio * 1.05
    bb_lower = row["BB_lower"] if pd.notna(row["BB_lower"]) else precio * 0.95
    bb_mid = row["BB_mid"] if pd.notna(row["BB_mid"]) else precio
    sma50 = row["SMA_50"] if pd.notna(row["SMA_50"]) else precio
    sma200 = row["SMA_200"] if pd.notna(row["SMA_200"]) else precio

    if rsi < 30: puntos += 2
    elif rsi < 40: puntos += 1
    elif rsi > 70: puntos -= 2
    elif rsi > 60: puntos -= 1

    if macd_val > macd_sig and macd_hist > macd_hist_prev: puntos += 2
    elif macd_val > macd_sig: puntos += 1
    elif macd_val < macd_sig and macd_hist < macd_hist_prev: puntos -= 2
    elif macd_val < macd_sig: puntos -= 1

    if precio <= bb_lower: puntos += 2
    elif precio >= bb_upper: puntos -= 2
    elif precio < bb_mid: puntos += 1

    if sma50 > sma200: puntos += 1
    elif sma50 < sma200: puntos -= 1

    if precio > sma50: puntos += 1
    else: puntos -= 1

    return puntos


# ── Motor de backtest parametrizado ──────────────────────────

def backtest_activo(df, simbolo, stop_pct, tp_pct, filtro=None, umbral=3):
    """
    Corre backtest con parámetros configurables.
    filtro: None, "estricto" (RSI<35 + Vol>1.2x), o "moderado" (RSI<45 + Vol>1.0x).
    """
    inicio_idx = df["SMA_200"].first_valid_index()
    if inicio_idx is None:
        return None
    df_bt = df.loc[inicio_idx:].copy()
    if len(df_bt) < 10:
        return None

    capital = CAPITAL_INICIAL
    posicion = None
    trades = []
    equity_diario = []

    for i in range(1, len(df_bt)):
        row = df_bt.iloc[i]
        prev = df_bt.iloc[i - 1]
        precio = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        # Verificar salida si hay posición
        if posicion is not None:
            hit_stop = low <= posicion["stop"]
            hit_tp = high >= posicion["tp"]
            precio_salida = motivo = None

            if hit_stop and hit_tp:
                precio_salida, motivo = posicion["stop"], "stop-loss"
            elif hit_stop:
                precio_salida, motivo = posicion["stop"], "stop-loss"
            elif hit_tp:
                precio_salida, motivo = posicion["tp"], "take-profit"

            if precio_salida is not None:
                pnl = (precio_salida - posicion["precio_entrada"]) * posicion["qty"]
                pnl_pct = (precio_salida / posicion["precio_entrada"] - 1) * 100
                capital += posicion["qty"] * precio_salida
                trades.append({"pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "motivo": motivo})
                posicion = None

        # Evaluar entrada
        if posicion is None:
            score = calcular_puntuacion(row, prev)
            entrar = score >= umbral

            if entrar and filtro:
                rsi = row["RSI_14"] if pd.notna(row["RSI_14"]) else 50.0
                vol = float(row["Volume"]) if pd.notna(row["Volume"]) else 0
                vol_avg = float(row["Vol_avg_20"]) if pd.notna(row["Vol_avg_20"]) else vol
                if filtro == "estricto":
                    entrar = rsi < 35 and (vol_avg > 0 and vol > vol_avg * 1.2)
                elif filtro == "moderado":
                    entrar = rsi < 45 and (vol_avg > 0 and vol > vol_avg * 1.0)

            if entrar:
                qty = math.floor(capital / precio)
                if qty > 0:
                    capital -= qty * precio
                    posicion = {
                        "precio_entrada": precio,
                        "qty": qty,
                        "stop": round(precio * (1 - stop_pct), 2),
                        "tp": round(precio * (1 + tp_pct), 2),
                    }

        valor_pos = posicion["qty"] * precio if posicion else 0
        equity_diario.append(capital + valor_pos)

    # Cerrar posición abierta al final
    if posicion is not None:
        pf = float(df_bt.iloc[-1]["Close"])
        pnl = (pf - posicion["precio_entrada"]) * posicion["qty"]
        pnl_pct = (pf / posicion["precio_entrada"] - 1) * 100
        capital += posicion["qty"] * pf
        trades.append({"pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "motivo": "cierre-final"})

    equity_final = capital
    equity_arr = np.array(equity_diario) if equity_diario else np.array([CAPITAL_INICIAL])
    retorno_pct = ((equity_final / CAPITAL_INICIAL) - 1) * 100

    # Buy-and-hold
    p0 = float(df_bt.iloc[0]["Close"])
    pf = float(df_bt.iloc[-1]["Close"])
    bh_pct = ((pf / p0) - 1) * 100

    ganadores = [t for t in trades if t["pnl"] > 0]
    perdedores = [t for t in trades if t["pnl"] < 0]
    total = len(trades)
    win_rate = (len(ganadores) / total * 100) if total > 0 else 0

    # Drawdown
    peak = np.maximum.accumulate(equity_arr)
    dd = (equity_arr - peak) / peak
    max_dd = float(dd.min()) * 100

    # Sharpe
    if len(equity_arr) > 1:
        rets = np.diff(equity_arr) / equity_arr[:-1]
        sharpe = (rets.mean() / rets.std()) * math.sqrt(252) if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0

    # Profit factor
    gan_bruta = sum(t["pnl"] for t in ganadores)
    per_bruta = abs(sum(t["pnl"] for t in perdedores))
    pf_ratio = (gan_bruta / per_bruta) if per_bruta > 0 else (float("inf") if gan_bruta > 0 else 0)

    return {
        "simbolo": simbolo,
        "retorno_pct": round(retorno_pct, 2),
        "total_trades": total,
        "ganadores": len(ganadores),
        "perdedores": len(perdedores),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(pf_ratio, 2),
        "bh_pct": round(bh_pct, 2),
    }


# ── Escenarios a probar ─────────────────────────────────────

ESCENARIOS = [
    # (nombre, stop_pct, tp_pct, filtro, simbolos)
    # ── Sin filtro, todos los activos ──
    ("BASE: SL5/TP10 | Todos",          0.05, 0.10, None,       TODOS_SIMBOLOS),
    ("SL8/TP15 | Todos",                0.08, 0.15, None,       TODOS_SIMBOLOS),
    ("SL10/TP20 | Todos",               0.10, 0.20, None,       TODOS_SIMBOLOS),
    ("SL7/TP14 | Todos",                0.07, 0.14, None,       TODOS_SIMBOLOS),
    # ── Sin filtro, solo SPY+AAPL ──
    ("SL5/TP10 | SPY+AAPL",             0.05, 0.10, None,       ["SPY", "AAPL"]),
    ("SL8/TP15 | SPY+AAPL",             0.08, 0.15, None,       ["SPY", "AAPL"]),
    ("SL10/TP20 | SPY+AAPL",            0.10, 0.20, None,       ["SPY", "AAPL"]),
    ("SL7/TP14 | SPY+AAPL",             0.07, 0.14, None,       ["SPY", "AAPL"]),
    # ── Filtro moderado (RSI<45 + Vol>1.0x), todos ──
    ("SL8/TP15 | Todos+FiltMod",        0.08, 0.15, "moderado", TODOS_SIMBOLOS),
    ("SL10/TP20 | Todos+FiltMod",       0.10, 0.20, "moderado", TODOS_SIMBOLOS),
    ("SL7/TP14 | Todos+FiltMod",        0.07, 0.14, "moderado", TODOS_SIMBOLOS),
    # ── Filtro moderado, SPY+AAPL ──
    ("SL8/TP15 | SPY+AAPL+FiltMod",     0.08, 0.15, "moderado", ["SPY", "AAPL"]),
    ("SL10/TP20 | SPY+AAPL+FiltMod",    0.10, 0.20, "moderado", ["SPY", "AAPL"]),
    ("SL7/TP14 | SPY+AAPL+FiltMod",     0.07, 0.14, "moderado", ["SPY", "AAPL"]),
    # ── Filtro estricto (RSI<35 + Vol>1.2x) ──
    ("SL10/TP20 | Todos+FiltEstr",      0.10, 0.20, "estricto", TODOS_SIMBOLOS),
    ("SL10/TP20 | SPY+AAPL+FiltEstr",   0.10, 0.20, "estricto", ["SPY", "AAPL"]),
    # ── Híbridos con TSLA (fue rentable con SL10/TP20) ──
    ("SL10/TP20 | SPY+AAPL+TSLA",       0.10, 0.20, None,       ["SPY", "AAPL", "TSLA"]),
    ("SL10/TP20 | SPY+AAPL+TSLA+FM",    0.10, 0.20, "moderado", ["SPY", "AAPL", "TSLA"]),
]


def correr_escenario(nombre, stop_pct, tp_pct, filtro, simbolos, datos_cache):
    """Corre un escenario completo sobre los simbolos dados."""
    resultados = []
    for s in simbolos:
        df = datos_cache[s]
        r = backtest_activo(df, s, stop_pct, tp_pct, filtro=filtro)
        if r:
            resultados.append(r)

    if not resultados:
        return None

    n = len(resultados)
    total_trades = sum(r["total_trades"] for r in resultados)
    total_gan = sum(r["ganadores"] for r in resultados)
    total_per = sum(r["perdedores"] for r in resultados)
    win_rate = (total_gan / total_trades * 100) if total_trades > 0 else 0
    retorno_prom = np.mean([r["retorno_pct"] for r in resultados])
    bh_prom = np.mean([r["bh_pct"] for r in resultados])
    sharpe_prom = np.mean([r["sharpe"] for r in resultados])
    dd_peor = min(r["max_drawdown"] for r in resultados)
    pf_prom = np.mean([r["profit_factor"] for r in resultados])

    return {
        "nombre": nombre,
        "sl": f"-{stop_pct*100:.0f}%",
        "tp": f"+{tp_pct*100:.0f}%",
        "filtro": filtro or "No",
        "activos": "+".join(simbolos),
        "retorno_prom": round(retorno_prom, 2),
        "bh_prom": round(bh_prom, 2),
        "alfa": round(retorno_prom - bh_prom, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "sharpe": round(sharpe_prom, 2),
        "max_drawdown": round(dd_peor, 2),
        "profit_factor": round(pf_prom, 2),
        "detalle": resultados,
    }


# ── Reporte ─────────────────────────────────────────────────

def generar_reporte(escenarios_resultado):
    """Genera el reporte comparativo completo."""
    L = []
    L.append("=" * 100)
    L.append(f"  OPTIMIZACIÓN DE ESTRATEGIA JARVIS — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    L.append(f"  Período: {PERIODO_ANOS} años | Capital: ${CAPITAL_INICIAL:,.0f}/activo | Umbral entrada: score >= 3")
    L.append("=" * 100)

    # ── Tabla comparativa ──
    L.append(f"\n  {'─' * 96}")
    L.append(f"  TABLA COMPARATIVA DE ESCENARIOS")
    L.append(f"  {'─' * 96}")

    header = (
        f"  {'#':>2}  {'Escenario':<35} {'Ret%':>7} {'B&H%':>7} {'Alfa':>7} "
        f"{'Trades':>6} {'Win%':>6} {'Sharpe':>7} {'MaxDD':>7} {'PF':>6}"
    )
    L.append(header)
    L.append(f"  {'─' * 96}")

    for i, e in enumerate(escenarios_resultado, 1):
        if e is None:
            continue
        L.append(
            f"  {i:>2}  {e['nombre']:<35} {e['retorno_prom']:>+7.2f} {e['bh_prom']:>+7.2f} {e['alfa']:>+7.2f} "
            f"{e['total_trades']:>6} {e['win_rate']:>5.1f}% {e['sharpe']:>7.2f} {e['max_drawdown']:>6.2f}% {e['profit_factor']:>6.2f}"
        )

    L.append(f"  {'─' * 96}")

    # ── Detalle por activo de los mejores ──
    validos = [e for e in escenarios_resultado if e is not None]

    # Filtrar candidatos viables: win rate > 40%
    candidatos = [e for e in validos if e["win_rate"] > 40]

    L.append(f"\n  {'─' * 96}")
    L.append(f"  ANÁLISIS DETALLADO POR ACTIVO — TOP ESCENARIOS (win rate > 40%)")
    L.append(f"  {'─' * 96}")

    if not candidatos:
        L.append(f"  Ningún escenario alcanza win rate > 40%.")
        L.append(f"  Mostrando los 3 mejores por Sharpe ratio:")
        candidatos = sorted(validos, key=lambda x: x["sharpe"], reverse=True)[:3]

    for e in candidatos:
        L.append(f"\n  >>> {e['nombre']}")
        L.append(f"      Retorno: {e['retorno_prom']:+.2f}% | Sharpe: {e['sharpe']:.2f} | Win: {e['win_rate']:.1f}% | DD: {e['max_drawdown']:.2f}%")
        for d in e["detalle"]:
            L.append(
                f"        {d['simbolo']:>4}: ret {d['retorno_pct']:+7.2f}% | "
                f"{d['total_trades']}T ({d['ganadores']}W/{d['perdedores']}L) | "
                f"win {d['win_rate']:5.1f}% | sharpe {d['sharpe']:+.2f} | dd {d['max_drawdown']:.2f}%"
            )

    # ── Recomendación ──
    L.append(f"\n{'=' * 100}")
    L.append(f"  RECOMENDACIÓN")
    L.append(f"{'=' * 100}")

    # Criterio: mejor Sharpe entre los que tienen win rate > 40%
    aptos = [e for e in validos if e["win_rate"] > 40]

    if aptos:
        mejor = max(aptos, key=lambda x: x["sharpe"])
        L.append(f"\n  CONFIGURACIÓN ÓPTIMA (mejor Sharpe con win rate > 40%):")
        L.append(f"  {'─' * 60}")
        L.append(f"    Escenario   : {mejor['nombre']}")
        L.append(f"    Stop-loss   : {mejor['sl']}")
        L.append(f"    Take-profit : {mejor['tp']}")
        L.append(f"    Filtro RSI+Vol: {mejor['filtro']}")
        L.append(f"    Activos     : {mejor['activos']}")
        L.append(f"  {'─' * 60}")
        L.append(f"    Retorno promedio : {mejor['retorno_prom']:+.2f}%")
        L.append(f"    vs Buy & Hold    : {mejor['alfa']:+.2f}pp")
        L.append(f"    Win rate         : {mejor['win_rate']:.1f}%")
        L.append(f"    Sharpe ratio     : {mejor['sharpe']:.2f}")
        L.append(f"    Max drawdown     : {mejor['max_drawdown']:.2f}%")
        L.append(f"    Profit factor    : {mejor['profit_factor']:.2f}")
        L.append(f"    Total trades     : {mejor['total_trades']}")

        if mejor["retorno_prom"] > 0 and mejor["sharpe"] > 0:
            L.append(f"\n  VEREDICTO: ESTRATEGIA VIABLE.")
            L.append(f"  Retorno positivo con riesgo controlado. Implementar con los parámetros indicados.")
        elif mejor["retorno_prom"] > 0:
            L.append(f"\n  VEREDICTO: MARGINALMENTE VIABLE.")
            L.append(f"  Retorno positivo pero Sharpe bajo. Operar con tamaño de posición reducido.")
        else:
            L.append(f"\n  VEREDICTO: NO VIABLE AÚN.")
            L.append(f"  Win rate aceptable pero retorno negativo. Requiere más ajuste.")
    else:
        L.append(f"\n  NINGÚN ESCENARIO cumple win rate > 40%.")
        L.append(f"  Seleccionando el menos malo por Sharpe ratio:\n")
        mejor = max(validos, key=lambda x: x["sharpe"])
        L.append(f"    Escenario   : {mejor['nombre']}")
        L.append(f"    Stop-loss   : {mejor['sl']}")
        L.append(f"    Take-profit : {mejor['tp']}")
        L.append(f"    Filtro      : {mejor['filtro']}")
        L.append(f"    Activos     : {mejor['activos']}")
        L.append(f"    Retorno     : {mejor['retorno_prom']:+.2f}% | Sharpe: {mejor['sharpe']:.2f} | Win: {mejor['win_rate']:.1f}%")

        L.append(f"\n  VEREDICTO: ESTRATEGIA NO VIABLE en su forma actual.")
        L.append(f"  Recomendaciones:")
        L.append(f"    1. Ampliar stop-loss para evitar salidas prematuras en activos volátiles")
        L.append(f"    2. Filtrar entradas con RSI + volumen para mayor selectividad")
        L.append(f"    3. Considerar operar solo activos de menor volatilidad (SPY, AAPL)")
        L.append(f"    4. Evaluar trailing stop en vez de stop-loss fijo")
        L.append(f"    5. Agregar filtro de tendencia: solo comprar si SMA50 > SMA200 (ya en score)")

    L.append(f"\n{'=' * 100}")
    return "\n".join(L)


def guardar_reporte(texto):
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_datos, f"optimizacion_{fecha}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    return os.path.abspath(ruta)


# ── Main ────────────────────────────────────────────────────

def main():
    print(f"Optimización de estrategia JARVIS — {PERIODO_ANOS} años, {len(ESCENARIOS)} escenarios\n")

    # Descargar datos una sola vez
    print("Descargando datos históricos...")
    datos_cache = {}
    for s in TODOS_SIMBOLOS:
        print(f"  {s}...", end=" ", flush=True)
        df = descargar_historico(s)
        datos_cache[s] = calcular_indicadores(df)
        print(f"OK ({len(df)} velas)")

    # Correr todos los escenarios
    print(f"\nEjecutando {len(ESCENARIOS)} escenarios...\n")
    resultados = []
    for i, (nombre, sl, tp, filtro, simbolos) in enumerate(ESCENARIOS, 1):
        print(f"  [{i:>2}/{len(ESCENARIOS)}] {nombre}...", end=" ", flush=True)
        r = correr_escenario(nombre, sl, tp, filtro, simbolos, datos_cache)
        resultados.append(r)
        if r:
            print(f"ret {r['retorno_prom']:+.2f}% | win {r['win_rate']:.1f}% | sharpe {r['sharpe']:.2f}")
        else:
            print("SKIP")

    # Generar reporte
    reporte = generar_reporte(resultados)
    print(f"\n{reporte}")

    ruta = guardar_reporte(reporte)
    print(f"\n  Reporte guardado en: {ruta}")


if __name__ == "__main__":
    main()
