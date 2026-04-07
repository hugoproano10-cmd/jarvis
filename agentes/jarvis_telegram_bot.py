#!/home/hproano/asistente_env/bin/python
"""
JARVIS Telegram Bot — Asistente personal bidireccional.
Responde mensajes libres via Model Router (nemotron-3-super / nemotron-3-nano).
Soporta mensajes de voz (Whisper STT + TTS) y modo voz persistente.
Comandos: /mercado, /portafolio, /cripto, /voz, /texto, /ayuda.
"""

import os
import sys
import tempfile
import logging
import subprocess
import importlib.util as ilu
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("jarvis-bot")

# ── Cargar módulos ──────────────────────���───────────────────

def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

alpaca = _load("alpaca_client", os.path.join(os.path.expanduser("~"), "trading", "alpaca_client.py"))
cripto = _load("jarvis_cripto", os.path.join(PROYECTO, "cripto", "jarvis_cripto.py"))
fuentes = _load("fuentes_mercado", os.path.join(PROYECTO, "datos", "fuentes_mercado.py"))
memoria = _load("memoria_jarvis", os.path.join(PROYECTO, "datos", "memoria_jarvis.py"))
_model_router = _load("model_router", os.path.join(PROYECTO, "agentes", "model_router.py"))
route_message = _model_router.route_message
health_check = _model_router.health_check

voz = _load("jarvis_voz", os.path.join(PROYECTO, "agentes", "jarvis_voz.py"))

PYTHON = os.path.join(os.path.expanduser("~"), "asistente_env", "bin", "python")
MONITOR = os.path.join(PROYECTO, "trading", "monitor_mercado.py")

# Modo voz: chats que reciben audio además de texto
_modo_voz = set()

# ── Seguridad: solo responder a mi chat ──���──────────────────

def autorizado(update: Update) -> bool:
    return str(update.effective_chat.id) == CHAT_ID

# ── System Prompt ──────────────────────────────────────────

SYSTEM_PROMPT = """\
INSTRUCCIÓN ABSOLUTA E INAMOVIBLE: Responde SIEMPRE en español. \
Sin excepciones. Sin importar el idioma del contexto, las fuentes de datos, \
las noticias o los documentos que recibas. \
Tu respuesta SIEMPRE debe estar en español. \
Si los datos están en inglés, tradúcelos al español en tu respuesta. \
NUNCA respondas en inglés.

Eres JARVIS, asistente personal de Hugo Proaño, trader e ingeniero en Ecuador.
Respondes en español, con tono profesional pero cercano.
Tienes conocimiento de mercados financieros, criptomonedas, tecnología y programación.
Sé conciso: máximo 3-4 párrafos por respuesta.
Cuando Hugo pregunte sobre su portafolio, mercado o cripto, USA LOS DATOS REALES que se te proporcionan en el contexto. NO inventes datos.
Hugo usa paper trading en Alpaca (acciones) y Binance Testnet (cripto).\
"""


# ── Keywords que van al brain (671B) — necesitan contexto reducido ──

import importlib.util as _ilu2
_cfg = _load("trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_ACTIVOS_SET = set(_cfg.ACTIVOS_OPERABLES)

_BRAIN_KEYWORDS = [
    "analiza en detalle", "análisis profundo", "analisis profundo",
    "investiga a fondo", "investigación completa", "investigacion completa",
    "estrategia completa", "plan completo", "plan detallado",
    "evalúa a fondo", "evalua a fondo", "análisis exhaustivo",
    "reporte completo", "dame todo", "análisis largo",
    "análisis completo", "analisis completo",
    "todas las señales", "todas las senales",
    "dime todo sobre", "panorama completo",
]


def _es_para_brain(texto):
    t = texto.lower()
    return any(kw in t for kw in _BRAIN_KEYWORDS)


def _detectar_simbolo(texto):
    """Detecta si el usuario menciona un símbolo de los 22 activos."""
    upper = texto.upper()
    for sym in _ACTIVOS_SET:
        if sym in upper.split() or f" {sym} " in f" {upper} " or f" {sym}," in f" {upper}," or f" {sym}?" in f" {upper}?":
            return sym
    # Buscar nombres comunes
    nombres = {
        "EXXON": "XOM", "JOHNSON": "JNJ", "TESLA": "TSLA", "APPLE": "AAPL",
        "COCA": "KO", "MCDONALDS": "MCD", "MCDONALD": "MCD", "VERIZON": "VZ",
        "DOMINION": "D", "GOLD": "GLD", "ORO": "GLD",
    }
    for nombre, sym in nombres.items():
        if nombre in upper:
            return sym
    return None


def obtener_contexto_brain(simbolo=None):
    """
    Contexto enriquecido para brain (~1250 chars): macro + datos específicos del símbolo.
    """
    partes = [f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}"]

    # Macro (~200 chars)
    try:
        from datos.contexto_mercado import obtener_fear_greed, obtener_vix
        fng = obtener_fear_greed()
        vix = obtener_vix()
        partes.append(f"F&G: {fng.get('valor','?')}/100 ({fng.get('clasificacion','?')})")
        partes.append(f"VIX: {vix.get('precio','?')} ({vix.get('nivel','?')})")
    except Exception:
        pass
    try:
        from datos.regimen_mercado import get_regimen_actual
        reg = get_regimen_actual()
        partes.append(f"Régimen: {reg['regimen']} ({reg.get('razon','')})")
    except Exception:
        pass

    # Portafolio básico (~150 chars)
    try:
        bal = alpaca.get_balance()
        partes.append(f"Equity: ${float(bal['equity']):,.2f}")
        pos = alpaca.get_positions()
        if pos:
            pos_sym = [p["symbol"] for p in pos]
            partes.append(f"Posiciones: {', '.join(pos_sym)}")
            if simbolo:
                for p in pos:
                    if p["symbol"] == simbolo:
                        pnl = float(p["unrealized_plpc"]) * 100
                        partes.append(f"{simbolo} en portafolio: {float(p['qty']):.0f} acc, P&L {pnl:+.1f}%")
    except Exception:
        pass

    if not simbolo:
        return "\n".join(partes)

    # Datos específicos del símbolo
    partes.append(f"\n--- Datos de {simbolo} ---")

    # Fundamentales (~200 chars)
    try:
        from datos.nasdaq_data import get_fundamentals
        fd = get_fundamentals(simbolo)
        if not fd.get("error"):
            pe_s = f"P/E:{fd['pe']:.1f}" if fd.get("pe") else ""
            rg_s = f"RevGr:{fd['revenue_growth_yoy']:+.0f}%" if fd.get("revenue_growth_yoy") is not None else ""
            roe_s = f"ROE:{fd['roe']:.0f}%" if fd.get("roe") is not None else ""
            partes.append(f"Fundamentales: {fd['senal']} ({pe_s} {rg_s} {roe_s})")
    except Exception:
        pass

    # Señales institucionales (~200 chars)
    try:
        si = fuentes.get_senales_institucionales(simbolo)
        partes.append(f"Institucional: {si.get('senal_general','?')}")
        for d in si.get("detalles", [])[:2]:
            partes.append(f"  {d[:80]}")
    except Exception:
        pass

    # Señales quant (~150 chars)
    try:
        from datos.quantconnect_estrategias import evaluar_activo
        qt = evaluar_activo(simbolo)
        mom = qt.get("momentum", {}).get("retorno_pct")
        rsi = qt.get("rsi_semanal", {}).get("rsi")
        partes.append(f"Quant: {qt['combinada']} (Mom:{mom:+.0f}% RSI:{rsi:.0f})" if mom and rsi else "")
    except Exception:
        pass

    # Earnings NLP (~100 chars)
    try:
        from datos.earnings_calls_nlp import analizar_earnings_call
        te = analizar_earnings_call(simbolo)
        if not te.get("error"):
            partes.append(f"Tono ejecutivos: score {te['score']:+d} → {te['senal']} ({te['quarter']})")
    except Exception:
        pass

    # Wikipedia (~100 chars)
    try:
        from datos.wikipedia_signals import get_wikipedia_signal
        ws = get_wikipedia_signal(simbolo)
        if ws.get("senal") in ("ALERTA", "FUERTE"):
            partes.append(f"Wikipedia: {ws['senal']} ({ws['vistas_hoy']:,} vistas, {ws['ratio_hoy']:.1f}x promedio)")
    except Exception:
        pass

    # Noticias (~300 chars)
    try:
        fd = fuentes.get_finnhub_datos(simbolo)
        noticias = fd.get("noticias", [])[:3]
        if noticias:
            partes.append("Noticias recientes:")
            for n in noticias:
                partes.append(f"  [{n['fecha']}] {n['titulo'][:70]}")
    except Exception:
        pass

    return "\n".join(p for p in partes if p)


# ── Recopilar contexto real ────────────────────────────────

def obtener_contexto_real() -> str:
    """Recopila datos reales de todas las fuentes disponibles."""
    partes = []
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    partes.append(f"Fecha y hora: {ahora}")

    # Precios cripto
    try:
        for par in cripto.PARES:
            precio = cripto.obtener_precio(par)
            nombre = cripto.NOMBRES.get(par, par)
            partes.append(f"{nombre} ({par}): ${precio:,.2f}")
    except Exception as e:
        partes.append(f"Cripto: error obteniendo precios ({e})")

    # Portafolio Alpaca
    try:
        balance = alpaca.get_balance()
        partes.append(
            f"Portafolio Alpaca: equity=${float(balance['equity']):,.2f}, "
            f"cash=${float(balance['cash']):,.2f}, "
            f"buying_power=${float(balance['buying_power']):,.2f}"
        )
        posiciones = alpaca.get_positions()
        if posiciones:
            pos_lines = []
            pnl_total = 0.0
            for p in posiciones:
                pnl = float(p["unrealized_pl"])
                pnl_pct = float(p["unrealized_plpc"]) * 100
                pnl_total += pnl
                pos_lines.append(
                    f"{p['symbol']}: {p['qty']} acc @ ${float(p['avg_entry_price']):,.2f} "
                    f"→ ${float(p['current_price']):,.2f} (P&L: ${pnl:+,.2f} / {pnl_pct:+.1f}%)"
                )
            partes.append(f"Posiciones ({len(posiciones)}): " + " | ".join(pos_lines))
            partes.append(f"P&L total no realizado: ${pnl_total:+,.2f}")
        else:
            partes.append("Sin posiciones abiertas en Alpaca.")
    except Exception as e:
        partes.append(f"Alpaca: error ({e})")

    # Contexto enriquecido: FRED macro + Finnhub analistas/noticias + Alpaca noticias + F&G + VIX
    try:
        texto_ctx, _ = fuentes.get_contexto_enriquecido()
        partes.append(f"\n{texto_ctx}")
    except Exception as e:
        partes.append(f"Contexto mercado enriquecido: error ({e})")

    return "\n".join(partes)


# ── Comandos ────────────────────────────────────────────────

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    msg = (
        "<b>JARVIS — Comandos disponibles</b>\n\n"
        "/mercado — Resumen de mercado (acciones + criptos)\n"
        "/portafolio — Balance y posiciones Alpaca\n"
        "/cripto — Estado BTC y ETH en Binance Testnet\n"
        "/voz — Activar modo voz (respuestas con audio)\n"
        "/texto — Desactivar modo voz (solo texto)\n"
        "/ayuda — Este mensaje\n\n"
        "Cualquier otro mensaje: JARVIS responde con IA\n"
        "🎤 Envía un mensaje de voz y JARVIS responde con audio"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_mercado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await update.message.reply_text("Consultando mercado...")
    try:
        result = subprocess.run(
            [PYTHON, MONITOR], capture_output=True, text=True, timeout=120,
        )
        salida = result.stdout.strip()
        if not salida:
            salida = f"Error: {result.stderr[:300]}"
        # Telegram límite 4096 chars
        if len(salida) > 4000:
            salida = salida[:4000] + "\n..."
        await update.message.reply_text(f"<pre>{salida}</pre>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_portafolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    try:
        balance = alpaca.get_balance()
        posiciones = alpaca.get_positions()

        msg = "<b>Portafolio Alpaca Paper Trading</b>\n\n"
        msg += f"Equity: ${float(balance['equity']):,.2f}\n"
        msg += f"Cash: ${float(balance['cash']):,.2f}\n"
        msg += f"Buying power: ${float(balance['buying_power']):,.2f}\n\n"

        if posiciones:
            msg += f"<b>Posiciones ({len(posiciones)}):</b>\n"
            pnl_total = 0.0
            for p in posiciones:
                pnl = float(p["unrealized_pl"])
                pnl_pct = float(p["unrealized_plpc"]) * 100
                pnl_total += pnl
                icono = "+" if pnl >= 0 else ""
                msg += (
                    f"  {p['symbol']}: {p['qty']} acc "
                    f"@ ${float(p['avg_entry_price']):,.2f} → "
                    f"${float(p['current_price']):,.2f} "
                    f"| P&amp;L: {icono}${pnl:,.2f} ({pnl_pct:+.1f}%)\n"
                )
            msg += f"\nP&amp;L total: ${pnl_total:+,.2f}"
        else:
            msg += "Sin posiciones abiertas."

        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error consultando Alpaca: {e}")


async def cmd_cripto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    try:
        # Balance
        balances = cripto.obtener_balance() or []
        bal_map = {}
        for b in balances:
            if b["activo"] in ("USDT", "BTC", "ETH"):
                bal_map[b["activo"]] = b

        msg = "<b>Cripto — Binance Testnet</b>\n\n"
        msg += f"USDT: ${bal_map.get('USDT', {}).get('total', 0):,.2f}\n"
        msg += f"BTC: {bal_map.get('BTC', {}).get('total', 0):.6f}\n"
        msg += f"ETH: {bal_map.get('ETH', {}).get('total', 0):.6f}\n\n"

        # Señales
        msg += "<b>Señales actuales:</b>\n"
        for par in cripto.PARES:
            s = cripto.evaluar_senal(par)
            nombre = cripto.NOMBRES.get(par, par)
            msg += (
                f"  {nombre}: ${s['precio']:,.2f} "
                f"| var 1h: {s['var_1h']:+.2f}% "
                f"| vol: {s['vol_ratio']:.1f}x "
                f"| <b>{s['senal']}</b>\n"
            )

        # Posiciones abiertas
        estado = cripto.cargar_estado()
        pos = estado.get("posiciones", {})
        if pos:
            msg += f"\n<b>Posiciones abiertas:</b>\n"
            for par, p in pos.items():
                nombre = cripto.NOMBRES.get(par, par)
                precio_actual = cripto.obtener_precio(par)
                pnl_pct = ((precio_actual / p["precio_entrada"]) - 1) * 100
                msg += (
                    f"  {nombre}: entrada ${p['precio_entrada']:,.2f} "
                    f"→ ${precio_actual:,.2f} ({pnl_pct:+.2f}%)\n"
                    f"  SL: ${p['sl']:,.2f} | TP: ${p['tp']:,.2f}\n"
                )
        else:
            msg += "\nSin posiciones cripto abiertas."

        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error consultando Binance: {e}")


# ── Modo voz ───────────────────────────────────────────────

async def cmd_voz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    _modo_voz.add(update.effective_chat.id)
    await update.message.reply_text("🔊 Modo voz activado. JARVIS responderá con audio y texto.\nUsa /texto para desactivar.")


async def cmd_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    _modo_voz.discard(update.effective_chat.id)
    await update.message.reply_text("📝 Modo texto activado. JARVIS responderá solo con texto.")


async def _enviar_audio(update: Update, texto: str):
    """Sintetiza y envía audio de respuesta por Telegram."""
    try:
        sintesis = voz.sintetizar_respuesta(texto)
        archivo = sintesis.get("archivo")
        if archivo and os.path.exists(archivo):
            with open(archivo, "rb") as f:
                await update.message.reply_voice(f, caption=f"🔊 {sintesis['motor']}")
            return True
    except Exception as e:
        log.error(f"Error sintetizando audio: {e}")
    return False


# ── Mensajes de voz ────────────────────────────────────────

async def mensaje_voz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe audio de voz, transcribe, procesa con LLM, responde con texto + audio."""
    if not autorizado(update):
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    await update.message.chat.send_action("typing")
    log.info(f"Mensaje de voz recibido ({voice.duration}s, {voice.file_size} bytes)")

    # 1. Descargar archivo de voz
    try:
        tg_file = await voice.get_file()
        suffix = ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False,
                                         dir=voz.RESPUESTAS_DIR) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        log.info(f"Audio descargado: {tmp_path}")
    except Exception as e:
        log.error(f"Error descargando audio: {e}")
        await update.message.reply_text(f"Error descargando tu audio: {e}")
        return

    # 2. Transcribir con Whisper
    try:
        transcripcion = voz.transcribir_audio(tmp_path)
        if transcripcion.get("error"):
            await update.message.reply_text(f"Error transcribiendo: {transcripcion['error']}")
            return
        texto_usuario = transcripcion["texto"]
        if not texto_usuario.strip():
            await update.message.reply_text("No pude entender el audio. Intenta de nuevo.")
            return
        log.info(f"Transcripción ({transcripcion['duracion']}s): {texto_usuario[:80]}")
        await update.message.reply_text(f"🎤 Escuché: <i>{texto_usuario}</i>", parse_mode="HTML")
    except Exception as e:
        log.error(f"Error en Whisper: {e}")
        await update.message.reply_text(f"Error transcribiendo audio: {e}")
        return
    finally:
        # Limpiar archivo temporal
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # 3. Procesar con LLM (mismo flujo que mensaje_libre)
    await update.message.chat.send_action("typing")
    try:
        ctx_memoria = ""
        try:
            ctx_memoria = memoria.obtener_contexto_memoria(texto_usuario, n=3)
        except Exception as e:
            log.warning(f"Error buscando memoria: {e}")

        if _es_para_brain(texto_usuario):
            sym = _detectar_simbolo(texto_usuario)
            contexto = obtener_contexto_brain(sym)
            log.info(f"Contexto brain para {sym or 'general'} ({len(contexto)} chars)")
        else:
            contexto = obtener_contexto_real()
        contexto_completo = f"Datos reales ahora mismo:\n{contexto}"
        if ctx_memoria:
            contexto_completo += f"\n\n{ctx_memoria}"

        resultado = route_message(
            texto_usuario,
            contexto=contexto_completo,
            system_prompt=SYSTEM_PROMPT + "\nTu respuesta será convertida a voz, así que sé conciso y natural. Máximo 3 oraciones.",
        )

        if resultado["error"]:
            await update.message.reply_text(resultado["respuesta"])
            return

        respuesta = resultado["respuesta"]

        # Guardar en memoria
        try:
            memoria.guardar_conversacion(
                pregunta=texto_usuario,
                respuesta=respuesta,
                modelo=resultado.get("modelo", ""),
                nodo=resultado.get("nodo", ""),
                tiempo=resultado.get("tiempo", 0),
            )
        except Exception as e:
            log.warning(f"Error guardando en memoria: {e}")

        # Truncar si excede límite
        if len(respuesta) > 4000:
            respuesta = respuesta[:4000] + "\n\n[respuesta truncada]"

        meta = f"\n\n⚙️ {resultado['modelo']} @ {resultado['nodo']} ({resultado['tiempo']}s)"
        if resultado["fallback"]:
            meta += " [fallback]"

        texto_enviar = respuesta
        if len(respuesta) + len(meta) <= 4096:
            texto_enviar += meta

        # 4. Enviar texto
        await update.message.reply_text(texto_enviar)

        # 5. Sintetizar y enviar audio de respuesta
        await update.message.chat.send_action("record_voice")
        await _enviar_audio(update, respuesta)

    except Exception as e:
        log.error(f"Error procesando voz: {e}")
        await update.message.reply_text(f"Error procesando tu mensaje de voz: {e}")


# ── Mensajes libres (IA) ────────────────────────────────────

async def mensaje_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return

    texto_usuario = update.message.text
    log.info(f"Mensaje de Hugo: {texto_usuario[:80]}")

    # Indicar que estamos escribiendo
    await update.message.chat.send_action("typing")

    try:
        # Buscar conversaciones previas relevantes en memoria
        ctx_memoria = ""
        try:
            ctx_memoria = memoria.obtener_contexto_memoria(texto_usuario, n=3)
            if ctx_memoria:
                log.info(f"Memoria inyectada ({len(ctx_memoria)} chars)")
        except Exception as e:
            log.warning(f"Error buscando memoria: {e}")

        # Contexto reducido para brain (671B con 4096 tokens), completo para el resto
        if _es_para_brain(texto_usuario):
            sym = _detectar_simbolo(texto_usuario)
            contexto = obtener_contexto_brain(sym)
            log.info(f"Contexto brain para {sym or 'general'} ({len(contexto)} chars)")
        else:
            contexto = obtener_contexto_real()
            log.info(f"Contexto completo ({len(contexto)} chars)")

        contexto_completo = f"Datos reales ahora mismo:\n{contexto}"
        if ctx_memoria:
            contexto_completo += f"\n\n{ctx_memoria}"

        resultado = route_message(
            texto_usuario,
            contexto=contexto_completo,
            system_prompt=SYSTEM_PROMPT,
        )

        if resultado["error"]:
            await update.message.reply_text(resultado["respuesta"])
            return

        respuesta = resultado["respuesta"]

        # Guardar conversacion en memoria
        try:
            memoria.guardar_conversacion(
                pregunta=texto_usuario,
                respuesta=respuesta,
                modelo=resultado.get("modelo", ""),
                nodo=resultado.get("nodo", ""),
                tiempo=resultado.get("tiempo", 0),
            )
        except Exception as e:
            log.warning(f"Error guardando en memoria: {e}")

        # Cortar si excede límite Telegram
        if len(respuesta) > 4000:
            respuesta = respuesta[:4000] + "\n\n[respuesta truncada]"

        # Agregar info del modelo usado
        meta = f"\n\n⚙️ {resultado['modelo']} @ {resultado['nodo']} ({resultado['tiempo']}s)"
        if resultado["fallback"]:
            meta += " [fallback]"

        if len(respuesta) + len(meta) <= 4096:
            respuesta += meta

        await update.message.reply_text(respuesta)

        # Si modo voz activo, también enviar audio
        if update.effective_chat.id in _modo_voz:
            await update.message.chat.send_action("record_voice")
            await _enviar_audio(update, resultado["respuesta"])
    except Exception as e:
        log.error(f"Error router: {e}")
        await update.message.reply_text(f"Error procesando tu mensaje: {e}")


# ── Main ────────────────────────────────────────────��───────

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN no configurado en .env")
        sys.exit(1)

    log.info("Iniciando JARVIS Telegram Bot...")
    log.info(f"  Chat autorizado: {CHAT_ID}")
    estado = health_check()
    for nodo_id, info in estado.items():
        status = "OK" if info["disponible"] else "NO DISPONIBLE"
        log.info(f"  {info['nombre']}: {info['modelo']} — {status}")
    mem_stats = memoria.stats()
    log.info(f"  Memoria: {mem_stats['conversaciones']} conversaciones, "
             f"{mem_stats['decisiones_trading']} decisiones trading")

    app = ApplicationBuilder().token(TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CommandHandler("start", cmd_ayuda))
    app.add_handler(CommandHandler("help", cmd_ayuda))
    app.add_handler(CommandHandler("mercado", cmd_mercado))
    app.add_handler(CommandHandler("portafolio", cmd_portafolio))
    app.add_handler(CommandHandler("cripto", cmd_cripto))
    app.add_handler(CommandHandler("voz", cmd_voz))
    app.add_handler(CommandHandler("texto", cmd_texto))

    # Mensajes de voz
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, mensaje_voz))

    # Mensajes libres (texto)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_libre))

    log.info("Bot listo. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
