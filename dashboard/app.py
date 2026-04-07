#!/home/hproano/asistente_env/bin/python
"""
JARVIS Intelligence Dashboard — Executive Edition.
FastAPI + HTML inline — http://localhost:8501
Auto-refresh 60s. Endpoint /api/status con JSON completo.
"""

import os
import sys
import glob
import json
import subprocess
import importlib.util as ilu
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cfg = _load("trading_config", os.path.join(PROYECTO, "trading", "config.py"))
alpaca = _load("alpaca_client",
               os.path.join(os.path.expanduser("~"), "trading", "alpaca_client.py"))
ctx_mod = _load("contexto_mercado", os.path.join(PROYECTO, "datos", "contexto_mercado.py"))
fuentes = _load("fuentes_mercado", os.path.join(PROYECTO, "datos", "fuentes_mercado.py"))
cripto_mod = _load("jarvis_cripto", os.path.join(PROYECTO, "cripto", "jarvis_cripto.py"))
memoria = _load("memoria_jarvis", os.path.join(PROYECTO, "datos", "memoria_jarvis.py"))

app = FastAPI(title="JARVIS Intelligence")

# ── helpers ────────────────────────────────────────────────────

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── data fetchers ──────────────────────────────────────────────

def get_portfolio():
    try:
        bal = alpaca.get_balance()
        pos = alpaca.get_positions()
        return bal, pos
    except Exception as e:
        return {"equity": "0", "cash": "0", "buying_power": "0", "error": str(e)}, []


def get_market():
    fng = _safe(ctx_mod.obtener_fear_greed,
                {"valor": None, "clasificacion": "N/D", "nota": ""})
    vix = _safe(ctx_mod.obtener_vix,
                {"precio": None, "nivel": "N/D", "nota": "", "variacion": 0})
    fed = _safe(fuentes.get_datos_macro_fed,
                {"fed_funds_rate": {"valor": None}, "interpretacion": ""})
    return fng, vix, fed


def get_institutional_cached():
    """Read institutional signals from the latest fuentes JSON (cached, fast)."""
    datos_dir = os.path.join(PROYECTO, "datos")
    jsons = sorted(glob.glob(os.path.join(datos_dir, "fuentes_*.json")), reverse=True)
    if not jsons:
        return {}
    try:
        with open(jsons[0], "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("senales_institucionales", {})
    except Exception:
        return {}


def get_decisions(n=10):
    results = memoria.buscar_memoria("trading decisión compra venta", n=n,
                                     coleccion="trading")
    return results


def get_crypto():
    senales = []
    for par in cripto_mod.PARES:
        try:
            senales.append(cripto_mod.evaluar_senal(par))
        except Exception:
            senales.append({"par": par, "precio": 0, "var_1h": 0,
                            "vol_ratio": 0, "senal": "ERROR"})
    return senales


def get_cluster():
    nodes = {}
    cluster_def = [
        ("jarvis-core",  "http://localhost:11434",       "nemotron-3-super",   "Trading RT (~44s)"),
        ("jarvis-power", "http://192.168.208.80:11435",  "nano:30b + ds-r1:70b", "Chat + Análisis (~15-60s)"),
        ("jarvis-brain", "http://192.168.202.53:11436",  "deepseek-r1:671b",   "Deep analysis (5-10 min)"),
    ]
    for name, url, expected, desc in cluster_def:
        try:
            import requests
            r = requests.get(f"{url}/api/tags", timeout=3)
            models = [m["name"] for m in r.json().get("models", [])]
            try:
                ps = requests.get(f"{url}/api/ps", timeout=3).json()
                running = [m.get("name", "") for m in ps.get("models", [])]
                vram = sum(m.get("size_vram", 0) for m in ps.get("models", [])) / 1e9
            except Exception:
                running, vram = [], 0
            nodes[name] = {"status": "online", "models": models, "expected": expected,
                           "running": running, "vram_gb": round(vram, 1), "url": url, "desc": desc}
        except Exception:
            nodes[name] = {"status": "offline", "models": [], "expected": expected,
                           "running": [], "vram_gb": 0, "url": url, "desc": desc}
    return nodes


def get_api_status():
    apis = {}
    checks = {
        "Alpaca": lambda: alpaca.get_balance() and "ok",
        "FRED": lambda: "ok" if fuentes.FRED_API_KEY else "no key",
        "Finnhub": lambda: "ok" if fuentes.FINNHUB_API_KEY else "no key",
        "Finnhub Premium": lambda: "ok" if fuentes.FINNHUB_PREMIUM_KEY else "no key",
        "Alpha Vantage": lambda: "ok" if fuentes.ALPHA_VANTAGE_KEY else "no key",
        "Tiingo": lambda: "ok" if fuentes.TIINGO_API_KEY else "no key",
        "Binance Testnet": lambda: cripto_mod.obtener_precio(cripto_mod.PARES[0]) and "ok",
    }
    for name, fn in checks.items():
        try:
            r = fn()
            apis[name] = "ok" if r else "error"
        except Exception:
            apis[name] = "error"
    return apis


# ── HTML builder ───────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:#fff;color:#111;
     padding:24px 32px;max-width:1440px;margin:0 auto;line-height:1.5}
.header{display:flex;align-items:center;justify-content:space-between;padding:0 0 24px;
        border-bottom:1px solid #e5e7eb;margin-bottom:28px}
.header h1{font-size:1.5rem;font-weight:700;letter-spacing:-0.02em}
.header h1 span{color:#2563EB}
.header-right{display:flex;align-items:center;gap:12px;font-size:0.82rem;color:#666}
.sys-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;
           border-radius:20px;font-size:0.75rem;font-weight:600}
.sys-badge .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.grid{display:grid;gap:20px;margin-bottom:24px}
.g4{grid-template-columns:repeat(4,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.g2{grid-template-columns:1fr 1fr}
.g1{grid-template-columns:1fr}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;
      box-shadow:0 2px 12px rgba(0,0,0,0.06)}
.card-title{font-size:0.78rem;font-weight:600;color:#666;text-transform:uppercase;
            letter-spacing:0.04em;margin-bottom:12px}
.kpi-val{font-size:1.75rem;font-weight:700;letter-spacing:-0.02em}
.kpi-label{font-size:0.78rem;color:#666;margin-top:2px}
.kpi-sub{font-size:0.75rem;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:0.82rem}
th{text-align:left;font-weight:600;color:#666;padding:8px 10px;
   border-bottom:2px solid #f1f5f9;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.03em}
td{padding:10px;border-bottom:1px solid #f1f5f9}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:0.72rem;font-weight:600}
.b-green{background:#ECFDF5;color:#00C896}
.b-red{background:#FFF1F2;color:#FF4757}
.b-blue{background:#EFF6FF;color:#2563EB}
.b-gray{background:#F1F5F9;color:#666}
.b-yellow{background:#FFFBEB;color:#D97706}
.progress-track{width:100%;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden;margin-top:4px}
.progress-fill{height:100%;border-radius:3px}
.gauge{width:120px;height:60px;position:relative;margin:0 auto 8px}
.gauge-bg{width:120px;height:60px;border-radius:60px 60px 0 0;background:#f1f5f9;overflow:hidden;position:relative}
.gauge-fill{position:absolute;bottom:0;left:0;width:120px;height:60px;border-radius:60px 60px 0 0;
            transform-origin:center bottom;background:conic-gradient(from 0.75turn,#00C896,#FFD700,#FF4757)}
.gauge-mask{position:absolute;bottom:0;left:10px;width:100px;height:50px;border-radius:50px 50px 0 0;background:#fff}
.gauge-val{position:absolute;bottom:4px;width:100%;text-align:center;font-size:1.3rem;font-weight:700}
.timeline-item{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #f1f5f9}
.timeline-item:last-child{border-bottom:none}
.tl-dot{width:8px;height:8px;border-radius:50%;margin-top:6px;flex-shrink:0}
.tl-content{flex:1;font-size:0.82rem}
.tl-time{font-size:0.72rem;color:#999}
.tl-model{font-size:0.7rem;color:#2563EB}
.section-title{font-size:1.05rem;font-weight:700;margin:28px 0 14px;padding-bottom:8px;
               border-bottom:2px solid #f1f5f9;color:#111}
.api-grid{display:flex;flex-wrap:wrap;gap:8px}
.api-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:8px;
           font-size:0.78rem;font-weight:500;border:1px solid #e5e7eb}
.api-badge .dot{width:8px;height:8px;border-radius:50%}
.footer{text-align:center;color:#999;font-size:0.75rem;margin-top:32px;padding-top:16px;border-top:1px solid #f1f5f9}
@media(max-width:900px){.g4,.g3{grid-template-columns:1fr 1fr}.g2{grid-template-columns:1fr}}
@media(max-width:600px){.g4,.g3,.g2{grid-template-columns:1fr}.header{flex-direction:column;gap:8px}}
"""


def build_html():
    now = datetime.now()
    ts = now.strftime("%d/%m/%Y %H:%M:%S")

    bal, pos = get_portfolio()
    fng, vix, fed = get_market()
    inst = get_institutional_cached()
    decisions = get_decisions(10)
    crypto = get_crypto()
    cluster = get_cluster()
    apis = get_api_status()

    equity = float(bal.get("equity", 0))
    cash = float(bal.get("cash", 0))
    pnl_day = sum(float(p.get("unrealized_pl", 0)) for p in pos)
    pnl_sign = "+" if pnl_day >= 0 else ""
    pnl_color = "#00C896" if pnl_day >= 0 else "#FF4757"
    n_pos = len(pos)

    # System status
    any_online = any(n["status"] == "online" for n in cluster.values())
    sys_color = "#00C896" if any_online else "#FF4757"
    sys_text = "Operativo" if any_online else "Offline"

    fng_val = fng.get("valor") or 0
    fng_clas = fng.get("clasificacion", "N/D")
    vix_val = vix.get("precio") or 0
    vix_nivel = vix.get("nivel", "N/D")
    ff_val = fed.get("fed_funds_rate", {}).get("valor")
    ff_str = f"{ff_val}%" if ff_val is not None else "N/D"

    # Regime detection
    if fng_val < 20 and vix_val > 25:
        regime = "Crisis / Miedo"
        regime_cls = "b-red"
    elif fng_val < 40:
        regime = "Cautela"
        regime_cls = "b-yellow"
    elif fng_val > 70 and vix_val < 18:
        regime = "Euforia"
        regime_cls = "b-yellow"
    elif vix_val < 20 and fng_val >= 40:
        regime = "Alcista"
        regime_cls = "b-green"
    else:
        regime = "Neutral"
        regime_cls = "b-gray"

    # Gauge rotation for F&G (0-100 → 0-180 deg)
    gauge_deg = (fng_val / 100) * 180

    # ── Sections ──

    # 1. KPIs
    n_crypto = len([s for s in crypto if s.get("senal") != "ERROR"])

    # 2. Positions table
    pos_html = ""
    for p in pos:
        sym = p["symbol"]
        qty = float(p["qty"])
        entry = float(p["avg_entry_price"])
        cur = float(p["current_price"])
        pnl = float(p["unrealized_pl"])
        pnl_pct = float(p["unrealized_plpc"]) * 100
        c = "#00C896" if pnl >= 0 else "#FF4757"
        bcls = "b-green" if pnl >= 0 else "b-red"

        # Progress bar: -10%(SL) to +20%(TP)
        # Map pnl_pct from [-10,+20] to [0,100]
        prog = max(0, min(100, ((pnl_pct + 10) / 30) * 100))
        bar_c = "#00C896" if pnl_pct >= 0 else "#FF4757"

        pos_html += f"""<tr>
            <td><b>{sym}</b></td>
            <td style="color:#666">{qty:.0f}</td>
            <td>${entry:,.2f}</td>
            <td>${cur:,.2f}</td>
            <td><span class="badge {bcls}">{pnl_pct:+.1f}%</span></td>
            <td style="color:{c};font-weight:600">${pnl:+,.2f}</td>
            <td style="width:100px">
                <div class="progress-track"><div class="progress-fill" style="width:{prog:.0f}%;background:{bar_c}"></div></div>
            </td>
        </tr>"""
    if not pos:
        pos_html = '<tr><td colspan="7" style="text-align:center;color:#999;padding:20px">Sin posiciones abiertas</td></tr>'

    # 3. Institutional signals
    inst_html = ""
    for sym, si in inst.items():
        sg = si.get("senal_general", "N/D")
        if sg in ("ALCISTA", "ALCISTA FUERTE"):
            bcls, icon = "b-green", "&#9650;"
        elif sg in ("BAJISTA", "BAJISTA FUERTE"):
            bcls, icon = "b-red", "&#9660;"
        else:
            bcls, icon = "b-gray", "&#9644;"

        details = si.get("detalles", [])
        det_html = ""
        for d in details:
            det_html += f'<div style="font-size:0.75rem;color:#666;margin-top:3px">{_esc(d)}</div>'

        inst_html += f"""<div class="card" style="padding:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <span style="font-weight:700;font-size:0.95rem">{sym}</span>
                <span class="badge {bcls}">{icon} {sg}</span>
            </div>
            {det_html}
        </div>"""

    # 4. Decisions timeline
    dec_html = ""
    for d in decisions[:10]:
        ts_d = d.get("timestamp", "")
        activo = d.get("activo", "?")
        accion = d.get("accion", "?")
        razon = _esc(d.get("razon", ""))[:120]
        modelo = d.get("modelo", "")

        if "COMPRAR" in accion.upper():
            dot_c, bcls = "#00C896", "b-green"
        elif "VENDER" in accion.upper():
            dot_c, bcls = "#FF4757", "b-red"
        else:
            dot_c, bcls = "#999", "b-gray"

        dec_html += f"""<div class="timeline-item">
            <div class="tl-dot" style="background:{dot_c}"></div>
            <div class="tl-content">
                <div><span class="badge {bcls}">{accion}</span> <b>{activo}</b></div>
                <div style="margin-top:2px">{razon}</div>
                <div class="tl-time">{ts_d} <span class="tl-model">{_esc(modelo)}</span></div>
            </div>
        </div>"""
    if not decisions:
        dec_html = '<div style="color:#999;text-align:center;padding:20px">Sin decisiones registradas</div>'

    # 5. Crypto
    crypto_html = ""
    for s in crypto:
        nombre = s.get("nombre", s.get("par", "?"))
        precio = s.get("precio", 0)
        var = s.get("var_1h", 0)
        senal = s.get("senal", "?")
        vc = "#00C896" if var > 0 else "#FF4757" if var < 0 else "#666"
        if senal == "COMPRAR":
            scls = "b-green"
        elif senal == "VENDER":
            scls = "b-red"
        else:
            scls = "b-gray"

        crypto_html += f"""<div class="card" style="padding:14px;text-align:center">
            <div style="font-weight:600;font-size:0.85rem;margin-bottom:4px">{_esc(nombre)}</div>
            <div style="font-size:1.3rem;font-weight:700">${precio:,.2f}</div>
            <div style="color:{vc};font-size:0.85rem;font-weight:600;margin:4px 0">{var:+.2f}%</div>
            <span class="badge {scls}">{senal}</span>
        </div>"""

    # 6. Cluster
    cluster_html = ""
    for name, info in cluster.items():
        st = "Online" if info["status"] == "online" else "Offline"
        bcls = "b-green" if info["status"] == "online" else "b-red"
        models_str = ", ".join(info.get("running", [])) or ", ".join(info.get("models", [])[:3]) or "ninguno"
        vram = info.get("vram_gb", 0)
        vram_str = f" &middot; {vram:.1f} GB VRAM" if vram > 0 else ""
        desc = info.get("desc", "")
        expected = info.get("expected", "")

        cluster_html += f"""<div class="card" style="padding:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:700">{name}</span>
                <span class="badge {bcls}">{st}</span>
            </div>
            <div style="font-size:0.82rem;font-weight:500;color:#111;margin-bottom:4px">{_esc(expected)}</div>
            <div style="font-size:0.78rem;color:#666">{_esc(desc)}</div>
            <div style="font-size:0.78rem;color:#666">Activos: {_esc(models_str)}{vram_str}</div>
            <div style="font-size:0.72rem;color:#999;margin-top:4px">{info['url']}</div>
        </div>"""

    # 7. API badges
    api_html = ""
    for name, status in apis.items():
        ok = status == "ok"
        dc = "#00C896" if ok else "#FF4757"
        api_html += f"""<div class="api-badge" style="{'border-color:#E5E7EB' if ok else 'border-color:#FECACA'}">
            <span class="dot" style="background:{dc}"></span> {name}
        </div>"""

    # ── Assemble ──
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS Intelligence</title>
<style>{CSS}</style>
</head>
<body>

<!-- 1. HEADER -->
<div class="header">
    <h1><span>JARVIS</span> Intelligence</h1>
    <div class="header-right">
        <span style="color:#999;font-weight:500">Powered by <span style="color:#2563EB;font-weight:700">HP</span></span>
        <span class="badge {regime_cls}" style="font-size:0.8rem;padding:4px 12px">{regime}</span>
        <span>{ts}</span>
        <span class="sys-badge" style="background:{'#ECFDF5' if any_online else '#FFF1F2'};color:{sys_color}">
            <span class="dot" style="background:{sys_color}"></span> {sys_text}
        </span>
    </div>
</div>

<!-- 2. KPIs -->
<div class="grid g4">
    <div class="card">
        <div class="card-title">Equity</div>
        <div class="kpi-val">${equity:,.2f}</div>
        <div class="kpi-sub" style="color:#666">Cash: ${cash:,.2f}</div>
    </div>
    <div class="card">
        <div class="card-title">P&amp;L Abierto</div>
        <div class="kpi-val" style="color:{pnl_color}">{pnl_sign}${abs(pnl_day):,.2f}</div>
        <div class="kpi-sub" style="color:{pnl_color}">{len([p for p in pos if float(p.get('unrealized_pl',0))>=0])} ganadoras / {len([p for p in pos if float(p.get('unrealized_pl',0))<0])} perdedoras</div>
    </div>
    <div class="card">
        <div class="card-title">Posiciones</div>
        <div class="kpi-val">{n_pos} <span style="font-size:0.9rem;color:#666">/ {cfg.MAX_POSICIONES}</span></div>
        <div class="kpi-sub" style="color:#666">{len(cfg.ACTIVOS_OPERABLES)} activos monitoreados</div>
    </div>
    <div class="card">
        <div class="card-title">Cripto</div>
        <div class="kpi-val">{n_crypto} activas</div>
        <div class="kpi-sub" style="color:#666">Binance Testnet</div>
    </div>
</div>

<!-- 3. MARKET REGIME -->
<div class="section-title">Regimen de Mercado</div>
<div class="grid g4">
    <div class="card" style="text-align:center">
        <div class="card-title">Fear &amp; Greed</div>
        <div class="gauge">
            <div class="gauge-bg">
                <div class="gauge-fill" style="clip-path:polygon(0 100%,100% 100%,100% 0,0 0);opacity:0.2"></div>
            </div>
            <div class="gauge-mask"></div>
            <div class="gauge-val" style="color:{'#FF4757' if fng_val<25 else '#D97706' if fng_val<45 else '#00C896' if fng_val<60 else '#D97706' if fng_val<75 else '#FF4757'}">{fng_val}</div>
        </div>
        <div style="font-size:0.82rem;font-weight:600">{_esc(fng_clas)}</div>
    </div>
    <div class="card" style="text-align:center">
        <div class="card-title">VIX</div>
        <div class="kpi-val" style="color:{'#00C896' if vix_val<20 else '#D97706' if vix_val<25 else '#FF4757'}">{vix_val}</div>
        <div style="font-size:0.82rem;color:#666;margin-top:4px">{_esc(vix_nivel)}</div>
    </div>
    <div class="card" style="text-align:center">
        <div class="card-title">Fed Funds Rate</div>
        <div class="kpi-val" style="color:#2563EB">{ff_str}</div>
        <div style="font-size:0.78rem;color:#666;margin-top:4px">{_esc(fed.get('interpretacion','')[:60])}</div>
    </div>
    <div class="card" style="text-align:center">
        <div class="card-title">Regimen Detectado</div>
        <div style="margin-top:10px"><span class="badge {regime_cls}" style="font-size:0.95rem;padding:6px 16px">{regime}</span></div>
        <div style="font-size:0.75rem;color:#999;margin-top:8px">F&amp;G {fng_val} + VIX {vix_val}</div>
    </div>
</div>

<!-- 4. POSITIONS -->
<div class="section-title">Posiciones Abiertas</div>
<div class="grid g1">
    <div class="card">
        <table>
            <tr><th>Activo</th><th>Qty</th><th>Entrada</th><th>Actual</th><th>P&amp;L</th><th>USD</th><th>SL ← → TP</th></tr>
            {pos_html}
        </table>
    </div>
</div>

<!-- 5. INSTITUTIONAL -->
<div class="section-title">Senales Institucionales</div>
<div class="grid g3">
    {inst_html}
</div>

<!-- 6. DECISIONS -->
<div class="section-title">Decisiones JARVIS</div>
<div class="grid g1">
    <div class="card">
        {dec_html}
    </div>
</div>

<!-- 7. CRYPTO -->
<div class="section-title">Cripto</div>
<div class="grid g{len(crypto) if len(crypto)<=4 else 4}">
    {crypto_html}
</div>

<!-- 8. CLUSTER -->
<div class="section-title">Estado del Cluster</div>
<div class="grid g3">
    {cluster_html}
</div>

<!-- 9. API STATUS -->
<div class="section-title">Fuentes de Datos</div>
<div class="grid g1">
    <div class="card">
        <div class="api-grid">{api_html}</div>
    </div>
</div>

<div class="footer">JARVIS Intelligence System &middot; Paper Trading Only &middot; Auto-refresh 60s</div>

</body>
</html>"""


# ── Endpoints ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return build_html()


@app.get("/api/status")
def api_status():
    bal, pos = get_portfolio()
    fng, vix, fed = get_market()
    crypto = get_crypto()
    cluster = get_cluster()
    apis = get_api_status()
    mem = memoria.stats()

    return {
        "timestamp": datetime.now().isoformat(),
        "portfolio": {
            "equity": float(bal.get("equity", 0)),
            "cash": float(bal.get("cash", 0)),
            "positions": len(pos),
            "max_positions": cfg.MAX_POSICIONES,
            "pnl_open": sum(float(p.get("unrealized_pl", 0)) for p in pos),
            "positions_detail": [
                {"symbol": p["symbol"], "qty": float(p["qty"]),
                 "entry": float(p["avg_entry_price"]),
                 "current": float(p["current_price"]),
                 "pnl": float(p["unrealized_pl"]),
                 "pnl_pct": float(p["unrealized_plpc"]) * 100}
                for p in pos
            ],
        },
        "market": {
            "fear_greed": fng,
            "vix": vix,
            "fed_funds_rate": fed.get("fed_funds_rate", {}),
        },
        "crypto": [
            {"pair": s.get("par", ""), "price": s.get("precio", 0),
             "var_1h": s.get("var_1h", 0), "signal": s.get("senal", "")}
            for s in crypto
        ],
        "cluster": cluster,
        "apis": apis,
        "memory": mem,
        "config": {
            "assets": cfg.ACTIVOS_OPERABLES,
            "total_assets": len(cfg.ACTIVOS_OPERABLES),
            "stop_loss": cfg.STOP_LOSS_PCT,
            "take_profit": cfg.TAKE_PROFIT_PCT,
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8501)
