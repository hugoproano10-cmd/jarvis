#!/home/hproano/asistente_env/bin/python
"""
Monitor de sistema JARVIS — Diagnóstico diario completo.
Verifica cluster, modelos, servicios, APIs, trading y memoria.
Envía reporte por Telegram. Se ejecuta diariamente a las 9AM.
"""

import os
import sys
import time
import subprocess
import shutil
from datetime import datetime

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FRED_KEY = os.getenv("FRED_API_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
AV_KEY = os.getenv("ALPHA_VANTAGE_PREMIUM_KEY", "")
TIINGO_KEY = os.getenv("TIINGO_API_KEY", "")

OK = "\u2705"
FAIL = "\U0001f534"
WARN = "\u26a0\ufe0f"
TIMEOUT = 30


def _check(nombre, fn):
    """Ejecuta fn(), retorna (ok, detalle, tiempo)."""
    t0 = time.time()
    try:
        resultado = fn()
        elapsed = round(time.time() - t0, 2)
        if resultado is True or resultado == "ok":
            return True, f"{elapsed}s", elapsed
        if isinstance(resultado, str):
            return True, f"{resultado} ({elapsed}s)", elapsed
        return True, f"{elapsed}s", elapsed
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return False, str(e)[:80], elapsed


# ══════════════════════════════════════════════════════════════
#  1. CLUSTER
# ══════════════════════════════════════════════════════════════

def check_cluster():
    resultados = []

    # jarvis-core
    ok, det, t = _check("jarvis-core", lambda: (
        requests.get("http://localhost:11434/api/tags", timeout=TIMEOUT).status_code == 200
    ))
    resultados.append(("jarvis-core (Ollama)", ok, det))

    # jarvis-power
    ok, det, t = _check("jarvis-power", lambda: (
        requests.get("http://192.168.208.80:11435/api/tags", timeout=TIMEOUT).status_code == 200
    ))
    resultados.append(("jarvis-power (Ollama)", ok, det))

    # jarvis-brain
    ok, det, t = _check("jarvis-brain", lambda: (
        requests.get("http://192.168.202.53:11436/health", timeout=TIMEOUT).status_code == 200
    ))
    resultados.append(("jarvis-brain (llama.cpp)", ok, det))

    # Modelos cargados en core
    try:
        ps = requests.get("http://localhost:11434/api/ps", timeout=5).json()
        modelos = [f"{m['name']} ({m.get('expires_at', 'forever')[:10]})"
                   for m in ps.get("models", [])]
        resultados.append(("Modelos cargados", True, ", ".join(modelos) or "ninguno"))
    except Exception as e:
        resultados.append(("Modelos cargados", False, str(e)[:60]))

    return resultados


# ══════════════════════════════════════════════════════════════
#  2. MODELOS — prueba de respuesta
# ══════════════════════════════════════════════════════════════

def check_modelos():
    resultados = []

    # Super 120B en core
    def test_super():
        payload = {"model": "nemotron-3-super", "messages": [
            {"role": "user", "content": "Responde solo: OK"}
        ], "stream": False, "options": {"temperature": 0.1}}
        r = requests.post("http://localhost:11434/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        return f"OK ({len(r.json()['message']['content'])} chars)"

    ok, det, t = _check("Super 120B", test_super)
    resultados.append((f"nemotron-3-super", ok, f"{det} {t}s"))

    # Nano 30B en power
    def test_nano():
        payload = {"model": "nemotron-3-nano:30b", "messages": [
            {"role": "user", "content": "Responde solo: OK"}
        ], "stream": False, "options": {"temperature": 0.1}}
        r = requests.post("http://192.168.208.80:11435/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        return f"OK ({len(r.json()['message']['content'])} chars)"

    ok, det, t = _check("Nano 30B", test_nano)
    resultados.append((f"nemotron-3-nano:30b", ok, f"{det} {t}s"))

    # 671B — solo health, no generar
    ok, det, t = _check("671B health", lambda: (
        requests.get("http://192.168.202.53:11436/health", timeout=10).status_code == 200
    ))
    resultados.append(("deepseek-r1:671b (health only)", ok, det))

    return resultados


# ══════════════════════════════════════════════════════════════
#  3. SERVICIOS SYSTEMD
# ══════════════════════════════════════════════════════════════

def check_servicios():
    resultados = []
    servicios_locales = ["jarvis-bot", "jarvis-voz", "ollama"]

    for svc in servicios_locales:
        # Probar user-level, luego system-level
        estado = "unknown"
        for cmd in [["systemctl", "--user", "is-active", f"{svc}.service"],
                    ["systemctl", "is-active", f"{svc}.service"]]:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                estado = r.stdout.strip()
                if estado == "active":
                    break
            except Exception:
                continue
        resultados.append((svc, estado == "active", estado))

    # Dashboard — check port
    try:
        r = requests.get("http://localhost:8501/health", timeout=5)
        resultados.append(("jarvis-dashboard:8501", True, "responding"))
    except Exception:
        try:
            r = requests.get("http://localhost:8501/", timeout=5)
            resultados.append(("jarvis-dashboard:8501", r.status_code == 200, f"HTTP {r.status_code}"))
        except Exception:
            resultados.append(("jarvis-dashboard:8501", False, "no response"))

    # Brain remote
    try:
        r = requests.get("http://192.168.202.53:11436/health", timeout=10)
        resultados.append(("jarvis-brain (remoto)", r.status_code == 200, "healthy"))
    except Exception as e:
        resultados.append(("jarvis-brain (remoto)", False, str(e)[:60]))

    return resultados


# ══════════════════════════════════════════════════════════════
#  4. APIS DE DATOS
# ══════════════════════════════════════════════════════════════

def check_apis():
    resultados = []

    # FRED
    def test_fred():
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": "FEDFUNDS", "api_key": FRED_KEY,
                                 "file_type": "json", "sort_order": "desc", "limit": 1},
                         timeout=TIMEOUT)
        r.raise_for_status()
        val = r.json()["observations"][0]["value"]
        return f"Fed Funds: {val}%"
    ok, det, _ = _check("FRED", test_fred)
    resultados.append(("FRED API", ok, det))

    # Finnhub
    def test_finnhub():
        r = requests.get("https://finnhub.io/api/v1/company-news",
                         params={"symbol": "XOM", "from": "2026-04-01", "to": "2026-04-04",
                                 "token": FINNHUB_KEY}, timeout=TIMEOUT)
        r.raise_for_status()
        return f"{len(r.json())} noticias"
    ok, det, _ = _check("Finnhub", test_finnhub)
    resultados.append(("Finnhub", ok, det))

    # Alpha Vantage
    def test_av():
        r = requests.get("https://www.alphavantage.co/query",
                         params={"function": "EARNINGS", "symbol": "XOM", "apikey": AV_KEY},
                         timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        n = len(data.get("quarterlyEarnings", []))
        return f"{n} trimestres"
    ok, det, _ = _check("Alpha Vantage", test_av)
    resultados.append(("Alpha Vantage", ok, det))

    # Tiingo
    def test_tiingo():
        r = requests.get("https://api.tiingo.com/iex/XOM/prices",
                         headers={"Authorization": f"Token {TIINGO_KEY}"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        precio = r.json()[0].get("last", 0) if r.json() else 0
        return f"XOM: ${precio:.2f}"
    ok, det, _ = _check("Tiingo", test_tiingo)
    resultados.append(("Tiingo", ok, det))

    # Binance Testnet
    def test_binance():
        sys.path.insert(0, PROYECTO)
        from cripto.jarvis_cripto import obtener_precio, PARES
        p = obtener_precio(PARES[0])
        return f"BTCUSDT: ${p:,.2f}"
    ok, det, _ = _check("Binance", test_binance)
    resultados.append(("Binance Testnet", ok, det))

    # Alpaca
    def test_alpaca():
        sys.path.insert(0, PROYECTO)
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("alpaca",
               os.path.join(os.path.expanduser("~"), "trading", "alpaca_client.py"))
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bal = mod.get_balance()
        return f"equity: ${float(bal['equity']):,.2f}"
    ok, det, _ = _check("Alpaca", test_alpaca)
    resultados.append(("Alpaca Paper", ok, det))

    return resultados


# ══════════════════════════════════════════════════════════════
#  5. TRADING
# ══════════════════════════════════════════════════════════════

def check_trading():
    resultados = []

    # Cron jobs
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lineas = [l for l in r.stdout.split("\n") if l.strip() and not l.startswith("#")]
        resultados.append(("Cron jobs", True, f"{len(lineas)} activos"))
    except Exception as e:
        resultados.append(("Cron jobs", False, str(e)[:60]))

    # Último log trading
    logs = sorted(
        [f for f in os.listdir(os.path.join(PROYECTO, "logs"))
         if f.startswith("jarvis_trading_")], reverse=True)
    if logs:
        ruta = os.path.join(PROYECTO, "logs", logs[0])
        with open(ruta, "r") as f:
            lineas = f.readlines()
        ultima = lineas[-1].strip() if lineas else "vacío"
        resultados.append(("Último log trading", True, f"{logs[0]} ({len(lineas)} líneas)"))
    else:
        resultados.append(("Último log trading", False, "sin logs"))

    # Último log cripto
    cron_cripto = os.path.join(PROYECTO, "logs", "cron_cripto.log")
    if os.path.exists(cron_cripto):
        mtime = os.path.getmtime(cron_cripto)
        age_h = (time.time() - mtime) / 3600
        resultados.append(("Log cripto", age_h < 24, f"hace {age_h:.1f}h"))
    else:
        resultados.append(("Log cripto", False, "no existe"))

    # Balance Alpaca
    try:
        sys.path.insert(0, PROYECTO)
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("alpaca",
               os.path.join(os.path.expanduser("~"), "trading", "alpaca_client.py"))
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bal = mod.get_balance()
        pos = mod.get_positions()
        equity = float(bal["equity"])
        resultados.append(("Balance", True,
                           f"${equity:,.2f} | {len(pos)} posiciones"))
    except Exception as e:
        resultados.append(("Balance", False, str(e)[:60]))

    return resultados


# ══════════════════════════════════════════════════════════════
#  6. MEMORIA Y RECURSOS
# ══════════════════════════════════════════════════════════════

def check_memoria():
    resultados = []

    # ChromaDB
    try:
        sys.path.insert(0, PROYECTO)
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("mem",
               os.path.join(PROYECTO, "datos", "memoria_jarvis.py"))
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        s = mod.stats()
        resultados.append(("ChromaDB", True,
                           f"{s['conversaciones']} conv, {s['decisiones_trading']} trades"))
    except Exception as e:
        resultados.append(("ChromaDB", False, str(e)[:60]))

    # Disco
    usage = shutil.disk_usage("/")
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    pct_used = ((total_gb - free_gb) / total_gb) * 100
    ok = free_gb > 10
    resultados.append(("Disco", ok, f"{free_gb:.0f} GB libres ({pct_used:.0f}% usado)"))

    # RAM
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0].rstrip(":")] = int(parts[1])
        total = mem["MemTotal"] / (1024 * 1024)
        avail = mem["MemAvailable"] / (1024 * 1024)
        pct = ((total - avail) / total) * 100
        resultados.append(("RAM", avail > 4, f"{avail:.0f} GB libre / {total:.0f} GB ({pct:.0f}% usado)"))
    except Exception as e:
        resultados.append(("RAM", False, str(e)[:60]))

    return resultados


# ══════════════════════════════════════════════════════════════
#  REPORTE
# ══════════════════════════════════════════════════════════════

def generar_reporte():
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    inicio = time.time()

    secciones = [
        ("CLUSTER", check_cluster),
        ("MODELOS", check_modelos),
        ("SERVICIOS", check_servicios),
        ("APIs DATOS", check_apis),
        ("TRADING", check_trading),
        ("MEMORIA/RECURSOS", check_memoria),
    ]

    total_ok = 0
    total_fail = 0
    fallos = []
    lineas = []

    for titulo, fn in secciones:
        try:
            resultados = fn()
        except Exception as e:
            resultados = [(titulo, False, f"Error en sección: {e}")]

        lineas.append(f"\n<b>{titulo}</b>")
        for nombre, ok, detalle in resultados:
            icono = OK if ok else FAIL
            if ok:
                total_ok += 1
            else:
                total_fail += 1
                fallos.append(f"{nombre}: {detalle}")
            lineas.append(f"  {icono} {nombre}: {detalle}")

    elapsed = round(time.time() - inicio, 1)

    # Resumen ejecutivo
    if total_fail == 0:
        resumen = f"{OK} <b>Todos los sistemas operativos</b> ({total_ok}/{total_ok} checks OK)"
        pie = "JARVIS: Todos los sistemas nominales. Listo para operar."
    else:
        resumen = (f"{FAIL} <b>{total_fail} problema(s) detectado(s)</b> "
                   f"({total_ok} OK, {total_fail} FAIL)")
        pie = "<b>Revisar:</b>\n" + "\n".join(f"  {FAIL} {f}" for f in fallos)

    msg = (
        f"\U0001f916 <b>JARVIS — Monitor de Sistema</b>\n"
        f"\U0001f4c5 {fecha} ({elapsed}s)\n\n"
        f"{resumen}\n"
        + "\n".join(lineas)
        + f"\n\n{pie}"
    )

    # Truncar si excede límite Telegram
    if len(msg) > 4000:
        msg = msg[:3950] + "\n\n[truncado]"

    return msg, total_ok, total_fail, fallos


def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=15)
    resp.raise_for_status()


def main():
    print("=" * 60)
    print(f"  JARVIS Monitor — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    msg, ok, fail, fallos = generar_reporte()

    # Imprimir en consola (sin HTML)
    import re
    console = re.sub(r"<[^>]+>", "", msg)
    print(console)

    # Enviar Telegram
    print(f"\nEnviando a Telegram...")
    try:
        enviar_telegram(msg)
        print("  Enviado.")
    except Exception as e:
        print(f"  Error: {e}")

    # Guardar log
    log_dir = os.path.join(PROYECTO, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(re.sub(r"<[^>]+>", "", msg))
    print(f"  Log: {log_path}")

    return ok, fail


if __name__ == "__main__":
    main()
