#!/home/hproano/asistente_env/bin/python
"""
JARVIS Cripto — Bot de trading para BTC, ETH, ADA, BNB en Binance.
Estrategia: Multi-source scoring (técnico + FinBERT + F&G cripto +
Google Trends + Unusual Whales + Reddit).

Take-profit: +6% parcial (50%), +10% total
Stop-loss: dinámico clamp(2*ATR%, 3%, 8%)  |  Trailing stop: +3% → breakeven
Cooldown: 3 días post-SL, 12h post-TP. Pausa compras si 2+ SL en 24h.
Umbral endurecido (4) si un par tuvo 2+ SL en 30 días.
Máximo $1,000/operación. Ejecución: cada 15 minutos via cron, 24/7.
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

# DB histórica — opcional, nunca bloquea el ciclo.
sys.path.insert(0, PROYECTO)
try:
    from datos import jarvis_db as _jdb
except Exception as _e:
    _jdb = None
    log.warning(f"jarvis_db no disponible: {_e}")

# ── Configuración ───────────────────────────────────────────

BINANCE_KEY = os.getenv("BINANCE_REAL_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_REAL_SECRET", "")

if not BINANCE_KEY or not BINANCE_SECRET:
    print("BINANCE keys no configuradas — skip")
    sys.exit(0)
BINANCE_BASE = "https://api.binance.com/api/v3"
# Mirrors oficiales de Binance para failover DNS/conectividad.
BINANCE_MIRRORS = [
    "https://api.binance.com/api/v3",
    "https://api1.binance.com/api/v3",
    "https://api2.binance.com/api/v3",
    "https://api3.binance.com/api/v3",
]

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
TAKE_PROFIT = 0.10                # TP total (vender todo) — mantenido
TAKE_PROFIT_PARCIAL = 0.06        # BONUS: TP parcial a +6% → vender 50%
STOP_LOSS = 0.06                  # Fallback si no hay ATR disponible
MAX_POR_TRADE = 1000.0
VOL_AVG_PERIODOS = 24

# MEJORA 2: SL dinámico por ATR (clamp en porcentaje)
SL_CRIPTO_MIN_PCT = 3.0
SL_CRIPTO_MAX_PCT = 8.0

# Scoring multi-fuente
UMBRAL_COMPRA_NORMAL = 2
UMBRAL_COMPRA_PANICO = 0
UMBRAL_COMPRA_OPORTUNIDAD = 1
UMBRAL_COMPRA_EUFORIA = 3
FNG_PANICO = 15
FNG_OPORTUNIDAD = 20
FNG_EUFORIA = 80

# MEJORA 6: trailing stop (antes 0.05)
TRAILING_ACTIVACION = 0.03

# MEJORA 1/3: cooldowns post-venta
COOLDOWN_TP_HORAS = 12            # TP/trailing → 12h (antes 4h)
COOLDOWN_SL_DIAS = 3              # SL → 3 días calendario

# MEJORA 4/5: gestión de historial de SL
MAX_SL_24H = 2                    # Si 2+ SL en 24h → pausar compras
SL_HISTORIAL_DIAS = 30            # Ventana para contar SL históricos por par
SL_HISTORIAL_UMBRAL = 2           # Número de SL que dispara el endurecimiento
UMBRAL_COMPRA_HISTORIAL = 4       # Umbral endurecido

# Paths
ESTADO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado_cripto.json")
LOG_DECISIONES = os.path.join(PROYECTO, "logs", "trading_decisiones_cripto.log")


# ── Binance API ────────────────────────────────────────────

def binance_get(endpoint, params=None):
    """GET a Binance (datos públicos, sin auth).

    Retry con backoff ante fallos DNS/conexión. Si el host principal no
    resuelve, intenta con los mirrors api1/2/3.binance.com.
    """
    ultimo_err = None
    for intento, base in enumerate(BINANCE_MIRRORS):
        try:
            resp = requests.get(f"{base}{endpoint}", params=params, timeout=10)
            resp.raise_for_status()
            if intento > 0:
                log.info(f"Binance: éxito vía mirror {base}")
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            ultimo_err = e
            # Errores de DNS / conexión / timeout → probar siguiente mirror
            log.warning(
                f"Binance DNS/conn falló en {base} ({str(e)[:80]})"
                + (f" — reintentando con {BINANCE_MIRRORS[intento+1]}..."
                   if intento + 1 < len(BINANCE_MIRRORS)
                   else " — sin mirrors restantes")
            )
            # Pequeña pausa antes del siguiente mirror
            time.sleep(5)
        except requests.HTTPError:
            # Error HTTP (400/500) no es DNS — no tiene sentido cambiar mirror
            raise
    # Todos los mirrors fallaron
    raise ultimo_err if ultimo_err else RuntimeError("Binance: todos los mirrors fallaron")


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
                "score": 0, "precio": 0,
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
    """Score de Google Trends cripto (-1 a +1). Contrarian.

    FIX P4: si Google Trends no está disponible (rate limit, error, sin
    cache), retornar 0 (neutro). Antes se devolvía +1 por defecto porque
    google_trends.py retornaba silenciosamente cripto_score=0, lo que
    disparaba la lógica `score < 20 → +1`.
    """
    try:
        _gt = _load("google_trends", os.path.join(PROYECTO, "datos", "google_trends.py"))
        trends = _gt.get_tendencias_mercado()
        # Si google_trends señaliza indisponibilidad, tratamos como neutro
        if not trends.get("disponible", True):
            return 0, "no disponible (sin cache)"
        cripto_score = trends.get("cripto_score")
        if cripto_score is None:
            return 0, "no disponible (score None)"
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


# ── MEJORA 2: Stop-loss dinámico por ATR ───────────────────

def _calcular_atr_pct_cripto(par, periodo=14):
    """ATR(14) expresado como % del precio actual, usando velas diarias de
    Binance. Retorna None si falla o hay pocos datos.
    """
    try:
        velas = obtener_klines(par, "1d", periodo + 2)
        if len(velas) < periodo + 1:
            return None
        closes = [v["close"] for v in velas]
        highs = [v["high"] for v in velas]
        lows = [v["low"] for v in velas]
        trs = []
        for i in range(1, len(velas)):
            pc = closes[i - 1]
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - pc),
                           abs(lows[i] - pc)))
        atr = sum(trs[-periodo:]) / periodo
        precio = closes[-1]
        if precio <= 0:
            return None
        return (atr / precio) * 100
    except Exception:
        return None


def _sl_dinamico_cripto(atr_pct):
    """SL dinámico cripto = clamp(2*ATR%, 3%, 8%). Retorna fracción (0.06 = 6%)."""
    if atr_pct is None:
        return STOP_LOSS
    sl_pct = 2.0 * atr_pct
    sl_pct = max(SL_CRIPTO_MIN_PCT, min(SL_CRIPTO_MAX_PCT, sl_pct))
    return sl_pct / 100.0


# ── MEJORA 1/3: Cooldowns post-venta ───────────────────────

def _get_ultima_venta(estado, par):
    """Retorna (ts_str, motivo) de la última venta del par, o (None, None).
    Compatibilidad con formato antiguo (string) y nuevo (dict).
    """
    u = estado.get("ultimas_ventas", {}).get(par)
    if u is None:
        return None, None
    if isinstance(u, str):
        return u, None  # formato antiguo
    return u.get("ts"), u.get("motivo")


def _en_cooldown_post_venta(estado, par):
    """Determina si el par está en cooldown por venta reciente.
    Retorna (en_cooldown: bool, hasta_str: str|None, motivo: str|None).
    SL → 3 días; TP/trailing → 12 horas.
    """
    ts_str, motivo = _get_ultima_venta(estado, par)
    if not ts_str:
        return False, None, None
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False, None, None

    if motivo == "stop-loss":
        fin = ts + timedelta(days=COOLDOWN_SL_DIAS)
        if datetime.now() < fin:
            return True, fin.strftime("%Y-%m-%d"), "stop-loss"
    else:
        fin = ts + timedelta(hours=COOLDOWN_TP_HORAS)
        if datetime.now() < fin:
            return True, fin.strftime("%Y-%m-%d %H:%M"), motivo or "venta"
    return False, None, None


# ── MEJORA 4/5: Conteo de SL del historial ─────────────────

def _contar_sl_24h(estado):
    """Cuenta stop-losses en las últimas 24h (todos los pares)."""
    ahora = datetime.now()
    n = 0
    for h in estado.get("historial", []):
        if h.get("motivo") != "stop-loss":
            continue
        ts_str = h.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if ahora - ts < timedelta(hours=24):
            n += 1
    return n


def _contar_sl_par_30d(estado, par):
    """Cuenta stop-losses del par en los últimos SL_HISTORIAL_DIAS días."""
    ahora = datetime.now()
    n = 0
    for h in estado.get("historial", []):
        if h.get("par") != par or h.get("motivo") != "stop-loss":
            continue
        ts_str = h.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if ahora - ts < timedelta(days=SL_HISTORIAL_DIAS):
            n += 1
    return n


# ── Verificación de salida ─────────────────────────────────

def verificar_salida(estado, par, precio_actual):
    """Verifica si una posición debe cerrarse.
    Retorna uno de: 'take-profit', 'take-profit-parcial',
    'stop-loss', 'trailing-stop', None.
    """
    if par not in estado["posiciones"]:
        return None

    pos = estado["posiciones"][par]
    entrada = pos["precio_entrada"]
    pnl_pct = (precio_actual / entrada) - 1

    # TP total (+10%) primero: prioridad máxima.
    # Preferimos el valor pre-calculado (redondeado) para evitar FP precision.
    tp_total = pos.get("tp") or round(entrada * (1 + TAKE_PROFIT), 2)
    if precio_actual >= tp_total:
        return "take-profit"

    # BONUS: TP parcial (+6%) si no se ha tomado aún
    tp_parcial = pos.get("tp_parcial") or round(entrada * (1 + TAKE_PROFIT_PARCIAL), 2)
    if precio_actual >= tp_parcial and not pos.get("tp_parcial_tomado"):
        return "take-profit-parcial"

    # MEJORA 2: SL dinámico (si está registrado en la posición)
    sl_pct_pos = pos.get("sl_dinamico_pct")
    if sl_pct_pos is not None:
        sl_limit = round(entrada * (1 - sl_pct_pos / 100.0), 2)
    else:
        sl_limit = pos.get("sl") or round(entrada * (1 - STOP_LOSS), 2)
    if precio_actual <= sl_limit:
        return "stop-loss"

    # MEJORA 6: trailing stop a breakeven tras +3%
    max_pnl = estado.get("max_pnl", {}).get(par, 0)
    if pnl_pct > max_pnl:
        estado.setdefault("max_pnl", {})[par] = pnl_pct
        max_pnl = pnl_pct

    if max_pnl >= TRAILING_ACTIVACION and precio_actual <= entrada:
        return "trailing-stop"

    return None


def ejecutar_compra(estado, par, precio, dry_run=False):
    """Registra una compra (y ejecuta en Binance si hay keys).
    MEJORA 2: el SL se calcula dinámicamente por ATR y se guarda en la posición.
    """
    qty = MAX_POR_TRADE / precio

    if par == "BTCUSDT":
        qty = round(qty, 5)
    else:
        qty = round(qty, 4)

    # MEJORA 2: SL dinámico por ATR
    atr_pct = _calcular_atr_pct_cripto(par)
    sl_frac = _sl_dinamico_cripto(atr_pct)

    resultado = {
        "par": par,
        "lado": "BUY",
        "qty": qty,
        "precio": precio,
        "monto": round(qty * precio, 2),
        "tp": round(precio * (1 + TAKE_PROFIT), 2),
        "tp_parcial": round(precio * (1 + TAKE_PROFIT_PARCIAL), 2),
        "sl": round(precio * (1 - sl_frac), 2),
        "sl_dinamico_pct": round(sl_frac * 100, 2),
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
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
        "tp_parcial": resultado["tp_parcial"],
        "sl": resultado["sl"],
        "sl_dinamico_pct": resultado["sl_dinamico_pct"],
        "atr_pct": resultado["atr_pct"],
        "tp_parcial_tomado": False,
        "fecha_entrada": resultado["timestamp"],
    }

    return resultado


def ejecutar_venta(estado, par, precio, motivo, dry_run=False):
    """Registra una venta total (y ejecuta en Binance si hay keys).
    MEJORA 1: registra el motivo en ultimas_ventas para distinguir SL de TP.
    """
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
    # MEJORA 1: guardar motivo junto al timestamp para cooldowns diferenciados
    estado.setdefault("ultimas_ventas", {})[par] = {
        "ts": resultado["timestamp"],
        "motivo": motivo,
    }
    estado.get("max_pnl", {}).pop(par, None)
    estado.setdefault("historial", []).append(resultado)

    return resultado


def ejecutar_venta_parcial(estado, par, precio, motivo, fraccion=0.5, dry_run=False):
    """BONUS: venta parcial (TP parcial +6% → 50%). Reduce qty sin cerrar la posición.
    No activa cooldown porque la posición sigue abierta.
    """
    pos = estado["posiciones"][par]
    qty_total = pos["qty"]
    qty_vender = round(qty_total * fraccion,
                       5 if par == "BTCUSDT" else 4)
    if qty_vender <= 0:
        return None
    qty_restante = round(qty_total - qty_vender,
                         5 if par == "BTCUSDT" else 4)
    pnl = (precio - pos["precio_entrada"]) * qty_vender
    pnl_pct = ((precio / pos["precio_entrada"]) - 1) * 100

    resultado = {
        "par": par,
        "lado": "SELL",
        "qty": qty_vender,
        "qty_restante": qty_restante,
        "precio_entrada": pos["precio_entrada"],
        "precio_salida": precio,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "motivo": motivo,
        "parcial": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not dry_run and BINANCE_KEY and "your_" not in BINANCE_KEY:
        try:
            order = binance_signed("/order", {
                "symbol": par,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(qty_vender),
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

    # Actualizar posición: reducir qty y marcar parcial tomado
    pos["qty"] = qty_restante
    pos["tp_parcial_tomado"] = True
    estado.setdefault("historial", []).append(resultado)
    # NO tocar ultimas_ventas: la posición sigue abierta → no hay cooldown

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
            f"${precio:>10,.2f} | {score_str} | {regla} | {motivo[:500]}\n")

    try:
        with open(LOG_DECISIONES, "a") as f:
            f.write(line)
    except Exception:
        pass

    # DB histórica — decisión siempre; trade sólo si BUY/SELL.
    # Si jarvis_db falla, se ignora para no bloquear el ciclo.
    if _jdb is not None:
        try:
            score_total = scores.get("total") if isinstance(scores, dict) else (
                scores if isinstance(scores, int) else None)
            detalle = scores if isinstance(scores, dict) else None
            _jdb.registrar_decision(
                mercado="cripto", simbolo=par, accion=accion.strip().upper(),
                score=score_total, regla=regla, motivo=motivo[:500],
                ejecutada=(accion.strip().upper() in ("BUY", "SELL")),
                score_detalle=detalle, timestamp=ts,
            )
        except Exception as _e:
            log.warning(f"jarvis_db decisión falló: {_e}")


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
    sl_pct = resultado.get("sl_dinamico_pct", STOP_LOSS * 100)
    atr_pct = resultado.get("atr_pct")
    sl_info = (f"-{sl_pct:.1f}% (ATR {atr_pct:.2f}%)"
               if atr_pct is not None else f"-{sl_pct:.1f}%")
    msg = (
        f"\U0001f7e2 <b>JARVIS Cripto — COMPRA{modo}</b>\n"
        f"\U0001f4b0 {nombre} ({resultado['par']})\n"
        f"  Precio: ${resultado['precio']:,.2f}\n"
        f"  Cantidad: {resultado['qty']}\n"
        f"  Monto: ~${resultado['monto']:,.2f}\n"
        f"  TP parcial: ${resultado['tp_parcial']:,.2f} (+{TAKE_PROFIT_PARCIAL*100:.0f}% / 50%)\n"
        f"  TP total: ${resultado['tp']:,.2f} (+{TAKE_PROFIT*100:.0f}%)\n"
        f"  SL dinámico: ${resultado['sl']:,.2f} ({sl_info})\n"
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
                         "score": 0, "precio": 0,
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
        if motivo == "take-profit-parcial":
            resultado = ejecutar_venta_parcial(estado, par, precio, motivo,
                                               fraccion=0.5, dry_run=dry_run)
            if resultado is None:
                continue
            acciones.append(("VENTA-PARCIAL", resultado, {}))
            notificar_venta(resultado)
            _log_decision_cripto(par, "SELL", precio, 0,
                                 f"{motivo} 50% — entrada ${resultado['precio_entrada']}",
                                 regla="R-TP-PARCIAL")
            if _jdb is not None:
                try:
                    _jdb.registrar_trade(
                        mercado="cripto", simbolo=par, accion="SELL",
                        qty=resultado.get("qty"), precio=precio,
                        regla="R-TP-PARCIAL",
                        pnl_pct=resultado.get("pnl_pct"),
                        pnl_usd=resultado.get("pnl"),
                        motivo=f"TP parcial 50% @ +{TAKE_PROFIT_PARCIAL*100:.0f}%",
                    )
                except Exception as _e:
                    log.warning(f"jarvis_db SELL parcial falló: {_e}")
            print(f"     {par}: VENTA-PARCIAL (50%) P&L ${resultado['pnl']:+,.2f} "
                  f"({resultado['pnl_pct']:+.2f}%) — queda {resultado['qty_restante']}")
        elif motivo:
            resultado = ejecutar_venta(estado, par, precio, motivo, dry_run)
            acciones.append(("VENTA", resultado, {}))
            notificar_venta(resultado)
            _log_decision_cripto(par, "SELL", precio, 0, motivo, regla=motivo)
            # DB histórica — trade SELL con P&L real
            if _jdb is not None:
                try:
                    _jdb.registrar_trade(
                        mercado="cripto", simbolo=par, accion="SELL",
                        qty=resultado.get("qty"), precio=precio,
                        regla=motivo, pnl_pct=resultado.get("pnl_pct"),
                        pnl_usd=resultado.get("pnl"),
                        motivo=f"{motivo} — entrada ${resultado.get('precio_entrada')}",
                    )
                except Exception as _e:
                    log.warning(f"jarvis_db SELL falló: {_e}")
            print(f"     {par}: VENTA ({motivo}) P&L ${resultado['pnl']:+,.2f} "
                  f"({resultado['pnl_pct']:+.2f}%)")
        else:
            if par in estado.get("posiciones", {}):
                pos = estado["posiciones"][par]
                pnl_pct = ((precio / pos["precio_entrada"]) - 1) * 100
                max_pnl = estado.get("max_pnl", {}).get(par, 0) * 100
                trailing = " [trailing activo]" if max_pnl >= TRAILING_ACTIVACION * 100 else ""
                sl_p = pos.get("sl_dinamico_pct")
                sl_info = f" SL:{sl_p:.1f}%" if sl_p else ""
                tp_parc = " [TP-parcial tomado]" if pos.get("tp_parcial_tomado") else ""
                print(f"     {par}: HOLD — P&L {pnl_pct:+.2f}% "
                      f"(max: {max_pnl:+.1f}%){sl_info}{trailing}{tp_parc}")

    # ── 6) Evaluar entradas ──
    print("  6) Evaluando entradas...")

    # MEJORA 4: si se ejecutaron >= MAX_SL_24H stop-losses en las últimas 24h
    # (incluyendo los de este ciclo, ya registrados en historial), pausar compras.
    sl_24h = _contar_sl_24h(estado)
    pause_compras = sl_24h >= MAX_SL_24H
    if pause_compras:
        print(f"     [R-PAUSE] {sl_24h} stop-losses en 24h "
              f"(>= {MAX_SL_24H}) — pausando compras cripto")

    for par in pares_activos:
        scores = scores_por_par.get(par, {})
        senal = senales.get(par, {})
        precio = senal.get("precio", 0)

        if par in estado.get("posiciones", {}):
            _log_decision_cripto(par, "SKIP", precio, scores, "ya tiene posición", regla="POS")
            continue

        # MEJORA 1/3: cooldown post-venta (3d para SL, 12h para TP/trailing)
        en_cd, cd_hasta, cd_motivo = _en_cooldown_post_venta(estado, par)
        if en_cd:
            if cd_motivo == "stop-loss":
                msg = f"R-COOLDOWN: {par} en cooldown post-SL hasta {cd_hasta}"
                regla_log = "R-COOLDOWN"
            else:
                msg = f"cooldown post-{cd_motivo or 'venta'} hasta {cd_hasta}"
                regla_log = "COOLDOWN"
            print(f"     {par}: SKIP — {msg}")
            _log_decision_cripto(par, "SKIP", precio, scores, msg, regla=regla_log)
            continue

        # MEJORA 4: pausa por múltiples SL en 24h
        if pause_compras:
            msg = f"R-PAUSE: {sl_24h} stop-losses en 24h, pausando compras cripto"
            print(f"     {par}: SKIP — {msg}")
            _log_decision_cripto(par, "SKIP", precio, scores, msg, regla="R-PAUSE")
            continue

        # MEJORA 5: umbral endurecido si 2+ SL en 30 días
        n_sl_30d = _contar_sl_par_30d(estado, par)
        umbral_par = umbral
        if n_sl_30d >= SL_HISTORIAL_UMBRAL:
            umbral_par = max(umbral, UMBRAL_COMPRA_HISTORIAL)
            if umbral_par != umbral:
                print(f"     [R-HISTORIAL] {par}: {n_sl_30d} SL en 30 días, "
                      f"umbral subido a {umbral_par}")

        score_total = scores.get("total", 0)
        if score_total < umbral_par:
            msg = f"score {score_total} < umbral {umbral_par}"
            if umbral_par != umbral:
                msg += f" (R-HISTORIAL: {n_sl_30d} SL en 30d)"
            print(f"     {par}: WAIT — {msg} ({_score_str(scores)})")
            _log_decision_cripto(par, "WAIT", precio, scores, msg,
                                 regla=("R-HISTORIAL" if umbral_par != umbral else "UMBRAL"))
            continue

        print(f"     {par}: COMPRA — score {score_total:+d} >= {umbral_par} ({_score_str(scores)})")
        resultado = ejecutar_compra(estado, par, precio, dry_run)
        acciones.append(("COMPRA", resultado, scores))
        notificar_compra(resultado, scores, regimen)
        motivo_buy = f"score {score_total:+d} >= umbral {umbral_par}"
        if resultado.get("atr_pct") is not None:
            motivo_buy += f" | SL dinámico {resultado['sl_dinamico_pct']:.1f}% (ATR {resultado['atr_pct']:.2f}%)"
        _log_decision_cripto(par, "BUY", precio, scores, motivo_buy, regla="MULTI-SCORE")
        # DB histórica — trade BUY
        if _jdb is not None:
            try:
                _jdb.registrar_trade(
                    mercado="cripto", simbolo=par, accion="BUY",
                    qty=resultado.get("qty"), precio=resultado.get("precio", precio),
                    score=score_total, regla="MULTI-SCORE",
                    motivo=motivo_buy,
                    score_detalle=scores,
                )
            except Exception as _e:
                log.warning(f"jarvis_db BUY falló: {_e}")

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

    # Snapshot cripto en DB histórica (best-effort, no bloquea).
    if _jdb is not None:
        try:
            posiciones_snap = []
            pnl_unrealized = 0.0
            for p_par, pos in estado.get("posiciones", {}).items():
                precio_ahora = senales.get(p_par, {}).get("precio") or 0
                if not precio_ahora:
                    continue
                qty = pos.get("qty", 0)
                entrada = pos.get("precio_entrada", 0) or 0
                valor = qty * precio_ahora
                pnl_usd = (precio_ahora - entrada) * qty
                pnl_pct = ((precio_ahora / entrada) - 1) * 100 if entrada else 0
                pnl_unrealized += pnl_usd
                posiciones_snap.append({
                    "par": p_par, "qty": qty,
                    "precio_entrada": entrada, "precio_actual": precio_ahora,
                    "valor_actual": round(valor, 2),
                    "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
                })
            # Balance USDT real si Binance responde, sino suma posiciones
            equity_cri = None
            cash_cri = None
            try:
                bal = obtener_balance()
                if bal:
                    total = 0.0
                    for b in bal:
                        if b["activo"] == "USDT":
                            cash_cri = b["total"]
                            total += b["total"]
                        else:
                            par = f"{b['activo']}USDT"
                            pr = senales.get(par, {}).get("precio")
                            if pr:
                                total += b["total"] * pr
                    equity_cri = round(total, 2)
            except Exception:
                pass
            datos_cri = {
                "equity": equity_cri,
                "cash": cash_cri,
                "posiciones": posiciones_snap,
                "pnl_dia": round(pnl_unrealized, 2),
            }
            _jdb.guardar_snapshot_diario(None, datos_cri)
            log.info("[db] snapshot cripto guardado")
        except Exception as _e:
            log.warning(f"[db] snapshot cripto falló: {_e}")


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
