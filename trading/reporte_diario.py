#!/home/hproano/asistente_env/bin/python
"""
Reporte diario matutino de JARVIS.
Se ejecuta a las 7:30 AM ECT y envía por Telegram:
  1. Estado del portafolio (IBKR real)
  2. Contexto de mercado
  3. Plan de JARVIS para hoy
  4. Resumen de ayer
"""

import os
import sys
import re
import json
import importlib.util
from datetime import datetime, timedelta

import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

# Importar con paths explícitos (config.py local colisiona con config/ package)
import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_alertas = _load("alertas", os.path.join(PROYECTO, "config", "alertas.py"))
enviar_telegram = _alertas.enviar_telegram

_ctx = _load("contexto_mercado", os.path.join(PROYECTO, "datos", "contexto_mercado.py"))
obtener_fear_greed = _ctx.obtener_fear_greed
obtener_vix = _ctx.obtener_vix
from dotenv import load_dotenv

load_dotenv(os.path.join(PROYECTO, ".env"))

# Config
_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)

# IBKR adapter (cuenta real)
from agentes.ibkr_trading import get_balance, get_positions, _precio_fallback

OLLAMA_URL = _cfg.OLLAMA_URL
MODEL_FAST = _cfg.MODEL_FAST

# JARVIS live config
POSICIONES_PROTEGIDAS = {"AMD", "NVDA"}
ACTIVOS_JARVIS = ["JNJ", "GLD", "HYG", "AGG", "IEF", "KO", "VZ", "XLU", "T", "D", "IBM"]
ACTIVOS = ACTIVOS_JARVIS
MAX_POSICIONES_JARVIS = 6
JARVIS_LIVE_CAPITAL = float(os.getenv("JARVIS_LIVE_CAPITAL", "2000"))
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.10
MAX_POR_TRADE = 750.0

ESTADO_PATH = os.path.join(PROYECTO, "logs", "ultimo_estado.json")


def _guardar_estado(balance, posiciones):
    """Guarda estado actual en JSON para fallback si IBKR no conecta."""
    os.makedirs(os.path.dirname(ESTADO_PATH), exist_ok=True)
    estado = {
        "timestamp": datetime.now().isoformat(),
        "balance": balance,
        "posiciones": posiciones,
    }
    with open(ESTADO_PATH, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)


def _cargar_estado():
    """Carga último estado guardado. Retorna (balance, posiciones, timestamp) o None."""
    if not os.path.exists(ESTADO_PATH):
        return None
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            estado = json.load(f)
        return estado["balance"], estado["posiciones"], estado["timestamp"]
    except Exception:
        return None


def _refrescar_precios(posiciones):
    """Actualiza precios de posiciones via Tiingo."""
    for p in posiciones:
        precio = _precio_fallback(p["symbol"])
        if precio is not None:
            entry = float(p["avg_entry_price"])
            qty = abs(float(p["qty"]))
            p["current_price"] = str(round(precio, 2))
            p["unrealized_pl"] = str(round((precio - entry) * qty, 2))
            p["unrealized_plpc"] = str(round((precio / entry) - 1, 4)) if entry > 0 else "0"
            p["market_value"] = str(round(precio * qty, 2))
    return posiciones


_TEST_MODE = "--test" in sys.argv


def _notificar(mensaje):
    """Envía a Telegram + WhatsApp."""
    if _TEST_MODE:
        print("  [NOTIF] (suprimida en test)")
        return
    enviar_telegram(mensaje)
    try:
        requests.post("http://localhost:8001/alerta",
                       json={"mensaje": mensaje}, timeout=10)
    except Exception:
        pass


# ── 1. Estado del portafolio (IBKR real) ──────────────────

def seccion_portafolio():
    """Retorna texto con estado de la cuenta IBKR y posiciones."""
    ibkr_ok = True
    try:
        balance = get_balance()
        posiciones = get_positions()
        # Refrescar precios via Tiingo (más confiable para RT)
        posiciones = _refrescar_precios(posiciones)
        # Guardar estado para fallback futuro
        _guardar_estado(balance, posiciones)
    except Exception as e:
        ibkr_ok = False
        _notificar(
            "\u26a0\ufe0f <b>JARVIS: No pudo conectar a IBKR para el reporte.</b>\n"
            f"Error: {e}\n"
            "TWS puede estar caído. Usando último estado guardado."
        )
        cached = _cargar_estado()
        if cached:
            balance, posiciones, ts = cached
            posiciones = _refrescar_precios(posiciones)  # Actualizar precios
            print(f"  Usando estado guardado de {ts}")
        else:
            balance = {"equity": "0", "cash": "0", "buying_power": "0",
                       "portfolio_value": "0"}
            posiciones = []

    equity = float(balance["equity"])
    cash = float(balance["cash"])
    buying_power = float(balance.get("buying_power", 0))

    # Separar posiciones JARVIS vs usuario
    pos_jarvis = [p for p in posiciones if p["symbol"] in set(ACTIVOS_JARVIS)]
    pos_usuario = [p for p in posiciones if p["symbol"] in POSICIONES_PROTEGIDAS]

    L = []
    fuente = "IBKR LIVE" if ibkr_ok else "CACHE + Tiingo"
    L.append(f"\U0001f4bc <b>PORTAFOLIO ({fuente})</b>")
    L.append(f"  Equity: ${equity:,.2f}")
    L.append(f"  Cash: ${cash:,.2f}")
    L.append(f"  Buying power: ${buying_power:,.2f}")

    # Posiciones JARVIS
    capital_jarvis_usado = sum(float(p["market_value"]) for p in pos_jarvis)
    capital_jarvis_disp = max(0, JARVIS_LIVE_CAPITAL - capital_jarvis_usado)
    L.append(f"\n  <b>Posiciones JARVIS</b> ({len(pos_jarvis)}/{MAX_POSICIONES_JARVIS} slots | "
             f"capital: ${capital_jarvis_usado:,.0f}/${JARVIS_LIVE_CAPITAL:,.0f})")

    pnl_jarvis = 0.0
    if pos_jarvis:
        for p in pos_jarvis:
            qty = float(p["qty"])
            entrada = float(p["avg_entry_price"])
            actual = float(p["current_price"])
            pnl = float(p["unrealized_pl"])
            pnl_pct = float(p["unrealized_plpc"]) * 100
            pnl_jarvis += pnl
            icono = "\u2705" if pnl >= 0 else "\U0001f534"
            L.append(f"  {icono} {p['symbol']}: {qty:.0f} acc @ ${entrada:,.2f} "
                     f"→ ${actual:,.2f} | P&amp;L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)")
        L.append(f"  P&amp;L JARVIS: ${pnl_jarvis:+,.2f} | Disp: ${capital_jarvis_disp:,.0f}")
    else:
        L.append(f"  Sin posiciones. Disponible: ${capital_jarvis_disp:,.0f}")

    # Posiciones usuario
    if pos_usuario:
        L.append(f"\n  <b>Posiciones usuario</b>")
        for p in pos_usuario:
            qty = float(p["qty"])
            entrada = float(p["avg_entry_price"])
            actual = float(p["current_price"])
            pnl = float(p["unrealized_pl"])
            pnl_pct = float(p["unrealized_plpc"]) * 100
            icono = "\u2705" if pnl >= 0 else "\U0001f534"
            L.append(f"  {icono} {p['symbol']}: {qty:.0f} acc @ ${entrada:,.2f} "
                     f"→ ${actual:,.2f} | P&amp;L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)")
            L.append(f"    \u26a0\ufe0f Posición usuario — JARVIS no opera")

    return "\n".join(L), balance, posiciones


# ── 2. Contexto de mercado (compacto) ───────────────────────

def seccion_contexto():
    """Retorna resumen compacto del contexto de mercado."""
    fng = obtener_fear_greed()
    vix = obtener_vix()

    L = []
    L.append(f"\U0001f30d <b>CONTEXTO DE MERCADO</b>")
    L.append(f"  Fear &amp; Greed: {fng['valor']}/100 — {fng['clasificacion']}")
    L.append(f"  {fng['nota']}")

    if vix["precio"]:
        L.append(f"  VIX: {vix['precio']} ({vix['variacion']:+.1f}%) — {vix['nivel']}")
        L.append(f"  {vix['nota']}")

    return "\n".join(L), fng, vix


# ── 3. Plan de JARVIS para hoy ──────────────────────────────

def seccion_plan(fng, vix, posiciones):
    """Genera el plan de JARVIS usando el modelo rápido (7b)."""
    pos_str = "ninguna"
    if posiciones:
        pos_str = ", ".join(p["symbol"] for p in posiciones)

    vix_precio = vix.get("precio", 0) or 0
    fng_valor = fng.get("valor", 50) or 50

    # Determinar modo
    modos = []
    if vix_precio > 30:
        modos.append("VIX alto → posición reducida al 50%")
    if fng_valor < 20:
        modos.append("F&G en pánico → modo oportunidad")

    activos_str = ", ".join(ACTIVOS_JARVIS)
    modo_str = " | ".join(modos) if modos else "operación normal"

    prompt = (
        f"Eres JARVIS. Genera un plan de trading para hoy en MÁXIMO 4 líneas en español.\n"
        f"Datos: VIX={vix_precio}, Fear&Greed={fng_valor}, "
        f"posiciones abiertas: {pos_str}, "
        f"activos monitoreados: {activos_str}, modo: {modo_str}.\n"
        f"Indica: qué activos priorizar, sesgo (alcista/bajista/neutral), y precauciones."
    )

    try:
        payload = {
            "model": MODEL_FAST,
            "messages": [
                {"role": "system", "content": "Eres JARVIS, asistente de trading. Responde SOLO en español, máximo 4 líneas."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        plan = resp.json()["message"]["content"]
        plan = re.sub(r"<think>.*?</think>", "", plan, flags=re.DOTALL).strip()
    except Exception:
        # Fallback sin LLM
        if fng_valor < 20:
            plan = (f"Mercado en pánico (F&G={fng_valor}). Buscar oportunidades en defensivos: "
                    f"JNJ, GLD, AGG. VIX en {vix_precio}: posiciones de $500 máx. Cautela.")
        elif vix_precio > 25:
            plan = (f"Volatilidad elevada (VIX={vix_precio}). Priorizar bonos y oro: "
                    f"AGG, IEF, GLD. Evitar entradas agresivas. Vigilar stops 3%.")
        else:
            plan = (f"Condiciones normales. Monitorear {', '.join(ACTIVOS_JARVIS)}. "
                    f"Prioridad: JNJ, GLD por mejor Sharpe. $500/trade, SL 3%.")

    L = []
    L.append(f"\U0001f4cb <b>PLAN DE HOY</b>")
    L.append(f"  Modo: {modo_str}")
    L.append(f"  {plan}")

    return "\n".join(L)


# ── 4. Resumen de ayer ──────────────────────────────────────

def seccion_ayer():
    """Revisa el log de decisiones de JARVIS de ayer."""
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Si es lunes, revisar el viernes
    if datetime.now().weekday() == 0:
        ayer = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    L = []
    L.append(f"\U0001f4ca <b>RESUMEN DE AYER ({ayer})</b>")

    # Leer log de decisiones
    log_decisiones = os.path.join(PROYECTO, "logs", "trading_decisiones.log")
    trades_ayer = []
    if os.path.exists(log_decisiones):
        with open(log_decisiones, "r", encoding="utf-8") as f:
            for linea in f:
                if linea.startswith(ayer):
                    trades_ayer.append(linea.strip())

    # También revisar log de sesión de JARVIS
    log_sesion = os.path.join(PROYECTO, "logs", f"jarvis_trading_{ayer}.txt")
    n_sesiones = 0
    if os.path.exists(log_sesion):
        with open(log_sesion, "r") as f:
            n_sesiones = f.read().count("=== ")

    if not trades_ayer:
        L.append(f"  Sin trades registrados ayer.")
        if n_sesiones:
            L.append(f"  JARVIS ejecutó {n_sesiones} sesión(es) de análisis.")
        return "\n".join(L)

    ejecutadas = [t for t in trades_ayer if "exec=SI" in t]
    L.append(f"  Decisiones registradas: {len(trades_ayer)}")
    L.append(f"  Órdenes ejecutadas: {len(ejecutadas)}")
    if n_sesiones:
        L.append(f"  Sesiones de análisis: {n_sesiones}")

    for t in ejecutadas[:10]:
        L.append(f"  {t[11:]}")  # Skip date prefix

    return "\n".join(L)


# ── Generar y enviar reporte ────────────────────────────────

def generar_reporte():
    """Genera el reporte completo y lo envía por Telegram."""
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

    print(f"Generando reporte diario — {fecha}\n")

    # Secciones
    print("  1/4 Portafolio...", end=" ", flush=True)
    txt_port, balance, posiciones = seccion_portafolio()
    print("OK")

    print("  2/4 Contexto...", end=" ", flush=True)
    txt_ctx, fng, vix = seccion_contexto()
    print("OK")

    print("  3/4 Plan de JARVIS...", end=" ", flush=True)
    txt_plan = seccion_plan(fng, vix, posiciones)
    print("OK")

    print("  4/4 Resumen de ayer...", end=" ", flush=True)
    txt_ayer = seccion_ayer()
    print("OK")

    # Construir mensaje
    mensaje = (
        f"\U0001f916 <b>JARVIS — Reporte Matutino</b>\n"
        f"\U0001f4c5 {fecha}\n\n"
        f"{txt_port}\n\n"
        f"{txt_ctx}\n\n"
        f"{txt_plan}\n\n"
        f"{txt_ayer}\n\n"
        f"\U0001f6e1 SL: -{STOP_LOSS_PCT*100:.0f}% | TP: +{TAKE_PROFIT_PCT*100:.0f}% | "
        f"Máx: ${MAX_POR_TRADE:,.0f}/trade | {MAX_POSICIONES_JARVIS} pos | "
        f"Activos: {', '.join(ACTIVOS_JARVIS)}"
    )

    # Enviar
    print(f"\nEnviando notificaciones...")
    _notificar(mensaje)
    print("  Telegram + WhatsApp enviados.")

    # Generar audio del reporte con Fish Speech (voz clonada de JARVIS)
    try:
        _voz = _load("jarvis_voz", os.path.join(PROYECTO, "agentes", "jarvis_voz.py"))
        # Resumen corto para audio (solo lo esencial, no el HTML completo)
        resumen_audio = (
            f"REPORTE: Buenos días Hugo. "
            f"Tu portafolio tiene un equity de {txt_port.split('Equity:')[1].split(chr(10))[0].strip() if 'Equity:' in txt_port else 'N/D'}. "
            f"El mercado está en {txt_ctx.split('Sentimiento:')[1].split(chr(10))[0].strip() if 'Sentimiento:' in txt_ctx else 'modo normal'}. "
            f"Hoy JARVIS recomienda cautela. Todos los sistemas operativos."
        )
        audio = _voz.sintetizar_respuesta(resumen_audio, premium=False)
        if audio.get("archivo"):
            print(f"  Audio reporte ({audio['motor']}): {audio['archivo']}")
            # Enviar audio por Telegram
            import requests as _req
            from dotenv import load_dotenv as _ld
            _ld(os.path.join(PROYECTO, ".env"))
            _bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            _chat_id = os.getenv("TELEGRAM_CHAT_ID")
            with open(audio["archivo"], "rb") as af:
                _req.post(
                    f"https://api.telegram.org/bot{_bot_token}/sendVoice",
                    data={"chat_id": _chat_id, "caption": "Reporte matutino JARVIS"},
                    files={"voice": af}, timeout=30,
                )
            print("  Audio enviado por Telegram.")
            # Enviar audio por WhatsApp (convertir WAV→MP3 si necesario)
            try:
                import base64
                import subprocess
                archivo_audio = audio["archivo"]
                if archivo_audio.endswith('.wav'):
                    archivo_mp3 = archivo_audio.replace('.wav', '.mp3')
                    conv = subprocess.run(
                        ['ffmpeg', '-y', '-i', archivo_audio,
                         '-codec:a', 'libmp3lame', '-qscale:a', '2',
                         archivo_mp3],
                        capture_output=True, timeout=30)
                    if conv.returncode == 0:
                        archivo_audio = archivo_mp3
                        print(f"  WAV convertido a MP3: {archivo_mp3}")
                    else:
                        print(f"  Error convirtiendo a MP3, usando WAV")
                with open(archivo_audio, "rb") as af:
                    audio_b64 = base64.b64encode(af.read()).decode("utf-8")
                size_kb = len(audio_b64) * 3 / 4 / 1024
                print(f"  Enviando audio WhatsApp ({size_kb:.0f} KB)...")
                resp_wa = _req.post(
                    "http://localhost:8001/alerta",
                    json={"mensaje": resumen_audio, "audio_base64": audio_b64},
                    timeout=60,
                )
                print(f"  WhatsApp audio response: {resp_wa.status_code} {resp_wa.text[:100]}")
            except Exception as wa_e:
                print(f"  WhatsApp audio error: {wa_e}")
    except Exception as e:
        print(f"  Audio reporte: error ({e})")

    # Guardar log
    dir_logs = os.path.join(PROYECTO, "logs")
    os.makedirs(dir_logs, exist_ok=True)
    ruta = os.path.join(dir_logs, f"reporte_{datetime.now().strftime('%Y-%m-%d')}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"Reporte matutino — {fecha}\n\n")
        f.write(f"{txt_port}\n\n{txt_ctx}\n\n{txt_plan}\n\n{txt_ayer}\n")
    print(f"  Log: {ruta}")

    # Imprimir en consola
    print(f"\n{'=' * 60}")
    print(txt_port)
    print()
    print(txt_ctx)
    print()
    print(txt_plan)
    print()
    print(txt_ayer)
    print(f"{'=' * 60}")


if __name__ == "__main__":
    if _TEST_MODE:
        print("=== TEST MODE: reporte sin enviar notificaciones ===\n")
    generar_reporte()
