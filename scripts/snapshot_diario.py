#!/home/hproano/asistente_env/bin/python
"""
Snapshot diario del portafolio (IBKR + Binance) → jarvis_history.db.

Se ejecuta una vez al día vía cron (9 PM por defecto). A diferencia de los
snapshots que escribe cada ciclo de trading, este NO depende de que haya
habido ciclo: consulta directamente IBKR y Binance para tener una foto
fresca al cierre del día, incluso en días donde no se operó.
"""

import os
import sys
from datetime import datetime

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

from datos import jarvis_db as db


def snapshot_acciones():
    """Consulta IBKR y arma dict {equity, cash, posiciones, pnl_dia}."""
    try:
        from agentes.ibkr_trading import get_balance, get_positions
    except Exception as e:
        print(f"  [acciones] ibkr_trading no importable: {e}")
        return None

    try:
        balance = get_balance() or {}
        posiciones = get_positions() or []
    except Exception as e:
        print(f"  [acciones] IBKR error: {e}")
        return None

    pos_snap = []
    pnl_dia = 0.0
    for p in posiciones:
        try:
            pnl_usd = float(p.get("unrealized_pl", 0) or 0)
            pos_snap.append({
                "simbolo": p.get("symbol"),
                "qty": float(p.get("qty", 0) or 0),
                "precio_entrada": float(p.get("avg_entry_price", 0) or 0),
                "precio_actual": float(p.get("current_price", 0) or 0),
                "valor_actual": float(p.get("market_value", 0) or 0),
                "pnl_pct": float(p.get("unrealized_plpc", 0) or 0) * 100,
                "pnl_usd": pnl_usd,
            })
            pnl_dia += pnl_usd
        except Exception:
            continue

    return {
        "equity": float(balance.get("equity") or 0) or None,
        "cash": float(balance.get("cash") or balance.get("settled_cash") or 0) or None,
        "posiciones": pos_snap,
        "pnl_dia": round(pnl_dia, 2),
    }


def snapshot_cripto():
    """Consulta Binance y estado local → dict."""
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "jarvis_cripto",
            os.path.join(PROYECTO, "cripto", "jarvis_cripto.py"),
        )
        jc = ilu.module_from_spec(spec)
        spec.loader.exec_module(jc)
    except SystemExit:
        # jarvis_cripto hace sys.exit(0) si no hay keys
        print("  [cripto] BINANCE keys no configuradas")
        return None
    except Exception as e:
        print(f"  [cripto] jarvis_cripto no importable: {e}")
        return None

    try:
        estado = jc.cargar_estado()
    except Exception as e:
        print(f"  [cripto] estado error: {e}")
        return None

    # Precios actuales
    precios = {}
    for par in jc.PARES:
        try:
            precios[par] = jc.obtener_precio(par)
        except Exception:
            precios[par] = None

    pos_snap = []
    pnl_unrealized = 0.0
    for par, pos in estado.get("posiciones", {}).items():
        precio = precios.get(par)
        if not precio:
            continue
        qty = pos.get("qty", 0)
        entrada = pos.get("precio_entrada", 0) or 0
        pnl_usd = (precio - entrada) * qty
        pnl_unrealized += pnl_usd
        pos_snap.append({
            "par": par,
            "qty": qty,
            "precio_entrada": entrada,
            "precio_actual": precio,
            "valor_actual": round(qty * precio, 2),
            "pnl_pct": round(((precio / entrada) - 1) * 100, 2) if entrada else 0,
            "pnl_usd": round(pnl_usd, 2),
        })

    # Balance Binance
    equity = None
    cash = None
    try:
        bal = jc.obtener_balance()
        if bal:
            total = 0.0
            for b in bal:
                if b["activo"] == "USDT":
                    cash = b["total"]
                    total += b["total"]
                else:
                    par = f"{b['activo']}USDT"
                    pr = precios.get(par)
                    if pr:
                        total += b["total"] * pr
            equity = round(total, 2)
    except Exception as e:
        print(f"  [cripto] balance error: {e}")

    # P&L realizado acumulado desde historial
    pnl_acum = 0.0
    for tr in estado.get("historial", []) or []:
        try:
            pnl_acum += float(tr.get("pnl", 0) or 0)
        except Exception:
            pass

    return {
        "equity": equity,
        "cash": cash,
        "posiciones": pos_snap,
        "pnl_dia": round(pnl_unrealized, 2),
        "pnl_acumulado": round(pnl_acum + pnl_unrealized, 2),
    }


def main():
    print("=" * 60)
    print(f"  SNAPSHOT DIARIO — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    print("\n1) Acciones (IBKR)...")
    da = snapshot_acciones()
    if da:
        print(f"   equity=${da['equity']:,.2f} cash=${da['cash']:,.2f} "
              f"pos={len(da['posiciones'])} pnl_dia=${da['pnl_dia']:+,.2f}")
    else:
        print("   (no disponible)")

    print("\n2) Cripto (Binance)...")
    dc = snapshot_cripto()
    if dc:
        eq = dc.get("equity")
        eq_str = f"${eq:,.2f}" if eq else "(n/d)"
        print(f"   equity={eq_str} pos={len(dc['posiciones'])} "
              f"pnl_dia=${dc['pnl_dia']:+,.2f} pnl_acum=${dc['pnl_acumulado']:+,.2f}")
    else:
        print("   (no disponible)")

    if not da and not dc:
        print("\nAmbos lados fallaron — no se guarda snapshot.")
        sys.exit(1)

    fecha = db.guardar_snapshot_diario(da, dc)
    print(f"\n   Snapshot guardado para {fecha}")


if __name__ == "__main__":
    main()
