#!/home/hproano/asistente_env/bin/python
"""
Sistema de alertas por Telegram.
Lee datos del monitor de mercado, envía alertas si algún activo
varía más del 2%, y envía el resumen diario completo.
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Agregar raíz del proyecto al path para importar trading.monitor_mercado
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from trading.monitor_mercado import (
    ACCIONES,
    CRIPTOS_BINANCE,
    NOMBRE_CRIPTO,
    obtener_accion,
    obtener_cripto_binance,
    construir_resumen,
    formatear_variacion,
    guardar_log,
)

# Cargar variables de entorno
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

UMBRAL_ALERTA = 2.0  # porcentaje


def enviar_telegram(mensaje: str) -> bool:
    """Envía un mensaje por Telegram. Intenta HTML, fallback a texto plano."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram: BOT_TOKEN o CHAT_ID no configurados")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        # HTML mal formado — reintentar sin parse_mode
        print(f"Telegram HTML error {resp.status_code}: {resp.text[:200]}")
        payload.pop("parse_mode")
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"Telegram texto plano error {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
    return resp.json().get("ok", False)


def generar_alertas(datos: list[dict]) -> list[str]:
    """Genera mensajes de alerta para activos que superen el umbral."""
    alertas = []
    for d in datos:
        variacion = d["variacion"]
        if abs(variacion) >= UMBRAL_ALERTA:
            flecha = "\u2b06\ufe0f" if variacion >= 0 else "\u2b07\ufe0f"
            tipo = "SUBE" if variacion >= 0 else "BAJA"
            alertas.append(
                f"{flecha} <b>ALERTA {tipo}:</b> {d['simbolo']}\n"
                f"   Precio: ${d['precio']:,.2f} USD\n"
                f"   Variacion: {formatear_variacion(variacion)}"
            )
    return alertas


def ejecutar_alertas():
    """Consulta el mercado, envía alertas y resumen completo por Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Error: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados en .env")
        sys.exit(1)

    print("Consultando precios de mercado...\n")

    datos_acciones = []
    datos_criptos = []
    errores = []

    for simbolo in ACCIONES:
        try:
            datos_acciones.append(obtener_accion(simbolo))
        except Exception as e:
            errores.append(f"{simbolo}: {e}")

    for par in CRIPTOS_BINANCE:
        try:
            datos_criptos.append(obtener_cripto_binance(par))
        except Exception as e:
            errores.append(f"{NOMBRE_CRIPTO.get(par, par)}: {e}")

    todos = datos_acciones + datos_criptos

    # 1) Enviar alertas individuales por activos que superen el umbral
    alertas = generar_alertas(todos)
    if alertas:
        encabezado = f"\U0001f6a8 <b>ALERTAS DE MERCADO</b> ({len(alertas)} activo(s) con movimiento > {UMBRAL_ALERTA}%)\n"
        mensaje_alertas = encabezado + "\n" + "\n\n".join(alertas)
        print("Enviando alertas por Telegram...")
        enviar_telegram(mensaje_alertas)
        print(f"  {len(alertas)} alerta(s) enviada(s).")
    else:
        print("Sin alertas: ningun activo supera el umbral de variacion.")

    # 2) Enviar resumen diario completo
    resumen = construir_resumen(datos_acciones, datos_criptos, errores)
    mensaje_resumen = f"\U0001f4ca <b>RESUMEN DIARIO DE MERCADO</b>\n\n<pre>{resumen}</pre>"
    print("\nEnviando resumen diario por Telegram...")
    enviar_telegram(mensaje_resumen)
    print("  Resumen enviado.")

    # 3) Guardar log
    ruta_log = guardar_log(resumen)
    print(f"\n  Log guardado en: {ruta_log}")

    # Mostrar resumen en consola
    print(f"\n{resumen}")


def enviar_prueba():
    """Envía un mensaje de prueba para verificar la configuracion."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Error: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados en .env")
        sys.exit(1)

    mensaje = (
        "\u2705 <b>Prueba exitosa</b>\n\n"
        "El sistema de alertas de mercado esta conectado correctamente.\n"
        "Recibiras:\n"
        "  - Alertas cuando un activo varie mas del 2%\n"
        "  - Resumen diario completo del mercado\n\n"
        "Activos monitoreados:\n"
        "  Acciones: AAPL, TSLA, SPY, NVDA\n"
        "  Criptos: BTC, ETH, BNB"
    )
    print("Enviando mensaje de prueba por Telegram...")
    ok = enviar_telegram(mensaje)
    if ok:
        print("  Mensaje de prueba enviado exitosamente!")
    else:
        print("  Error al enviar mensaje de prueba.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--prueba":
        enviar_prueba()
    else:
        ejecutar_alertas()
