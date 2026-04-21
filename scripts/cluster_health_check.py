#!/home/hproano/asistente_env/bin/python
"""
JARVIS Cluster Health Check — Ping cruzado a los 3 nodos y servicios locales.
Se ejecuta cada 5 minutos via cron.

Servicios vigilados:
  core     — Ollama Nemotron en jarvis-core        (localhost:11434/api/tags)
  power    — Ollama DeepSeek 70B en jarvis-power   (192.168.208.80:11435/api/tags)
  finbert  — FinBERT API en jarvis-power           (192.168.208.80:8002/docs)
  brain    — Ollama DeepSeek 671B en jarvis-brain  (192.168.202.53:11436/api/tags)
  ibkr     — IB Gateway Docker (TCP)               (localhost:4001)
  wa-bot   — Node.js WhatsApp bot                  (localhost:8001)
  wa-api   — FastAPI WhatsApp API                  (localhost:8000/health)

Alertas:
  - Solo al cambiar de estado (OK→DOWN o DOWN→OK).
  - Estado persistido en /tmp/jarvis_cluster_status.json.
  - Alerta por WhatsApp (POST localhost:8001/alerta). Si el bot está caído,
    se escribe una línea CRITICAL en logs/cluster_health.log.
"""

import os
import sys
import json
import socket
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

TIMEOUT = 5
TIEMPO_MAX = 10.0

LOG_PATH = os.path.join(PROYECTO, "logs", "cluster_health.log")
ESTADO_PATH = "/tmp/jarvis_cluster_status.json"
WHATSAPP_URL = "http://localhost:8001/alerta"

# clave → (nombre_humano, descripcion_modelo/servicio)
ETIQUETAS = {
    "core":    ("jarvis-core",    "Nemotron 120B"),
    "power":   ("jarvis-power",   "DeepSeek 70B"),
    "finbert": ("jarvis-power",   "FinBERT API"),
    "brain":   ("jarvis-brain",   "DeepSeek 671B"),
    "ibkr":    ("IB Gateway",     "IBKR conexión"),
    "wa-bot":  ("WhatsApp Bot",   "Node.js bot"),
    "wa-api":  ("WhatsApp API",   "FastAPI server"),
}

# Modelo esperado por endpoint Ollama (substring, case-insensitive).
# None = no se valida modelo específico.
MODELO_ESPERADO = {
    "core":  "nemotron",
    "power": "70b",
    "brain": "671",
}


# ── Checks individuales ────────────────────────────────────

def _check_http(url, validar_modelo=None, aceptar_4xx=False):
    """Retorna (ok, tiempo_seg, detalle).

    aceptar_4xx: si True, un 4xx prueba que el proceso escucha (útil para
                 servicios sin endpoint público de health como el bot Node).
    """
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT)
        elapsed = time.time() - t0
        if r.status_code >= 500 or (r.status_code >= 400 and not aceptar_4xx):
            return False, elapsed, f"HTTP {r.status_code}"
        if elapsed > TIEMPO_MAX:
            return False, elapsed, f"timeout ({elapsed:.1f}s > {TIEMPO_MAX}s)"

        if validar_modelo:
            try:
                modelos = [m.get("name", "") for m in r.json().get("models", [])]
            except Exception:
                modelos = []
            # jarvis-brain registra el modelo como hash sha256-... sin nombre
            # humano: si hay al menos un modelo cargado, aceptar.
            if modelos and any(m.startswith("sha256-") for m in modelos):
                return True, elapsed, f"OK (modelo-hash: {modelos[0][:20]}...)"
            if modelos and not any(validar_modelo.lower() in m.lower() for m in modelos):
                return False, elapsed, f"modelo esperado '{validar_modelo}' no cargado (hay: {modelos[:3]})"
        return True, elapsed, "OK"
    except requests.Timeout:
        return False, time.time() - t0, "timeout"
    except requests.ConnectionError as e:
        return False, time.time() - t0, f"conn-refused: {str(e)[:60]}"
    except Exception as e:
        return False, time.time() - t0, str(e)[:80]


def _check_tcp(host, puerto):
    """TCP connect test (para servicios no-HTTP como IB Gateway)."""
    t0 = time.time()
    try:
        with socket.create_connection((host, puerto), timeout=TIMEOUT) as _:
            return True, time.time() - t0, "OK"
    except Exception as e:
        return False, time.time() - t0, str(e)[:80]


def run_checks():
    resultados = {}

    ok, t, det = _check_http("http://localhost:11434/api/tags",
                              MODELO_ESPERADO.get("core"))
    resultados["core"] = {"ok": ok, "t": t, "detalle": det}

    ok, t, det = _check_http("http://192.168.208.80:11435/api/tags",
                              MODELO_ESPERADO.get("power"))
    resultados["power"] = {"ok": ok, "t": t, "detalle": det}

    ok, t, det = _check_http("http://192.168.208.80:8002/docs")
    resultados["finbert"] = {"ok": ok, "t": t, "detalle": det}

    ok, t, det = _check_http("http://192.168.202.53:11436/api/tags",
                              MODELO_ESPERADO.get("brain"))
    resultados["brain"] = {"ok": ok, "t": t, "detalle": det}

    ok, t, det = _check_tcp("localhost", 4001)
    resultados["ibkr"] = {"ok": ok, "t": t, "detalle": det}

    # wa-bot: GET a la raíz. Cualquier respuesta HTTP (incluso 404) prueba
    # que el proceso escucha; solo conn-refused indica caída.
    ok, t, det = _check_http("http://localhost:8001/", aceptar_4xx=True)
    resultados["wa-bot"] = {"ok": ok, "t": t, "detalle": det}

    ok, t, det = _check_http("http://localhost:8000/health")
    resultados["wa-api"] = {"ok": ok, "t": t, "detalle": det}

    return resultados


# ── Estado persistido ──────────────────────────────────────

def cargar_estado():
    if not os.path.exists(ESTADO_PATH):
        return {}
    try:
        with open(ESTADO_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(estado):
    try:
        with open(ESTADO_PATH, "w") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  No se pudo guardar estado: {e}")


# ── Alertas ────────────────────────────────────────────────

def _enviar_whatsapp(mensaje):
    """Retorna True si la alerta se pudo enviar."""
    try:
        r = requests.post(WHATSAPP_URL, json={"mensaje": mensaje}, timeout=5)
        return r.status_code < 400
    except Exception:
        return False


def _log_linea(linea):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def _formatear_log(resultados, ts):
    partes = []
    for clave in ["core", "power", "finbert", "brain", "ibkr", "wa-bot", "wa-api"]:
        r = resultados[clave]
        estado = "OK" if r["ok"] else "DOWN"
        partes.append(f"{clave}:{estado}({r['t']:.1f}s)")
    return f"{ts} | " + " ".join(partes)


def procesar_transiciones(resultados, estado_prev):
    """
    Compara resultados actuales vs estado previo.
    Emite alertas solo en transiciones. Actualiza estado_prev in-place.
    Retorna lista de (clave, tipo, mensaje) para los cambios.
    """
    ahora_hm = datetime.now().strftime("%H:%M")
    ahora_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cambios = []

    for clave, r in resultados.items():
        prev = estado_prev.get(clave, {})
        prev_ok = prev.get("ok")  # None en primera ejecución

        nodo, modelo = ETIQUETAS[clave]

        if prev_ok is None:
            # Primera vez que se ve el servicio: inicializar sin alertar.
            estado_prev[clave] = {
                "ok": r["ok"],
                "desde": ahora_full,
                "detalle": r["detalle"],
            }
            continue

        if prev_ok and not r["ok"]:
            msg = (f"\u26a0\ufe0f JARVIS CLUSTER: {nodo} NO RESPONDE \u2014 "
                   f"{modelo} offline desde {ahora_hm} ({r['detalle']})")
            cambios.append((clave, "DOWN", msg))
            estado_prev[clave] = {
                "ok": False,
                "desde": ahora_full,
                "detalle": r["detalle"],
            }

        elif (not prev_ok) and r["ok"]:
            desde = prev.get("desde", "?")
            msg = (f"\u2705 JARVIS CLUSTER: {nodo} RECUPERADO \u2014 "
                   f"{modelo} online (caído desde {desde})")
            cambios.append((clave, "UP", msg))
            estado_prev[clave] = {
                "ok": True,
                "desde": ahora_full,
                "detalle": r["detalle"],
            }

        else:
            # Sin cambio, solo refresca detalle
            estado_prev[clave]["detalle"] = r["detalle"]
            estado_prev[clave]["ok"] = r["ok"]

    return cambios


# ── Main ───────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resultados = run_checks()
    linea_log = _formatear_log(resultados, ts)
    print(linea_log)

    # Transiciones
    estado_prev = cargar_estado()
    cambios = procesar_transiciones(resultados, estado_prev)
    guardar_estado(estado_prev)

    # Log normal
    _log_linea(linea_log)

    # Alertas por transición
    for clave, tipo, msg in cambios:
        print(f"  ALERTA ({tipo}) {clave}: {msg}")
        enviado = _enviar_whatsapp(msg)
        if not enviado:
            _log_linea(f"{ts} | CRITICAL | WhatsApp no disponible | {msg}")


if __name__ == "__main__":
    main()
