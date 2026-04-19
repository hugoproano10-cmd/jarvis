#!/home/hproano/asistente_env/bin/python
"""
JARVIS Cripto — Bot de trading para BTC, ETH, ADA, BNB en Binance.
Estrategia: Multi-source scoring (técnico + FinBERT + F&G cripto +
Google Trends + Unusual Whales + Reddit).

Take-profit: +10% | Stop-loss: -6% | Trailing stop: +5% → breakeven
Máximo $1,000/operación | Anti-duplicación: 4h cooldown
Ejecución: cada 15 minutos via cron, 24/7.
"""

import os
import sys
import json
import time
import importlib.util as ilu
from datetime import datetime, timedelta

import logging
import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("jarvis_cripto")


def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _enviar_whatsapp(mensaje):
    try:
        requests.post("http://localhost:8001/alerta",
                      json={"mensaje": mensaje}, timeout=10)
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

# ── Configuración ───────────────────────────────────────────

BINANCE_KEY = os.getenv("BINANCE_REAL_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_REAL_SECRET", "")

if not BINANCE_KEY or not BINANCE_SECRET:
    print("BINANCE keys no configuradas — skip")
    sys.exit(0)
BINANCE_BASE = "https://api.binance.com/api/v3"

PARES = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT"]
NOMBRES = {
    "BTCUSDT": "Bitcoin",
    "ETHUSDT": "Ethereum",
    "ADAUSDT": "Cardano",
    "BNBUSDT": "BNB",
}

# Keywords para noticias FinBERT por par
CRIPTO_NEWS_KEYWORDS = {
    "BTCUSDT": "Bitcoin",
    "ETHUSDT": "Ethereum",
    "ADAUSDT": "Cardano",
    "BNBUSDT": "BNB",
}

# ETFs cripto para flujo institucional (Unusual Whales)
CRIPTO_ETF_MAP = {
    "BTCUSDT": ["BITO", "IBIT"],
    "ETHUSDT": ["ETHA"],
}

# Reddit subreddits cripto
REDDIT_CRIPTO_SUBS = ["cryptocurrency", "bitcoin", "CryptoMarkets"]

# Parámetros de la estrategia (del backtest ganador)
VOL_RATIO_MIN = 2.0
PRECIO_SUBE_MIN = 2.0
TAKE_PROFIT = 0.10
STOP_LOSS = 0.06
MAX_POR_TRADE = 1000.0
VOL_AVG_PERIODOS = 24

# Scoring multi-fuente
UMBRAL_COMPRA_NORMAL = 2
UMBRAL_COMPRA_PANICO = 0
UMBRAL_COMPRA_OPORTUNIDAD = 1
UMBRAL_COMPRA_EUFORIA = 3
FNG_PANICO = 15
FNG_OPORTUNIDAD = 20
FNG_EUFORIA = 80

# Trailing stop
TRAILING_ACTIVACION = 0.05

# Anti-duplicación
COOLDOWN_HORAS = 4

# Paths
ESTADO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado_cripto.json")
LOG_DECISIONES = os.path.join(PROYECTO, "logs", "trading_decisiones_cripto.log")


# ── Binance API ────────────────────────────────────────────

def binance_get(endpoint, params=None):
    """GET a Binance (datos públicos, sin auth)."""
    resp = requests.get(f"{BINANCE_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def binance_signed(endpoint, params=None, method="POST"):
    """Request firmado a Binance (requiere API key)."""
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
    url = f"{BINANCE_BASE}{endpoint}"

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
    """Obtiene últimas N velas."""
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


# ── Score técnico (momentum + volumen) ─────────────────────

def evaluar_senal(par):
    """
    Score técnico basado en momentum + volumen.
    +2 si ambas condiciones, +1 si solo una, 0 si ninguna.
    """
    velas = obtener_klines(par, "1h", VOL_AVG_PERIODOS + 2)

    if len(velas) < VOL_AVG_PERIODOS + 1:
        return {"par": par, "nombre": NOMBRES.get(par, par),
                "senal": "ERROR", "score": 0, "precio": 0,
                "var_1h": 0, "vol_ratio": 0, "razon": "Datos insuficientes"}

    ultima = velas[-1]
    precio_actual = ultima["close"]
    var_1h = ((ultima["close"] / ultima["open"]) - 1) * 100

    vol_historico = [v["volume_usd"] for v in velas[-(VOL_AVG_PERIODOS + 1):-1]]
    vol_avg = sum(vol_historico) / len(vol_historico) if vol_historico else 0
    vol_actual = ultima["volume_usd"]
    vol_ratio = (vol_actual / vol_avg) if vol_avg > 0 else 0

    momentum_ok = var_1h >= PRECIO_SUBE_MIN
    volumen_ok = vol_ratio >= VOL_RATIO_MIN

    if momentum_ok and volumen_ok:
        score = 2
    elif momentum_ok or volumen_ok:
        score = 1
    else:
        score = 0

    return {
        "par": par,
        "nombre": NOMBRES.get(par, par),
        "precio": round(precio_actual, 2),
        "var_1h": round(var_1h, 2),
        "vol_actual_usd": round(vol_actual, 0),
        "vol_avg_usd": round(vol_avg, 0),
        "vol_ratio": round(vol_ratio, 2),
        "score": score,
        "senal": "COMPRAR" if (momentum_ok and volumen_ok) else "ESPERAR",
        "razon": f"var={var_1h:+.2f}% | vol={vol_ratio:.1f}x avg",
    }


# ══════════════════════════════════════════════════════════════
#  FUENTES DE DATOS MULTI-SOURCE
# ══════════════════════════════════════════════════════════════

def _obtener_fng_cripto():
    """Fear & Greed Index cripto (alternative.me)."""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = resp.json()["data"][0]
        return {"valor": int(data["value"]), "clasificacion": data["value_classification"]}
    except Exception as e:
        log.warning(f"F&G cripto no disponible: {e}")
        return {"valor": 50, "clasificacion": "Neutral"}


def _obtener_finbert_cripto(par):
    """Score FinBERT para noticias cripto (-2 a +2)."""
    keyword = CRIPTO_NEWS_KEYWORDS.get(par)
    if not keyword:
        return 0, "sin keyword"

    try:
        resp = requests.get(
            "https://min-api.cryptocompare.com/data/v2/news/",
            params={"categories": keyword, "extraParams": "jarvis"},
            timeout=5,
        )
        articles = resp.json().get("Data", [])[:5]
        titulos = [a.get("title", "") for a in articles if a.get("title")]
    except Exception:
        titulos = []

    if not titulos:
        return 0, "sin noticias"

    try:
        resp = requests.post(
            "http://192.168.208.80:8002/score",
            json={"simbolo": keyword, "noticias": titulos},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            return data.get("score", 0), data.get("detalle", f"{len(titulos)} noticias")
    except Exception:
        pass
    return 0, "FinBERT no disponible"


def _obtener_trends_cripto():
    """Score de Google Trends cripto (-1 a +1). Contrarian."""
    try:
        _gt = _load("google_trends", os.path.join(PROYECTO, "datos", "google_trends.py"))
        trends = _gt.get_tendencias_mercado()
        cripto_score = trends.get("cripto_score", 50)
        if cripto_score < 20:
            return 1, f"interés bajo ({cripto_score}) contrarian compra"
        elif cripto_score > 60:
            return -1, f"interés alto ({cripto_score}) cautela"
        return 0, f"interés normal ({cripto_score})"
    except Exception as e:
        log.warning(f"Google Trends no disponible: {e}")
        return 0, "no disponible"


def _obtener_whales_cripto(par):
    """Score de flujo institucional en ETFs cripto (-1 a +1)."""
    etfs = CRIPTO_ETF_MAP.get(par, [])
    if not etfs:
        return 0, "sin ETF proxy"
    try:
        _uw = _load("unusual_whales_scorer",
                     os.path.join(PROYECTO, "datos", "unusual_whales_scorer.py"))
        scores = [_uw.get_institutional_flow(etf) for etf in etfs]
        avg = sum(scores) / len(scores) if scores else 0
        etf_str = ",".join(etfs)
        if avg > 0.5:
            return 1, f"flujo bullish {etf_str}"
        elif avg < -0.5:
            return -1, f"flujo bearish {etf_str}"
        return 0, f"flujo neutral {etf_str}"
    except Exception as e:
        log.warning(f"Unusual Whales no disponible: {e}")
        return 0, "no disponible"


def _obtener_reddit_cripto(par):
    """Score de sentimiento Reddit para cripto (-1 a +1)."""
    keyword = NOMBRES.get(par, par.replace("USDT", ""))

    POSITIVAS = {"buy", "long", "bullish", "moon", "pump", "hodl",
                 "accumulate", "undervalued", "breakout", "rally", "green", "rocket"}
    NEGATIVAS = {"sell", "short", "bearish", "crash", "dump", "rug", "scam",
                 "overvalued", "bubble", "dead", "rip", "fear", "red"}

    pos_count = 0
    neg_count = 0
    menciones = 0

    for sub in REDDIT_CRIPTO_SUBS:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/search.json",
                params={"q": keyword, "sort": "new", "t": "day", "limit": 10},
                headers={"User-Agent": "jarvis-cripto/1.0"},
                timeout=5,
            )
            if resp.status_code != 200:
                continue
            posts = resp.json().get("data", {}).get("children", [])
            for p in posts:
                title = p["data"].get("title", "").lower()
                menciones += 1
                words = set(title.split())
                pos_count += len(words & POSITIVAS)
                neg_count += len(words & NEGATIVAS)
        except Exception:
            continue

    if menciones == 0:
        return 0, "sin menciones"

    total = pos_count + neg_count
    if total == 0:
        return 0, f"{menciones} menciones, neutral"

    ratio = pos_count / total
    if ratio > 0.65:
        return 1, f"{menciones} menciones, positivo ({ratio:.0%})"
    elif ratio < 0.35:
        return -1, f"{menciones} menciones, negativo ({ratio:.0%})"
    return 0, f"{menciones} menciones, mixto ({ratio:.0%})"


# ══════════════════════════════════════════════════════════════
#  RÉGIMEN DE MERCADO CRIPTO
# ══════════════════════════════════════════════════════════════

def _detectar_regimen_cripto(fng):
    """
    BEAR: BTC bajo SMA200 Y F&G < 25 → solo BTC/ETH
    BULL: BTC sobre SMA200 Y F&G > 50 → todos los pares
    LATERAL: todo lo demás
    """
    fng_valor = fng.get("valor", 50)

    btc_precio = None
    btc_sma200 = None
    btc_sobre_sma200 = None
    try:
        velas = obtener_klines("BTCUSDT", "1d", 200)
        if len(velas) >= 200:
            closes = [v["close"] for v in velas]
            btc_sma200 = sum(closes) / len(closes)
            btc_precio = closes[-1]
            btc_sobre_sma200 = btc_precio > btc_sma200
    except Exception:
        pass

    if btc_sobre_sma200 is False and fng_valor < 25:
        regimen = "BEAR"
        pares_permitidos = ["BTCUSDT", "ETHUSDT"]
        nota = f"BTC ${btc_precio:,.0f} < SMA200 ${btc_sma200:,.0f} + F&G {fng_valor}"
    elif btc_sobre_sma200 is True and fng_valor > 50:
        regimen = "BULL"
        pares_permitidos = list(PARES)
        nota = f"BTC ${btc_precio:,.0f} > SMA200 ${btc_sma200:,.0f} + F&G {fng_valor}"
    else:
        regimen = "LATERAL"
        pares_permitidos = list(PARES)
        if btc_precio and btc_sma200:
            nota = f"BTC ${btc_precio:,.0f} vs SMA200 ${btc_sma200:,.0f} + F&G {fng_valor}"
        else:
            nota = f"SMA200 N/D + F&G {fng_valor}"

    return {
        "regimen": regimen,
        "pares_permitidos": pares_permitidos,
        "nota": nota,
        "btc_precio": btc_precio,
        "btc_sma200": btc_sma200,
        "fng_valor": fng_valor,
    }


# ══════════════════════════════════════════════════════════════
#  GESTIÓN DE POSICIONES
# ══════════════════════════════════════════════════════════════

def cargar_estado():
    if os.path.exists(ESTADO_PATH):
        with open(ESTADO_PATH, "r") as f:
            return json.load(f)
    return {"posiciones": {}, "trades_hoy": [], "ultimo_check": None,
            "ultimas_ventas": {}, "max_pnl": {}}


def guardar_estado(estado):
    estado["ultimo_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ESTADO_PATH, "w") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)


def verificar_salida(estado, par, precio_actual):
    """Verifica si una posición debe cerrarse (TP, SL, o trailing stop)."""
    if par not in estado["posiciones"]:
        return None

    pos = estado["posiciones"][par]
    entrada = pos["precio_entrada"]
    pnl_pct = (precio_actual / entrada) - 1

    if precio_actual >= entrada * (1 + TAKE_PROFIT):
        return "take-profit"
    if precio_actual <= entrada * (1 - STOP_LOSS):
        return "stop-loss"

    # Trailing stop: si alguna vez subió +5%, cerrar si cae a breakeven
    max_pnl = estado.get("max_pnl", {}).get(par, 0)
    if pnl_pct > max_pnl:
        estado.setdefault("max_pnl", {})[par] = pnl_pct
        max_pnl = pnl_pct

    if max_pnl >= TRAILING_ACTIVACION and precio_actual <= entrada:
        return "trailing-stop"

    return None


def _en_cooldown(estado, par):
    """Verifica si un par fue vendido en las últimas COOLDOWN_HORAS."""
    ultima = estado.get("ultimas_ventas", {}).get(par)
    if not ultima:
        return False
    try:
        ts = datetime.strptime(ultima, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - ts < timedelta(hours=COOLDOWN_HORAS)
    except Exception:
        return False


def ejecutar_compra(estado, par, precio, dry_run=False):
    """Registra una compra (y ejecuta en Binance si hay keys)."""
    qty = MAX_POR_TRADE / precio

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

    estado["posiciones"][par] = {
        "precio_entrada": precio,
        "qty": qty,
        "tp": resultado["tp"],
        "sl": resultado["sl"],
        "fecha_entrada": resultado["timestamp"],
    }

    return resultado


def ejecutar_venta(estado, par, precio, motivo, dry_run=False):
    """Registra una venta (y ejecuta en Binance si hay keys)."""
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

    del estado["posiciones"][par]
    estado.setdefault("ultimas_ventas", {})[par] = resultado["timestamp"]
    estado.get("max_pnl", {}).pop(par, None)
    estado.setdefault("historial", []).append(resultado)

    return resultado


# ══════════════════════════════════════════════════════════════
#  LOGGING DE DECISIONES
# ══════════════════════════════════════════════════════════════

def _log_decision_cripto(par, accion, precio, scores, motivo, regla=""):
    """Escribe una línea en trading_decisiones_cripto.log."""
    os.makedirs(os.path.dirname(LOG_DECISIONES), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(scores, dict):
        score_str = (f"total:{scores.get('total', 0):+d} "
                     f"tec:{scores.get('tecnico', 0):+d} "
                     f"finbert:{scores.get('finbert', 0):+d} "
                     f"trends:{scores.get('trends', 0):+d} "
                     f"whales:{scores.get('whales', 0):+d} "
                     f"reddit:{scores.get('reddit', 0):+d}")
    else:
        score_str = f"score:{scores}"

    line = (f"{ts} | {accion:<4} | {par:<8} | "
            f"${precio:>10,.2f} | {score_str} | {regla} | {motivo[:100]}\n")

    try:
        with open(LOG_DECISIONES, "a") as f:
            f.write(line)
    except Exception:
        pass


def _score_str(scores):
    """Formatea desglose de scores para notificación."""
    parts = []
    labels = {"tecnico": "Téc", "finbert": "FinBERT", "trends": "Trends",
              "whales": "Whales", "reddit": "Reddit"}
    for key, label in labels.items():
        v = scores.get(key, 0)
        if v != 0:
            parts.append(f"{label}:{v:+d}")
    return " | ".join(parts) if parts else "sin señales activas"


# ══════════════════════════════════════════════════════════════
#  NOTIFICACIONES
# ══════════════════════════════════════════════════════════════

def notificar_compra(resultado, scores, regimen):
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
        f"  Score: {scores.get('total', 0):+d} ({_score_str(scores)})\n"
        f"  Régimen: {regimen.get('regimen', '?')} | "
        f"F&amp;G: {regimen.get('fng_valor', '?')}/100"
    )
    _enviar_whatsapp(msg)


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
    _enviar_whatsapp(msg)


# ══════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════

def run(dry_run=False):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"=== JARVIS Cripto Multi-Source — {ahora} ===")
    log.info(f"  API Key: {'OK (' + BINANCE_KEY[:8] + '...)' if BINANCE_KEY and 'your_' not in BINANCE_KEY else 'NO CONFIGURADA'}")
    log.info(f"  Binance URL: {BINANCE_BASE}")
    log.info(f"  Pares: {', '.join(PARES)}")
    print(f"JARVIS Cripto Multi-Source — {ahora}")
    if dry_run:
        print("  Modo: DRY-RUN (no ejecuta órdenes)\n")

    try:
        binance_get("/ping")
        log.info("  Conexión Binance: OK")
    except Exception as e:
        log.error(f"  Conexión Binance FALLIDA: {e}")
        _enviar_whatsapp(f"\u26a0\ufe0f JARVIS Cripto: No se puede conectar a Binance \u2014 {e}")
        return

    estado = cargar_estado()

    # ── 1) Fear & Greed cripto ──
    print("  1) Fear & Greed cripto...")
    fng = _obtener_fng_cripto()
    fng_valor = fng["valor"]
    print(f"     F&G: {fng_valor}/100 ({fng['clasificacion']})")

    if fng_valor < FNG_PANICO:
        umbral = UMBRAL_COMPRA_PANICO
        modo_label = f"PANICO (umbral={umbral})"
    elif fng_valor < FNG_OPORTUNIDAD:
        umbral = UMBRAL_COMPRA_OPORTUNIDAD
        modo_label = f"OPORTUNIDAD (umbral={umbral})"
    elif fng_valor > FNG_EUFORIA:
        umbral = UMBRAL_COMPRA_EUFORIA
        modo_label = f"EUFORIA (umbral={umbral})"
    else:
        umbral = UMBRAL_COMPRA_NORMAL
        modo_label = f"NORMAL (umbral={umbral})"
    print(f"     Modo: {modo_label}")

    # ── 2) Régimen de mercado cripto ──
    print("  2) Régimen de mercado cripto...")
    regimen = _detectar_regimen_cripto(fng)
    pares_activos = regimen["pares_permitidos"]
    print(f"     Régimen: {regimen['regimen']} — {regimen['nota']}")
    print(f"     Pares activos: {', '.join(pares_activos)}")

    # ── 3) Google Trends cripto ──
    print("  3) Google Trends cripto...")
    trends_score, trends_nota = _obtener_trends_cripto()
    print(f"     Trends: {trends_score:+d} ({trends_nota})")

    # ── 4) Evaluar cada par ──
    print("  4) Evaluando pares...")
    senales = {}
    scores_por_par = {}

    for par in PARES:
        # Score técnico
        try:
            senal_tec = evaluar_senal(par)
        except Exception as e:
            senal_tec = {"par": par, "nombre": NOMBRES.get(par, par),
                         "senal": "ERROR", "score": 0, "precio": 0,
                         "var_1h": 0, "vol_ratio": 0, "razon": str(e)}
        senales[par] = senal_tec

        # FinBERT
        finbert_score, finbert_nota = _obtener_finbert_cripto(par)

        # Unusual Whales
        whales_score, whales_nota = _obtener_whales_cripto(par)

        # Reddit
        reddit_score, reddit_nota = _obtener_reddit_cripto(par)

        # Score combinado
        total = senal_tec.get("score", 0) + finbert_score + trends_score + whales_score + reddit_score
        scores = {
            "total": total,
            "tecnico": senal_tec.get("score", 0),
            "finbert": finbert_score,
            "trends": trends_score,
            "whales": whales_score,
            "reddit": reddit_score,
        }
        scores_por_par[par] = scores

        pos_str = ""
        if par in estado.get("posiciones", {}):
            pos = estado["posiciones"][par]
            pnl = ((senal_tec.get("precio", 0) / pos["precio_entrada"]) - 1) * 100 if pos["precio_entrada"] else 0
            pos_str = f" | POS: {pnl:+.1f}%"

        print(f"     {par}: ${senal_tec.get('precio', 0):,.2f} | "
              f"score={total:+d} "
              f"(tec:{scores['tecnico']:+d} finbert:{finbert_score:+d} "
              f"trends:{trends_score:+d} whales:{whales_score:+d} "
              f"reddit:{reddit_score:+d}) | "
              f"var={senal_tec.get('var_1h', 0):+.2f}% "
              f"vol={senal_tec.get('vol_ratio', 0):.1f}x{pos_str}")

    # ── 5) Verificar salidas de posiciones abiertas ──
    print("  5) Verificando posiciones abiertas...")
    acciones = []
    for par in list(estado.get("posiciones", {}).keys()):
        precio = senales.get(par, {}).get("precio")
        if not precio:
            try:
                precio = obtener_precio(par)
            except Exception:
                continue
        motivo = verificar_salida(estado, par, precio)
        if motivo:
            resultado = ejecutar_venta(estado, par, precio, motivo, dry_run)
            acciones.append(("VENTA", resultado, {}))
            notificar_venta(resultado)
            _log_decision_cripto(par, "SELL", precio, 0, motivo, regla=motivo)
            print(f"     {par}: VENTA ({motivo}) P&L ${resultado['pnl']:+,.2f} "
                  f"({resultado['pnl_pct']:+.2f}%)")
        else:
            if par in estado.get("posiciones", {}):
                pos = estado["posiciones"][par]
                pnl_pct = ((precio / pos["precio_entrada"]) - 1) * 100
                max_pnl = estado.get("max_pnl", {}).get(par, 0) * 100
                trailing = " [trailing activo]" if max_pnl >= TRAILING_ACTIVACION * 100 else ""
                print(f"     {par}: HOLD — P&L {pnl_pct:+.2f}% (max: {max_pnl:+.1f}%){trailing}")

    # ── 6) Evaluar entradas ──
    print("  6) Evaluando entradas...")
    for par in pares_activos:
        scores = scores_por_par.get(par, {})
        senal = senales.get(par, {})
        precio = senal.get("precio", 0)

        if par in estado.get("posiciones", {}):
            _log_decision_cripto(par, "SKIP", precio, scores, "ya tiene posición", regla="POS")
            continue

        if _en_cooldown(estado, par):
            print(f"     {par}: SKIP — cooldown ({COOLDOWN_HORAS}h)")
            _log_decision_cripto(par, "SKIP", precio, scores,
                                f"cooldown {COOLDOWN_HORAS}h", regla="COOLDOWN")
            continue

        score_total = scores.get("total", 0)
        if score_total < umbral:
            _log_decision_cripto(par, "WAIT", precio, scores,
                                f"score {score_total} < umbral {umbral}", regla="UMBRAL")
            continue

        print(f"     {par}: COMPRA — score {score_total:+d} >= {umbral} ({_score_str(scores)})")
        resultado = ejecutar_compra(estado, par, precio, dry_run)
        acciones.append(("COMPRA", resultado, scores))
        notificar_compra(resultado, scores, regimen)
        _log_decision_cripto(par, "BUY", precio, scores,
                            f"score {score_total:+d} >= umbral {umbral}", regla="MULTI-SCORE")

    # ── 7) Pares excluidos por régimen ──
    for par in PARES:
        if par not in pares_activos and par not in estado.get("posiciones", {}):
            scores = scores_por_par.get(par, {})
            precio = senales.get(par, {}).get("precio", 0)
            print(f"     {par}: SKIP — régimen {regimen['regimen']}")
            _log_decision_cripto(par, "SKIP", precio, scores,
                                f"régimen {regimen['regimen']}", regla="REGIMEN")

    # ── 8) Resumen ──
    print(f"\n  === Resumen ===")
    print(f"    F&G: {fng_valor}/100 | Régimen: {regimen['regimen']} | "
          f"Umbral: {umbral} | Modo: {modo_label}")
    print(f"    Posiciones: {len(estado.get('posiciones', {}))}")
    if acciones:
        print(f"    Acciones: {len(acciones)}")
        for tipo, r, sc in acciones:
            if tipo == "COMPRA":
                print(f"      {tipo} {r['par']}: {r['qty']} @ ${r['precio']:,.2f} "
                      f"(~${r['monto']:,.2f}) score:{sc.get('total', 0):+d}")
            else:
                print(f"      {tipo} {r['par']}: P&L ${r['pnl']:+,.2f} "
                      f"({r['pnl_pct']:+.2f}%) [{r['motivo']}]")
    else:
        print(f"    Sin acciones este ciclo.")

    guardar_estado(estado)
    print(f"  Estado guardado: {ESTADO_PATH}")


# ══════════════════════════════════════════════════════════════
#  BALANCE Y CLI
# ══════════════════════════════════════════════════════════════

def obtener_balance():
    """Consulta el balance de la cuenta Binance."""
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
    """Muestra balance formateado de la cuenta Binance."""
    print("=" * 50)
    print("  BALANCE CUENTA BINANCE REAL")
    print("=" * 50)

    try:
        binance_get("/ping")
        print(f"  Conexión: OK")
    except Exception as e:
        print(f"  Conexión: ERROR — {e}")
        return

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

    print(f"\n  Precios actuales:")
    for par in PARES:
        try:
            precio = obtener_precio(par)
            print(f"    {par}: ${precio:,.2f}")
        except Exception:
            pass

    print("=" * 50)


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
