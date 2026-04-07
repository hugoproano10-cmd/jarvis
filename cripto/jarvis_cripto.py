#!/home/hproano/asistente_env/bin/python
"""
JARVIS Cripto — Bot de trading para BTC, ETH, ADA, BNB en Binance Testnet.
Estrategia: Momentum + Volumen (ganadora del backtest cripto).

Señal de compra: volumen 1h > 2x promedio 20h Y precio sube > 2% en 1h
Take-profit: +10% | Stop-loss: -6% | Máximo $2,000/operación
Ejecución: cada 15 minutos via cron, 24/7.
"""

import os
import sys
import json
import time
import importlib.util as ilu
from datetime import datetime

import logging
import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# Logging explícito para diagnosticar fallos en cron
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("jarvis_cripto")

# Cargar config y alertas
def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

try:
    _alertas = _load("alertas", os.path.join(PROYECTO, "config", "alertas.py"))
    enviar_telegram = _alertas.enviar_telegram
except Exception as e:
    log.error(f"No se pudo cargar módulo de alertas: {e}")
    enviar_telegram = lambda msg: None

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Configuración ───────────────────────────────────────────

BINANCE_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_TESTNET_SECRET", "")

# Validar variables de entorno al inicio
_env_ok = True
if not BINANCE_KEY or "your_" in BINANCE_KEY:
    log.warning("BINANCE_TESTNET_API_KEY no configurada o tiene placeholder")
    _env_ok = False
if not BINANCE_SECRET or "your_" in BINANCE_SECRET:
    log.warning("BINANCE_TESTNET_SECRET no configurada o tiene placeholder")
    _env_ok = False
if not _env_ok:
    try:
        enviar_telegram("⚠️ JARVIS Cripto: API key de Binance no configurada, revisar .env")
    except Exception:
        pass
TESTNET_BASE = os.getenv("BINANCE_TESTNET_URL", "https://testnet.binance.vision") + "/api/v3"

PARES = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT"]
NOMBRES = {
    "BTCUSDT": "Bitcoin",
    "ETHUSDT": "Ethereum",
    "ADAUSDT": "Cardano",
    "BNBUSDT": "BNB",
}

# Parámetros de la estrategia (del backtest ganador)
VOL_RATIO_MIN = 2.0       # Volumen > 2x promedio
PRECIO_SUBE_MIN = 2.0     # Precio sube > 2% en 1h
TAKE_PROFIT = 0.10        # +10%
STOP_LOSS = 0.06          # -6%
MAX_POR_TRADE = 2000.0    # USD máximo por operación (ampliado de 500)
VOL_AVG_PERIODOS = 24     # Promedio de volumen en 24 horas

# Estado persistente (archivo JSON)
ESTADO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado_cripto.json")


# ── Binance Testnet API (sin firma para datos públicos) ─────

def binance_get(endpoint, params=None):
    """GET a Binance testnet (datos públicos, sin auth)."""
    resp = requests.get(f"{TESTNET_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def binance_signed(endpoint, params=None, method="POST"):
    """Request firmado a Binance testnet (requiere API key)."""
    import hmac
    import hashlib
    from urllib.parse import urlencode

    if not BINANCE_KEY or "your_" in BINANCE_KEY:
        return None

    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000

    query = urlencode(params)
    signature = hmac.new(
        BINANCE_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature

    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    url = f"{TESTNET_BASE}{endpoint}"

    if method == "POST":
        resp = requests.post(url, headers=headers, params=params, timeout=10)
    elif method == "GET":
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    else:
        resp = requests.delete(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Datos de mercado ────────────────────────────────────────

def obtener_klines(par, interval="1h", limit=25):
    """Obtiene últimas N velas horarias."""
    data = binance_get("/klines", {"symbol": par, "interval": interval, "limit": limit})
    velas = []
    for k in data:
        velas.append({
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "volume_usd": float(k[7]),
            "timestamp": k[0],
        })
    return velas


def obtener_precio(par):
    """Precio actual."""
    data = binance_get("/ticker/price", {"symbol": par})
    return float(data["price"])


def evaluar_senal(par):
    """
    Evalúa la señal de Momentum + Volumen para un par.
    Retorna dict con señal y datos.
    """
    velas = obtener_klines(par, "1h", VOL_AVG_PERIODOS + 2)

    if len(velas) < VOL_AVG_PERIODOS + 1:
        return {"senal": "ERROR", "razon": "Datos insuficientes"}

    ultima = velas[-1]
    previa = velas[-2]
    precio_actual = ultima["close"]

    # Variación % de la última hora
    var_1h = ((ultima["close"] / ultima["open"]) - 1) * 100

    # Volumen promedio de las últimas 20 horas (excluyendo la actual)
    vol_historico = [v["volume_usd"] for v in velas[-(VOL_AVG_PERIODOS + 1):-1]]
    vol_avg = sum(vol_historico) / len(vol_historico) if vol_historico else 0
    vol_actual = ultima["volume_usd"]
    vol_ratio = (vol_actual / vol_avg) if vol_avg > 0 else 0

    # Señal de compra: precio sube >2% Y volumen >2x promedio
    comprar = var_1h >= PRECIO_SUBE_MIN and vol_ratio >= VOL_RATIO_MIN

    return {
        "par": par,
        "nombre": NOMBRES.get(par, par),
        "precio": round(precio_actual, 2),
        "var_1h": round(var_1h, 2),
        "vol_actual_usd": round(vol_actual, 0),
        "vol_avg_usd": round(vol_avg, 0),
        "vol_ratio": round(vol_ratio, 2),
        "senal": "COMPRAR" if comprar else "ESPERAR",
        "razon": (
            f"Momentum: {var_1h:+.2f}% | Vol: {vol_ratio:.1f}x avg"
            if comprar else
            f"var={var_1h:+.2f}% (req >+{PRECIO_SUBE_MIN}%) | vol={vol_ratio:.1f}x (req >{VOL_RATIO_MIN}x)"
        ),
    }


# ── Gestión de posiciones (estado local) ────────────────────

def cargar_estado():
    """Carga el estado de posiciones desde archivo."""
    if os.path.exists(ESTADO_PATH):
        with open(ESTADO_PATH, "r") as f:
            return json.load(f)
    return {"posiciones": {}, "trades_hoy": [], "ultimo_check": None}


def guardar_estado(estado):
    """Guarda el estado a archivo."""
    estado["ultimo_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ESTADO_PATH, "w") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)


def verificar_salida(estado, par, precio_actual):
    """Verifica si una posición abierta debe cerrarse (TP o SL)."""
    if par not in estado["posiciones"]:
        return None

    pos = estado["posiciones"][par]
    entrada = pos["precio_entrada"]

    tp_precio = entrada * (1 + TAKE_PROFIT)
    sl_precio = entrada * (1 - STOP_LOSS)

    if precio_actual >= tp_precio:
        return "take-profit"
    elif precio_actual <= sl_precio:
        return "stop-loss"
    return None


def ejecutar_compra(estado, par, precio, dry_run=False):
    """Registra una compra (y ejecuta en testnet si hay keys)."""
    qty = MAX_POR_TRADE / precio

    # Ajustar precisión según par
    if par == "BTCUSDT":
        qty = round(qty, 5)
    else:
        qty = round(qty, 4)

    resultado = {
        "par": par,
        "lado": "BUY",
        "qty": qty,
        "precio": precio,
        "monto": round(qty * precio, 2),
        "tp": round(precio * (1 + TAKE_PROFIT), 2),
        "sl": round(precio * (1 - STOP_LOSS), 2),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not dry_run and BINANCE_KEY and "your_" not in BINANCE_KEY:
        try:
            order = binance_signed("/order", {
                "symbol": par,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": str(round(MAX_POR_TRADE, 2)),
            })
            resultado["order_id"] = order.get("orderId")
            resultado["status"] = order.get("status")
            resultado["ejecutada"] = True
        except Exception as e:
            resultado["error"] = str(e)
            resultado["ejecutada"] = False
    else:
        resultado["ejecutada"] = False
        resultado["modo"] = "dry-run" if dry_run else "sin-keys"

    # Registrar posición
    estado["posiciones"][par] = {
        "precio_entrada": precio,
        "qty": qty,
        "tp": resultado["tp"],
        "sl": resultado["sl"],
        "fecha_entrada": resultado["timestamp"],
    }

    return resultado


def ejecutar_venta(estado, par, precio, motivo, dry_run=False):
    """Registra una venta (y ejecuta en testnet si hay keys)."""
    pos = estado["posiciones"][par]
    qty = pos["qty"]
    pnl = (precio - pos["precio_entrada"]) * qty
    pnl_pct = ((precio / pos["precio_entrada"]) - 1) * 100

    resultado = {
        "par": par,
        "lado": "SELL",
        "qty": qty,
        "precio_entrada": pos["precio_entrada"],
        "precio_salida": precio,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "motivo": motivo,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not dry_run and BINANCE_KEY and "your_" not in BINANCE_KEY:
        try:
            order = binance_signed("/order", {
                "symbol": par,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(qty),
            })
            resultado["order_id"] = order.get("orderId")
            resultado["status"] = order.get("status")
            resultado["ejecutada"] = True
        except Exception as e:
            resultado["error"] = str(e)
            resultado["ejecutada"] = False
    else:
        resultado["ejecutada"] = False
        resultado["modo"] = "dry-run" if dry_run else "sin-keys"

    # Remover posición
    del estado["posiciones"][par]

    # Log trade
    estado.setdefault("historial", []).append(resultado)

    return resultado


# ── Telegram ────────────────────────────────────────────────

def notificar_compra(resultado, senal):
    nombre = NOMBRES.get(resultado["par"], resultado["par"])
    modo = f" ({resultado.get('modo', 'REAL')})" if not resultado.get("ejecutada") else ""
    msg = (
        f"\U0001f7e2 <b>JARVIS Cripto — COMPRA{modo}</b>\n"
        f"\U0001f4b0 {nombre} ({resultado['par']})\n"
        f"  Precio: ${resultado['precio']:,.2f}\n"
        f"  Cantidad: {resultado['qty']}\n"
        f"  Monto: ~${resultado['monto']:,.2f}\n"
        f"  TP: ${resultado['tp']:,.2f} (+{TAKE_PROFIT*100:.0f}%)\n"
        f"  SL: ${resultado['sl']:,.2f} (-{STOP_LOSS*100:.0f}%)\n"
        f"  Señal: {senal['razon']}"
    )
    enviar_telegram(msg)


def notificar_venta(resultado):
    nombre = NOMBRES.get(resultado["par"], resultado["par"])
    icono = "\U0001f534" if resultado["pnl"] < 0 else "\u2705"
    modo = f" ({resultado.get('modo', 'REAL')})" if not resultado.get("ejecutada") else ""
    msg = (
        f"{icono} <b>JARVIS Cripto — VENTA{modo}</b>\n"
        f"\U0001f4b0 {nombre} ({resultado['par']})\n"
        f"  Entrada: ${resultado['precio_entrada']:,.2f}\n"
        f"  Salida: ${resultado['precio_salida']:,.2f}\n"
        f"  P&amp;L: ${resultado['pnl']:+,.2f} ({resultado['pnl_pct']:+.2f}%)\n"
        f"  Motivo: {resultado['motivo']}"
    )
    enviar_telegram(msg)


def notificar_resumen(senales, estado):
    """Resumen silencioso (solo en consola, no Telegram) para cada check."""
    for s in senales:
        pos_str = ""
        if s["par"] in estado["posiciones"]:
            pos = estado["posiciones"][s["par"]]
            pnl_pct = ((s["precio"] / pos["precio_entrada"]) - 1) * 100
            pos_str = f" | POS: {pnl_pct:+.2f}%"
        print(f"  {s['par']}: ${s['precio']:,.2f} | {s['senal']} | {s['razon']}{pos_str}")


# ── Loop principal ──────────────────────────────────────────

def run(dry_run=False):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"=== JARVIS Cripto iniciando — {ahora} ===")
    log.info(f"  API Key: {'OK (' + BINANCE_KEY[:8] + '...)' if BINANCE_KEY and 'your_' not in BINANCE_KEY else 'NO CONFIGURADA'}")
    log.info(f"  Testnet URL: {TESTNET_BASE}")
    log.info(f"  Pares: {', '.join(PARES)}")
    print(f"JARVIS Cripto — {ahora}")
    if dry_run:
        print("  Modo: DRY-RUN (no ejecuta órdenes)\n")

    # Verificar conexión a Binance testnet
    try:
        binance_get("/ping")
        log.info("  Conexión Binance testnet: OK")
    except Exception as e:
        log.error(f"  Conexión Binance testnet FALLIDA: {e}")
        try:
            enviar_telegram(f"⚠️ JARVIS Cripto: No se puede conectar a Binance testnet — {e}")
        except Exception:
            pass
        return

    estado = cargar_estado()

    # 1) Evaluar señales
    senales = []
    for par in PARES:
        try:
            s = evaluar_senal(par)
            senales.append(s)
        except Exception as e:
            print(f"  ERROR {par}: {e}")

    # 2) Verificar salidas de posiciones abiertas
    acciones = []
    for par in list(estado["posiciones"].keys()):
        precio = next((s["precio"] for s in senales if s["par"] == par), None)
        if precio is None:
            continue
        motivo = verificar_salida(estado, par, precio)
        if motivo:
            resultado = ejecutar_venta(estado, par, precio, motivo, dry_run)
            acciones.append(("VENTA", resultado))
            notificar_venta(resultado)

    # 3) Evaluar entradas
    for s in senales:
        par = s["par"]
        if s["senal"] != "COMPRAR":
            continue
        if par in estado["posiciones"]:
            continue  # Ya tenemos posición

        resultado = ejecutar_compra(estado, par, s["precio"], dry_run)
        acciones.append(("COMPRA", resultado))
        notificar_compra(resultado, s)

    # 4) Resumen
    notificar_resumen(senales, estado)

    if acciones:
        print(f"\n  Acciones ejecutadas: {len(acciones)}")
        for tipo, r in acciones:
            if tipo == "COMPRA":
                print(f"    {tipo} {r['par']}: {r['qty']} @ ${r['precio']:,.2f} (~${r['monto']:,.2f})")
            else:
                print(f"    {tipo} {r['par']}: P&L ${r['pnl']:+,.2f} ({r['pnl_pct']:+.2f}%) [{r['motivo']}]")
    else:
        print(f"\n  Sin acciones. Posiciones abiertas: {len(estado['posiciones'])}")

    # 5) Guardar estado
    guardar_estado(estado)
    print(f"  Estado guardado: {ESTADO_PATH}")


def obtener_balance():
    """Consulta el balance de la cuenta testnet."""
    data = binance_signed("/account", method="GET")
    if data is None:
        return None
    balances = []
    for b in data.get("balances", []):
        free = float(b["free"])
        locked = float(b["locked"])
        if free > 0 or locked > 0:
            balances.append({
                "activo": b["asset"],
                "libre": free,
                "bloqueado": locked,
                "total": free + locked,
            })
    return balances


def mostrar_balance():
    """Muestra balance formateado de la cuenta testnet."""
    print("=" * 50)
    print("  BALANCE CUENTA BINANCE TESTNET")
    print("=" * 50)

    # Test conexión
    try:
        ping = binance_get("/ping")
        print(f"  Conexión: OK")
    except Exception as e:
        print(f"  Conexión: ERROR — {e}")
        return

    # Test auth
    balances = obtener_balance()
    if balances is None:
        print(f"  Auth: ERROR — API key no configurada o inválida")
        print(f"  Key: {BINANCE_KEY[:8]}...{BINANCE_KEY[-4:]}" if len(BINANCE_KEY) > 12 else "  Key: (vacía)")
        return

    print(f"  Auth: OK")
    print(f"  Key: {BINANCE_KEY[:8]}...{BINANCE_KEY[-4:]}")
    print()

    if not balances:
        print("  Sin fondos en la cuenta.")
    else:
        print(f"  {'Activo':<8} {'Libre':>14} {'Bloqueado':>14} {'Total':>14}")
        print(f"  {'─' * 52}")
        for b in balances:
            print(f"  {b['activo']:<8} {b['libre']:>14,.4f} {b['bloqueado']:>14,.4f} {b['total']:>14,.4f}")

    # Precios actuales
    print(f"\n  Precios actuales:")
    for par in PARES:
        try:
            precio = obtener_precio(par)
            print(f"    {par}: ${precio:,.2f}")
        except Exception:
            pass

    print("=" * 50)


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--balance" in sys.argv:
        mostrar_balance()
    elif "--status" in sys.argv:
        estado = cargar_estado()
        print(json.dumps(estado, indent=2, ensure_ascii=False))
    elif "--reset" in sys.argv:
        if os.path.exists(ESTADO_PATH):
            os.remove(ESTADO_PATH)
            print("Estado reseteado.")
        else:
            print("No hay estado previo.")
    else:
        run(dry_run=dry)
