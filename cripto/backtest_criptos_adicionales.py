#!/home/hproano/asistente_env/bin/python
"""
Backtest de estrategia Momentum+Volumen ganadora en criptos adicionales.
Estrategia: Comprar si volumen >2x promedio Y precio sube >2% en 1 hora.
             Stop-loss -6%, Take-profit +10%.
Activos: SOL, BNB, AVAX, ADA, DOT (2 años de datos).
Objetivo: identificar cuáles agregar a Binance Testnet.
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

warnings.filterwarnings("ignore")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SIMBOLOS = ["SOL-USD", "BNB-USD", "AVAX-USD", "ADA-USD", "DOT-USD"]
PERIODO_ANOS = 2
CAPITAL = 10000.0
TP_PCT = 0.10   # +10%
SL_PCT = 0.06   # -6%

# Resultados del backtest anterior para comparar
REF_BTC = {"ret": 37.63, "sharpe": 1.27, "wr": 80.0, "trades": 5}
REF_ETH = {"ret": 21.61, "sharpe": 0.75, "wr": 57.1, "trades": 7}


# ── Datos ────────────────────────────────────────────────────

def descargar_datos_1h(simbolo, intentos=3):
    """Descarga datos horarios (1h) para el período máximo disponible (~730 días)."""
    fin = datetime.now()
    inicio = fin - timedelta(days=PERIODO_ANOS * 365)
    for i in range(intentos):
        df = yf.download(simbolo, start=inicio.strftime("%Y-%m-%d"),
                         end=fin.strftime("%Y-%m-%d"), interval="1h",
                         progress=False, auto_adjust=True)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        if i < intentos - 1:
            time.sleep(2)
    return None


def descargar_datos_diario(simbolo, intentos=3):
    """Fallback a datos diarios si 1h no está disponible."""
    fin = datetime.now()
    inicio = fin - timedelta(days=PERIODO_ANOS * 365)
    for i in range(intentos):
        df = yf.download(simbolo, start=inicio.strftime("%Y-%m-%d"),
                         end=fin.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df, "diario"
        if i < intentos - 1:
            time.sleep(2)
    raise ValueError(f"Sin datos para {simbolo}")


def descargar_cripto(simbolo):
    """Intenta 1h, fallback a diario."""
    df = descargar_datos_1h(simbolo)
    if df is not None and len(df) > 100:
        return df, "1h"
    df, freq = descargar_datos_diario(simbolo)
    return df, freq


def preparar_datos(df):
    """Agrega columnas de señal: retorno %, volumen promedio y ratio."""
    df = df.copy()
    df["ret_1p"] = df["Close"].pct_change() * 100  # variación % por período
    df["vol_avg_20"] = df["Volume"].rolling(20).mean()
    df["vol_ratio"] = df["Volume"] / df["vol_avg_20"]
    return df


# ── Señal Momentum + Volumen ─────────────────────────────────

def signal_momentum_volumen(row, prev):
    """Comprar si volumen >2x promedio Y precio sube >2% en el período."""
    ret = row["ret_1p"]
    vol_ratio = row["vol_ratio"]
    return (pd.notna(ret) and pd.notna(vol_ratio)
            and ret >= 2.0 and vol_ratio >= 2.0)


# ── Motor de backtest ────────────────────────────────────────

def backtest(df, tp_pct, sl_pct):
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
                    "fecha_entrada": pos["fecha"],
                    "fecha_salida": str(df.index[i])[:10],
                    "precio_entrada": pos["pe"],
                    "precio_salida": px,
                })
                pos = None

        # Evaluar entrada
        if pos is None:
            if signal_momentum_volumen(row, prev):
                q = capital / precio
                if q > 0:
                    capital -= q * precio
                    pos = {
                        "pe": precio,
                        "q": q,
                        "sl": round(precio * (1 - sl_pct), 2),
                        "tp": round(precio * (1 + tp_pct), 2),
                        "fecha": str(df.index[i])[:10],
                    }

        val_pos = pos["q"] * precio if pos else 0
        equity.append(capital + val_pos)

    # Cerrar posición abierta
    if pos is not None:
        pf = float(df.iloc[-1]["Close"])
        pnl = (pf - pos["pe"]) * pos["q"]
        capital += pos["q"] * pf
        trades.append({
            "pnl": round(pnl, 2),
            "pnl_pct": round((pf / pos["pe"] - 1) * 100, 2),
            "motivo": "cierre",
            "fecha_entrada": pos["fecha"],
            "fecha_salida": str(df.index[-1])[:10],
            "precio_entrada": pos["pe"],
            "precio_salida": pf,
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

    # Sharpe ratio (anualizado)
    if len(eq_arr) > 1:
        rets = np.diff(eq_arr) / eq_arr[:-1]
        sharpe = (rets.mean() / rets.std()) * math.sqrt(365) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    g = sum(t["pnl"] for t in gan)
    p = abs(sum(t["pnl"] for t in per))
    pf_r = (g / p) if p > 0 else (float("inf") if g > 0 else 0)

    avg_win = np.mean([t["pnl_pct"] for t in gan]) if gan else 0
    avg_loss = np.mean([t["pnl_pct"] for t in per]) if per else 0

    return {
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
        "avg_win": round(float(avg_win), 2),
        "avg_loss": round(float(avg_loss), 2),
        "equity_final": round(eq_final, 2),
        "trade_list": trades,
    }


# ── Evaluación y recomendación ───────────────────────────────

def evaluar_cripto(r, simbolo):
    """
    Puntaje compuesto para decidir si agregar a Binance Testnet.
    Criterios:
      - Sharpe > 0.5           (rentabilidad ajustada al riesgo)
      - Win rate > 50%         (consistencia)
      - Retorno > 0%           (rentable)
      - Alfa vs B&H > 0        (supera hodl)
      - Max DD > -30%          (riesgo controlado)
      - Trades >= 3            (muestra suficiente)
    """
    score = 0
    reasons = []

    if r["sharpe"] > 0.5:
        score += 3
        reasons.append(f"Sharpe excelente ({r['sharpe']:.2f})")
    elif r["sharpe"] > 0:
        score += 1
        reasons.append(f"Sharpe positivo ({r['sharpe']:.2f})")
    else:
        reasons.append(f"Sharpe negativo ({r['sharpe']:.2f})")

    if r["wr"] >= 60:
        score += 2
        reasons.append(f"Win rate alto ({r['wr']:.0f}%)")
    elif r["wr"] >= 50:
        score += 1
        reasons.append(f"Win rate aceptable ({r['wr']:.0f}%)")
    else:
        reasons.append(f"Win rate bajo ({r['wr']:.0f}%)")

    if r["ret"] > 20:
        score += 2
        reasons.append(f"Retorno fuerte ({r['ret']:+.1f}%)")
    elif r["ret"] > 0:
        score += 1
        reasons.append(f"Retorno positivo ({r['ret']:+.1f}%)")
    else:
        reasons.append(f"Retorno negativo ({r['ret']:+.1f}%)")

    if r["alfa"] > 0:
        score += 1
        reasons.append(f"Supera B&H ({r['alfa']:+.1f}pp)")
    else:
        reasons.append(f"No supera B&H ({r['alfa']:+.1f}pp)")

    if r["dd"] > -30:
        score += 1
        reasons.append(f"DD controlado ({r['dd']:.1f}%)")
    else:
        reasons.append(f"DD excesivo ({r['dd']:.1f}%)")

    if r["trades"] >= 5:
        score += 1
        reasons.append(f"Muestra suficiente ({r['trades']}T)")
    elif r["trades"] >= 3:
        reasons.append(f"Muestra limitada ({r['trades']}T)")
    else:
        score -= 1
        reasons.append(f"Muestra insuficiente ({r['trades']}T)")

    # Decisión
    if score >= 6:
        decision = "AGREGAR"
    elif score >= 4:
        decision = "CONSIDERAR"
    elif score >= 2:
        decision = "OBSERVAR"
    else:
        decision = "NO AGREGAR"

    return {
        "simbolo": simbolo,
        "score": score,
        "decision": decision,
        "reasons": reasons,
    }


# ── Main ─────────────────────────────────────────────────────

def main():
    print("=" * 100)
    print(f"  BACKTEST CRIPTOS ADICIONALES — Estrategia Momentum + Volumen")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Regla: Comprar si Volumen > 2x promedio Y Precio sube > 2%")
    print(f"  Stop-loss: -6% | Take-profit: +10% | Capital: ${CAPITAL:,.0f} | Período: {PERIODO_ANOS} años")
    print("=" * 100)

    # Descargar datos
    print("\nDescargando datos...")
    datos = {}
    freqs = {}
    for s in SIMBOLOS:
        print(f"  {s}...", end=" ", flush=True)
        try:
            df, freq = descargar_cripto(s)
            datos[s] = preparar_datos(df)
            freqs[s] = freq
            print(f"OK ({len(df)} velas, {freq})")
        except Exception as e:
            print(f"ERROR: {e}")

    if not datos:
        print("\nNo se pudieron descargar datos. Abortando.")
        sys.exit(1)

    # Ejecutar backtests
    print(f"\nEjecutando backtest Momentum+Volumen en {len(datos)} activos...\n")
    resultados = {}
    evaluaciones = []

    for s in sorted(datos.keys()):
        df = datos[s]
        nombre = s.replace("-USD", "")
        print(f"  {nombre:>5}...", end=" ", flush=True)
        r = backtest(df, TP_PCT, SL_PCT)
        resultados[s] = r
        ev = evaluar_cripto(r, s)
        evaluaciones.append(ev)
        print(f"ret {r['ret']:+.2f}% | sharpe {r['sharpe']:.2f} | {r['trades']}T | wr {r['wr']:.1f}% | dd {r['dd']:.1f}%")

    # ── Tabla de resultados ──
    print(f"\n{'=' * 100}")
    print(f"  RESULTADOS — Momentum + Volumen (SL -6% / TP +10%)")
    print(f"{'=' * 100}")
    print(f"  {'Cripto':<10} {'Freq':>5} {'Ret%':>8} {'B&H%':>8} {'Alfa':>8} {'Trades':>7} "
          f"{'W/L':>7} {'Win%':>7} {'Sharpe':>8} {'MaxDD%':>8} {'PF':>7} {'AvgW':>7} {'AvgL':>7} {'EqFinal':>10}")
    print(f"  {'─' * 96}")

    for s in sorted(resultados.keys()):
        r = resultados[s]
        nombre = s.replace("-USD", "")
        freq = freqs.get(s, "?")
        print(
            f"  {nombre:<10} {freq:>5} {r['ret']:>+8.2f} {r['bh']:>+8.2f} {r['alfa']:>+8.2f} "
            f"{r['trades']:>7} {r['wins']:>3}/{r['losses']:<3} {r['wr']:>6.1f}% {r['sharpe']:>+8.2f} "
            f"{r['dd']:>7.2f}% {r['pf']:>7.2f} {r['avg_win']:>+6.1f}% {r['avg_loss']:>+6.1f}% "
            f"${r['equity_final']:>9,.2f}"
        )

    # ── Referencia: BTC y ETH del backtest anterior ──
    print(f"\n  {'─' * 96}")
    print(f"  Referencia backtest anterior:")
    print(f"  {'BTC':<10} {'daily':>5} {REF_BTC['ret']:>+8.2f} {'':>8} {'':>8} {REF_BTC['trades']:>7} "
          f"{'':>7} {REF_BTC['wr']:>6.1f}% {REF_BTC['sharpe']:>+8.2f}")
    print(f"  {'ETH':<10} {'daily':>5} {REF_ETH['ret']:>+8.2f} {'':>8} {'':>8} {REF_ETH['trades']:>7} "
          f"{'':>7} {REF_ETH['wr']:>6.1f}% {REF_ETH['sharpe']:>+8.2f}")

    # ── Detalle de trades por activo ──
    print(f"\n{'=' * 100}")
    print(f"  DETALLE DE TRADES")
    print(f"{'=' * 100}")

    for s in sorted(resultados.keys()):
        r = resultados[s]
        nombre = s.replace("-USD", "")
        trades = r["trade_list"]
        if not trades:
            print(f"\n  {nombre}: Sin trades")
            continue
        print(f"\n  {nombre} ({len(trades)} trades):")
        print(f"    {'#':>3} {'Entrada':>12} {'Salida':>12} {'P.Entrada':>12} {'P.Salida':>12} {'PnL%':>8} {'PnL$':>10} {'Motivo':<12}")
        print(f"    {'─' * 82}")
        for j, t in enumerate(trades, 1):
            print(
                f"    {j:>3} {t['fecha_entrada']:>12} {t['fecha_salida']:>12} "
                f"{t['precio_entrada']:>12.4f} {t['precio_salida']:>12.4f} "
                f"{t['pnl_pct']:>+7.2f}% ${t['pnl']:>9,.2f} {t['motivo']:<12}"
            )

    # ── Evaluación y recomendación ──
    evaluaciones.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n{'=' * 100}")
    print(f"  EVALUACION PARA BINANCE TESTNET")
    print(f"{'=' * 100}")
    print(f"  Criterios: Sharpe>0.5 (+3), WR>=60% (+2), Ret>20% (+2), Alfa>0 (+1), DD>-30% (+1), Trades>=5 (+1)")
    print(f"  Umbral: >=6 AGREGAR | >=4 CONSIDERAR | >=2 OBSERVAR | <2 NO AGREGAR\n")

    for ev in evaluaciones:
        nombre = ev["simbolo"].replace("-USD", "")
        r = resultados[ev["simbolo"]]
        marca = {"AGREGAR": "[+]", "CONSIDERAR": "[~]", "OBSERVAR": "[?]", "NO AGREGAR": "[-]"}
        print(f"  {marca[ev['decision']]} {nombre:<6} — {ev['decision']:<12} (score: {ev['score']}/10)")
        for reason in ev["reasons"]:
            print(f"       {reason}")
        print()

    # ── Recomendación final ──
    agregar = [ev for ev in evaluaciones if ev["decision"] == "AGREGAR"]
    considerar = [ev for ev in evaluaciones if ev["decision"] == "CONSIDERAR"]

    print(f"{'=' * 100}")
    print(f"  RECOMENDACION FINAL")
    print(f"{'=' * 100}")

    if agregar:
        nombres_agregar = [ev["simbolo"].replace("-USD", "") for ev in agregar]
        print(f"\n  AGREGAR a Binance Testnet:")
        for ev in agregar:
            nombre = ev["simbolo"].replace("-USD", "")
            r = resultados[ev["simbolo"]]
            par = f"{nombre}USDT"
            print(f"    {par:<12} Sharpe {r['sharpe']:.2f} | Ret {r['ret']:+.1f}% | WR {r['wr']:.0f}% | {r['trades']}T")
    else:
        print(f"\n  Ninguna cripto cumple todos los criterios para agregar directamente.")

    if considerar:
        print(f"\n  CONSIDERAR (monitorear en paper trading):")
        for ev in considerar:
            nombre = ev["simbolo"].replace("-USD", "")
            r = resultados[ev["simbolo"]]
            par = f"{nombre}USDT"
            print(f"    {par:<12} Sharpe {r['sharpe']:.2f} | Ret {r['ret']:+.1f}% | WR {r['wr']:.0f}% | {r['trades']}T")

    no_agregar = [ev for ev in evaluaciones if ev["decision"] in ("OBSERVAR", "NO AGREGAR")]
    if no_agregar:
        print(f"\n  NO AGREGAR (por ahora):")
        for ev in no_agregar:
            nombre = ev["simbolo"].replace("-USD", "")
            r = resultados[ev["simbolo"]]
            print(f"    {nombre:<6} Sharpe {r['sharpe']:.2f} | Ret {r['ret']:+.1f}% | {ev['decision']}")

    # Resumen de configuración sugerida
    if agregar or considerar:
        recomendadas = agregar + considerar
        print(f"\n  Configuracion sugerida para jarvis_cripto.py:")
        print(f"    PARES_ACTIVOS = [")
        print(f'        "BTCUSDT", "ETHUSDT",  # ya activos')
        for ev in recomendadas:
            nombre = ev["simbolo"].replace("-USD", "")
            print(f'        "{nombre}USDT",  # {ev["decision"].lower()} — score {ev["score"]}/10')
        print(f"    ]")

    print(f"\n{'=' * 100}")

    # Guardar reporte
    dir_datos = os.path.join(PROYECTO, "datos")
    os.makedirs(dir_datos, exist_ok=True)
    ruta = os.path.join(dir_datos, f"backtest_adicionales_{datetime.now().strftime('%Y-%m-%d')}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"Backtest Criptos Adicionales — Momentum+Volumen\n")
        f.write(f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        f.write(f"SL -6% / TP +10% | Capital ${CAPITAL:,.0f} | Periodo {PERIODO_ANOS} anos\n\n")

        f.write(f"{'Cripto':<10} {'Ret%':>8} {'B&H%':>8} {'Alfa':>8} {'Trades':>7} {'Win%':>7} {'Sharpe':>8} {'DD%':>8} {'PF':>7} {'Decision':<12}\n")
        f.write("-" * 90 + "\n")

        for ev in evaluaciones:
            s = ev["simbolo"]
            r = resultados[s]
            nombre = s.replace("-USD", "")
            f.write(
                f"{nombre:<10} {r['ret']:>+8.2f} {r['bh']:>+8.2f} {r['alfa']:>+8.2f} "
                f"{r['trades']:>7} {r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['dd']:>7.2f}% "
                f"{r['pf']:>7.2f} {ev['decision']:<12}\n"
            )

        f.write(f"\nReferencia: BTC ret +{REF_BTC['ret']}% sharpe {REF_BTC['sharpe']} | "
                f"ETH ret +{REF_ETH['ret']}% sharpe {REF_ETH['sharpe']}\n")

        if agregar:
            f.write(f"\nRECOMENDACION: Agregar {', '.join(ev['simbolo'].replace('-USD','') for ev in agregar)}\n")
        if considerar:
            f.write(f"CONSIDERAR: {', '.join(ev['simbolo'].replace('-USD','') for ev in considerar)}\n")

    print(f"  Reporte guardado en: {ruta}")


if __name__ == "__main__":
    main()
