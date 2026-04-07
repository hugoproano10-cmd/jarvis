#!/home/hproano/asistente_env/bin/python
"""
Interactive Brokers adapter para JARVIS.
Misma interfaz que alpaca_client.py: get_balance, get_positions, buy, sell, get_orders.
Usa ib_insync para conectar a TWS/Gateway via API.

Configuración en .env:
  IBKR_HOST=127.0.0.1
  IBKR_PORT=4001       (Gateway Docker: 4001, TWS live: 7496, paper: 7497)
  IBKR_CLIENT_ID=1
"""

import os
import sys
import math
import logging
from datetime import datetime

from ib_insync import IB, Stock, MarketOrder, LimitOrder, util
from dotenv import load_dotenv
import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))
TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "")
ESTADO_PATH = os.path.join(PROYECTO, "logs", "ultimo_estado.json")

log = logging.getLogger("ibkr")

# Suprimir warnings esperados de IBKR (10089=no market data, 300=can't find EId)
logging.getLogger("ib_insync.wrapper").setLevel(logging.ERROR)
logging.getLogger("ib_insync.client").setLevel(logging.ERROR)

# Símbolos que siempre usan Tiingo (no tienen market data RT en IBKR sin suscripción)
_SKIP_IBKR_PRICE = {"AMD", "NVDA"}

_ib = None
_ibkr_available = None  # None=no probado, True/False=resultado


# ── Estado cached ──────────────────────────────────────────

def _guardar_estado(balance=None, posiciones=None):
    """Guarda estado actual para fallback. Merge parcial si solo llega un campo."""
    import json
    try:
        os.makedirs(os.path.dirname(ESTADO_PATH), exist_ok=True)
        # Cargar existente para merge parcial
        existing = {}
        if os.path.exists(ESTADO_PATH):
            with open(ESTADO_PATH, "r") as f:
                existing = json.load(f)
        if balance is not None:
            existing["balance"] = balance
        if posiciones is not None:
            existing["posiciones"] = posiciones
        existing["ts"] = datetime.now().isoformat()
        with open(ESTADO_PATH, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


def _cargar_estado():
    """Retorna (balance, posiciones) del cache o (None, None)."""
    import json
    if not os.path.exists(ESTADO_PATH):
        return None, None
    try:
        with open(ESTADO_PATH, "r") as f:
            d = json.load(f)
        log.info(f"IBKR no disponible — usando datos cached ({d.get('ts', '?')})")
        return d.get("balance"), d.get("posiciones")
    except Exception:
        return None, None


# ── Conexión ─────────────────────────────────────────────────

def _connect():
    """Conecta a IBKR Gateway/TWS. Reutiliza conexión si ya existe."""
    global _ib, _ibkr_available
    if _ib and _ib.isConnected():
        return _ib
    if _ibkr_available is False:
        raise ConnectionError("IBKR marcado como no disponible en esta sesión")

    log.info(f"Conectando a IBKR {IBKR_HOST}:{IBKR_PORT} (clientId={IBKR_CLIENT_ID}, timeout=10s)...")
    _ib = IB()
    try:
        _ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, readonly=False, timeout=10)
    except Exception as e:
        _ibkr_available = False
        _ib = None
        raise ConnectionError(f"IBKR {IBKR_HOST}:{IBKR_PORT} — {e}") from e
    _ibkr_available = True
    log.info(f"Conectado OK a IBKR {IBKR_HOST}:{IBKR_PORT}")
    return _ib


def disconnect():
    """Desconecta de IBKR."""
    global _ib
    if _ib and _ib.isConnected():
        _ib.disconnect()
        log.info("Desconectado de IBKR")
    _ib = None


def is_connected():
    """Verifica si hay conexión activa."""
    return _ib is not None and _ib.isConnected()


def _precio_fallback(symbol):
    """Fallback: obtener precio via Tiingo cuando IBKR no tiene datos RT (error 10089)."""
    if not TIINGO_API_KEY:
        log.warning(f"{symbol}: sin TIINGO_API_KEY para fallback")
        return None
    try:
        url = f"https://api.tiingo.com/iex/{symbol}"
        resp = requests.get(url, headers={"Authorization": f"Token {TIINGO_API_KEY}"}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            price = data[0].get("last") or data[0].get("tngoLast")
            if price:
                log.info(f"{symbol}: precio fallback Tiingo ${price}")
                return float(price)
    except Exception as e:
        log.warning(f"{symbol}: fallback Tiingo falló: {e}")
    return None


def _get_market_price(contract, ib):
    """Obtiene precio de mercado via IBKR (3s timeout), con fallback a Tiingo."""
    symbol = contract.symbol

    # Símbolos sin market data RT → directo a Tiingo
    if symbol in _SKIP_IBKR_PRICE:
        return _precio_fallback(symbol)

    try:
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, "", False, False)

        # Esperar máximo 3 segundos por un precio válido
        for _ in range(6):
            util.sleep(0.5)
            price = ticker.marketPrice()
            if price == price and price is not None:  # not NaN
                ib.cancelMktData(contract)
                return price

        # Timeout: intentar close price antes de cancelar
        price = ticker.close if ticker.close == ticker.close else None
        ib.cancelMktData(contract)
        if price is not None:
            return price
    except Exception:
        pass

    # Fallback a Tiingo
    return _precio_fallback(symbol)


# ── Balance (misma interfaz que alpaca_client) ───────────────

def get_balance():
    """
    Retorna balance compatible con formato Alpaca.
    Si IBKR no conecta, usa cache de ultimo_estado.json.
    """
    global _ibkr_available
    try:
        ib = _connect()
        ib.reqAccountSummary()
        util.sleep(1)

        summary = {}
        for item in ib.accountSummary():
            summary[item.tag] = item.value

        equity = float(summary.get("NetLiquidation", 0))
        cash = float(summary.get("TotalCashValue", 0))
        buying_power = float(summary.get("BuyingPower", 0))

        bal = {
            "equity": str(equity),
            "cash": str(cash),
            "buying_power": str(buying_power),
            "portfolio_value": str(equity),
            "currency": summary.get("Currency", "USD"),
            "status": "ACTIVE",
        }
        _guardar_estado(balance=bal)
        return bal

    except Exception as e:
        _ibkr_available = False
        log.warning(f"get_balance falló: {e}")
        cached_bal, _ = _cargar_estado()
        if cached_bal:
            cached_bal["status"] = "CACHED"
            return cached_bal
        return {"equity": "0", "cash": "0", "buying_power": "0",
                "portfolio_value": "0", "currency": "USD", "status": "DISCONNECTED"}


# ── Posiciones (misma interfaz que alpaca_client) ────────────

def get_positions():
    """
    Retorna posiciones abiertas, formato compatible con Alpaca.
    Si IBKR no conecta, usa cache de ultimo_estado.json.
    """
    global _ibkr_available
    try:
        ib = _connect()
        positions = ib.positions()
        log.info(f"IBKR ib.positions() devolvió {len(positions)} entradas")

        result = []
        for pos in positions:
            contract = pos.contract
            qty = pos.position
            avg_cost = pos.avgCost

            if qty == 0:
                continue

            log.info(f"  {contract.symbol}: qty={qty}, avgCost={avg_cost}")

            try:
                current_price = _get_market_price(contract, ib)
            except Exception:
                current_price = None

            if current_price is None:
                current_price = avg_cost

            unrealized_pl = (current_price - avg_cost) * qty
            unrealized_plpc = ((current_price / avg_cost) - 1) if avg_cost > 0 else 0

            result.append({
                "symbol": contract.symbol,
                "qty": str(int(abs(qty))),
                "side": "long" if qty > 0 else "short",
                "avg_entry_price": str(round(avg_cost, 2)),
                "current_price": str(round(current_price, 2)),
                "unrealized_pl": str(round(unrealized_pl, 2)),
                "unrealized_plpc": str(round(unrealized_plpc, 4)),
                "market_value": str(round(current_price * abs(qty), 2)),
            })

        log.info(f"Posiciones retornadas: {[r['symbol'] for r in result]}")
        # Guardar estado exitoso
        _guardar_estado(None, result)  # balance se guarda por separado
        return result

    except Exception as e:
        _ibkr_available = False
        log.warning(f"get_positions falló: {e}")
        _, cached_pos = _cargar_estado()
        return cached_pos if cached_pos else []


def get_position(symbol):
    """Retorna la posición de un símbolo específico."""
    positions = get_positions()
    for p in positions:
        if p["symbol"] == symbol:
            return p
    return None


# ── Órdenes (misma interfaz que alpaca_client) ──────────────

def buy(symbol, qty=None, notional=None, order_type="market", time_in_force="day"):
    """Ejecuta orden de compra. Compatible con alpaca_client.buy()."""
    global _ibkr_available
    if qty is None and notional is None:
        raise ValueError("Debes especificar qty o notional")

    try:
        ib = _connect()
    except Exception as e:
        _ibkr_available = False
        log.warning(f"IBKR no disponible — buy({symbol}) no ejecutada: {e}")
        return None

    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    if qty is None and notional is not None:
        price = _get_market_price(contract, ib)
        if price is None or price <= 0:
            raise ValueError(f"Sin precio disponible para {symbol}")
        qty = math.floor(notional / price)
        if qty < 1:
            log.warning(f"{symbol}: qty < 1 (precio ${price:.2f} > capital ${notional:.2f}), orden omitida")
            return None

    qty = int(qty)  # IBKR no acepta fracciones (error 10243)

    if order_type == "market":
        order = MarketOrder("BUY", qty)
    else:
        raise ValueError(f"Tipo de orden no soportado aún: {order_type}")

    trade = ib.placeOrder(contract, order)
    util.sleep(1)

    return {
        "id": str(trade.order.orderId),
        "symbol": symbol,
        "side": "buy",
        "qty": str(qty),
        "type": order_type,
        "status": trade.orderStatus.status,
    }


def sell(symbol, qty=None, notional=None, order_type="market", time_in_force="day"):
    """Ejecuta orden de venta. Compatible con alpaca_client.sell()."""
    global _ibkr_available
    if qty is None and notional is None:
        raise ValueError("Debes especificar qty o notional")

    try:
        ib = _connect()
    except Exception as e:
        _ibkr_available = False
        log.warning(f"IBKR no disponible — sell({symbol}) no ejecutada: {e}")
        return None

    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    if qty is None and notional is not None:
        price = _get_market_price(contract, ib)
        if price is None or price <= 0:
            raise ValueError(f"Sin precio disponible para {symbol}")
        qty = math.floor(notional / price)
        if qty < 1:
            log.warning(f"{symbol}: qty < 1 (precio ${price:.2f} > capital ${notional:.2f}), orden omitida")
            return None

    qty = int(qty)  # IBKR no acepta fracciones (error 10243)

    if order_type == "market":
        order = MarketOrder("SELL", qty)
    else:
        raise ValueError(f"Tipo de orden no soportado aún: {order_type}")

    trade = ib.placeOrder(contract, order)
    util.sleep(1)

    return {
        "id": str(trade.order.orderId),
        "symbol": symbol,
        "side": "sell",
        "qty": str(qty),
        "type": order_type,
        "status": trade.orderStatus.status,
    }


def get_orders(status="all", limit=50):
    """Retorna historial de órdenes. Compatible con alpaca_client.get_orders()."""
    global _ibkr_available
    try:
        ib = _connect()
    except Exception as e:
        _ibkr_available = False
        log.warning(f"IBKR no disponible — get_orders devuelve lista vacía: {e}")
        return []

    if status == "open":
        trades = ib.openTrades()
    else:
        trades = ib.trades()

    result = []
    for trade in trades[:limit]:
        o = trade.order
        s = trade.orderStatus
        result.append({
            "id": str(o.orderId),
            "symbol": trade.contract.symbol,
            "side": o.action.lower(),
            "qty": str(o.totalQuantity),
            "type": o.orderType.lower(),
            "status": s.status,
            "filled_qty": str(s.filled),
            "filled_avg_price": str(s.avgFillPrice),
        })

    return result


def cancel_order(order_id):
    """Cancela una orden por ID."""
    global _ibkr_available
    try:
        ib = _connect()
    except Exception as e:
        _ibkr_available = False
        log.warning(f"IBKR no disponible — cancel_order({order_id}) no ejecutada: {e}")
        return {"status": "error", "id": order_id, "error": str(e)}
    for trade in ib.openTrades():
        if str(trade.order.orderId) == str(order_id):
            ib.cancelOrder(trade.order)
            return {"status": "cancelled", "id": order_id}
    return {"status": "not_found", "id": order_id}


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IBKR Trading Adapter")
    parser.add_argument("--test", action="store_true", help="Test conexión: balance + posiciones")
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 60)
    print(f"  IBKR Trading Adapter — {'Test conexión LIVE' if args.test else 'Info'}")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Host: {IBKR_HOST}:{IBKR_PORT} (clientId={IBKR_CLIENT_ID})")
    print(f"  Modo: {'Gateway (4001)' if IBKR_PORT == 4001 else 'TWS (' + str(IBKR_PORT) + ')'}")
    print("=" * 60)

    if not args.test:
        print(f"\n  Usa --test para probar conexión con TWS")
        print(f"  Ejemplo: python agentes/ibkr_trading.py --test")
        print(f"\n  Requisitos:")
        print(f"    1. TWS abierto y logueado")
        print(f"    2. API habilitada: Edit → Global Configuration → API → Settings")
        print(f"       → Enable ActiveX and Socket Clients ✅")
        print(f"       → Socket port: {IBKR_PORT}")
        print(f"       → Trusted IP: {IBKR_HOST}")
        sys.exit(0)

    print(f"\n  Conectando a IBKR TWS...")
    try:
        bal = get_balance()
        print(f"  ✓ Conectado a cuenta LIVE")
        print(f"\n  ── Balance ──")
        print(f"  Equity:       ${float(bal['equity']):,.2f}")
        print(f"  Cash:         ${float(bal['cash']):,.2f}")
        print(f"  Buying power: ${float(bal['buying_power']):,.2f}")
        print(f"  Status:       {bal['status']}")

        print(f"\n  ── Posiciones ──")
        positions = get_positions()
        if positions:
            for p in positions:
                pnl = float(p["unrealized_pl"])
                pnl_pct = float(p["unrealized_plpc"]) * 100
                mv = float(p["market_value"])
                print(f"    {p['symbol']:6s} {p['qty']:>6s} acc @ ${float(p['avg_entry_price']):>9,.2f} "
                      f"→ ${float(p['current_price']):>9,.2f}  "
                      f"P&L: ${pnl:>+10,.2f} ({pnl_pct:>+6.1f}%)  "
                      f"MV: ${mv:>12,.2f}")
        else:
            print(f"    Sin posiciones abiertas.")

        print(f"\n  ── Órdenes recientes ──")
        orders = get_orders(limit=5)
        if orders:
            for o in orders:
                print(f"    [{o['status']:10s}] {o['side']:4s} {o['qty']:>6s} {o['symbol']:6s} ({o['type']})")
        else:
            print(f"    Sin órdenes.")

        disconnect()
        print(f"\n  ✓ Test completado. Desconectado.")
    except ConnectionRefusedError:
        print(f"\n  ✗ ERROR: No se pudo conectar a IBKR en {IBKR_HOST}:{IBKR_PORT}")
        print(f"  Verifica:")
        print(f"    1. TWS está abierto y logueado")
        print(f"    2. API habilitada en: Edit → Global Configuration → API → Settings")
        print(f"       → Enable ActiveX and Socket Clients ✅")
        print(f"       → Socket port: {IBKR_PORT}")
        print(f"       → Trusted IP: {IBKR_HOST}")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ✗ ERROR: {e}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
