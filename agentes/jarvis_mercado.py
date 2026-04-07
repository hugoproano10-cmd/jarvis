#!/home/hproano/asistente_env/bin/python
"""
Agente JARVIS de mercado.
Ejecuta el monitor, envía los datos a JARVIS (OpenClaw API),
obtiene un análisis inteligente y lo envía a Telegram.
"""

import os
import sys
import subprocess
import requests
from dotenv import load_dotenv
from datetime import datetime

# Rutas del proyecto
PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from config.alertas import enviar_telegram

load_dotenv(os.path.join(PROYECTO, ".env"))

OPENCLAW_URL = "http://localhost:18789/v1/chat/completions"
PYTHON = os.path.join(os.path.expanduser("~"), "asistente_env", "bin", "python")
MONITOR_SCRIPT = os.path.join(PROYECTO, "trading", "monitor_mercado.py")

SYSTEM_PROMPT = """\
Eres JARVIS, un analista financiero experto. Recibirás datos del mercado \
(acciones estadounidenses y criptomonedas) y debes generar un comentario \
inteligente en español.

Tu análisis debe incluir:
1. Evaluación general del día (alcista, bajista, mixto)
2. Qué activos destacan y por qué podrían estar moviéndose así
3. Relación entre mercado tradicional y cripto
4. Una recomendación general breve (sin ser asesoría financiera formal)

Reglas:
- Escribe en español, tono profesional pero accesible
- Sé conciso: máximo 3-4 párrafos
- Usa datos específicos del reporte para respaldar tu análisis
- No repitas los números en formato tabla, interprétalos
- Cierra con una frase memorable tipo analista de mercado\
"""


def ejecutar_monitor() -> str:
    """Ejecuta monitor_mercado.py y captura su salida."""
    resultado = subprocess.run(
        [PYTHON, MONITOR_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
    )
    salida = resultado.stdout
    if resultado.returncode != 0:
        salida += f"\n[STDERR]: {resultado.stderr}"
    return salida


def consultar_jarvis(datos_mercado: str) -> str:
    """Envía datos a JARVIS via OpenClaw y obtiene análisis."""
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Analiza el siguiente reporte de mercado de hoy "
                    f"{datetime.now().strftime('%d/%m/%Y %H:%M')} y genera "
                    f"tu comentario:\n\n{datos_mercado}"
                ),
            },
        ],
        "temperature": 0.7,
    }

    resp = requests.post(OPENCLAW_URL, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    return data["choices"][0]["message"]["content"]


def guardar_log(datos_mercado: str, analisis: str) -> str:
    """Guarda el análisis de JARVIS en logs."""
    dir_logs = os.path.join(PROYECTO, "logs")
    os.makedirs(dir_logs, exist_ok=True)

    fecha = datetime.now().strftime("%Y-%m-%d")
    ruta = os.path.join(dir_logs, f"jarvis_{fecha}.txt")

    with open(ruta, "a", encoding="utf-8") as f:
        f.write(f"=== {datetime.now().strftime('%H:%M:%S')} ===\n")
        f.write(f"{analisis}\n\n")

    return ruta


def generar_analisis_local(datos_mercado: str) -> str:
    """Genera un análisis básico sin LLM, parseando la salida del monitor."""
    lineas = datos_mercado.split("\n")

    # Extraer variaciones del texto
    activos = []
    nombre_actual = None
    for linea in lineas:
        stripped = linea.strip()
        if stripped.startswith("Variación día"):
            partes = stripped.split()
            for p in partes:
                p_clean = p.replace("%", "").replace("+", "")
                try:
                    var = float(p_clean)
                    activos.append((nombre_actual or "?", var))
                    break
                except ValueError:
                    continue
        elif stripped and not stripped.startswith(("Precio", "Apertura", "Cierre", "---", "===", "Sent", "Varia", "Activ", "Prom", "Mayor", "*", "Fuent", "Error", "Log", "Consul")):
            if not any(c in stripped for c in [":", "=", "|"]):
                nombre_actual = stripped

    if not activos:
        return "No se pudieron extraer datos suficientes para el análisis."

    positivos = [(n, v) for n, v in activos if v > 0]
    negativos = [(n, v) for n, v in activos if v < 0]
    promedio = sum(v for _, v in activos) / len(activos)

    parrafos = []

    # Evaluación general
    if promedio > 1:
        parrafos.append("Jornada claramente alcista en los mercados.")
    elif promedio > 0:
        parrafos.append("Sesión mixta con ligero sesgo positivo.")
    elif promedio > -1:
        parrafos.append("Sesión mixta con presión vendedora moderada.")
    else:
        parrafos.append("Jornada bajista con ventas generalizadas.")

    # Destacados
    if negativos:
        peor = min(negativos, key=lambda x: x[1])
        parrafos.append(f"La mayor caída es {peor[0]} con {peor[1]:+.2f}%.")
    if positivos:
        mejor = max(positivos, key=lambda x: x[1])
        parrafos.append(f"Destaca {mejor[0]} al alza con {mejor[1]:+.2f}%.")

    # Divergencia
    acc = [v for n, v in activos if "BTC" not in n and "ETH" not in n and "BNB" not in n]
    cry = [v for n, v in activos if "BTC" in n or "ETH" in n or "BNB" in n]
    if acc and cry:
        prom_a = sum(acc) / len(acc)
        prom_c = sum(cry) / len(cry)
        if abs(prom_a - prom_c) > 2:
            if prom_c > prom_a:
                parrafos.append("Las criptomonedas muestran fortaleza relativa frente a las acciones, posible rotación de capital hacia activos digitales.")
            else:
                parrafos.append("Las acciones superan a las criptomonedas, sugiriendo preferencia por activos tradicionales hoy.")

    parrafos.append("Como siempre, los mercados premian la paciencia y castigan la impulsividad.")

    return " ".join(parrafos)


def main():
    modo_test = len(sys.argv) > 1 and sys.argv[1] == "--test"

    # 1) Ejecutar monitor y capturar datos
    print("Ejecutando monitor de mercado...")
    datos_mercado = ejecutar_monitor()
    if not datos_mercado.strip():
        print("Error: el monitor no devolvió datos.")
        sys.exit(1)
    print("  Datos obtenidos.\n")

    # 2) Obtener análisis
    if modo_test:
        print("Modo prueba: generando análisis local...\n")
        analisis = generar_analisis_local(datos_mercado)
    else:
        print("Consultando a JARVIS (OpenClaw)...")
        try:
            analisis = consultar_jarvis(datos_mercado)
        except requests.ConnectionError:
            print("Error: no se pudo conectar a OpenClaw en localhost:18789.")
            print("  Verifica que el servicio esté corriendo.")
            print("  Usa --test para probar el flujo sin OpenClaw.")
            sys.exit(1)
        except Exception as e:
            print(f"Error al consultar JARVIS: {e}")
            sys.exit(1)
    print("  Análisis listo.\n")

    # 3) Mostrar en consola
    print("=" * 60)
    print("  ANÁLISIS JARVIS")
    print("=" * 60)
    print(analisis)
    print("=" * 60)

    # 4) Enviar a Telegram
    fecha_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    etiqueta = " (test)" if modo_test else ""
    mensaje_tg = (
        f"\U0001f916 <b>JARVIS — Análisis de Mercado{etiqueta}</b>\n"
        f"\U0001f4c5 {fecha_hora}\n\n"
        f"{analisis}"
    )
    print("\nEnviando análisis a Telegram...")
    enviar_telegram(mensaje_tg)
    print("  Enviado.")

    # 5) Guardar log
    ruta = guardar_log(datos_mercado, analisis)
    print(f"  Log guardado en: {ruta}")


if __name__ == "__main__":
    main()
