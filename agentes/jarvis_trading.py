#!/home/hproano/asistente_env/bin/python
"""
Agente JARVIS de Trading — v2 (reglas automáticas).
Las decisiones de trading las toman REGLAS basadas en indicadores técnicos.
El LLM solo valida (veto por noticias), explica y sugiere.
"""

import os
import sys
import json
import math
import re
import requests
from datetime import datetime, timedelta

# Rutas del proyecto
PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)
sys.path.insert(0, os.path.join(PROYECTO, ".."))

from trading.monitor_mercado import (
    ACCIONES,
    CRIPTOS_BINANCE,
    NOMBRE_CRIPTO,
    obtener_accion,
    obtener_cripto_binance,
    construir_resumen,
)
from trading.indicadores_tecnicos import (
    descargar_historico,
    calcular_indicadores,
    generar_senal,
)

# IBKR trading adapter (cuenta real, Gateway Docker localhost:4001)
from agentes.ibkr_trading import get_balance, get_positions, buy, sell, _precio_fallback

from datos.contexto_mercado import get_contexto_completo
from datos.fuentes_mercado import get_senales_institucionales
from datos.regimen_mercado import get_regimen_actual
from datos.quantconnect_estrategias import get_senales_quant
from datos.earnings_calls_nlp import get_tono_ejecutivos
from datos.memoria_jarvis import guardar_decision_trading as _guardar_decision
from datos.marketdata_scorer import get_opciones_signal as _get_opciones_signal
from datos.unusual_whales_scorer import get_institutional_flow as _get_institutional_flow
from datos.quiver_scorer import get_quiver_score as _get_quiver_score

# DB histórica (opcional — si falla el import, trading sigue funcionando).
try:
    from datos import jarvis_db as _jdb
except Exception as _e:
    _jdb = None
    print(f"[jarvis_trading] jarvis_db no disponible: {_e}")

from dotenv import load_dotenv

load_dotenv(os.path.join(PROYECTO, ".env"))

# Config centralizada
import importlib.util as _ilu
_cfg_path = os.path.join(PROYECTO, "trading", "config.py")
_cfg_spec = _ilu.spec_from_file_location("trading_config", _cfg_path)
_cfg = _ilu.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)

ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES
PRIORIDAD_SHARPE = _cfg.PRIORIDAD_SHARPE
OLLAMA_URL = _cfg.OLLAMA_URL
OLLAMA_URL_CORE = _cfg.OLLAMA_URL_CORE
OLLAMA_URL_POWER = _cfg.OLLAMA_URL_POWER
MODEL_DEEP = _cfg.MODEL_DEEP
MODEL_FAST = _cfg.MODEL_FAST
# Mapeo modelo → URL del nodo correcto
_MODEL_URL = {MODEL_DEEP: OLLAMA_URL_CORE, MODEL_FAST: OLLAMA_URL_POWER}
MAX_POR_OPERACION = _cfg.MAX_POR_OPERACION
MAX_POSICIONES = _cfg.MAX_POSICIONES
STOP_LOSS_PCT = _cfg.STOP_LOSS_PCT
TAKE_PROFIT_PCT = _cfg.TAKE_PROFIT_PCT

# ── LIVE TRADING GUARDRAILS ────────────────────────────────
# Overrides para operar con capital limitado en cuenta real IBKR.
# Posiciones existentes (AMD, NVDA) son del usuario y no se tocan.
JARVIS_LIVE_CAPITAL = 15000.0  # Capital operando en acciones IBKR (real ~$10.8k + margen)
JARVIS_LIVE_HASTA = os.getenv("JARVIS_LIVE_HASTA", "2026-12-31")
POSICIONES_PROTEGIDAS = {"AMD", "NVDA"}  # No vender/comprar — son del usuario
ACTIVOS_DEFENSIVOS = ["JNJ", "GLD", "HYG", "AGG", "IEF", "KO", "VZ", "XLU", "T", "D", "IBM"]
ACTIVOS_REFUGIO = {"GLD", "IEF", "AGG"}  # Safe-haven en modo pánico

# Universo completo de config.py (22 activos) para BULL/LATERAL
_UNIVERSO_COMPLETO = _cfg.ACTIVOS_OPERABLES
_PRIORIDAD_COMPLETA = _cfg.PRIORIDAD_SHARPE

# Overrides: $1500/trade, 10 posiciones max
MAX_POR_OPERACION = 1500.0
MAX_POR_TRADE = MAX_POR_OPERACION
MAX_POSICIONES = 10

# Determinar régimen al inicio para seleccionar universo de activos
try:
    _regimen_inicial = get_regimen_actual().get("regimen", "LATERAL")
except Exception:
    _regimen_inicial = "LATERAL"

if _regimen_inicial == "BEAR":
    ACTIVOS_OPERABLES = ACTIVOS_DEFENSIVOS
    SIMBOLOS_OPERABLES = set(ACTIVOS_DEFENSIVOS)
    PRIORIDAD_SHARPE = {s: i for i, s in enumerate(ACTIVOS_DEFENSIVOS)}
else:
    # BULL o LATERAL: universo completo de config.py
    ACTIVOS_OPERABLES = _UNIVERSO_COMPLETO
    SIMBOLOS_OPERABLES = set(_UNIVERSO_COMPLETO)
    PRIORIDAD_SHARPE = _PRIORIDAD_COMPLETA

JARVIS_EXCLUIR = POSICIONES_PROTEGIDAS  # {"AMD", "NVDA"} — nunca tocar
MAX_POSICIONES_JARVIS = MAX_POSICIONES  # 10
HOLD_MINIMO_MINUTOS = 30  # No vender posición nueva antes de 30 min
POSICIONES_TS_PATH = os.path.join(PROYECTO, "logs", "jarvis_posiciones_ts.json")


def get_posiciones_jarvis(posiciones_todas):
    """Retorna solo posiciones gestionadas por JARVIS (excluye AMD, NVDA y fracciones <1 acc)."""
    return [p for p in posiciones_todas
            if p["symbol"] not in JARVIS_EXCLUIR
            and p["symbol"] in SIMBOLOS_OPERABLES
            and abs(float(p.get("qty", 0))) >= 1.0]


def _cargar_timestamps_posiciones():
    """Carga timestamps de entrada de posiciones JARVIS."""
    try:
        with open(POSICIONES_TS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _guardar_timestamp_posicion(simbolo, ts=None):
    """Registra timestamp de entrada al comprar una posición."""
    if ts is None:
        ts = datetime.now().isoformat()
    datos = _cargar_timestamps_posiciones()
    datos[simbolo] = ts
    os.makedirs(os.path.dirname(POSICIONES_TS_PATH), exist_ok=True)
    with open(POSICIONES_TS_PATH, "w") as f:
        json.dump(datos, f, indent=2)


def _eliminar_timestamp_posicion(simbolo):
    """Elimina timestamp al vender completamente una posición."""
    datos = _cargar_timestamps_posiciones()
    if simbolo in datos:
        del datos[simbolo]
        with open(POSICIONES_TS_PATH, "w") as f:
            json.dump(datos, f, indent=2)


def _minutos_en_posicion(simbolo):
    """Retorna minutos desde la entrada, o None si no hay registro."""
    datos = _cargar_timestamps_posiciones()
    ts_str = datos.get(simbolo)
    if not ts_str:
        return None
    try:
        ts_entry = datetime.fromisoformat(ts_str)
        return (datetime.now() - ts_entry).total_seconds() / 60
    except Exception:
        return None


# ── MEJORA 1/4/5: Cooldown y historial de stop-losses ───────

def _cargar_sl_state():
    """Carga el estado de cooldown e historial de SL. Estructura:
    {"cooldowns": {sym: iso_ts_sl}, "historial": {sym: [iso_ts, ...]}}
    """
    try:
        with open(COOLDOWN_SL_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("cooldowns", {})
    data.setdefault("historial", {})
    return data


def _guardar_sl_state(data):
    os.makedirs(os.path.dirname(COOLDOWN_SL_PATH), exist_ok=True)
    with open(COOLDOWN_SL_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _registrar_sl_ejecutado(simbolo, ts=None):
    """Registra un stop-loss ejecutado: activa cooldown de N días y añade al historial."""
    if ts is None:
        ts = datetime.now().isoformat()
    state = _cargar_sl_state()
    state["cooldowns"][simbolo] = ts
    hist = state["historial"].setdefault(simbolo, [])
    hist.append(ts)
    # Recortar historial a los últimos 90 días para mantener archivo pequeño
    limite = datetime.now() - timedelta(days=90)
    hist_filtrado = []
    for t in hist:
        ts_parsed = _parse_ts_safe(t)
        if ts_parsed and ts_parsed >= limite:
            hist_filtrado.append(t)
    state["historial"][simbolo] = hist_filtrado
    _guardar_sl_state(state)


def _parse_ts_safe(ts_str):
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def _esta_en_cooldown_sl(simbolo):
    """Retorna (en_cooldown: bool, fecha_fin_iso: str|None)."""
    state = _cargar_sl_state()
    ts_str = state["cooldowns"].get(simbolo)
    if not ts_str:
        return False, None
    ts_sl = _parse_ts_safe(ts_str)
    if ts_sl is None:
        return False, None
    fin = ts_sl + timedelta(days=COOLDOWN_SL_DIAS)
    if datetime.now() < fin:
        return True, fin.strftime("%Y-%m-%d")
    return False, None


def _contar_sl_hoy():
    """Cuenta stop-losses ejecutados hoy (todos los símbolos) según el historial."""
    state = _cargar_sl_state()
    hoy = datetime.now().date()
    total = 0
    for sym, ts_list in state["historial"].items():
        for ts_str in ts_list:
            ts = _parse_ts_safe(ts_str)
            if ts and ts.date() == hoy:
                total += 1
    return total


def _contar_sl_simbolo_30d(simbolo):
    """Cuenta stop-losses del símbolo en los últimos SL_HISTORIAL_DIAS días."""
    state = _cargar_sl_state()
    ts_list = state["historial"].get(simbolo, [])
    limite = datetime.now() - timedelta(days=SL_HISTORIAL_DIAS)
    count = 0
    for ts_str in ts_list:
        ts = _parse_ts_safe(ts_str)
        if ts and ts >= limite:
            count += 1
    return count


# ── MEJORA 2: Stop-loss dinámico basado en ATR ──────────────

def _calcular_atr_pct(df, periodo=14):
    """ATR(14) expresado como % del último precio de cierre. None si no hay datos."""
    try:
        import pandas as _pd
        if len(df) < periodo + 1:
            return None
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        close_prev = close.shift(1)
        tr = _pd.concat([
            high - low,
            (high - close_prev).abs(),
            (low - close_prev).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(window=periodo).mean()
        precio = float(close.iloc[-1])
        atr_val = float(atr.iloc[-1])
        if precio <= 0 or atr_val != atr_val:  # NaN check
            return None
        return (atr_val / precio) * 100
    except Exception:
        return None


def _sl_dinamico_desde_atr(atr_pct):
    """SL dinámico = clamp(2 * ATR%, 2%, 6%). Retorna fracción (0.03 = 3%)."""
    if atr_pct is None:
        return STOP_LOSS_REGLA_PCT
    sl_pct = 2.0 * atr_pct
    sl_pct = max(SL_DINAMICO_MIN_PCT, min(SL_DINAMICO_MAX_PCT, sl_pct))
    return sl_pct / 100.0


# ══════════════════════════════════════════════════════════════
#  REGLAS AUTOMÁTICAS — Umbrales
# ══════════════════════════════════════════════════════════════

UMBRAL_COMPRA_NORMAL = 2        # Score mínimo en BULL (antes 3)
UMBRAL_COMPRA_BEAR = 1          # Score mínimo en BEAR (antes 2)
UMBRAL_COMPRA_AGRESIVO = 0      # Score en pánico extremo (F&G < 15): comprar cualquier defensivo
UMBRAL_REFUGIO_PANICO = 0       # Score mínimo para refugios (GLD, IEF, AGG) en pánico
MAX_REFUGIOS_PANICO = 3         # Permitir hasta 3 refugios en modo pánico
STOP_LOSS_REGLA_PCT = 0.03      # -3% pérdida → venta obligatoria (fallback si ATR no disponible)
SL_DINAMICO_MIN_PCT = 2.0       # MEJORA 2: piso del SL dinámico (2%)
SL_DINAMICO_MAX_PCT = 6.0       # MEJORA 2: techo del SL dinámico (6%)
TAKE_PROFIT_PARCIAL_PCT = 0.08  # +8% ganancia → vender 50% (antes 10%)
TAKE_PROFIT_TOTAL_PCT = 0.15    # +15% ganancia → vender todo
TRAILING_STOP_ACTIVACION = 0.03 # MEJORA 6: +3% → mover stop-loss a breakeven (antes 5%)
VIX_UMBRAL_ALTO = 30            # Reducir posición al 50%
VIX_EXTREMO = 35                # Reducir posición al 30%
FACTOR_VIX_ALTO = 0.50
FACTOR_VIX_EXTREMO = 0.30
FNG_UMBRAL_PANICO = 20          # Modo oportunidad + priorizar refugios
FNG_AGRESIVO = 15               # Modo agresivo: umbral compra = 0
ROTACION_DIAS_FLAT = 10         # MEJORA 3: días sin movimiento para rotar (antes 5)
ROTACION_PCT_FLAT = 0.01        # <1% movimiento = "flat"
ROTACION_SCORE_MINIMO = 4       # MEJORA 3: score mínimo del reemplazo (antes 3)

# MEJORA 1/4/5: gestión post stop-loss
COOLDOWN_SL_DIAS = 3            # Días calendario de cooldown tras un SL
MAX_SL_POR_DIA = 3              # Si >=3 SL hoy, pausar compras
SL_HISTORIAL_DIAS = 30          # Ventana para contar SL históricos por activo
SL_HISTORIAL_UMBRAL = 2         # SL en ventana para endurecer umbral
UMBRAL_COMPRA_HISTORIAL = 4     # Umbral endurecido para activos con 2+ SL en 30d
COOLDOWN_SL_PATH = os.path.join(PROYECTO, "logs", "jarvis_sl_cooldown.json")

PALABRAS_NEGATIVAS = [
    "downgrade", "lawsuit", "sued", "fraud", "investigation", "recall",
    "layoff", "bankruptcy", "SEC", "crash", "plunge", "scandal",
    "demanda", "fraude", "quiebra", "despidos",
]


LOG_DECISIONES = os.path.join(PROYECTO, "logs", "trading_decisiones.log")


def _log_wa_warning(msg):
    """Escribe warning de WhatsApp en trading_decisiones.log."""
    os.makedirs(os.path.dirname(LOG_DECISIONES), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_DECISIONES, "a") as f:
        f.write(f"{ts} | WARNING | {msg}\n")


def _notificar(mensaje):
    """Envía notificación solo a WhatsApp (texto plano via puerto 8001)."""
    texto_plano = re.sub(r'<[^>]+>', '', mensaje)
    texto_plano = (texto_plano
                   .replace('&amp;', '&').replace('&lt;', '<')
                   .replace('&gt;', '>').replace('&#39;', "'"))
    try:
        requests.post("http://localhost:8001/alerta",
                      json={"mensaje": texto_plano}, timeout=10)
    except Exception:
        pass  # WhatsApp alertas opcional, no interrumpir trading


def _get_finbert_score(simbolo: str, noticias: list) -> int:
    """Obtiene score de sentimiento FinBERT para un activo. Retorna -2 a +2."""
    try:
        resp = requests.post(
            "http://192.168.208.80:8002/score",
            json={"simbolo": simbolo, "noticias": noticias},
            timeout=5
        )
        if resp.ok:
            return resp.json().get("score", 0)
    except Exception:
        pass
    return 0


def _db_accion_map(accion, regla=""):
    """Normaliza la acción para la DB: COMPRAR→BUY, VENDER→SELL,
    MANTENER→HOLD; si regla es VETO-LLM/SKIP/SLOT-FULL, usa SKIP."""
    a = (accion or "").upper()
    if a in ("BUY", "COMPRAR"):
        return "BUY"
    if a in ("SELL", "VENDER"):
        return "SELL"
    if regla in ("VETO-LLM", "SKIP", "SLOT-FULL", "R-NEWS"):
        return "SKIP"
    return "HOLD"


def _log_decision(simbolo, accion, precio_actual=None, precio_entrada=None,
                  pnl_pct=None, motivo="", score=None, regla="",
                  score_detalle=None, **_kw):
    """Escribe una línea en trading_decisiones.log.
    Formato:
      BUY:  ts | BUY  | SYM  | $price | score:+N | técnico:+N finbert:+N opciones:+N flow:+N | señales
      SELL: ts | SELL | SYM  | $price | entrada:$X | P&L:+X.X% | motivo:reason
      HOLD: ts | HOLD | SYM  | $price | score:+N | reason
      SKIP: ts | SKIP | SYM  | score:+N | reason
    """
    os.makedirs(os.path.dirname(LOG_DECISIONES), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Mapear acción
    if accion in ("COMPRAR", "BUY"):
        tag = "BUY "
    elif accion in ("VENDER", "SELL"):
        tag = "SELL"
    elif regla in ("R-NEWS", "SLOT-FULL", "VETO-LLM", "SKIP"):
        tag = "SKIP"
    else:
        tag = "HOLD"

    parts = [ts, tag, f"{simbolo:<4}"]

    if tag == "SELL":
        if precio_actual:
            parts.append(f"${precio_actual:.2f}")
        if precio_entrada is not None:
            parts.append(f"entrada:${precio_entrada:.2f}")
        if pnl_pct is not None:
            parts.append(f"P&L:{pnl_pct:+.1f}%")
        parts.append(f"motivo:{motivo}")
    elif tag == "BUY ":
        if precio_actual:
            parts.append(f"${precio_actual:.2f}")
        if score is not None:
            parts.append(f"score:{score:+d}")
        if score_detalle:
            parts.append(score_detalle)
        parts.append(motivo)
    elif tag == "SKIP":
        if score is not None:
            parts.append(f"score:{score:+d}")
        if score_detalle:
            parts.append(score_detalle)
        parts.append(motivo)
    else:  # HOLD
        if precio_actual:
            parts.append(f"${precio_actual:.2f}")
        if score is not None:
            parts.append(f"score:{score:+d}")
        parts.append(motivo)

    line = " | ".join(p for p in parts if p)
    with open(LOG_DECISIONES, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _get_activos_operados_hoy():
    """Lee el log de decisiones y retorna símbolos comprados y vendidos hoy."""
    hoy = datetime.now().strftime("%Y-%m-%d")
    comprados = set()
    vendidos = set()
    try:
        with open(LOG_DECISIONES, "r") as f:
            for linea in f:
                if not linea.startswith(hoy):
                    continue
                if "| SELL |" in linea or "| SELL|" in linea:
                    partes = linea.split("|")
                    if len(partes) >= 3:
                        vendidos.add(partes[2].strip())
                elif "| BUY  |" in linea or "| BUY |" in linea:
                    partes = linea.split("|")
                    if len(partes) >= 3:
                        comprados.add(partes[2].strip())
    except FileNotFoundError:
        pass
    return comprados, vendidos


def refrescar_precios_tiingo(posiciones):
    """Actualiza current_price de cada posición via Tiingo (fuente confiable)."""
    for p in posiciones:
        sym = p["symbol"]
        precio = _precio_fallback(sym)
        if precio is not None:
            entry = float(p["avg_entry_price"])
            qty = abs(float(p["qty"]))
            p["current_price"] = str(round(precio, 2))
            p["unrealized_pl"] = str(round((precio - entry) * qty, 2))
            p["unrealized_plpc"] = str(round((precio / entry) - 1, 4)) if entry > 0 else "0"
            p["market_value"] = str(round(precio * qty, 2))
    return posiciones


# ══════════════════════════════════════════════════════════════
#  1. CONDICIONES DE MERCADO
# ══════════════════════════════════════════════════════════════

def evaluar_condiciones_mercado(datos_contexto):
    """Evalúa VIX, F&G, noticias y régimen de mercado. Retorna ajustes para la sesión."""
    fng = datos_contexto.get("fear_greed", {})
    vix = datos_contexto.get("vix", {})
    noticias = datos_contexto.get("noticias", {})

    reglas = []
    max_trade_ajustado = MAX_POR_OPERACION
    modo_agresivo = False
    activos_excluidos = set()

    vix_precio = vix.get("precio") or 0
    fng_valor = fng.get("valor") or 50

    # ── Régimen de mercado ──
    try:
        regimen = get_regimen_actual()
    except Exception:
        regimen = {"regimen": "LATERAL", "confianza": 0, "razon": "No disponible",
                   "activos_permitidos": list(_UNIVERSO_COMPLETO),
                   "max_posiciones": MAX_POSICIONES, "umbral_compra": UMBRAL_COMPRA_NORMAL}

    regimen_tipo = regimen["regimen"]
    max_posiciones_regimen = regimen["max_posiciones"]
    umbral_regimen = regimen["umbral_compra"]
    activos_permitidos_regimen = set(regimen.get("activos_permitidos", _UNIVERSO_COMPLETO))

    reglas.append(
        f"R-REGIMEN: {regimen_tipo} (confianza {regimen.get('confianza', 0)}/3) — {regimen.get('razon', '')}"
    )

    if regimen_tipo == "BEAR":
        # En BEAR, excluir todo activo no defensivo (globals ya fijados en main())
        activos_no_defensivos = set(_UNIVERSO_COMPLETO) - set(ACTIVOS_DEFENSIVOS)
        activos_excluidos |= activos_no_defensivos
        reglas.append(
            f"R-BEAR: Solo defensivos permitidos: {', '.join(sorted(ACTIVOS_DEFENSIVOS))}"
        )
    elif regimen_tipo == "BULL":
        reglas.append(
            f"R-BULL: Universo completo, max pos {max_posiciones_regimen}, umbral {umbral_regimen}"
        )

    # ── Regla VIX extremo (> 35) → 30% ──
    if vix_precio > VIX_EXTREMO:
        max_trade_ajustado = round(MAX_POR_OPERACION * FACTOR_VIX_EXTREMO, 2)
        reglas.append(
            f"R-VIX35: VIX en {vix_precio} (>{VIX_EXTREMO}): posición al {FACTOR_VIX_EXTREMO*100:.0f}% "
            f"→ máx ${max_trade_ajustado:,.0f}/trade"
        )
    # Regla VIX alto (> 30) → 50%
    elif vix_precio > VIX_UMBRAL_ALTO:
        max_trade_ajustado = round(MAX_POR_OPERACION * FACTOR_VIX_ALTO, 2)
        reglas.append(
            f"R-VIX30: VIX en {vix_precio} (>{VIX_UMBRAL_ALTO}): posición al {FACTOR_VIX_ALTO*100:.0f}% "
            f"→ máx ${max_trade_ajustado:,.0f}/trade"
        )

    # ── Regla F&G pánico / agresivo ──
    modo_panico = False
    if fng_valor < FNG_AGRESIVO:
        modo_agresivo = True
        modo_panico = True
        reglas.append(
            f"R-FNG15: Fear & Greed en {fng_valor} (<{FNG_AGRESIVO}): "
            f"MODO PÁNICO — score >= {UMBRAL_COMPRA_AGRESIVO} para defensivos, "
            f"refugios {','.join(sorted(ACTIVOS_REFUGIO))} priorizados (hasta {MAX_REFUGIOS_PANICO})"
        )
    elif fng_valor < FNG_UMBRAL_PANICO:
        modo_panico = True
        reglas.append(
            f"R-FNG20: Fear & Greed en {fng_valor} (<{FNG_UMBRAL_PANICO}): "
            f"Modo oportunidad — refugios {','.join(sorted(ACTIVOS_REFUGIO))} priorizados"
        )

    # ── Noticias negativas → excluir activo ──
    for activo, arts in noticias.items():
        activo_upper = activo.upper()
        for art in arts:
            if "error" in art:
                continue
            texto = (art.get("titulo", "") + " " + art.get("resumen", "")).lower()
            for palabra in PALABRAS_NEGATIVAS:
                if palabra in texto:
                    activos_excluidos.add(activo_upper)
                    reglas.append(
                        f"R-NEWS: {activo_upper} excluido — \"{palabra}\" en \"{art['titulo'][:50]}...\""
                    )
                    break
            if activo_upper in activos_excluidos:
                break

    # ── Google Trends contrarian ──
    # FIX P4: si Google Trends no está disponible (rate limit, sin cache),
    # tratar como NEUTRAL — nunca activar modo_agresivo con datos faltantes.
    trends_senal = "NEUTRAL"
    try:
        from datos.google_trends import get_tendencias_mercado
        gt = get_tendencias_mercado()
        if not gt.get("disponible", True):
            reglas.append("R-TRENDS: Google Trends no disponible — neutral")
        else:
            trends_senal = gt.get("senal", "NEUTRAL")
            if trends_senal in ("COMPRA", "COMPRA_LEVE"):
                modo_agresivo = True
                reglas.append(
                    f"R-TRENDS: Pánico retail ({gt.get('panico_score', 0)}/100) → modo agresivo contrarian"
                )
            elif trends_senal in ("VENTA", "VENTA_LEVE"):
                reglas.append(
                    f"R-TRENDS: Euforia retail ({gt.get('euforia_score', 0)}/100) → cautela en compras"
                )
            else:
                reglas.append(f"R-TRENDS: Sentimiento retail neutral")
    except Exception as e:
        reglas.append(f"R-TRENDS: No disponible ({e})")

    # Determinar umbral final por régimen y modo
    if modo_agresivo:
        umbral_compra = UMBRAL_COMPRA_AGRESIVO       # 0 en pánico extremo
    elif regimen_tipo == "BEAR":
        umbral_compra = UMBRAL_COMPRA_BEAR            # 1 en BEAR
    elif regimen_tipo == "BULL":
        umbral_compra = min(umbral_regimen, UMBRAL_COMPRA_NORMAL)  # 2 en BULL
    else:
        umbral_compra = UMBRAL_COMPRA_NORMAL          # 2 LATERAL

    return {
        "max_por_trade": max_trade_ajustado,
        "modo_agresivo": modo_agresivo,
        "modo_panico": modo_panico,
        "umbral_compra": umbral_compra,
        "activos_excluidos": activos_excluidos,
        "reglas": reglas,
        "vix_precio": vix_precio,
        "fng_valor": fng_valor,
        "regimen": regimen_tipo,
        "regimen_confianza": regimen.get("confianza", 0),
        "max_posiciones_regimen": max_posiciones_regimen,
        "activos_permitidos_regimen": activos_permitidos_regimen,
        "google_trends": trends_senal,
    }


# ══════════════════════════════════════════════════════════════
#  2. DATOS DE MERCADO E INDICADORES TÉCNICOS
# ══════════════════════════════════════════════════════════════

def obtener_datos_mercado():
    """Obtiene datos crudos del mercado (acciones operables + criptos como contexto)."""
    datos_acciones = []
    datos_criptos = []
    errores = []

    for simbolo in ACTIVOS_OPERABLES:
        try:
            datos_acciones.append(obtener_accion(simbolo))
        except Exception as e:
            errores.append(f"{simbolo}: {e}")

    for par in CRIPTOS_BINANCE:
        try:
            datos_criptos.append(obtener_cripto_binance(par))
        except Exception as e:
            errores.append(f"{NOMBRE_CRIPTO.get(par, par)}: {e}")

    return datos_acciones, datos_criptos, errores


def calcular_senales_tecnicas():
    """Calcula indicadores técnicos y señales para todos los activos operables.
    Enriquece cada señal con atr_pct y sl_dinamico (MEJORA 2).
    """
    senales = {}
    for simbolo in ACTIVOS_OPERABLES:
        try:
            df = descargar_historico(simbolo)
            df = calcular_indicadores(df)
            senal = generar_senal(df, simbolo)
            # MEJORA 2: SL dinámico por ATR
            atr_pct = _calcular_atr_pct(df)
            senal["atr_pct"] = round(atr_pct, 3) if atr_pct is not None else None
            senal["sl_dinamico"] = _sl_dinamico_desde_atr(atr_pct)
            senales[simbolo] = senal
        except Exception as e:
            senales[simbolo] = {
                "simbolo": simbolo,
                "precio": 0,
                "senal": "ERROR",
                "puntuacion": 0,
                "razones": [f"Error: {e}"],
                "indicadores": {},
                "atr_pct": None,
                "sl_dinamico": STOP_LOSS_REGLA_PCT,
            }
    return senales


# ══════════════════════════════════════════════════════════════
#  3. MOTOR DE REGLAS AUTOMÁTICAS
# ══════════════════════════════════════════════════════════════

def aplicar_reglas_automaticas(senales, posiciones, condiciones, datos_acciones,
                               senales_inst=None, senales_qt=None, tono_exec=None,
                               noticias_por_activo=None):
    """
    Aplica reglas automáticas de trading. NO usa LLM.
    Retorna lista de decisiones con la regla que las activó.

    Reglas de venta:
      R-SL:         Pérdida > 3% → VENDER todo (stop-loss)
      R-TP-TOTAL:   Ganancia > 15% → VENDER todo
      R-TP-PARCIAL: Ganancia > 8% → VENDER 50%
      R-TRAILING:   Subió +5% y volvió a breakeven → VENDER
      R-OPT-SELL:   Opciones mayoría PUTS → VENDER anticipado
      R-ROTACION:   Flat >10 días y mejor candidato (score>=4) → rotar capital
    Reglas de compra:
      R-BUY:      Score >= umbral AND sin posición AND slots → COMPRAR
      R-REFUGIO:  Activo refugio en pánico (score >= 0) → COMPRAR
      R-OPT-BUY:  CALLS inusuales refuerzan señal (score >= umbral-1)
      R-QUANT:    Señales quant ajustan score (+2/+1/-1)
      R-EARNINGS: Tono ejecutivos ajusta score (+1/-1/-2)
    Umbrales: BULL >= 2, BEAR >= 1, Pánico >= 0 (refugios)
    """
    if senales_inst is None:
        senales_inst = {}
    if senales_qt is None:
        senales_qt = {}
    if tono_exec is None:
        tono_exec = {}

    posiciones_map = {p["symbol"]: p for p in posiciones}
    precios_map = {d["simbolo"]: d["precio"] for d in datos_acciones}
    n_posiciones = len(posiciones_map)

    umbral = condiciones["umbral_compra"]
    excluidos = condiciones["activos_excluidos"]

    decisiones = []
    log_reglas = []

    # ── Fase 1: Evaluar posiciones abiertas ──
    # (stop-loss / trailing-stop / take-profit parcial y total / hold-min / opciones / rotación)
    rotacion_candidatas = []  # posiciones flat para posible rotación

    # Anti-day-trading: detectar operaciones de hoy (una sola lectura del log)
    comprados_hoy, vendidos_hoy = _get_activos_operados_hoy()

    for sym, pos in posiciones_map.items():
        entry = float(pos["avg_entry_price"])
        current = float(pos["current_price"])
        qty = float(pos["qty"])
        pnl_pct = ((current / entry) - 1) if entry > 0 else 0

        # R-SL: Pérdida > SL dinámico (ATR) → venta obligatoria (siempre activo, ignora day-trading)
        # MEJORA 2: SL dinámico por activo — clamp(2*ATR%, 2%, 6%)
        senal_sym = senales.get(sym, {}) if isinstance(senales, dict) else {}
        sl_pct_sym = senal_sym.get("sl_dinamico", STOP_LOSS_REGLA_PCT)
        atr_sym = senal_sym.get("atr_pct")
        if pnl_pct <= -sl_pct_sym:
            atr_str = f"ATR%={atr_sym:.1f}" if atr_sym is not None else "ATR=n/d"
            regla = (f"R-SL: pérdida {pnl_pct*100:+.1f}% "
                     f"(>-{sl_pct_sym*100:.1f}% {atr_str})")
            decisiones.append({
                "simbolo": sym,
                "accion": "VENDER",
                "qty": qty,
                "razon": regla,
                "regla": "R-SL",
                "precio_entrada": entry,
                "precio_actual": current,
                "pnl_pct": round(pnl_pct * 100, 2),
                "sl_dinamico_pct": round(sl_pct_sym * 100, 2),
                "atr_pct": atr_sym,
            })
            log_reglas.append(
                f"  [R-SL] {sym}: VENDER todo — pérdida {pnl_pct*100:+.1f}% "
                f"(umbral dinámico {sl_pct_sym*100:.1f}%, {atr_str})"
            )
            continue

        # R-NODAYTR (inverso): no vender activos comprados hoy (excepto R-SL ya evaluado arriba)
        if sym in comprados_hoy:
            decisiones.append({
                "simbolo": sym, "accion": "MANTENER",
                "razon": f"R-NODAYTR: comprado hoy — no vender mismo día (Cash account)",
                "regla": "R-NODAYTR", "pnl_pct": round(pnl_pct * 100, 2),
            })
            log_reglas.append(f"  [R-NODAYTR] {sym}: comprado hoy — skip venta (anti-day-trading)")
            continue

        # R-TRAILING: Si ganancia >= +5%, mover stop a breakeven
        # Efectivamente: si subió +5% pero ahora volvió a <= 0% → vender
        if pnl_pct <= 0 and entry > 0:
            # Verificar si alguna vez superó +5% (usando max_price del timestamp)
            # Aproximación: si current <= entry pero el histórico subió, usar Tiingo high
            pass  # El trailing stop se evalúa abajo como breakeven

        # R-TP-TOTAL: Ganancia >= 15% → vender TODO
        if pnl_pct >= TAKE_PROFIT_TOTAL_PCT:
            regla = (f"R-TP-TOTAL: ganancia {pnl_pct*100:+.1f}% "
                     f"(>+{TAKE_PROFIT_TOTAL_PCT*100:.0f}%) → vender todo")
            decisiones.append({
                "simbolo": sym,
                "accion": "VENDER",
                "qty": qty,
                "razon": regla,
                "regla": "R-TP-TOTAL",
                "precio_entrada": entry,
                "precio_actual": current,
                "pnl_pct": round(pnl_pct * 100, 2),
            })
            log_reglas.append(
                f"  [R-TP-TOTAL] {sym}: VENDER todo ({int(qty)}) — ganancia {pnl_pct*100:+.1f}%")
            continue

        # R-TP-PARCIAL: Ganancia >= 8% → vender 50%
        if pnl_pct >= TAKE_PROFIT_PARCIAL_PCT:
            qty_mitad = max(1, int(qty // 2))
            if qty_mitad > 0:
                regla = (f"R-TP-PARCIAL: ganancia {pnl_pct*100:+.1f}% "
                         f"(>+{TAKE_PROFIT_PARCIAL_PCT*100:.0f}%) → vender mitad")
                decisiones.append({
                    "simbolo": sym,
                    "accion": "VENDER",
                    "qty": qty_mitad,
                    "razon": regla,
                    "regla": "R-TP-PARCIAL",
                    "precio_entrada": entry,
                    "precio_actual": current,
                    "pnl_pct": round(pnl_pct * 100, 2),
                })
                log_reglas.append(
                    f"  [R-TP-PARCIAL] {sym}: VENDER mitad ({qty_mitad}) — ganancia {pnl_pct*100:+.1f}%")
            continue

        # R-TRAILING: posición que subió >= +5% y ahora volvió a breakeven → vender
        # Usamos el timestamp de entrada: si P&L actual <= 0 pero la posición
        # tiene historial de ganancia, activamos trailing-stop a breakeven.
        # Guardamos max_pnl en el timestamp file para tracking preciso.
        ts_data = _cargar_timestamps_posiciones()
        max_pnl_key = f"{sym}_max_pnl"
        max_pnl_historico = ts_data.get(max_pnl_key, 0)
        if pnl_pct > max_pnl_historico:
            # Actualizar max P&L
            ts_data[max_pnl_key] = round(pnl_pct, 4)
            os.makedirs(os.path.dirname(POSICIONES_TS_PATH), exist_ok=True)
            with open(POSICIONES_TS_PATH, "w") as f:
                json.dump(ts_data, f, indent=2)
            max_pnl_historico = pnl_pct

        if max_pnl_historico >= TRAILING_STOP_ACTIVACION and pnl_pct <= 0:
            regla = (f"R-TRAILING: fue +{max_pnl_historico*100:.1f}%, ahora {pnl_pct*100:+.1f}% "
                     f"— trailing stop a breakeven activado")
            decisiones.append({
                "simbolo": sym,
                "accion": "VENDER",
                "qty": qty,
                "razon": regla,
                "regla": "R-TRAILING",
                "precio_entrada": entry,
                "precio_actual": current,
                "pnl_pct": round(pnl_pct * 100, 2),
            })
            log_reglas.append(
                f"  [R-TRAILING] {sym}: VENDER — trailing stop (max +{max_pnl_historico*100:.1f}%, "
                f"ahora {pnl_pct*100:+.1f}%)")
            continue

        # HOLD-MIN: posición nueva (< 30 min) — solo SL/TP pueden vender
        minutos = _minutos_en_posicion(sym)
        if minutos is not None and minutos < HOLD_MINIMO_MINUTOS:
            decisiones.append({
                "simbolo": sym,
                "accion": "MANTENER",
                "razon": (f"HOLD forzado (posición de {minutos:.0f} min, "
                          f"mínimo {HOLD_MINIMO_MINUTOS} min) P&L {pnl_pct*100:+.1f}%"),
                "regla": "HOLD-MIN",
                "pnl_pct": round(pnl_pct * 100, 2),
            })
            log_reglas.append(
                f"  [HOLD-MIN] {sym}: HOLD forzado — posición de {minutos:.0f} min "
                f"(mínimo {HOLD_MINIMO_MINUTOS})")
            continue

        # R-OPT-SELL: Opciones mayoría PUTS → venta anticipada
        si = senales_inst.get(sym, {})
        opc = si.get("opciones", {})
        opc_senal = opc.get("senal", "")
        if opc_senal == "BAJISTA":
            n_inusuales = len(opc.get("inusuales", []))
            pc_ratio = opc.get("put_call_ratio", 0)
            regla = (f"R-OPT-SELL: opciones bajistas (P/C={pc_ratio}, "
                     f"{n_inusuales} flujos inusuales mayoría PUTS) — {opc.get('nota', '')}")
            decisiones.append({
                "simbolo": sym,
                "accion": "VENDER",
                "qty": qty,
                "razon": regla,
                "regla": "R-OPT-SELL",
                "precio_entrada": entry,
                "precio_actual": current,
                "pnl_pct": round(pnl_pct * 100, 2),
            })
            log_reglas.append(
                f"  [R-OPT-SELL] {sym}: VENDER — opciones bajistas "
                f"P/C={pc_ratio}, {n_inusuales} flujos PUTS, P&L {pnl_pct*100:+.1f}%"
            )
            continue

        # R-ROTACION: posición flat > ROTACION_DIAS_FLAT días — candidata para rotación
        minutos_pos = _minutos_en_posicion(sym)
        dias_en_pos = (minutos_pos / 1440) if minutos_pos is not None else 999
        if dias_en_pos >= ROTACION_DIAS_FLAT and abs(pnl_pct) < ROTACION_PCT_FLAT:
            rotacion_candidatas.append({
                "simbolo": sym, "dias": dias_en_pos, "pnl_pct": pnl_pct,
                "qty": qty, "entry": entry, "current": current,
            })
            # No hacer continue — también se marca como HOLD por ahora

        # Posición OK → mantener
        decisiones.append({
            "simbolo": sym,
            "accion": "MANTENER",
            "razon": f"posición abierta P&L {pnl_pct*100:+.1f}%, sin regla activa",
            "regla": "HOLD",
            "pnl_pct": round(pnl_pct * 100, 2),
        })

    # ── Fase 2: Evaluar señales de compra por indicadores ──
    # Contar slots libres (ventas de fase 1 liberan slots)
    reglas_venta_libera = ("R-SL", "R-OPT-SELL", "R-TP-TOTAL", "R-TRAILING")
    ventas_fase1 = sum(1 for d in decisiones if d["accion"] == "VENDER"
                       and d.get("regla") in reglas_venta_libera)
    max_pos_efectivo = min(condiciones.get("max_posiciones_regimen", MAX_POSICIONES), MAX_POSICIONES)
    slots_disponibles = max_pos_efectivo - n_posiciones + ventas_fase1

    modo_panico = condiciones.get("modo_panico", False)

    # En modo pánico, permitir hasta MAX_REFUGIOS_PANICO slots extra para refugios
    refugios_actuales = sum(1 for s in posiciones_map if s in ACTIVOS_REFUGIO)

    # MEJORA 1/4/5: cargar estado de SL una sola vez y derivar métricas.
    _sl_state = _cargar_sl_state()
    _hoy = datetime.now().date()
    _limite_30d = datetime.now() - timedelta(days=SL_HISTORIAL_DIAS)

    def _sl_hoy_prev_count():
        total = 0
        for _ts_list in _sl_state["historial"].values():
            for _ts in _ts_list:
                _p = _parse_ts_safe(_ts)
                if _p and _p.date() == _hoy:
                    total += 1
        return total

    def _cooldown_para(sym):
        ts_str = _sl_state["cooldowns"].get(sym)
        if not ts_str:
            return False, None
        p = _parse_ts_safe(ts_str)
        if p is None:
            return False, None
        fin = p + timedelta(days=COOLDOWN_SL_DIAS)
        if datetime.now() < fin:
            return True, fin.strftime("%Y-%m-%d")
        return False, None

    def _sl_30d_para(sym):
        n = 0
        for _ts in _sl_state["historial"].get(sym, []):
            _p = _parse_ts_safe(_ts)
            if _p and _p >= _limite_30d:
                n += 1
        return n

    # MEJORA 4: contar SL ejecutados hoy (prior + planificados en este ciclo).
    sl_hoy_prev = _sl_hoy_prev_count()
    sl_hoy_ciclo = sum(1 for d in decisiones if d.get("regla") == "R-SL")
    sl_total_hoy = sl_hoy_prev + sl_hoy_ciclo
    pause_compras = sl_total_hoy >= MAX_SL_POR_DIA
    if pause_compras:
        log_reglas.append(
            f"  [R-PAUSE] {sl_total_hoy} stop-losses hoy "
            f"(>= {MAX_SL_POR_DIA}), pausando compras"
        )

    compras_candidatas = []
    for sym in ACTIVOS_OPERABLES:
        if sym not in SIMBOLOS_OPERABLES:
            continue  # No operable en régimen actual
        if sym in posiciones_map:
            continue  # Ya evaluada en fase 1
        if sym in vendidos_hoy:
            decisiones.append({
                "simbolo": sym, "accion": "MANTENER",
                "razon": f"R-NODAYTR: vendido hoy — no recomprar mismo día (Cash account)",
                "regla": "R-NODAYTR",
            })
            log_reglas.append(f"  [R-NODAYTR] {sym}: vendido hoy — skip compra (anti-day-trading)")
            continue

        # MEJORA 1: cooldown post stop-loss (3 días calendario)
        en_cd, cd_fin = _cooldown_para(sym)
        if en_cd:
            decisiones.append({
                "simbolo": sym, "accion": "MANTENER",
                "razon": f"R-COOLDOWN: {sym} en cooldown post-SL hasta {cd_fin}",
                "regla": "R-COOLDOWN",
            })
            log_reglas.append(
                f"  [R-COOLDOWN] {sym}: cooldown post-SL activo hasta {cd_fin}"
            )
            continue

        # MEJORA 4: si hoy hubo >= 3 SL, pausar compras
        if pause_compras:
            decisiones.append({
                "simbolo": sym, "accion": "MANTENER",
                "razon": f"R-PAUSE: {sl_total_hoy} stop-losses hoy, pausando compras",
                "regla": "R-PAUSE",
            })
            continue

        senal = senales.get(sym, {})
        score = senal.get("puntuacion", 0)
        precio = precios_map.get(sym, 0)

        # R-QUANT: Señales quant ajustan el score
        qt = senales_qt.get(sym, {})
        qt_comb = qt.get("combinada", "NEUTRAL")
        qt_rsi = qt.get("rsi_semanal", {}).get("rsi")
        quant_bonus = 0
        quant_notas = []

        if qt_comb == "COMPRA_FUERTE":
            quant_bonus = 2
            quant_notas.append(f"R-QUANT +2 (compra fuerte: {qt.get('alcistas', 0)}/3 estrategias)")
        elif qt_comb == "COMPRA_LEVE":
            quant_bonus = 1
            quant_notas.append(f"R-QUANT +1 (compra leve)")

        if qt_rsi is not None and qt_rsi > 80:
            quant_bonus -= 1
            quant_notas.append(f"R-QUANT -1 (RSI semanal {qt_rsi:.0f} sobrecompra)")

        # R-EARNINGS: Tono de ejecutivos ajusta score
        earnings_bonus = 0
        earnings_notas = []
        te = tono_exec.get(sym, {})
        te_score = te.get("score", 0)
        te_prev = te.get("score_anterior")

        if te_score and not te.get("error"):
            if te_score > 70:
                earnings_bonus = 1
                earnings_notas.append(f"R-EARNINGS +1 (ejecutivos confiados: {te_score:+d})")
            elif te_score < 20:
                earnings_bonus = -1
                earnings_notas.append(f"R-EARNINGS -1 (ejecutivos preocupados: {te_score:+d})")

            # Deterioro vs trimestre anterior
            if te_prev and te_prev.get("score"):
                diff = te_score - te_prev["score"]
                if diff < -30:
                    earnings_bonus -= 2
                    earnings_notas.append(
                        f"R-EARNINGS -2 (deterioro {diff:+d} vs {te_prev['quarter']})")

        # R-FINBERT: Sentimiento de noticias FinBERT ajusta score
        finbert_bonus = 0
        finbert_notas = []
        if noticias_por_activo:
            arts = noticias_por_activo.get(sym, [])
            titulos = [a.get("titulo", "") for a in arts if "error" not in a and a.get("titulo")]
            if titulos:
                finbert_bonus = _get_finbert_score(sym, titulos)
                if finbert_bonus != 0:
                    finbert_notas.append(f"R-FINBERT {finbert_bonus:+d} ({len(titulos)} noticias)")

        # R-OPCIONES: Señal de opciones (Marketdata.app)
        opciones_bonus = _get_opciones_signal(sym)
        opciones_notas = []
        if opciones_bonus != 0:
            opciones_notas.append(f"R-OPCIONES {opciones_bonus:+d}")

        # R-FLOW: Flujo institucional (Unusual Whales)
        flow_bonus = _get_institutional_flow(sym)
        flow_notas = []
        if flow_bonus != 0:
            flow_notas.append(f"R-FLOW {flow_bonus:+d}")

        # R-QUIVER: Congressional + insider trading (Quiver Quantitative)
        quiver_bonus = _get_quiver_score(sym)
        quiver_notas = []
        if quiver_bonus != 0:
            quiver_notas.append(f"R-QUIVER {quiver_bonus:+d}")

        score_ajustado = score + quant_bonus + earnings_bonus + finbert_bonus + opciones_bonus + flow_bonus + quiver_bonus

        if earnings_notas:
            log_reglas.append(f"  [R-EARNINGS] {sym}: {', '.join(earnings_notas)}")
        if quant_notas:
            log_reglas.append(f"  [R-QUANT] {sym}: score {score:+d} → {score_ajustado:+d} ({', '.join(quant_notas)})")
        if finbert_notas:
            log_reglas.append(f"  [R-FINBERT] {sym}: {', '.join(finbert_notas)}")
        if opciones_notas:
            log_reglas.append(f"  [R-OPCIONES] {sym}: {', '.join(opciones_notas)}")
        if flow_notas:
            log_reglas.append(f"  [R-FLOW] {sym}: {', '.join(flow_notas)}")
        if quiver_notas:
            log_reglas.append(f"  [R-QUIVER] {sym}: {', '.join(quiver_notas)}")

        # Determinar umbral efectivo para este activo
        # En pánico: refugios (GLD, IEF, AGG) aceptan score >= 0
        es_refugio = sym in ACTIVOS_REFUGIO
        if modo_panico and es_refugio:
            umbral_sym = UMBRAL_REFUGIO_PANICO
        else:
            umbral_sym = umbral

        # MEJORA 5: activos con 2+ SL en 30 días endurecen su umbral.
        n_sl_30d = _sl_30d_para(sym)
        if n_sl_30d >= SL_HISTORIAL_UMBRAL:
            umbral_sym_anterior = umbral_sym
            umbral_sym = max(umbral_sym, UMBRAL_COMPRA_HISTORIAL)
            if umbral_sym != umbral_sym_anterior:
                log_reglas.append(
                    f"  [R-HISTORIAL] {sym}: {n_sl_30d} SL en 30 días, "
                    f"umbral subido a {umbral_sym}"
                )

        # R-OPT-BUY: CALLS inusuales refuerzan señal — acepta score >= umbral-1
        si = senales_inst.get(sym, {})
        opc = si.get("opciones", {})
        opc_senal = opc.get("senal", "")
        umbral_efectivo = umbral_sym

        if opc_senal == "ALCISTA" and score_ajustado >= umbral_sym - 1 and score_ajustado < umbral_sym:
            umbral_efectivo = umbral_sym - 1

        if score_ajustado >= umbral_efectivo and sym not in excluidos:
            razones_tecnicas = ", ".join(senal.get("razones", [])[:3])
            # Determine which rule was decisive
            if modo_panico and es_refugio and score_ajustado < umbral:
                regla_nombre = "R-REFUGIO"
            elif quant_bonus > 0 and score < umbral_efectivo:
                regla_nombre = "R-QUANT"
            elif umbral_efectivo < umbral_sym:
                regla_nombre = "R-OPT-BUY"
            else:
                regla_nombre = "R-BUY"

            razon_base = f"score {score:+d}"
            total_bonus = quant_bonus + earnings_bonus + finbert_bonus + opciones_bonus + flow_bonus + quiver_bonus
            if total_bonus != 0:
                razon_base += f" +adj({total_bonus:+d})={score_ajustado:+d}"
            razon_base += f" (>={umbral_efectivo}) — {razones_tecnicas}"
            notas_todas = quant_notas + earnings_notas
            if notas_todas:
                razon_base += f" | {notas_todas[0]}"

            _sd = (f"técnico:{score:+d} quant:{quant_bonus:+d} earn:{earnings_bonus:+d} "
                   f"finbert:{finbert_bonus:+d} opc:{opciones_bonus:+d} flow:{flow_bonus:+d} quiver:{quiver_bonus:+d}")
            compras_candidatas.append({
                "simbolo": sym,
                "accion": "COMPRAR",
                "razon": f"{regla_nombre}: {razon_base}",
                "regla": regla_nombre,
                "score": score_ajustado,
                "score_detalle": _sd,
                "precio": precio,
                "es_refugio": es_refugio,
                "razones_indicadores": senal.get("razones", []),
            })
        elif sym in excluidos:
            decisiones.append({
                "simbolo": sym,
                "accion": "MANTENER",
                "razon": f"excluido por noticias negativas (score: {score:+d})",
                "regla": "R-NEWS",
                "score": score,
            })
        else:
            total_bonus = quant_bonus + earnings_bonus + finbert_bonus + opciones_bonus + flow_bonus + quiver_bonus
            extra = f" [adj:{total_bonus:+d}→{score_ajustado:+d}]" if total_bonus != 0 else ""
            decisiones.append({
                "simbolo": sym,
                "accion": "MANTENER",
                "razon": f"score {score_ajustado:+d} < umbral {umbral_efectivo}{extra}",
                "regla": "HOLD",
                "score": score_ajustado,
            })

    # ── Priorizar compras ──
    # En pánico: refugios primero, luego por Sharpe
    if modo_panico:
        compras_candidatas.sort(
            key=lambda d: (0 if d.get("es_refugio") else 1, PRIORIDAD_SHARPE.get(d["simbolo"], 999)))
    else:
        compras_candidatas.sort(key=lambda d: PRIORIDAD_SHARPE.get(d["simbolo"], 999))

    # En pánico, permitir refugios extra (hasta MAX_REFUGIOS_PANICO)
    compras_aprobadas = []
    compras_descartadas = []
    slots_usados = 0
    for d in compras_candidatas:
        es_ref = d.get("es_refugio", False)
        if slots_usados < slots_disponibles:
            compras_aprobadas.append(d)
            slots_usados += 1
        elif modo_panico and es_ref and refugios_actuales < MAX_REFUGIOS_PANICO:
            # Slot extra para refugio en pánico
            compras_aprobadas.append(d)
            refugios_actuales += 1
            log_reglas.append(
                f"  [R-REFUGIO] {d['simbolo']}: slot extra pánico "
                f"({refugios_actuales}/{MAX_REFUGIOS_PANICO} refugios)")
        else:
            compras_descartadas.append(d)

    for d in compras_aprobadas:
        decisiones.append(d)
        log_reglas.append(
            f"  [{d['regla']}] {d['simbolo']}: COMPRAR — score {d['score']:+d}, "
            f"prio #{PRIORIDAD_SHARPE.get(d['simbolo'], '?')}"
        )

    for d in compras_descartadas:
        decisiones.append({
            "simbolo": d["simbolo"],
            "accion": "MANTENER",
            "razon": f"señal de compra (score {d['score']:+d}) pero sin slot ({max_pos_efectivo} máx)",
            "regla": "SLOT-FULL",
            "score": d["score"],
        })
        log_reglas.append(
            f"  [SLOT-FULL] {d['simbolo']}: descartada — sin slot disponible"
        )

    # ── Fase 3: Rotación inteligente ──
    # Si hay posiciones flat > ROTACION_DIAS_FLAT Y compras descartadas con score >= ROTACION_SCORE_MINIMO, rotar
    if rotacion_candidatas and compras_descartadas:
        compras_descartadas.sort(key=lambda d: -d["score"])
        for rot in rotacion_candidatas:
            reemplazo = next((c for c in compras_descartadas
                              if c["score"] >= ROTACION_SCORE_MINIMO), None)
            if reemplazo is None:
                break
            sym_viejo = rot["simbolo"]
            sym_nuevo = reemplazo["simbolo"]
            log_reglas.append(
                f"  [R-ROTACION] Rotación: vendiendo {sym_viejo} "
                f"({rot['dias']:.0f} días flat) → comprando {sym_nuevo} (score {reemplazo['score']:+d})")

            # Agregar venta de posición flat
            decisiones.append({
                "simbolo": sym_viejo,
                "accion": "VENDER",
                "qty": rot["qty"],
                "razon": (f"R-ROTACION: {rot['dias']:.0f} días flat (P&L {rot['pnl_pct']*100:+.1f}%) "
                          f"→ rotar a {sym_nuevo} (score {reemplazo['score']:+d})"),
                "regla": "R-ROTACION",
                "precio_entrada": rot["entry"],
                "precio_actual": rot["current"],
                "pnl_pct": round(rot["pnl_pct"] * 100, 2),
            })
            # Quitar el HOLD previo de esta posición
            decisiones[:] = [d for d in decisiones
                             if not (d["simbolo"] == sym_viejo and d.get("regla") == "HOLD")]
            # Agregar compra del reemplazo
            decisiones.append(reemplazo)
            compras_descartadas.remove(reemplazo)

    return decisiones, log_reglas, len(compras_aprobadas), len(compras_descartadas)


# ══════════════════════════════════════════════════════════════
#  4. LLM — Veto por noticias + Explicación
# ══════════════════════════════════════════════════════════════

def llamar_ollama(modelo, system_prompt, user_message, temperature=0.4):
    """Llama a Ollama API en el nodo correcto según el modelo.
    Timeouts: 60s para Nemotron (core), 30s para DeepSeek 70B (power).
    Fallback: si el nodo primario falla, intenta el otro con su modelo.
    """
    url_primario = _MODEL_URL.get(modelo, OLLAMA_URL)
    timeout = 60 if url_primario == OLLAMA_URL_CORE else 30

    # Definir fallback: core↔power
    if url_primario == OLLAMA_URL_CORE:
        fallback_url, fallback_model, fallback_timeout = OLLAMA_URL_POWER, MODEL_FAST, 30
    else:
        fallback_url, fallback_model, fallback_timeout = OLLAMA_URL_CORE, MODEL_DEEP, 60

    def _call(url, mdl, tout):
        payload = {
            "model": mdl,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        resp = requests.post(url, json=payload, timeout=tout)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    try:
        return _call(url_primario, modelo, timeout)
    except Exception as e:
        print(f"   LLM fallback: {modelo} falló ({e}), intentando {fallback_model}...")
        return _call(fallback_url, fallback_model, fallback_timeout)


def limpiar_think(texto):
    """Elimina bloques <think>...</think> de la respuesta de DeepSeek-R1."""
    return re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()


SYSTEM_VETO = """\
Eres JARVIS, validador de señales de trading. Responde SOLO en español.
Tu trabajo es detectar si hay noticias ESPECÍFICAS y NEGATIVAS del activo concreto \
que contradigan una señal técnica de compra.

REGLAS ESTRICTAS:
- Solo vetar si encuentras una noticia que MENCIONE EXPLÍCITAMENTE el símbolo/empresa \
Y contenga un evento grave: fraude, bancarrota, demanda, recall, investigación SEC, \
crash específico del activo, downgrade significativo.
- "Mercado en pánico general", caídas del mercado amplio, volatilidad alta, \
o Fear & Greed bajo NO son razones válidas para vetar. De hecho, F&G < 15 \
es señal de compra contrarian — el pánico general FAVORECE la compra.
- Si no hay noticias específicas del activo, responde NO.
- Si solo hay noticias genéricas de mercado (aranceles, tasas, macro), responde NO.

Responde en UNA sola línea con formato: SÍ|NO — explicación breve.
Ejemplos:
  NO — Sin noticias negativas específicas de XYZ, señal técnica válida.
  NO — Pánico general del mercado no afecta fundamentos de XYZ.
  SÍ — SEC investiga a XYZ por fraude contable, evitar hasta que se aclare.
  SÍ — XYZ anuncia recall masivo de productos, riesgo operativo alto.\
"""

SYSTEM_EXPLICACION = """\
Eres JARVIS, analista de trading. Responde SOLO en español, máximo 4 líneas.
LÍNEA 1: Evaluación general del mercado (alcista/bajista/mixto).
LÍNEA 2: Activos que destacan hoy.
LÍNEA 3: Resumen de las acciones automáticas ejecutadas.
LÍNEA 4: Sugerencia de ajuste al portafolio (solo sugerencia, no ejecutar).\
"""


def verificar_llm_veto(simbolo, score, noticias_texto, contexto_mercado=""):
    """
    Pregunta al LLM si alguna noticia ESPECÍFICA del activo contradice la compra.
    Solo veta por eventos graves del activo concreto, no por pánico general.
    Retorna (vetado: bool, explicacion: str).
    """
    prompt = (
        f"Indicadores técnicos muestran señal de COMPRA para {simbolo} con score {score}.\n"
        f"¿Hay alguna noticia ESPECÍFICA de {simbolo} que indique un evento grave "
        f"(fraude, bancarrota, demanda, recall, investigación regulatoria, crash del activo)?\n"
        f"IMPORTANTE: Caídas generales del mercado, aranceles, pánico macro, o volatilidad "
        f"alta NO son razones para vetar. Solo veta por noticias específicas de {simbolo}.\n\n"
        f"Noticias recientes de {simbolo}:\n{noticias_texto}\n"
    )

    try:
        resp = llamar_ollama(MODEL_FAST, SYSTEM_VETO, prompt, temperature=0.2)
        resp = limpiar_think(resp).strip()
        primera_linea = resp.split("\n")[0].strip().upper()

        # Parseo robusto: el LLM a veces responde "SÍ|NO" como formato,
        # lo cual no indica veto real. Solo es veto si:
        # 1) Empieza con SÍ (sin |NO pegado) y
        # 2) La explicación contiene palabras de riesgo específico
        PALABRAS_RIESGO = [
            "fraude", "fraud", "bancarrota", "bankruptcy", "demanda", "lawsuit",
            "recall", "investigación", "sec", "regulat", "crash", "quiebra",
            "scandal", "escándalo", "downgrade", "criminal", "delisted",
        ]
        empieza_si = primera_linea.startswith("SÍ") or primera_linea.startswith("SI ")
        tiene_no = "SÍ|NO" in primera_linea or "SI|NO" in primera_linea or "SÍ | NO" in primera_linea
        resp_lower = resp.lower()
        tiene_riesgo = any(p in resp_lower for p in PALABRAS_RIESGO)

        # Veta solo si dice SÍ claramente (no "SÍ|NO") Y menciona riesgo específico
        vetado = empieza_si and not tiene_no and tiene_riesgo
        return vetado, resp
    except Exception as e:
        return False, f"LLM no disponible ({e}), señal técnica se mantiene"


def obtener_explicacion_llm(decisiones, contexto_mercado, condiciones):
    """
    Pide al LLM que explique las decisiones tomadas y sugiera ajustes.
    NO toma decisiones — solo comenta.
    """
    resumen_decisiones = []
    for d in decisiones:
        if d["accion"] != "MANTENER":
            resumen_decisiones.append(
                f"  {d['accion']} {d['simbolo']} — regla: {d.get('regla', '?')} — {d['razon']}"
            )

    if not resumen_decisiones:
        acciones_texto = "No se ejecutaron compras ni ventas esta sesión."
    else:
        acciones_texto = "\n".join(resumen_decisiones)

    prompt = (
        f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"VIX: {condiciones['vix_precio']} | F&G: {condiciones['fng_valor']}/100\n"
        f"Modo agresivo: {'SÍ' if condiciones['modo_agresivo'] else 'NO'}\n\n"
        f"Acciones ejecutadas por reglas automáticas:\n{acciones_texto}\n\n"
        f"Contexto de mercado:\n{contexto_mercado[:2000]}\n\n"
        f"Explica brevemente y sugiere ajustes al portafolio."
    )

    try:
        resp = llamar_ollama(MODEL_DEEP, SYSTEM_EXPLICACION, prompt)
        return limpiar_think(resp)
    except Exception as e:
        return f"LLM no disponible para explicación: {e}"


# ══════════════════════════════════════════════════════════════
#  5. CONTEXTO Y EJECUCIÓN
# ══════════════════════════════════════════════════════════════

def construir_contexto(datos_acciones, datos_criptos, errores, contexto_mercado_texto=""):
    """Construye el contexto completo: precios + cuenta + contexto macro."""
    resumen = construir_resumen(datos_acciones, datos_criptos, errores)

    balance = get_balance()
    posiciones = get_positions()

    contexto = f"{resumen}\n\n"
    if contexto_mercado_texto:
        contexto += f"{contexto_mercado_texto}\n\n"

    contexto += "--- CUENTA IBKR LIVE ---\n"
    contexto += f"  Equity: ${float(balance['equity']):,.2f}\n"
    contexto += f"  Cash disponible: ${float(balance['cash']):,.2f}\n"
    contexto += f"  Poder de compra: ${float(balance['buying_power']):,.2f}\n\n"

    if posiciones:
        contexto += "  Posiciones abiertas:\n"
        for p in posiciones:
            pnl = float(p["unrealized_pl"])
            signo = "+" if pnl >= 0 else ""
            contexto += (
                f"    {p['symbol']}: {p['qty']} acciones "
                f"@ ${float(p['avg_entry_price']):,.2f} | "
                f"Actual: ${float(p['current_price']):,.2f} | "
                f"P&L: {signo}${pnl:,.2f}\n"
            )
    else:
        contexto += "  Posiciones abiertas: ninguna\n"

    return contexto, balance, posiciones


def ejecutar_ordenes(decisiones, datos_acciones, posiciones, max_por_trade=None,
                     balance=None):
    """Ejecuta órdenes basadas en las decisiones de las reglas. Retorna log de ejecución."""
    if max_por_trade is None:
        max_por_trade = MAX_POR_TRADE
    posiciones_map = {p["symbol"]: p for p in posiciones}
    precios_map = {d["simbolo"]: d["precio"] for d in datos_acciones}
    # Usar cash liquidado (T+1) en vez de cash total para evitar órdenes rechazadas
    cash_disponible = float(balance.get("settled_cash", balance.get("cash", 0))) if balance else 0
    n_posiciones = len(posiciones_map)
    resultados = []

    for d in decisiones:
        simbolo = d["simbolo"].upper()
        accion = d["accion"].upper()
        razon = d.get("razon", "")
        regla = d.get("regla", "")
        # Metadata de la decisión para logging
        _meta = {k: d[k] for k in ("pnl_pct", "precio_entrada", "precio_actual", "score")
                 if k in d}

        if simbolo not in SIMBOLOS_OPERABLES:
            continue

        if accion == "MANTENER":
            resultados.append({
                "simbolo": simbolo,
                "accion": "MANTENER",
                "razon": razon,
                "regla": regla,
                "ejecutada": False,
                **_meta,
            })
            continue

        precio = precios_map.get(simbolo)
        if not precio or precio <= 0:
            resultados.append({
                "simbolo": simbolo,
                "accion": accion,
                "razon": razon,
                "regla": regla,
                "ejecutada": False,
                "error": "Precio no disponible",
            })
            continue

        if accion == "COMPRAR":
            # FIX 3: Verificaciones pre-compra
            if simbolo in posiciones_map:
                resultados.append({
                    "simbolo": simbolo, "accion": "COMPRAR", "razon": razon,
                    "regla": "SKIP", "ejecutada": False,
                    "error": f"ya tengo posición en {simbolo}",
                })
                continue
            if n_posiciones >= MAX_POSICIONES_JARVIS:
                resultados.append({
                    "simbolo": simbolo, "accion": "COMPRAR", "razon": razon,
                    "regla": "SKIP", "ejecutada": False,
                    "error": f"máximo de posiciones alcanzado ({n_posiciones}/{MAX_POSICIONES_JARVIS})",
                })
                continue
            if cash_disponible < max_por_trade:
                resultados.append({
                    "simbolo": simbolo, "accion": "COMPRAR", "razon": razon,
                    "regla": "SKIP", "ejecutada": False,
                    "error": f"cash insuficiente (${cash_disponible:,.0f} < ${max_por_trade:,.0f})",
                })
                continue

            qty = math.floor(max_por_trade / precio)  # Acciones enteras (IBKR)
            if qty < 1:
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "COMPRAR",
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": False,
                    "error": f"Precio ${precio:,.2f} excede límite de ${max_por_trade}",
                })
                continue

            try:
                orden = buy(simbolo, qty=qty)
                if orden is None:
                    resultados.append({
                        "simbolo": simbolo, "accion": "COMPRAR", "qty": qty,
                        "razon": razon, "regla": regla, "ejecutada": False,
                        "error": "Orden omitida (qty < 1 tras precio RT)",
                    })
                    continue
                n_posiciones += 1
                cash_disponible -= qty * precio
                _guardar_timestamp_posicion(simbolo)
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "COMPRAR",
                    "qty": qty,
                    "monto_aprox": round(qty * precio, 2),
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": True,
                    "order_id": orden["id"],
                    "status": orden["status"],
                    **_meta,
                })
            except Exception as e:
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "COMPRAR",
                    "qty": qty,
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": False,
                    "error": str(e),
                })

        elif accion == "VENDER":
            pos = posiciones_map.get(simbolo)
            if not pos:
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "VENDER",
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": False,
                    "error": "No hay posición abierta para vender",
                })
                continue

            # Usar qty de la decisión (para ventas parciales R-TP15) o todo
            qty_decision = d.get("qty")
            qty_pos = int(float(pos["qty"]))  # Acciones enteras (IBKR)
            if qty_decision and int(qty_decision) < qty_pos:
                qty = int(qty_decision)
            else:
                qty = qty_pos

            try:
                orden = sell(simbolo, qty=qty)
                if orden is None:
                    resultados.append({
                        "simbolo": simbolo, "accion": "VENDER", "qty": qty,
                        "razon": razon, "regla": regla, "ejecutada": False,
                        "error": "Orden omitida (qty < 1)",
                    })
                    continue
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "VENDER",
                    "qty": qty,
                    "monto_aprox": round(qty * precio, 2),
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": True,
                    "order_id": orden["id"],
                    "status": orden["status"],
                    **_meta,
                })
                # Actualizar contadores para que compras posteriores vean el slot/cash libre
                n_posiciones -= 1
                cash_disponible += qty * precio
                # Limpiar timestamp + max_pnl en venta completa
                if qty >= qty_pos:
                    _eliminar_timestamp_posicion(simbolo)
                    # Limpiar max_pnl tracking del trailing stop
                    ts_data = _cargar_timestamps_posiciones()
                    if f"{simbolo}_max_pnl" in ts_data:
                        del ts_data[f"{simbolo}_max_pnl"]
                        with open(POSICIONES_TS_PATH, "w") as f:
                            json.dump(ts_data, f, indent=2)
            except Exception as e:
                resultados.append({
                    "simbolo": simbolo,
                    "accion": "VENDER",
                    "qty": qty,
                    "razon": razon,
                    "regla": regla,
                    "ejecutada": False,
                    "error": str(e),
                })

    return resultados


# ══════════════════════════════════════════════════════════════
#  6. MENSAJES Y LOG
# ══════════════════════════════════════════════════════════════

def _esc(text):
    """Escapa texto para HTML de Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def construir_mensaje_telegram(explicacion_llm, resultados, balance_final, condiciones, log_reglas):
    """Construye el mensaje para Telegram con reglas + explicación LLM + balance."""
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

    msg = f"\U0001f916 <b>JARVIS — LIVE Trading (IBKR)</b>\n"
    msg += f"\U0001f4c5 {fecha}\n"
    msg += f"Capital: ${JARVIS_LIVE_CAPITAL:,.0f} | Max/trade: ${MAX_POR_TRADE:,.0f} | SL: {STOP_LOSS_REGLA_PCT*100:.0f}%\n"
    msg += f"Activos: {', '.join(ACTIVOS_OPERABLES)}\n"
    msg += f"VIX: {condiciones['vix_precio']} | F&amp;G: {condiciones['fng_valor']}/100"
    if condiciones["modo_agresivo"]:
        msg += " | AGRESIVO"
    msg += "\n\n"

    # Reglas activas
    msg += "<b>Reglas sesión:</b>\n"
    for r in condiciones["reglas"]:
        msg += f"  {_esc(r)}\n"
    msg += "\n"

    # Órdenes
    ordenes_ejecutadas = [r for r in resultados if r.get("ejecutada")]
    mantener = [r for r in resultados if r["accion"] == "MANTENER"]
    pendientes = [r for r in resultados if not r.get("ejecutada") and r["accion"] != "MANTENER" and not r.get("error")]
    errores = [r for r in resultados if not r.get("ejecutada") and r["accion"] != "MANTENER" and r.get("error")]

    if ordenes_ejecutadas:
        msg += "\U0001f4b9 <b>Órdenes ejecutadas:</b>\n"
        for r in ordenes_ejecutadas:
            icono = "\u2705" if r["accion"] == "COMPRAR" else "\U0001f534"
            msg += (
                f"  {icono} {r['accion']} {r['qty']} {r['simbolo']} "
                f"(~${r['monto_aprox']:,.2f})\n"
                f"     {r['regla']}: {_esc(r['razon'][:80])}\n"
            )
        msg += "\n"
    else:
        msg += "\U0001f4ad <b>Sin órdenes ejecutadas</b> — ninguna regla activa.\n\n"

    if pendientes:
        msg += "\U0001f4cb <b>Señales activas (dry-run):</b>\n"
        for r in pendientes:
            icono = "+" if r["accion"] == "COMPRAR" else "-"
            msg += f"  [{icono}] {r['simbolo']} [{r.get('regla','')}]: {_esc(r.get('razon','')[:80])}\n"
        msg += "\n"

    if mantener:
        syms = ", ".join(r["simbolo"] for r in mantener)
        msg += f"\u23f8 <b>Mantener:</b> {syms}\n\n"

    if errores:
        msg += "\u26a0\ufe0f <b>No ejecutadas:</b>\n"
        for r in errores:
            msg += f"  {r['simbolo']} [{r.get('regla','')}]: {_esc(r.get('error', '?'))}\n"
        msg += "\n"

    # Explicación LLM
    if explicacion_llm:
        llm_safe = _esc(explicacion_llm).replace("**", "")
        msg += f"<b>Análisis JARVIS (LLM):</b>\n{llm_safe}\n\n"

    # Balance
    msg += "\U0001f4b0 <b>Balance:</b>\n"
    msg += f"  Equity: ${float(balance_final['equity']):,.2f}\n"
    msg += f"  Cash: ${float(balance_final['cash']):,.2f}\n"
    msg += f"\n\U0001f6e1 Límite/trade: ${condiciones['max_por_trade']:,.0f}"

    return msg


def guardar_log(explicacion_llm, decisiones, resultados, balance, condiciones,
                log_reglas, log_vetos, senales_tecnicas):
    """Guarda log detallado de la sesión: reglas, vetos, indicadores, resultados."""
    dir_logs = os.path.join(PROYECTO, "logs")
    os.makedirs(dir_logs, exist_ok=True)

    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_logs, f"jarvis_trading_{fecha}.txt")

    with open(ruta, "a", encoding="utf-8") as f:
        f.write(f"{'='*70}\n")
        f.write(f"=== {datetime.now().strftime('%H:%M:%S')} — Trading Agent v2 ===\n")
        f.write(f"{'='*70}\n\n")

        # Condiciones
        f.write(f"CONDICIONES:\n")
        f.write(f"  VIX: {condiciones['vix_precio']} | F&G: {condiciones['fng_valor']}/100\n")
        f.write(f"  Modo agresivo: {'SÍ' if condiciones['modo_agresivo'] else 'NO'}\n")
        f.write(f"  Umbral compra: {condiciones['umbral_compra']}\n")
        f.write(f"  Máx por trade: ${condiciones['max_por_trade']:,.0f}\n")
        f.write(f"  Excluidos: {', '.join(condiciones['activos_excluidos']) or 'ninguno'}\n\n")

        # Reglas sesión
        f.write(f"REGLAS SESIÓN:\n")
        for r in condiciones["reglas"]:
            f.write(f"  {r}\n")
        f.write("\n")

        # Indicadores técnicos
        f.write(f"INDICADORES TÉCNICOS:\n")
        for sym in ACTIVOS_OPERABLES:
            s = senales_tecnicas.get(sym, {})
            score = s.get("puntuacion", 0)
            senal = s.get("senal", "?")
            razones = s.get("razones", [])
            f.write(f"  {sym:<5}: score {score:+d} → {senal}\n")
            for r in razones[:3]:
                f.write(f"         {r}\n")
        f.write("\n")

        # Log de reglas aplicadas
        f.write(f"REGLAS APLICADAS:\n")
        if log_reglas:
            for lr in log_reglas:
                f.write(f"{lr}\n")
        else:
            f.write("  Ninguna regla activada.\n")
        f.write("\n")

        # Vetos LLM
        if log_vetos:
            f.write(f"VETOS LLM:\n")
            for v in log_vetos:
                f.write(f"  {v}\n")
            f.write("\n")

        # Decisiones finales
        f.write(f"DECISIONES FINALES:\n")
        for d in decisiones:
            f.write(
                f"  [{d.get('regla','?'):<10}] {d['simbolo']:<5}: "
                f"{d['accion']:<9} — {d['razon']}\n"
            )
        f.write("\n")

        # Resultados de ejecución
        f.write(f"RESULTADOS EJECUCIÓN:\n")
        f.write(json.dumps(resultados, indent=2, ensure_ascii=False))
        f.write("\n\n")

        # Explicación LLM
        if explicacion_llm:
            f.write(f"EXPLICACIÓN LLM:\n{explicacion_llm}\n\n")

        # Balance
        f.write(f"BALANCE: {json.dumps(balance, indent=2)}\n\n")

    return ruta


# ══════════════════════════════════════════════════════════════
#  7. MAIN
# ══════════════════════════════════════════════════════════════

def main():
    global ACTIVOS_OPERABLES, SIMBOLOS_OPERABLES, PRIORIDAD_SHARPE

    solo_analisis = "--dry-run" in sys.argv

    # ── Detectar régimen PRIMERO y fijar universo de activos ──
    try:
        _reg_actual = get_regimen_actual().get("regimen", "LATERAL")
    except Exception:
        _reg_actual = "LATERAL"

    if _reg_actual == "BEAR":
        activos_ciclo = list(ACTIVOS_DEFENSIVOS)
        PRIORIDAD_SHARPE = {s: i for i, s in enumerate(ACTIVOS_DEFENSIVOS)}
    else:
        activos_ciclo = list(_UNIVERSO_COMPLETO)
        PRIORIDAD_SHARPE = _PRIORIDAD_COMPLETA

    # Fijar globals para que TODAS las funciones usen el universo correcto
    ACTIVOS_OPERABLES = activos_ciclo
    SIMBOLOS_OPERABLES = set(activos_ciclo)

    # ── LIVE GUARD: fecha límite ──
    hoy = datetime.now().date()
    fecha_limite = datetime.strptime(JARVIS_LIVE_HASTA, "%Y-%m-%d").date()
    expirado = hoy > fecha_limite

    print("=" * 70)
    print(f"  JARVIS LIVE TRADING — IBKR")
    print(f"  Capital JARVIS: ${JARVIS_LIVE_CAPITAL:,.0f} | Max/trade: ${MAX_POR_TRADE:,.0f}")
    print(f"  Activos ({len(activos_ciclo)}): {', '.join(activos_ciclo)}")
    print(f"  Protegidas (no tocar): {', '.join(POSICIONES_PROTEGIDAS)}")
    print(f"  Stop-loss: {STOP_LOSS_REGLA_PCT*100:.0f}% | Fecha límite: {JARVIS_LIVE_HASTA}")
    if expirado:
        print(f"  >>> EXPIRADO: hoy {hoy} > límite {fecha_limite} — solo análisis")
        solo_analisis = True
    print("=" * 70)

    # Health check WhatsApp server
    wa_ok = False
    try:
        r = requests.get("http://localhost:8000/health", timeout=3)
        wa_ok = r.status_code == 200
    except Exception:
        pass
    if wa_ok:
        print(f"  WhatsApp server: OK")
    else:
        print(f"  WARNING: WhatsApp server no disponible")
        _log_wa_warning("WhatsApp server no disponible al inicio del ciclo")

    print()

    # 1) Contexto de mercado (noticias, VIX, F&G, earnings)
    print("1) Obteniendo contexto de mercado...")
    contexto_mkt_texto, contexto_mkt_datos = get_contexto_completo()
    condiciones = evaluar_condiciones_mercado(contexto_mkt_datos)

    # Forzar límites LIVE: nunca exceder $500/trade ni 4 posiciones
    condiciones["max_por_trade"] = min(condiciones["max_por_trade"], MAX_POR_TRADE)
    condiciones["max_posiciones_regimen"] = min(
        condiciones.get("max_posiciones_regimen", MAX_POSICIONES), MAX_POSICIONES
    )
    # Excluir posiciones protegidas del universo operable
    condiciones["activos_excluidos"] |= POSICIONES_PROTEGIDAS

    reg_icono = {"BULL": ">>", "BEAR": "<<", "LATERAL": "=="}.get(condiciones.get("regimen", "?"), "??")
    print(f"   F&G: {condiciones['fng_valor']}/100 | VIX: {condiciones['vix_precio']} | "
          f"Régimen: {reg_icono} {condiciones.get('regimen', '?')} "
          f"(conf {condiciones.get('regimen_confianza', 0)}/3)")
    print(f"   Umbral compra: score >= {condiciones['umbral_compra']} | "
          f"Max posiciones: {condiciones.get('max_posiciones_regimen', MAX_POSICIONES)}")
    print(f"   Máx por trade: ${condiciones['max_por_trade']:,.0f}")
    if condiciones["modo_agresivo"]:
        print(f"   >>> MODO AGRESIVO (F&G < {FNG_AGRESIVO})")
    if condiciones["activos_excluidos"]:
        print(f"   >>> Excluidos: {', '.join(condiciones['activos_excluidos'])}")
    for r in condiciones["reglas"]:
        print(f"   {r}")
    print()

    # 2) Precios del mercado
    print("2) Obteniendo precios...")
    datos_acciones, datos_criptos, errores = obtener_datos_mercado()
    if not datos_acciones:
        print("Error: no se obtuvieron datos de acciones.")
        sys.exit(1)
    print(f"   {len(datos_acciones)} acciones, {len(datos_criptos)} criptos\n")

    # 3) Indicadores técnicos
    print("3) Calculando indicadores técnicos...")
    senales = calcular_senales_tecnicas()
    compra_count = sum(1 for s in senales.values() if s.get("senal") == "COMPRAR")
    venta_count = sum(1 for s in senales.values() if s.get("senal") == "VENDER")
    print(f"   Señales: {compra_count} COMPRAR, {venta_count} VENDER, "
          f"{len(senales) - compra_count - venta_count} MANTENER")
    for sym in activos_ciclo:
        s = senales.get(sym, {})
        print(f"   {sym:<5}: score {s.get('puntuacion', 0):+d} → {s.get('senal', '?')}")
    print()

    # 4) Cuenta IBKR
    print("4) Consultando cuenta IBKR...")
    ibkr_ok = True
    try:
        contexto, balance, posiciones_todas = construir_contexto(
            datos_acciones, datos_criptos, errores, contexto_mkt_texto
        )
    except Exception as e:
        ibkr_ok = False
        print(f"   ERROR conexión IBKR: {e}")
        _notificar(
            "\u26a0\ufe0f <b>JARVIS — No pude conectar a IBKR</b>\n"
            f"Error: {_esc(e)}\n"
            "Verifica que TWS esté abierto y con API habilitada.\n"
            "Continúo solo con análisis (sin ejecutar órdenes)."
        )
        solo_analisis = True
        balance = {"equity": "0", "cash": "0", "buying_power": "0",
                   "portfolio_value": "0", "currency": "USD", "status": "DISCONNECTED"}
        posiciones_todas = []
        resumen = construir_resumen(datos_acciones, datos_criptos, errores)
        contexto = f"{resumen}\n\n--- CUENTA IBKR (DESCONECTADA) ---\n"

    # Filtrar: JARVIS solo gestiona posiciones que NO son AMD/NVDA
    posiciones = get_posiciones_jarvis(posiciones_todas)
    otras = [p for p in posiciones_todas if p["symbol"] in JARVIS_EXCLUIR]
    print(f"   Balance: ${float(balance['equity']):,.2f} | Posiciones totales: {len(posiciones_todas)}")
    if otras:
        print(f"   No-JARVIS (no tocar): {', '.join(p['symbol'] for p in otras)}")

    # Refrescar precios via Tiingo (más confiable que IBKR RT para estas posiciones)
    if posiciones:
        print(f"   Refrescando precios Tiingo para {len(posiciones)} posiciones JARVIS...")
        posiciones = refrescar_precios_tiingo(posiciones)

    n_jarvis = len(posiciones)
    cash_total = float(balance["cash"])
    slots_libres = MAX_POSICIONES_JARVIS - n_jarvis
    resumen_pos = ", ".join(f"{p['symbol']}({p['qty']})" for p in posiciones) if posiciones else "ninguna"

    # Log de inicio de ciclo
    print(f"   Posiciones JARVIS: {resumen_pos} — {n_jarvis}/{MAX_POSICIONES_JARVIS} slots"
          f" | Cash: ${cash_total:,.0f} | Slots libres: {slots_libres}")

    for p in posiciones:
        entry = float(p["avg_entry_price"])
        cur = float(p["current_price"])
        pnl = float(p["unrealized_pl"])
        pnl_pct = float(p["unrealized_plpc"]) * 100
        print(f"     {p['symbol']}: {p['qty']} acc @ ${entry:,.2f} "
              f"→ ${cur:,.2f} (P&L: ${pnl:+,.2f} / {pnl_pct:+.1f}%)")

    if slots_libres <= 0:
        print(f"   >>> Sin slots libres ({n_jarvis}/{MAX_POSICIONES_JARVIS})"
              f" — solo monitorear stop-loss y take-profit")

    # Verificar capital disponible para JARVIS
    capital_jarvis_usado = sum(float(p["market_value"]) for p in posiciones)
    capital_jarvis_disponible = min(cash_total, JARVIS_LIVE_CAPITAL - capital_jarvis_usado)
    print(f"   Cash disponible JARVIS: ${max(0, capital_jarvis_disponible):,.0f} "
          f"(capital ${JARVIS_LIVE_CAPITAL:,.0f} - usado ${capital_jarvis_usado:,.0f})\n")

    if capital_jarvis_disponible <= 0 and ibkr_ok:
        print("   >>> Capital JARVIS agotado — compras bloqueadas, ventas (SL/TP) activas")
        # No activar solo_analisis: ventas protectivas (R-SL, R-TRAILING, R-TP) deben ejecutarse.
        # Clampar cash a 0 para que ejecutar_ordenes rechace compras por cash insuficiente.
        balance = {**balance, "settled_cash": "0", "cash": "0"}

    # 4b) Señales institucionales (Finnhub Premium)
    print("4b) Obteniendo señales institucionales...")
    senales_inst = {}
    activos_a_evaluar = set(p["symbol"] for p in posiciones) | set(activos_ciclo)
    for sym in activos_a_evaluar:
        try:
            senales_inst[sym] = get_senales_institucionales(sym)
            si = senales_inst[sym]
            opc = si.get("opciones", {})
            opc_senal = opc.get("senal", "N/D")
            n_inu = len(opc.get("inusuales", []))
            print(f"   {sym:<5}: {si.get('senal_general', 'N/D'):<15} "
                  f"(opciones: {opc_senal}, {n_inu} flujos inusuales)")
        except Exception as e:
            print(f"   {sym}: error — {e}")
    print()

    # 4c) Señales quant (Momentum 12-1, RSI semanal, Golden/Death Cross)
    print("4c) Calculando señales quant...")
    senales_qt = {}
    try:
        senales_qt = get_senales_quant(activos_ciclo)
        for sym in activos_ciclo:
            sq = senales_qt.get(sym, {})
            comb = sq.get("combinada", "N/D")
            if comb != "NEUTRAL":
                mom = sq.get("momentum", {}).get("retorno_pct")
                rsi = sq.get("rsi_semanal", {}).get("rsi")
                print(f"   {sym:<5}: {comb:<14} Mom:{mom:+.0f}% RSI:{rsi:.0f}" if mom and rsi else f"   {sym:<5}: {comb}")
        fuertes = [s for s, d in senales_qt.items() if d.get("combinada") == "COMPRA_FUERTE"]
        if fuertes:
            print(f"   >>> COMPRA FUERTE quant: {', '.join(fuertes)}")
    except Exception as e:
        print(f"   Error señales quant: {e}")
    print()

    # 4d) Tono ejecutivos (earnings calls NLP)
    print("4d) Analizando tono ejecutivos (earnings)...")
    tono_exec = {}
    try:
        tono_exec = get_tono_ejecutivos(activos_ciclo[:5])
        for sym, te in tono_exec.items():
            if te.get("error"):
                continue
            sc = te.get("score", 0)
            senal = te.get("senal", "N/D")
            print(f"   {sym:<5}: score {sc:+d} → {senal} ({te.get('quarter', '?')})")
    except Exception as e:
        print(f"   Error earnings NLP: {e}")
    print()

    # 5) Aplicar reglas automáticas
    print("5) Aplicando reglas automáticas...")
    noticias_mkt = contexto_mkt_datos.get("noticias", {})
    decisiones, log_reglas, n_compras, n_descartadas = aplicar_reglas_automaticas(
        senales, posiciones, condiciones, datos_acciones, senales_inst, senales_qt, tono_exec,
        noticias_por_activo=noticias_mkt
    )

    if log_reglas:
        for lr in log_reglas:
            print(f"  {lr}")
    else:
        print("   Ninguna regla activada.")

    compras = [d for d in decisiones if d["accion"] == "COMPRAR"]
    ventas = [d for d in decisiones if d["accion"] == "VENDER"]
    print(f"   Total: {len(compras)} compras, {len(ventas)} ventas, "
          f"{len(decisiones) - len(compras) - len(ventas)} mantener")
    if n_descartadas > 0:
        print(f"   {n_descartadas} compra(s) descartada(s) por límite de slots")
    print()

    # 6) Veto LLM — solo para compras borderline (score cerca del umbral).
    #    - score > umbral + 1  → compra directa (señal fuerte, sin consulta)
    #    - score <= umbral + 1 → consultar Nemotron para vetar eventos graves
    #    - VENDER / MANTENER   → nunca vetar (protectivas o sin acción)
    #    - Si Nemotron falla, la compra procede (filtro adicional, no gate).
    log_vetos = []
    umbral = condiciones.get("umbral_compra", UMBRAL_COMPRA_NORMAL)
    print(f"6) Veto LLM (solo borderline, umbral={umbral})...")

    for d in decisiones:
        if d["accion"] != "COMPRAR":
            continue

        sym = d["simbolo"]
        score = d.get("score", 0)

        if score > umbral + 1:
            msg = f"VETO-LLM: {sym} score fuerte ({score}>{umbral}+1), compra directa sin veto"
            print(f"   {msg}")
            log_vetos.append(msg)
            continue

        # Borderline: consultar Nemotron
        if score == umbral:
            msg_chk = f"VETO-LLM: {sym} score borderline ({score}={umbral}), consultando Nemotron..."
        else:
            msg_chk = f"VETO-LLM: {sym} score borderline ({score} vs umbral {umbral}), consultando Nemotron..."
        print(f"   {msg_chk}")
        log_vetos.append(msg_chk)

        arts = noticias_mkt.get(sym, []) if isinstance(noticias_mkt, dict) else []
        titulos = [a.get("titulo", "") for a in arts
                   if isinstance(a, dict) and a.get("titulo") and "error" not in a]
        noticias_texto = ("\n".join(f"- {t}" for t in titulos[:10])
                          if titulos else "(sin noticias recientes)")

        vetado, explicacion = verificar_llm_veto(sym, score, noticias_texto)
        explicacion_corta = explicacion.split("\n")[0].strip()[:200]

        if vetado:
            msg_v = f"VETO-LLM: {sym} VETADO por Nemotron — {explicacion_corta}"
            print(f"   {msg_v}")
            log_vetos.append(msg_v)
            d["accion"] = "MANTENER"
            d["regla"] = "VETO-LLM"
            d["razon"] = f"vetado por LLM: {explicacion_corta}"
            d["veto_llm"] = {"vetado": True, "explicacion": explicacion_corta}
            _log_decision(
                simbolo=sym, accion="MANTENER",
                precio_actual=d.get("precio", 0),
                motivo=f"vetado por LLM: {explicacion_corta}",
                score=score, regla="VETO-LLM",
                score_detalle=d.get("score_detalle"),
            )
        else:
            msg_a = f"VETO-LLM: {sym} APROBADO por Nemotron — {explicacion_corta}"
            print(f"   {msg_a}")
            log_vetos.append(msg_a)
            d["veto_llm"] = {"vetado": False, "explicacion": explicacion_corta}

    print()

    # 7) Mostrar decisiones finales
    print("=" * 70)
    etiqueta_modo = " [DRY-RUN]" if solo_analisis else " [LIVE]"
    max_pos_efectivo = min(condiciones.get("max_posiciones_regimen", MAX_POSICIONES), MAX_POSICIONES)
    reg_label = condiciones.get("regimen", "?")
    print(f"  DECISIONES FINALES{etiqueta_modo} ({len(activos_ciclo)} activos, "
          f"máx {max_pos_efectivo} pos, ${condiciones['max_por_trade']:,.0f}/trade, "
          f"régimen {reg_label}, SL {STOP_LOSS_REGLA_PCT*100:.0f}%)")
    print("=" * 70)
    for d in decisiones:
        icono = {"COMPRAR": "+", "VENDER": "-", "MANTENER": "="}
        regla = d.get("regla", "?")
        prio = PRIORIDAD_SHARPE.get(d["simbolo"].upper(), "?")
        print(f"  [{icono.get(d['accion'], '?')}] {d['simbolo']:<5}: {d['accion']:<9} "
              f"[{regla:<10}] — {d['razon'][:80]}  (prio #{prio})")
    print("=" * 70)

    # 8) Ejecutar órdenes + logging de decisiones
    precios_map = {d["simbolo"]: d["precio"] for d in datos_acciones}

    if solo_analisis:
        print("\n>>> MODO DRY-RUN: no se ejecutan órdenes.\n")
        resultados = []
        for d in decisiones:
            r = {"simbolo": d["simbolo"], "accion": d["accion"],
                 "razon": d["razon"], "regla": d.get("regla", ""),
                 "ejecutada": False}
            if d.get("qty"):
                precio = precios_map.get(d["simbolo"], 0)
                r["qty"] = d["qty"]
                r["monto_aprox"] = round(d["qty"] * precio, 2) if precio else 0
            resultados.append(r)
            # Log cada decisión (incluso en dry-run)
            _log_decision(
                simbolo=d["simbolo"], accion=d["accion"],
                precio_actual=precios_map.get(d["simbolo"], 0),
                precio_entrada=d.get("precio_entrada"),
                pnl_pct=d.get("pnl_pct"),
                motivo=d.get("razon", "")[:500],
                score=d.get("score"), regla=d.get("regla", ""),
                score_detalle=d.get("score_detalle"),
            )
            # DB histórica — decisión (dry-run, no ejecuta)
            if _jdb is not None:
                try:
                    _jdb.registrar_decision(
                        mercado="acciones", simbolo=d["simbolo"],
                        accion=_db_accion_map(d["accion"], d.get("regla", "")),
                        score=d.get("score"), regla=d.get("regla", ""),
                        motivo=d.get("razon", "")[:500],
                        ejecutada=False,
                        score_detalle=d.get("score_detalle"),
                    )
                except Exception as _e:
                    print(f"   [db] registrar_decision dry-run falló: {_e}")
    else:
        print("\n8) Ejecutando órdenes...")
        resultados = ejecutar_ordenes(
            decisiones, datos_acciones, posiciones, condiciones["max_por_trade"],
            balance=balance,
        )
        ejecutadas = [r for r in resultados if r.get("ejecutada")]
        print(f"   {len(ejecutadas)} orden(es) ejecutada(s).")

        # Log + alerta inmediata por cada orden ejecutada
        for r in resultados:
            _log_decision(
                simbolo=r["simbolo"], accion=r["accion"],
                precio_actual=precios_map.get(r["simbolo"], 0),
                precio_entrada=r.get("precio_entrada"),
                pnl_pct=r.get("pnl_pct"),
                motivo=r.get("razon", "")[:500],
                score=r.get("score"), regla=r.get("regla", ""),
                score_detalle=r.get("score_detalle"),
            )
            # DB histórica — decisión SIEMPRE; trade sólo si ejecutada
            if _jdb is not None:
                try:
                    sym = r["simbolo"]
                    accion_db = _db_accion_map(r["accion"], r.get("regla", ""))
                    precio = precios_map.get(sym, 0)
                    _jdb.registrar_decision(
                        mercado="acciones", simbolo=sym, accion=accion_db,
                        score=r.get("score"), regla=r.get("regla", ""),
                        motivo=r.get("razon", "")[:500],
                        ejecutada=bool(r.get("ejecutada")),
                        score_detalle=r.get("score_detalle"),
                    )
                    if r.get("ejecutada") and accion_db in ("BUY", "SELL"):
                        pnl_pct = r.get("pnl_pct")
                        pnl_usd = None
                        entrada = r.get("precio_entrada")
                        qty = r.get("qty")
                        if (accion_db == "SELL" and pnl_pct is not None
                                and entrada and qty):
                            try:
                                pnl_usd = round(
                                    (precio - float(entrada)) * float(qty), 2)
                            except Exception:
                                pnl_usd = None
                        _jdb.registrar_trade(
                            mercado="acciones", simbolo=sym, accion=accion_db,
                            qty=qty, precio=precio,
                            score=r.get("score"), regla=r.get("regla", ""),
                            pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                            motivo=r.get("razon", "")[:500],
                            score_detalle=r.get("score_detalle"),
                        )
                except Exception as _e:
                    print(f"   [db] registrar trade/decisión falló: {_e}")

            if not r.get("ejecutada"):
                continue

            regla = r.get("regla", "")
            sym = r["simbolo"]
            qty_str = r.get("qty", "?")
            monto = r.get("monto_aprox", 0)

            if regla in ("R-SL", "R-TRAILING"):
                precio_acc = precios_map.get(sym, 0)
                entrada = r.get("precio_entrada", 0)
                label = "STOP-LOSS" if regla == "R-SL" else "TRAILING-STOP"
                # MEJORA 1: registrar cooldown SOLO tras un SL real ejecutado
                if regla == "R-SL":
                    try:
                        _registrar_sl_ejecutado(sym)
                        print(f"   [R-COOLDOWN] {sym}: cooldown de "
                              f"{COOLDOWN_SL_DIAS} días activado")
                    except Exception as _e:
                        print(f"   [R-COOLDOWN] error registrando SL: {_e}")
                alerta = (
                    f"\U0001f534 {label}: {sym} @ ${precio_acc:,.2f}"
                    f" | P&amp;L:{r.get('pnl_pct', '?')}%"
                    f" | Entrada:${entrada:,.2f}"
                )
                _notificar(alerta)
                print(f"   >>> Alerta {label} enviada: {sym}")

            elif regla in ("R-TP-PARCIAL", "R-TP-TOTAL"):
                precio_acc = precios_map.get(sym, 0)
                label = "TAKE-PROFIT (50%)" if regla == "R-TP-PARCIAL" else "TAKE-PROFIT (100%)"
                alerta = (
                    f"\U0001f7e1 {label}: {sym} @ ${precio_acc:,.2f}"
                    f" | Ganancia:{r.get('pnl_pct', '?')}%"
                )
                _notificar(alerta)
                print(f"   >>> Alerta {label} enviada: {sym}")

            elif regla == "R-ROTACION":
                precio_acc = precios_map.get(sym, 0)
                alerta = (
                    f"\U0001f504 ROTACIÓN: vendiendo {sym} @ ${precio_acc:,.2f}"
                    f" | {_esc(r.get('razon', '')[:80])}"
                )
                _notificar(alerta)
                print(f"   >>> Alerta ROTACIÓN enviada: {sym}")

            elif r["accion"] == "COMPRAR":
                precio_acc = precios_map.get(sym, 0)
                alerta = (
                    f"\U0001f7e2 COMPRA: {sym} {qty_str}acc @ ${precio_acc:,.2f}"
                    f" | Score:{r.get('score', '?')}"
                    f" | {_esc(r.get('razon', '')[:100])}"
                )
                _notificar(alerta)
                print(f"   >>> Alerta COMPRA enviada: {sym}")

    # 9) Balance actualizado
    print("\n9) Balance actualizado...")
    try:
        balance_final = get_balance()
    except Exception:
        balance_final = balance
    print(f"   Equity: ${float(balance_final['equity']):,.2f}")
    print(f"   Cash: ${float(balance_final['cash']):,.2f}")

    # 10) Explicación LLM eliminada — mensaje basado solo en reglas
    print("\n10) LLM desactivado — usando resumen de reglas.")
    explicacion_llm = ""

    # 11) Enviar a Telegram
    etiqueta = " (dry-run)" if solo_analisis else ""
    mensaje = construir_mensaje_telegram(
        explicacion_llm, resultados, balance_final, condiciones, log_reglas
    )
    if etiqueta:
        mensaje = mensaje.replace("Trading Agent v2", f"Trading Agent v2{etiqueta}", 1)
    if len(mensaje) > 4000:
        mensaje = mensaje[:4000] + "\n\n[truncado]"

    print("\n11) Enviando notificaciones...")
    _notificar(mensaje)
    print("   WhatsApp enviado.")

    # 12) Guardar decisiones en memoria ChromaDB
    for r in resultados:
        try:
            _guardar_decision(
                activo=r["simbolo"],
                accion=r["accion"],
                precio=r.get("monto_aprox", 0),
                razon=r.get("razon", ""),
                ejecutada=r.get("ejecutada", False),
                order_id=r.get("order_id", ""),
                modelo=f"reglas-v2/{r.get('regla', '')}",
            )
        except Exception as e:
            print(f"   Error guardando en memoria: {e}")

    # 13) Guardar log detallado
    ruta = guardar_log(
        explicacion_llm, decisiones, resultados, balance_final,
        condiciones, log_reglas, log_vetos, senales
    )
    print(f"   Log: {ruta}")

    # 14) Snapshot diario en DB histórica (lado acciones).
    # El lado cripto se añade vía cripto/jarvis_cripto.py o snapshot_diario.py.
    if _jdb is not None:
        try:
            posiciones_snap = []
            for p in posiciones or []:
                try:
                    posiciones_snap.append({
                        "simbolo": p.get("symbol"),
                        "qty": float(p.get("qty", 0)),
                        "precio_entrada": float(p.get("avg_entry_price", 0) or 0),
                        "precio_actual": float(p.get("current_price", 0) or 0),
                        "valor_actual": float(p.get("market_value", 0) or 0),
                        "pnl_pct": float(p.get("unrealized_plpc", 0) or 0) * 100,
                        "pnl_usd": float(p.get("unrealized_pl", 0) or 0),
                    })
                except Exception:
                    continue
            datos_acc = {
                "equity": float(balance_final.get("equity", 0) or 0),
                "cash": float(balance_final.get("cash", 0) or 0),
                "posiciones": posiciones_snap,
                "pnl_dia": sum(p["pnl_usd"] for p in posiciones_snap),
            }
            _jdb.guardar_snapshot_diario(datos_acc, None)
            print("   [db] snapshot acciones guardado")
        except Exception as _e:
            print(f"   [db] snapshot falló: {_e}")


if __name__ == "__main__":
    main()
