#!/home/hproano/asistente_env/bin/python
"""
JARVIS Cripto Performance — Reporte diario de performance del bot cripto.
Se ejecuta diario a las 8:00 PM (mercado cripto es 24/7).

Flujo:
  1. Lee posiciones desde cripto/estado_cripto.json
  2. Consulta precios actuales de los 4 pares en Binance
  3. Consulta balance real en Binance (/account firmado)
  4. Parsea trades del día desde logs/trading_decisiones_cripto.log
  5. Calcula P&L abiertas (unrealized), cerradas (realized), total hoy, acumulado
  6. Envía resumen por WhatsApp
  7. Guarda snapshot en logs/cripto_performance_YYYY-MM-DD.json
"""

import os
import sys
import json
import re
import importlib.util as ilu
from datetime import datetime

import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

# Cargar jarvis_cripto como módulo para reutilizar binance_get/signed/precios.
def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

jc = _load("jarvis_cripto", os.path.join(PROYECTO, "cripto", "jarvis_cripto.py"))

_TEST_MODE = "--test" in sys.argv

# Capital inicial desplegado en la estrategia cripto (USDT).
# Override: exportar CRIPTO_CAPITAL_INICIAL en .env
CAPITAL_INICIAL = float(os.getenv("CRIPTO_CAPITAL_INICIAL", "4254"))

ESTADO_PATH = jc.ESTADO_PATH
LOG_DECISIONES = jc.LOG_DECISIONES
PARES = jc.PARES
NOMBRES = jc.NOMBRES


# ── Utilidades ─────────────────────────────────────────────

def _notificar_whatsapp(mensaje):
    if _TEST_MODE:
        print("  [WA] (suprimido en test)")
        print(mensaje)
        return
    try:
        requests.post("http://localhost:8001/alerta",
                      json={"mensaje": mensaje}, timeout=10)
    except Exception as e:
        print(f"  WhatsApp error: {e}")


def _simbolo_base(par):
    """BTCUSDT -> BTC."""
    return par[:-4] if par.endswith("USDT") else par


# ── 1. Precios actuales ─────────────────────────────────────

def obtener_precios_actuales():
    precios = {}
    for par in PARES:
        try:
            precios[par] = jc.obtener_precio(par)
        except Exception as e:
            print(f"  Error precio {par}: {e}")
            precios[par] = None
    return precios


# ── 2. Balance en USDT ──────────────────────────────────────

def obtener_balance_usdt(precios):
    """Retorna (total_usdt, desglose_por_activo)."""
    balances = jc.obtener_balance()
    if not balances:
        return None, []

    desglose = []
    total = 0.0
    for b in balances:
        activo = b["activo"]
        qty = b["total"]
        if activo == "USDT":
            valor = qty
        else:
            par = f"{activo}USDT"
            precio = precios.get(par)
            if precio is None:
                try:
                    precio = jc.obtener_precio(par)
                except Exception:
                    precio = None
            if precio is None:
                continue
            valor = qty * precio
        total += valor
        desglose.append({"activo": activo, "qty": qty, "valor_usdt": round(valor, 2)})
    return round(total, 2), desglose


# ── 3. P&L abiertas ─────────────────────────────────────────

def calcular_pnl_abiertas(estado, precios):
    detalles = []
    pnl_total = 0.0
    for par, pos in estado.get("posiciones", {}).items():
        precio_actual = precios.get(par)
        if precio_actual is None:
            continue
        entrada = pos["precio_entrada"]
        qty = pos["qty"]
        valor_actual = qty * precio_actual
        costo = qty * entrada
        pnl = valor_actual - costo
        pnl_pct = ((precio_actual / entrada) - 1) * 100 if entrada else 0
        pnl_total += pnl
        detalles.append({
            "par": par,
            "simbolo": _simbolo_base(par),
            "qty": qty,
            "precio_entrada": entrada,
            "precio_actual": precio_actual,
            "valor_actual": round(valor_actual, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })
    return round(pnl_total, 2), detalles


# ── 4. P&L realizado (historial) y trades del día ───────────

def pnl_realizado_hoy(estado, hoy):
    pnl = 0.0
    cerrados = []
    for trade in estado.get("historial", []):
        ts = trade.get("timestamp", "")
        if ts.startswith(hoy):
            pnl += float(trade.get("pnl", 0))
            cerrados.append(trade)
    return round(pnl, 2), cerrados


def pnl_realizado_acumulado(estado):
    pnl = 0.0
    for trade in estado.get("historial", []):
        pnl += float(trade.get("pnl", 0))
    return round(pnl, 2)


def contar_trades_dia(hoy):
    """Cuenta BUY/SELL ejecutados en el log del día."""
    compras = 0
    ventas = 0
    if not os.path.exists(LOG_DECISIONES):
        return 0, 0
    pat = re.compile(r"^(\S+ \S+)\s*\|\s*(BUY|SELL)\s*\|")
    with open(LOG_DECISIONES, "r", encoding="utf-8") as f:
        for linea in f:
            m = pat.match(linea)
            if not m:
                continue
            ts, accion = m.group(1), m.group(2)
            if not ts.startswith(hoy):
                continue
            if accion == "BUY":
                compras += 1
            elif accion == "SELL":
                ventas += 1
    return compras, ventas


# ── 5. Formateo del mensaje ─────────────────────────────────

def formatear_mensaje(fecha_dmy, balance_total, pnl_hoy, pnl_hoy_pct,
                      pnl_acum, pnl_acum_pct, detalles_abiertas,
                      compras, ventas, capital_actual):
    def signo(v):
        return "+" if v >= 0 else ""

    lineas = []
    lineas.append("\U0001F4CA JARVIS Cripto \u2014 Reporte Diario")
    lineas.append(f"\U0001F4C5 {fecha_dmy}")
    lineas.append("")

    if balance_total is not None:
        lineas.append(f"\U0001F4B0 Balance: ${balance_total:,.2f} USDT")
    else:
        lineas.append("\U0001F4B0 Balance: (no disponible)")
    lineas.append(f"\U0001F4C8 P&L hoy: {signo(pnl_hoy)}${pnl_hoy:,.2f} "
                  f"({signo(pnl_hoy_pct)}{pnl_hoy_pct:.2f}%)")
    lineas.append(f"\U0001F4C8 P&L acumulado: {signo(pnl_acum)}${pnl_acum:,.2f} "
                  f"({signo(pnl_acum_pct)}{pnl_acum_pct:.2f}%)")
    lineas.append("")

    if detalles_abiertas:
        lineas.append("Posiciones abiertas:")
        for d in detalles_abiertas:
            lineas.append(
                f"  {d['simbolo']}: {d['qty']} @ ${d['precio_entrada']:,.2f} "
                f"\u2192 ${d['valor_actual']:,.2f} "
                f"(P&L: {signo(d['pnl_pct'])}{d['pnl_pct']:.2f}%)"
            )
    else:
        lineas.append("Sin posiciones abiertas.")
    lineas.append("")

    lineas.append(f"Trades hoy: {compras} compras, {ventas} ventas")
    cap_actual_str = (f"${capital_actual:,.2f}" if capital_actual is not None
                      else "(no disponible)")
    lineas.append(f"Capital inicial: ${CAPITAL_INICIAL:,.2f} | "
                  f"Capital actual: {cap_actual_str}")

    return "\n".join(lineas)


# ── Main ───────────────────────────────────────────────────

def main():
    print(f"{'='*60}")
    print(f"  JARVIS CRIPTO PERFORMANCE \u2014 "
          f"{datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}\n")

    hoy = datetime.now().strftime("%Y-%m-%d")
    fecha_dmy = datetime.now().strftime("%d/%m/%Y")

    # 1) Estado
    print("  1) Cargando estado_cripto.json...")
    estado = jc.cargar_estado()
    posiciones = estado.get("posiciones", {})
    print(f"     Posiciones: {len(posiciones)}")

    # 2) Precios
    print("  2) Consultando precios Binance...")
    precios = obtener_precios_actuales()
    for par, p in precios.items():
        if p is not None:
            print(f"     {par}: ${p:,.2f}")

    # 3) Balance
    print("  3) Consultando balance Binance...")
    balance_total, balance_desglose = obtener_balance_usdt(precios)
    if balance_total is not None:
        print(f"     Balance total: ${balance_total:,.2f} USDT")
    else:
        print("     Balance: NO DISPONIBLE (auth falló o sin keys)")

    # 4) P&L abiertas
    print("  4) Calculando P&L abiertas...")
    pnl_abiertas, detalles_abiertas = calcular_pnl_abiertas(estado, precios)
    print(f"     Unrealized: ${pnl_abiertas:+,.2f}")

    # 5) P&L cerradas hoy / acumulado
    print("  5) Calculando P&L cerradas...")
    pnl_cerrado_hoy, cerrados_hoy = pnl_realizado_hoy(estado, hoy)
    pnl_acum_realizado = pnl_realizado_acumulado(estado)
    print(f"     Realized hoy: ${pnl_cerrado_hoy:+,.2f} ({len(cerrados_hoy)} trades)")
    print(f"     Realized acum: ${pnl_acum_realizado:+,.2f}")

    # 6) Trades del día
    print("  6) Contando trades del día...")
    compras, ventas = contar_trades_dia(hoy)
    print(f"     Compras: {compras} | Ventas: {ventas}")

    # 7) Totales
    pnl_hoy = round(pnl_abiertas + pnl_cerrado_hoy, 2)
    pnl_acum = round(pnl_abiertas + pnl_acum_realizado, 2)
    pnl_hoy_pct = (pnl_hoy / CAPITAL_INICIAL * 100) if CAPITAL_INICIAL else 0
    pnl_acum_pct = (pnl_acum / CAPITAL_INICIAL * 100) if CAPITAL_INICIAL else 0

    capital_actual = balance_total if balance_total is not None else None

    # 8) Mensaje
    mensaje = formatear_mensaje(
        fecha_dmy, balance_total, pnl_hoy, pnl_hoy_pct,
        pnl_acum, pnl_acum_pct, detalles_abiertas,
        compras, ventas, capital_actual,
    )
    print("\n  Mensaje WhatsApp:")
    print("  " + mensaje.replace("\n", "\n  "))
    _notificar_whatsapp(mensaje)

    # 9) Snapshot JSON
    snapshot = {
        "fecha": hoy,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "capital_inicial": CAPITAL_INICIAL,
        "balance_total_usdt": balance_total,
        "balance_desglose": balance_desglose,
        "precios": precios,
        "posiciones_abiertas": detalles_abiertas,
        "pnl_unrealized": pnl_abiertas,
        "pnl_realizado_hoy": pnl_cerrado_hoy,
        "pnl_realizado_acumulado": pnl_acum_realizado,
        "pnl_hoy_total": pnl_hoy,
        "pnl_hoy_pct": round(pnl_hoy_pct, 2),
        "pnl_acumulado_total": pnl_acum,
        "pnl_acumulado_pct": round(pnl_acum_pct, 2),
        "trades_hoy": {"compras": compras, "ventas": ventas},
        "trades_cerrados_hoy": cerrados_hoy,
    }

    ruta_json = os.path.join(PROYECTO, "logs", f"cripto_performance_{hoy}.json")
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    print(f"\n  Snapshot guardado: {ruta_json}")

    print(f"\n{'='*60}")
    print("  Performance cripto completado.")
    print(f"{'='*60}")


if __name__ == "__main__":
    if _TEST_MODE:
        print("=== TEST MODE ===\n")
    main()
