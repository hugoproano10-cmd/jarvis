#!/home/hproano/asistente_env/bin/python
"""
Monitor de mercado: acciones y criptomonedas.
Consulta precios actuales y variación porcentual del día.
Genera análisis de sentimiento y guarda log automáticamente.
"""

import os
import sys
import yfinance as yf
import requests
from datetime import datetime

ACCIONES = ["AAPL", "TSLA", "SPY", "NVDA"]
CRIPTOS_BINANCE = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

NOMBRE_CRIPTO = {
    "BTCUSDT": "Bitcoin (BTC/USDT)",
    "ETHUSDT": "Ethereum (ETH/USDT)",
    "BNBUSDT": "BNB (BNB/USDT)",
}

LINEA = "=" * 60


def obtener_accion(simbolo: str) -> dict:
    """Obtiene precio actual y variación del día para una acción."""
    ticker = yf.Ticker(simbolo)
    info = ticker.fast_info

    precio_actual = info.last_price
    precio_apertura = info.open
    precio_cierre_anterior = info.previous_close

    if precio_cierre_anterior and precio_cierre_anterior > 0:
        variacion = ((precio_actual - precio_cierre_anterior) / precio_cierre_anterior) * 100
    else:
        variacion = 0.0

    return {
        "simbolo": simbolo,
        "precio": precio_actual,
        "apertura": precio_apertura,
        "cierre_anterior": precio_cierre_anterior,
        "variacion": variacion,
    }


def obtener_cripto_binance(par: str) -> dict:
    """Obtiene precio actual de una cripto desde la API pública de Binance."""
    url_stats = f"https://api.binance.com/api/v3/ticker/24hr?symbol={par}"

    resp = requests.get(url_stats, timeout=10)
    resp.raise_for_status()
    stats = resp.json()

    precio_actual = float(stats["lastPrice"])
    precio_apertura = float(stats["openPrice"])
    variacion = float(stats["priceChangePercent"])

    return {
        "simbolo": NOMBRE_CRIPTO.get(par, par),
        "precio": precio_actual,
        "apertura": precio_apertura,
        "cierre_anterior": None,
        "variacion": variacion,
    }


def formatear_variacion(variacion: float) -> str:
    signo = "+" if variacion >= 0 else ""
    return f"{signo}{variacion:.2f}%"


def analizar_sentimiento(datos: list[dict]) -> list[str]:
    """Genera un análisis breve de sentimiento basado en los datos."""
    variaciones = [d["variacion"] for d in datos]
    promedio = sum(variaciones) / len(variaciones) if variaciones else 0
    positivos = sum(1 for v in variaciones if v > 0)
    negativos = sum(1 for v in variaciones if v < 0)
    total = len(variaciones)

    # Sentimiento general
    if promedio > 2:
        sentimiento = "MUY ALCISTA"
    elif promedio > 0.5:
        sentimiento = "ALCISTA"
    elif promedio > -0.5:
        sentimiento = "NEUTRAL"
    elif promedio > -2:
        sentimiento = "BAJISTA"
    else:
        sentimiento = "MUY BAJISTA"

    lineas = []
    lineas.append(f"  Sentimiento general : {sentimiento}")
    lineas.append(f"  Variación promedio  : {formatear_variacion(promedio)}")
    lineas.append(f"  Activos al alza     : {positivos}/{total}")
    lineas.append(f"  Activos a la baja   : {negativos}/{total}")

    # Detalle acciones vs criptos
    acciones = [d for d in datos if d["cierre_anterior"] is not None]
    criptos = [d for d in datos if d["cierre_anterior"] is None]

    if acciones:
        prom_acc = sum(d["variacion"] for d in acciones) / len(acciones)
        lineas.append(f"  Promedio acciones   : {formatear_variacion(prom_acc)}")
    if criptos:
        prom_cry = sum(d["variacion"] for d in criptos) / len(criptos)
        lineas.append(f"  Promedio criptos    : {formatear_variacion(prom_cry)}")

    # Mayor ganador y perdedor
    mejor = max(datos, key=lambda d: d["variacion"])
    peor = min(datos, key=lambda d: d["variacion"])
    lineas.append(f"  Mayor alza          : {mejor['simbolo']} ({formatear_variacion(mejor['variacion'])})")
    lineas.append(f"  Mayor baja          : {peor['simbolo']} ({formatear_variacion(peor['variacion'])})")

    # Observaciones
    lineas.append("")
    if promedio < -2:
        lineas.append("  * Jornada de fuertes caídas. Considerar cautela.")
    elif promedio < -0.5:
        lineas.append("  * Sesión con tendencia negativa. Monitorear soportes.")
    elif promedio > 2:
        lineas.append("  * Rally generalizado. Vigilar posibles correcciones.")
    elif promedio > 0.5:
        lineas.append("  * Sesión positiva. El mercado muestra fortaleza.")
    else:
        lineas.append("  * Mercado sin dirección clara. Esperar confirmación.")

    if acciones and criptos:
        prom_acc = sum(d["variacion"] for d in acciones) / len(acciones)
        prom_cry = sum(d["variacion"] for d in criptos) / len(criptos)
        diff = abs(prom_acc - prom_cry)
        if diff > 3:
            if prom_cry > prom_acc:
                lineas.append("  * Divergencia notable: criptos superan a acciones.")
            else:
                lineas.append("  * Divergencia notable: acciones superan a criptos.")

    return lineas


def construir_resumen(datos_acciones: list[dict], datos_criptos: list[dict],
                      errores: list[str]) -> str:
    """Construye el texto completo del resumen."""
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lineas = []

    lineas.append(LINEA)
    lineas.append(f"  RESUMEN DE MERCADO  —  {ahora}")
    lineas.append(LINEA)

    # Acciones
    if datos_acciones:
        lineas.append("\n  --- ACCIONES ---")
        for d in datos_acciones:
            flecha = "▲" if d["variacion"] >= 0 else "▼"
            lineas.append(f"\n  {d['simbolo']}")
            lineas.append(f"    Precio actual : ${d['precio']:,.2f} USD")
            lineas.append(f"    Apertura      : ${d['apertura']:,.2f} USD")
            if d["cierre_anterior"]:
                lineas.append(f"    Cierre ayer   : ${d['cierre_anterior']:,.2f} USD")
            lineas.append(f"    Variación día : {flecha} {formatear_variacion(d['variacion'])}")

    # Criptomonedas
    if datos_criptos:
        lineas.append(f"\n  --- CRIPTOMONEDAS ---")
        for d in datos_criptos:
            flecha = "▲" if d["variacion"] >= 0 else "▼"
            lineas.append(f"\n  {d['simbolo']}")
            lineas.append(f"    Precio actual : ${d['precio']:,.2f} USD")
            lineas.append(f"    Apertura 24h  : ${d['apertura']:,.2f} USD")
            lineas.append(f"    Variación día : {flecha} {formatear_variacion(d['variacion'])}")

    # Sentimiento
    todos = datos_acciones + datos_criptos
    if todos:
        lineas.append(f"\n  --- ANÁLISIS DE SENTIMIENTO ---")
        lineas.extend(analizar_sentimiento(todos))

    lineas.append("\n" + LINEA)
    lineas.append("  Fuentes: Yahoo Finance (acciones) | Binance (criptos)")
    lineas.append(LINEA)

    if errores:
        lineas.append("\n  Errores al consultar:")
        for err in errores:
            lineas.append(f"    - {err}")

    return "\n".join(lineas)


def guardar_log(contenido: str) -> str:
    """Guarda el resumen en logs/mercado_FECHA.txt y retorna la ruta."""
    dir_logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    os.makedirs(dir_logs, exist_ok=True)

    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_logs, f"mercado_{fecha}.txt")

    # Agregar al archivo si ya existe (múltiples ejecuciones en el día)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(contenido + "\n\n")

    return os.path.abspath(ruta)


def main():
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

    resumen = construir_resumen(datos_acciones, datos_criptos, errores)
    print(resumen)

    ruta_log = guardar_log(resumen)
    print(f"\n  Log guardado en: {ruta_log}")


if __name__ == "__main__":
    main()
