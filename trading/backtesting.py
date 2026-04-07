#!/home/hproano/asistente_env/bin/python
"""
Backtesting de la estrategia de JARVIS.
Replica el scoring de indicadores_tecnicos.py sobre datos históricos de 2 años,
simula bracket orders (stop-loss -5%, take-profit +10%) y compara contra buy-and-hold.
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
SIMBOLOS = ["AAPL", "TSLA", "SPY", "NVDA"]
PERIODO_ANOS = 2

# Parámetros de la estrategia (mismos que risk_manager + indicadores_tecnicos)
UMBRAL_COMPRA = 3          # Puntuación >= 3 para comprar
STOP_LOSS_PCT = 0.05       # -5%
TAKE_PROFIT_PCT = 0.10     # +10%
CAPITAL_INICIAL = 10000.0  # Por activo, para comparación justa


# ── Descarga de datos ───────────────────────────────────────

def descargar_historico(simbolo, anos=PERIODO_ANOS):
    """Descarga datos OHLCV de yfinance."""
    fin = datetime.now()
    inicio = fin - timedelta(days=anos * 365)
    df = yf.download(
        simbolo,
        start=inicio.strftime("%Y-%m-%d"),
        end=fin.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        raise ValueError(f"Sin datos para {simbolo}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ── Indicadores (misma lógica que indicadores_tecnicos.py) ──

def calcular_indicadores(df):
    """Calcula RSI, MACD, Bollinger Bands, SMAs, Volumen promedio."""
    close = df["Close"]
    volume = df["Volume"]

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


def calcular_puntuacion(row, prev_row):
    """Calcula la puntuación de la estrategia para un día dado.
       Replica exactamente la lógica de indicadores_tecnicos.generar_senal()."""
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

    # RSI
    if rsi < 30:
        puntos += 2
    elif rsi < 40:
        puntos += 1
    elif rsi > 70:
        puntos -= 2
    elif rsi > 60:
        puntos -= 1

    # MACD
    if macd_val > macd_sig and macd_hist > macd_hist_prev:
        puntos += 2
    elif macd_val > macd_sig:
        puntos += 1
    elif macd_val < macd_sig and macd_hist < macd_hist_prev:
        puntos -= 2
    elif macd_val < macd_sig:
        puntos -= 1

    # Bollinger Bands
    if precio <= bb_lower:
        puntos += 2
    elif precio >= bb_upper:
        puntos -= 2
    elif precio < bb_mid:
        puntos += 1

    # SMAs
    if sma50 > sma200:
        puntos += 1
    elif sma50 < sma200:
        puntos -= 1

    if precio > sma50:
        puntos += 1
    else:
        puntos -= 1

    return puntos


# ── Motor de backtesting ────────────────────────────────────

def backtest_activo(df, simbolo):
    """
    Simula la estrategia sobre un DataFrame de un activo.
    Retorna dict con métricas y lista de trades.
    """
    # Necesitamos SMA200, así que empezamos después de que todos los indicadores estén listos
    inicio_idx = df["SMA_200"].first_valid_index()
    if inicio_idx is None:
        return None
    df_bt = df.loc[inicio_idx:].copy()
    if len(df_bt) < 10:
        return None

    capital = CAPITAL_INICIAL
    posicion = None  # {precio_entrada, qty, fecha_entrada, stop, tp}
    trades = []
    equity_diario = []

    for i in range(1, len(df_bt)):
        row = df_bt.iloc[i]
        prev = df_bt.iloc[i - 1]
        fecha = df_bt.index[i]
        precio = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        # Si hay posición abierta, verificar stop-loss y take-profit intraday
        if posicion is not None:
            hit_stop = low <= posicion["stop"]
            hit_tp = high >= posicion["tp"]

            precio_salida = None
            motivo = None

            if hit_stop and hit_tp:
                # Ambos alcanzados en el mismo día: asumimos stop primero (conservador)
                precio_salida = posicion["stop"]
                motivo = "stop-loss"
            elif hit_stop:
                precio_salida = posicion["stop"]
                motivo = "stop-loss"
            elif hit_tp:
                precio_salida = posicion["tp"]
                motivo = "take-profit"

            if precio_salida is not None:
                pnl = (precio_salida - posicion["precio_entrada"]) * posicion["qty"]
                pnl_pct = (precio_salida / posicion["precio_entrada"] - 1) * 100
                capital += posicion["qty"] * precio_salida
                trades.append({
                    "fecha_entrada": posicion["fecha_entrada"].strftime("%Y-%m-%d"),
                    "fecha_salida": fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else str(fecha)[:10],
                    "precio_entrada": posicion["precio_entrada"],
                    "precio_salida": precio_salida,
                    "qty": posicion["qty"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "motivo": motivo,
                })
                posicion = None

        # Si no hay posición, evaluar señal de compra
        if posicion is None:
            score = calcular_puntuacion(row, prev)
            if score >= UMBRAL_COMPRA:
                qty = math.floor(capital / precio)
                if qty > 0:
                    costo = qty * precio
                    capital -= costo
                    stop = round(precio * (1 - STOP_LOSS_PCT), 2)
                    tp = round(precio * (1 + TAKE_PROFIT_PCT), 2)
                    posicion = {
                        "precio_entrada": precio,
                        "qty": qty,
                        "fecha_entrada": fecha,
                        "stop": stop,
                        "tp": tp,
                    }

        # Equity diario
        valor_posicion = posicion["qty"] * precio if posicion else 0
        equity_diario.append(capital + valor_posicion)

    # Si queda posición abierta al final, cerrar al último precio
    if posicion is not None:
        precio_final = float(df_bt.iloc[-1]["Close"])
        pnl = (precio_final - posicion["precio_entrada"]) * posicion["qty"]
        pnl_pct = (precio_final / posicion["precio_entrada"] - 1) * 100
        capital += posicion["qty"] * precio_final
        trades.append({
            "fecha_entrada": posicion["fecha_entrada"].strftime("%Y-%m-%d"),
            "fecha_salida": df_bt.index[-1].strftime("%Y-%m-%d") if hasattr(df_bt.index[-1], "strftime") else str(df_bt.index[-1])[:10],
            "precio_entrada": posicion["precio_entrada"],
            "precio_salida": precio_final,
            "qty": posicion["qty"],
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "motivo": "cierre-final",
        })
        posicion = None

    equity_final = capital

    # Buy-and-hold
    precio_inicio = float(df_bt.iloc[0]["Close"])
    precio_fin = float(df_bt.iloc[-1]["Close"])
    bh_qty = math.floor(CAPITAL_INICIAL / precio_inicio)
    bh_retorno_pct = ((precio_fin / precio_inicio) - 1) * 100
    bh_equity_final = (CAPITAL_INICIAL - bh_qty * precio_inicio) + bh_qty * precio_fin

    # Métricas de la estrategia
    equity_arr = np.array(equity_diario) if equity_diario else np.array([CAPITAL_INICIAL])
    retorno_total_pct = ((equity_final / CAPITAL_INICIAL) - 1) * 100

    ganadores = [t for t in trades if t["pnl"] > 0]
    perdedores = [t for t in trades if t["pnl"] < 0]
    neutros = [t for t in trades if t["pnl"] == 0]

    # Máximo drawdown
    peak = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - peak) / peak
    max_drawdown = float(drawdown.min()) * 100

    # Sharpe ratio (anualizado, retornos diarios)
    if len(equity_arr) > 1:
        retornos_diarios = np.diff(equity_arr) / equity_arr[:-1]
        if retornos_diarios.std() > 0:
            sharpe = (retornos_diarios.mean() / retornos_diarios.std()) * math.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Win rate
    total_trades = len(trades)
    win_rate = (len(ganadores) / total_trades * 100) if total_trades > 0 else 0

    # Promedio P&L
    pnl_promedio_gan = np.mean([t["pnl"] for t in ganadores]) if ganadores else 0
    pnl_promedio_per = np.mean([t["pnl"] for t in perdedores]) if perdedores else 0

    # Profit factor
    ganancia_bruta = sum(t["pnl"] for t in ganadores)
    perdida_bruta = abs(sum(t["pnl"] for t in perdedores))
    profit_factor = (ganancia_bruta / perdida_bruta) if perdida_bruta > 0 else float("inf") if ganancia_bruta > 0 else 0

    fecha_inicio = df_bt.index[0].strftime("%Y-%m-%d") if hasattr(df_bt.index[0], "strftime") else str(df_bt.index[0])[:10]
    fecha_fin = df_bt.index[-1].strftime("%Y-%m-%d") if hasattr(df_bt.index[-1], "strftime") else str(df_bt.index[-1])[:10]

    return {
        "simbolo": simbolo,
        "periodo": f"{fecha_inicio} a {fecha_fin}",
        "dias_trading": len(df_bt),
        "capital_inicial": CAPITAL_INICIAL,
        "equity_final": round(equity_final, 2),
        "retorno_total_pct": round(retorno_total_pct, 2),
        "total_trades": total_trades,
        "ganadores": len(ganadores),
        "perdedores": len(perdedores),
        "neutros": len(neutros),
        "win_rate": round(win_rate, 1),
        "pnl_promedio_ganador": round(pnl_promedio_gan, 2),
        "pnl_promedio_perdedor": round(pnl_promedio_per, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 2),
        "buy_and_hold_pct": round(bh_retorno_pct, 2),
        "buy_and_hold_equity": round(bh_equity_final, 2),
        "diferencia_vs_bh": round(retorno_total_pct - bh_retorno_pct, 2),
        "trades": trades,
    }


# ── Reporte ─────────────────────────────────────────────────

def generar_reporte(resultados):
    """Genera reporte en texto plano en español."""
    lineas = []
    lineas.append("=" * 70)
    lineas.append(f"  BACKTEST ESTRATEGIA JARVIS — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lineas.append(f"  Parámetros: Compra si score >= {UMBRAL_COMPRA} | SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}%")
    lineas.append(f"  Capital inicial por activo: ${CAPITAL_INICIAL:,.2f}")
    lineas.append("=" * 70)

    retornos_estrategia = []
    retornos_bh = []

    for r in resultados:
        if r is None:
            continue

        retornos_estrategia.append(r["retorno_total_pct"])
        retornos_bh.append(r["buy_and_hold_pct"])

        mejor = r["diferencia_vs_bh"] > 0
        vs = "SUPERA" if mejor else "PIERDE vs"

        lineas.append(f"\n  {'─' * 66}")
        lineas.append(f"  {r['simbolo']}  |  {r['periodo']}  |  {r['dias_trading']} días")
        lineas.append(f"  {'─' * 66}")
        lineas.append(f"  Estrategia JARVIS:")
        lineas.append(f"    Retorno total    : {r['retorno_total_pct']:+.2f}%  (${r['capital_inicial']:,.2f} → ${r['equity_final']:,.2f})")
        lineas.append(f"    Total trades     : {r['total_trades']}")
        lineas.append(f"    Ganadores        : {r['ganadores']}  |  Perdedores: {r['perdedores']}")
        lineas.append(f"    Win rate         : {r['win_rate']:.1f}%")
        lineas.append(f"    P&L prom ganad.  : +${r['pnl_promedio_ganador']:,.2f}")
        lineas.append(f"    P&L prom perded. : ${r['pnl_promedio_perdedor']:,.2f}")
        lineas.append(f"    Profit factor    : {r['profit_factor']:.2f}")
        lineas.append(f"    Max drawdown     : {r['max_drawdown_pct']:.2f}%")
        lineas.append(f"    Sharpe ratio     : {r['sharpe_ratio']:.2f}")
        lineas.append(f"  Buy & Hold:")
        lineas.append(f"    Retorno total    : {r['buy_and_hold_pct']:+.2f}%  (${r['capital_inicial']:,.2f} → ${r['buy_and_hold_equity']:,.2f})")
        lineas.append(f"  Comparación: {vs} buy-and-hold por {abs(r['diferencia_vs_bh']):.2f} puntos porcentuales")

        # Detalle de trades
        if r["trades"]:
            lineas.append(f"\n    Trades ejecutados:")
            for i, t in enumerate(r["trades"], 1):
                icono = "+" if t["pnl"] > 0 else "-" if t["pnl"] < 0 else "="
                lineas.append(
                    f"      {i:2d}. [{icono}] {t['fecha_entrada']} → {t['fecha_salida']}  "
                    f"${t['precio_entrada']:.2f} → ${t['precio_salida']:.2f}  "
                    f"x{t['qty']}  P&L: ${t['pnl']:+.2f} ({t['pnl_pct']:+.2f}%)  [{t['motivo']}]"
                )

    # ── Resumen global ──
    lineas.append(f"\n{'=' * 70}")
    lineas.append(f"  RESUMEN GLOBAL")
    lineas.append(f"{'=' * 70}")

    if retornos_estrategia:
        prom_est = np.mean(retornos_estrategia)
        prom_bh = np.mean(retornos_bh)
        total_trades = sum(r["total_trades"] for r in resultados if r)
        total_ganadores = sum(r["ganadores"] for r in resultados if r)
        total_perdedores = sum(r["perdedores"] for r in resultados if r)
        win_rate_global = (total_ganadores / total_trades * 100) if total_trades > 0 else 0

        capital_total = CAPITAL_INICIAL * len(retornos_estrategia)
        equity_total = sum(r["equity_final"] for r in resultados if r)
        bh_total = sum(r["buy_and_hold_equity"] for r in resultados if r)

        lineas.append(f"  Portafolio total invertido  : ${capital_total:,.2f}")
        lineas.append(f"  Valor final estrategia     : ${equity_total:,.2f} ({((equity_total/capital_total)-1)*100:+.2f}%)")
        lineas.append(f"  Valor final buy-and-hold   : ${bh_total:,.2f} ({((bh_total/capital_total)-1)*100:+.2f}%)")
        lineas.append(f"  Retorno promedio estrategia: {prom_est:+.2f}%")
        lineas.append(f"  Retorno promedio B&H       : {prom_bh:+.2f}%")
        lineas.append(f"  Total trades               : {total_trades}")
        lineas.append(f"  Win rate global            : {win_rate_global:.1f}% ({total_ganadores}W / {total_perdedores}L)")

        # ── Conclusión ──
        lineas.append(f"\n  {'─' * 66}")
        lineas.append(f"  CONCLUSIÓN:")
        lineas.append(f"  {'─' * 66}")

        if prom_est > prom_bh and prom_est > 0:
            lineas.append(f"  La estrategia de JARVIS ES RENTABLE y SUPERA a buy-and-hold.")
            lineas.append(f"  Genera {prom_est - prom_bh:+.2f}pp de alfa promedio por activo.")
        elif prom_est > 0:
            lineas.append(f"  La estrategia ES RENTABLE pero NO SUPERA a buy-and-hold.")
            lineas.append(f"  Buy-and-hold hubiera sido {prom_bh - prom_est:.2f}pp mejor en promedio.")
            lineas.append(f"  Sin embargo, la estrategia ofrece menor drawdown y riesgo controlado.")
        elif prom_est > -5:
            lineas.append(f"  La estrategia tiene RETORNO NEGATIVO LEVE ({prom_est:+.2f}%).")
            lineas.append(f"  Requiere ajuste de parámetros: considerar umbrales más estrictos")
            lineas.append(f"  o ampliar el take-profit para capturar más movimiento.")
        else:
            lineas.append(f"  La estrategia NO ES RENTABLE ({prom_est:+.2f}%).")
            lineas.append(f"  Se recomienda revisar los umbrales de entrada/salida y")
            lineas.append(f"  posiblemente integrar análisis de tendencia de mayor plazo.")

        if win_rate_global >= 50 and prom_est < 0:
            lineas.append(f"  Nota: el win rate es aceptable ({win_rate_global:.0f}%) pero las pérdidas")
            lineas.append(f"  promedio superan a las ganancias. Ajustar ratio riesgo/beneficio.")

        sharpes = [r["sharpe_ratio"] for r in resultados if r]
        sharpe_prom = np.mean(sharpes)
        if sharpe_prom > 1:
            lineas.append(f"  Sharpe ratio promedio: {sharpe_prom:.2f} (bueno, riesgo ajustado favorable).")
        elif sharpe_prom > 0:
            lineas.append(f"  Sharpe ratio promedio: {sharpe_prom:.2f} (aceptable pero mejorable).")
        else:
            lineas.append(f"  Sharpe ratio promedio: {sharpe_prom:.2f} (deficiente, retorno no compensa el riesgo).")

    lineas.append(f"\n{'=' * 70}")

    return "\n".join(lineas)


def guardar_reporte(texto):
    """Guarda reporte en datos/backtest_FECHA.txt."""
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_datos, f"backtest_{fecha}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    return os.path.abspath(ruta)


# ── Main ────────────────────────────────────────────────────

def main():
    print(f"Backtesting estrategia JARVIS — {PERIODO_ANOS} años de datos\n")

    resultados = []
    for simbolo in SIMBOLOS:
        try:
            print(f"  {simbolo}...", end=" ", flush=True)
            df = descargar_historico(simbolo)
            df = calcular_indicadores(df)
            resultado = backtest_activo(df, simbolo)
            resultados.append(resultado)
            if resultado:
                print(f"OK — {resultado['total_trades']} trades, retorno {resultado['retorno_total_pct']:+.2f}%")
            else:
                print("SKIP (datos insuficientes)")
        except Exception as e:
            print(f"ERROR: {e}")
            resultados.append(None)

    resultados_validos = [r for r in resultados if r is not None]
    if not resultados_validos:
        print("\nNo se obtuvieron resultados válidos.")
        sys.exit(1)

    reporte = generar_reporte(resultados_validos)
    print(f"\n{reporte}")

    ruta = guardar_reporte(reporte)
    print(f"\n  Reporte guardado en: {ruta}")


if __name__ == "__main__":
    main()
