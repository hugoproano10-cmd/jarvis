#!/home/hproano/asistente_env/bin/python
"""
JARVIS Dashboard — Flask + Plotly, tema oscuro estilo TradingView.
Puerto 8050. Auto-refresh cada 5 minutos.

Páginas:
  /            Resumen general (equity histórico, P&L, posiciones)
  /trades      Historial + gráficos P&L (filtrable por mercado/símbolo/fecha)
  /analisis    Win rate por regla, scores, distribución P&L
  /cripto      Posiciones y trades cripto

Fuente de datos: datos/jarvis_history.db (SQLite).
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict

from flask import Flask, request, render_template_string
import plotly.graph_objects as go
import plotly.io as pio

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from datos import jarvis_db as db  # noqa: E402

import requests as _req  # para consultar Binance público

from dotenv import load_dotenv as _load_dotenv  # noqa: E402
_load_dotenv(os.path.join(PROYECTO, ".env"))


# BUG-01: cache persistente en disco de posiciones acciones de IBKR.
# Cuando IBKR no responde en un reload (race condition con el Gateway),
# usamos el último set conocido. Persistir en /tmp sobrevive restarts
# del servicio — la primera respuesta exitosa sirve a reinicios futuros.
_POS_CACHE_FILE = "/tmp/jarvis_dashboard_pos_acc.json"


def _cargar_pos_cache():
    try:
        with open(_POS_CACHE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _guardar_pos_cache(pos):
    try:
        with open(_POS_CACHE_FILE, "w") as f:
            json.dump(pos, f)
    except Exception as e:
        print(f"[dashboard] no pude guardar cache posiciones: {e}")


_pos_acc_cache = _cargar_pos_cache()


def _acciones_posiciones_live():
    """Consulta IBKR vía agentes/ibkr_trading.py. En caso de error o
    respuesta vacía, retorna la última respuesta exitosa cacheada (en
    memoria + disco). Nunca retorna lista vacía si alguna vez hubo datos.
    """
    global _pos_acc_cache
    try:
        from agentes.ibkr_trading import get_positions
        posiciones = get_positions() or []
    except Exception as e:
        print(f"[dashboard] IBKR get_positions falló: {e}")
        return list(_pos_acc_cache)
    out = []
    for p in posiciones:
        try:
            qty = float(p.get("qty", 0) or 0)
            # BUG-NEW: IBKR devuelve símbolos cerrados (AMD, NVDA) con qty=0.
            # Filtrar para que no aparezcan en la tabla.
            if qty <= 0:
                continue
            out.append({
                "simbolo": p.get("symbol"),
                "qty": qty,
                "precio_entrada": float(p.get("avg_entry_price", 0) or 0),
                "precio_actual": float(p.get("current_price", 0) or 0),
                "valor_actual": float(p.get("market_value", 0) or 0),
                "pnl_pct": float(p.get("unrealized_plpc", 0) or 0) * 100,
                "pnl_usd": float(p.get("unrealized_pl", 0) or 0),
            })
        except Exception:
            continue
    if out:
        _pos_acc_cache = out
        _guardar_pos_cache(out)
    elif _pos_acc_cache:
        # IBKR respondió pero vacío — probable timing/race; usar cache
        return list(_pos_acc_cache)
    return out


def _acciones_equity_live():
    """Equity de acciones: IBKR get_balance().equity si responde, si no el
    último snapshot con equity_acciones > 0.
    """
    try:
        from agentes.ibkr_trading import get_balance
        bal = get_balance() or {}
        eq = float(bal.get("equity") or 0)
        cash = float(bal.get("cash") or bal.get("settled_cash") or 0)
        if eq > 0:
            return eq, cash, "ibkr-live"
    except Exception as e:
        print(f"[dashboard] IBKR get_balance falló: {e}")
    # Fallback: snapshot más reciente con datos
    for s in reversed(db.get_snapshots()):
        if s.get("equity_acciones"):
            return s["equity_acciones"], s.get("cash_acciones") or 0, f"snap-{s['fecha']}"
    return 0, 0, "sin-datos"


def _cripto_equity_live():
    """Equity cripto en tiempo real: valor posiciones live + USDT cash.

    USDT cash se obtiene de Binance /account (firmado). Si la firma falla,
    cae al cash del último snapshot con cash_cripto > 0.
    Retorna (equity_total, valor_posiciones, cash_usdt, fuente_cash).
    """
    posiciones = _cripto_posiciones_live()
    valor_pos = sum((p.get("valor_actual") or 0) for p in posiciones)

    cash = None
    fuente = "no-disponible"
    key = os.getenv("BINANCE_REAL_API_KEY", "")
    secret = os.getenv("BINANCE_REAL_SECRET", "")
    if key and secret and "your_" not in key:
        try:
            import hmac as _hmac
            import hashlib as _hashlib
            import time as _time
            from urllib.parse import urlencode as _uenc
            params = {"timestamp": int(_time.time() * 1000), "recvWindow": 10000}
            sig = _hmac.new(
                secret.encode(), _uenc(params).encode(), _hashlib.sha256
            ).hexdigest()
            params["signature"] = sig
            r = _req.get(
                "https://api.binance.com/api/v3/account",
                headers={"X-MBX-APIKEY": key},
                params=params, timeout=4,
            )
            if r.ok:
                for b in r.json().get("balances", []):
                    if b["asset"] == "USDT":
                        cash = float(b["free"]) + float(b["locked"])
                        fuente = "binance-live"
                        break
        except Exception as e:
            print(f"[dashboard] Binance /account falló: {e}")

    if cash is None:
        for s in reversed(db.get_snapshots()):
            if s.get("cash_cripto"):
                cash = s["cash_cripto"]
                fuente = f"snap-{s['fecha']}"
                break
    cash = cash or 0
    return round(valor_pos + cash, 2), round(valor_pos, 2), round(cash, 2), fuente


def _cripto_posiciones_live():
    """Lee cripto/estado_cripto.json y consulta precios actuales de Binance.

    Best-effort: si el archivo no existe o Binance no responde, retorna [].
    No requiere API key (precios son endpoint público).
    """
    ruta = os.path.join(PROYECTO, "cripto", "estado_cripto.json")
    if not os.path.exists(ruta):
        return []
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            estado = json.load(f)
    except Exception:
        return []

    pos = estado.get("posiciones", {}) or {}
    filas = []
    for par, p in pos.items():
        precio = None
        try:
            r = _req.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": par}, timeout=3)
            if r.ok:
                precio = float(r.json()["price"])
        except Exception:
            pass
        qty = p.get("qty", 0) or 0
        entrada = p.get("precio_entrada", 0) or 0
        valor = (qty * precio) if precio else None
        pnl_usd = ((precio - entrada) * qty) if (precio and entrada) else None
        pnl_pct = (((precio / entrada) - 1) * 100) if (precio and entrada) else None
        filas.append({
            "par": par, "simbolo": par,
            "qty": qty, "precio_entrada": entrada,
            "precio_actual": precio, "valor_actual": valor,
            "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
        })
    return filas

app = Flask(__name__)

# Tema dark plotly custom
pio.templates["jarvis_dark"] = go.layout.Template(
    layout=dict(
        paper_bgcolor="#0e1116",
        plot_bgcolor="#161b22",
        font=dict(color="#c9d1d9", family="system-ui,-apple-system,sans-serif"),
        xaxis=dict(gridcolor="#30363d", zerolinecolor="#30363d"),
        yaxis=dict(gridcolor="#30363d", zerolinecolor="#30363d"),
        colorway=["#26a69a", "#ef5350", "#ffb020", "#42a5f5", "#ab47bc"],
    )
)
pio.templates.default = "jarvis_dark"


# ── CSS base (tema dark tipo TradingView) ──────────────────
BASE_CSS = """
<style>
:root {
  --bg:#0b0e14; --panel:#161b22; --border:#30363d;
  --text:#c9d1d9; --muted:#8b949e;
  --green:#26a69a; --red:#ef5350; --amber:#ffb020;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font-family:system-ui,-apple-system,sans-serif;
       padding-bottom:42px; }
footer.jarvis { position:fixed; left:0; right:0; bottom:0;
                text-align:center; color:#666; font-size:12px;
                padding:10px; border-top:1px solid #333;
                background:var(--bg); }
header { background:var(--panel); border-bottom:1px solid var(--border);
         padding:12px 24px; display:flex; align-items:center; gap:24px; }
header h1 { margin:0; font-size:18px; font-weight:600; }
header nav a { color:var(--muted); text-decoration:none; margin-right:18px;
               font-size:14px; padding:6px 10px; border-radius:6px; }
header nav a.active, header nav a:hover { color:var(--text); background:#21262d; }
main { padding:24px; max-width:1400px; margin:0 auto; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
        gap:16px; margin-bottom:24px; }
.card { background:var(--panel); border:1px solid var(--border);
        border-radius:8px; padding:16px; }
.card h3 { margin:0 0 8px; font-size:12px; color:var(--muted);
           text-transform:uppercase; letter-spacing:.5px; font-weight:500; }
.card .value { font-size:24px; font-weight:600; }
.value.pos { color:var(--green); }
.value.neg { color:var(--red); }
.panel { background:var(--panel); border:1px solid var(--border);
         border-radius:8px; padding:20px; margin-bottom:20px; }
.panel h2 { margin:0 0 16px; font-size:16px; font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { padding:8px 12px; text-align:left; border-bottom:1px solid var(--border); }
th { color:var(--muted); font-weight:500; text-transform:uppercase;
     font-size:11px; letter-spacing:.5px; }
tr:hover td { background:#1c2128; }
.pos { color:var(--green); }
.neg { color:var(--red); }
.filter-bar { display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
.filter-bar input, .filter-bar select, .filter-bar button {
  background:#0b0e14; color:var(--text); border:1px solid var(--border);
  border-radius:6px; padding:6px 10px; font-size:13px; }
.filter-bar button { cursor:pointer; }
.filter-bar button:hover { background:#21262d; }
.pill { display:inline-block; padding:2px 8px; border-radius:10px;
        font-size:11px; background:#21262d; }
.pill.buy { background:rgba(38,166,154,.2); color:var(--green); }
.pill.sell { background:rgba(239,83,80,.2); color:var(--red); }
.pill.skip, .pill.hold, .pill.wait { background:#21262d; color:var(--muted); }
</style>
"""

NAV = """
<header>
  <h1>JARVIS Dashboard</h1>
  <nav>
    <a href='/' {nav_home}>Resumen</a>
    <a href='/trades' {nav_trades}>Trades</a>
    <a href='/analisis' {nav_analisis}>Análisis</a>
    <a href='/cripto' {nav_cripto}>Cripto</a>
  </nav>
  <span style='margin-left:auto;color:var(--muted);font-size:12px'>
    Actualizado <span id='live-ts'>{ts}</span>{refresh_note}
  </span>
</header>
"""


_HOME_REFRESH_JS = """
<script>
(function(){
  async function jarvisRefresh(){
    try{
      const r = await fetch('/api/resumen', {cache:'no-store'});
      if(!r.ok) return;
      const d = await r.json();
      const live = d.live || {};
      for(const [k,v] of Object.entries(live)){
        const el = document.getElementById('live-'+k);
        if(el && v !== null && v !== undefined) el.textContent = v;
        const cls = document.getElementById('live-'+k+'-cls');
        if(cls && live[k+'_cls']) cls.className = 'value ' + live[k+'_cls'];
      }
      const ts = document.getElementById('live-ts');
      if(ts) ts.textContent = new Date().toTimeString().slice(0,8);
    }catch(e){}
  }
  setInterval(jarvisRefresh, 300000);
})();
</script>
"""


def _page(title, active, body):
    def _nav_attrs(is_active):
        return "class='active' aria-current='page'" if is_active else ""
    flags = {
        "nav_home": _nav_attrs(active == "home"),
        "nav_trades": _nav_attrs(active == "trades"),
        "nav_analisis": _nav_attrs(active == "analisis"),
        "nav_cripto": _nav_attrs(active == "cripto"),
    }
    favicon = (
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        "<text y='.9em' font-size='90'>🤖</text></svg>\">"
    )
    # BUG-16: auto-refresh sólo en Resumen, vía JS (no hard reload)
    refresh_note = " · auto-refresh 5 min" if active == "home" else ""
    refresh_js = _HOME_REFRESH_JS if active == "home" else ""

    html = (
        "<!DOCTYPE html><html lang='es'><head>"
        f"<title>JARVIS · {title}</title>"
        "<meta charset='utf-8'>"
        # BUG-15 viewport
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"{favicon}"
        f"{BASE_CSS}"
        "<script src='https://cdn.plot.ly/plotly-2.32.0.min.js'></script>"
        "</head><body>"
        + NAV.format(ts=datetime.now().strftime("%H:%M:%S"),
                     refresh_note=refresh_note, **flags)
        + f"<main>{body}</main>"
        "<footer class='jarvis'>Powered by hproano</footer>"
        f"{refresh_js}"
        "</body></html>"
    )
    return html


def _fmt_money(v, signo=False):
    """Formato contable: -$9.86 (negativos), +$9.86 (signo=True, positivos),
    $9.86 (signo=False, positivos). Nunca $-9.86."""
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    if v < 0:
        return f"-${abs(v):,.2f}"
    return f"{'+' if signo else ''}${v:,.2f}"


# Alias para claridad / requerido por spec BUG-13
formato_moneda = _fmt_money


def _fmt_pct(v):
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _cls_pnl(v):
    try:
        return "pos" if float(v) >= 0 else "neg"
    except Exception:
        return ""


def _plot_html(fig, div_id, aria_label=None):
    inner = pio.to_html(fig, include_plotlyjs=False, full_html=False,
                        div_id=div_id, config={"displayModeBar": False})
    # BUG-18: envolver el gráfico en contenedor aria-labelled para lectores
    label = aria_label or f"Gráfico: {div_id}"
    label = label.replace("'", "&apos;").replace('"', "&quot;")
    return f"<div role='img' aria-label='{label}'>{inner}</div>"


def _dedup_trades_30min(trs):
    """Dedup visual: si existe un trade más nuevo con el mismo (símbolo,
    acción) dentro de 30 minutos, descartar el más viejo.

    Motivo: el log histórico registró la misma intención de BUY tres veces
    (18:34, 18:45, 19:00) antes de que finalmente se ejecutara. Ver sólo
    el último evita triplicados en la UI. `trs` debe venir ordenado
    descendentemente por timestamp (como lo retorna get_trades).
    """
    kept = []
    aceptados_ts = {}  # (sym, acción) -> list[datetime] aceptados (más nuevos que el actual)
    for t in trs:
        key = (t.get("simbolo"), t.get("accion"))
        try:
            ts = datetime.strptime(t["timestamp"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            kept.append(t)
            continue
        hay_mas_nuevo_cercano = any(
            0 <= (a - ts).total_seconds() <= 1800
            for a in aceptados_ts.get(key, [])
        )
        if hay_mas_nuevo_cercano:
            continue
        kept.append(t)
        aceptados_ts.setdefault(key, []).append(ts)
    return kept


# ══════════════════════════════════════════════════════════════
#  PÁGINA 1 — Resumen general
# ══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    snapshots = db.get_snapshots()
    resumen = db.get_resumen_general()
    ultimo_trade = (db.get_trades(limit=1) or [None])[0]

    # Equity ACTUAL: acciones (live IBKR → fallback snapshot) + cripto (live Binance).
    # Esto NO depende del último snapshot — resiste la carrera cripto/acciones.
    eq_acc_actual, cash_acc_actual, fuente_acc = _acciones_equity_live()
    eq_cri_actual, _, _, fuente_cri = _cripto_equity_live()
    equity_actual = round(eq_acc_actual + eq_cri_actual, 2)

    # Carry-forward por fecha: para cada snapshot date, arrastrar el último
    # valor conocido de cada lado. Así un snapshot que solo tocó cripto no
    # deja el gráfico caer a $0 en acciones (y vice-versa).
    fechas = [s["fecha"] for s in snapshots]
    eq_acc_cf, eq_cri_cf = [], []
    prev_acc = prev_cri = None
    for s in snapshots:
        v_acc = s.get("equity_acciones")
        v_cri = s.get("equity_cripto")
        if v_acc and v_acc > 0:
            prev_acc = v_acc
        if v_cri and v_cri > 0:
            prev_cri = v_cri
        eq_acc_cf.append(prev_acc)
        eq_cri_cf.append(prev_cri)
    eq_total_cf = [(a or 0) + (c or 0) if (a or c) else None
                   for a, c in zip(eq_acc_cf, eq_cri_cf)]

    def _equity_hasta(target_fecha):
        """Mejor estimado de equity total a esa fecha (carry-forward)."""
        acc = cri = None
        for s in snapshots:
            if s["fecha"] > target_fecha:
                break
            if s.get("equity_acciones") and s["equity_acciones"] > 0:
                acc = s["equity_acciones"]
            if s.get("equity_cripto") and s["equity_cripto"] > 0:
                cri = s["equity_cripto"]
        if acc is None and cri is None:
            return None
        return (acc or 0) + (cri or 0)

    hoy_str = datetime.now().strftime("%Y-%m-%d")
    ayer_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    semana_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    mes_str = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    eq_ayer = _equity_hasta(ayer_str)
    eq_semana = _equity_hasta(semana_str)
    eq_mes = _equity_hasta(mes_str)
    pnl_hoy = round(equity_actual - eq_ayer, 2) if eq_ayer else None
    pnl_sem = round(equity_actual - eq_semana, 2) if eq_semana else None
    pnl_mes = round(equity_actual - eq_mes, 2) if eq_mes else None

    # BUG-07: si no hay 30 días de histórico, caer a P&L total desde inicio
    pnl_largo_label = "P&L 30 días"
    if pnl_mes is None and snapshots:
        primer = snapshots[0]
        primer_eq = ((primer.get("equity_acciones") or 0)
                     + (primer.get("equity_cripto") or 0))
        if primer_eq > 0:
            pnl_mes = round(equity_actual - primer_eq, 2)
            pnl_largo_label = "P&L Total"

    # BUG-06: si el último snapshot ya es de HOY, reemplazarlo con el punto live
    # en vez de agregar un segundo punto para el mismo día.
    if fechas and fechas[-1] == hoy_str:
        fechas_plot = fechas[:-1] + [hoy_str]
        eq_acc_plot = eq_acc_cf[:-1] + [eq_acc_actual]
        eq_cri_plot = eq_cri_cf[:-1] + [eq_cri_actual]
        eq_total_plot = eq_total_cf[:-1] + [equity_actual]
    else:
        fechas_plot = fechas + [hoy_str]
        eq_acc_plot = eq_acc_cf + [eq_acc_actual]
        eq_cri_plot = eq_cri_cf + [eq_cri_actual]
        eq_total_plot = eq_total_cf + [equity_actual]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=fechas_plot, y=eq_total_plot, name="Total",
                             mode="lines+markers", connectgaps=True,
                             line=dict(color="#42a5f5", width=2)))
    fig.add_trace(go.Scatter(x=fechas_plot, y=eq_acc_plot, name="Acciones",
                             mode="lines", connectgaps=True,
                             line=dict(color="#26a69a", dash="dot")))
    fig.add_trace(go.Scatter(x=fechas_plot, y=eq_cri_plot, name="Cripto",
                             mode="lines", connectgaps=True,
                             line=dict(color="#ffb020", dash="dot")))
    fig.update_layout(height=360, margin=dict(l=40, r=20, t=20, b=40),
                      legend=dict(orientation="h", y=1.1))

    # Posiciones abiertas:
    #  - Acciones: live desde IBKR (fallback: snapshot más reciente con datos)
    #  - Cripto: live desde cripto/estado_cripto.json + Binance público
    #  Ambos independientes de qué lado escribió último el snapshot.
    filas = []
    posiciones_acc = _acciones_posiciones_live()
    if not posiciones_acc:
        for s in reversed(snapshots):
            detalle = s.get("detalle_posiciones") or {}
            if isinstance(detalle, dict) and detalle.get("acciones"):
                posiciones_acc = detalle["acciones"]
                break
    for p in posiciones_acc:
        # BUG-NEW: defensivo — si viene del fallback de snapshot puede traer
        # qty=0 para posiciones cerradas. Saltar.
        try:
            if float(p.get("qty", 0) or 0) <= 0:
                continue
        except Exception:
            pass
        filas.append(
            f"<tr><td><span class='pill'>ACC</span></td>"
            f"<td>{p.get('simbolo','?')}</td>"
            f"<td>{p.get('qty','?')}</td>"
            f"<td>{_fmt_money(p.get('precio_entrada'))}</td>"
            f"<td>{_fmt_money(p.get('precio_actual'))}</td>"
            f"<td class='{_cls_pnl(p.get('pnl_usd'))}'>{_fmt_money(p.get('pnl_usd'), True)}</td>"
            f"<td class='{_cls_pnl(p.get('pnl_pct'))}'>{_fmt_pct(p.get('pnl_pct'))}</td></tr>"
        )
    # Cripto live
    for p in _cripto_posiciones_live():
        sym = p.get("par") or p.get("simbolo", "?")
        filas.append(
            f"<tr><td><span class='pill'>CRIPTO</span></td>"
            f"<td>{sym}</td>"
            f"<td>{p.get('qty','?')}</td>"
            f"<td>{_fmt_money(p.get('precio_entrada'))}</td>"
            f"<td>{_fmt_money(p.get('precio_actual'))}</td>"
            f"<td class='{_cls_pnl(p.get('pnl_usd'))}'>{_fmt_money(p.get('pnl_usd'), True)}</td>"
            f"<td class='{_cls_pnl(p.get('pnl_pct'))}'>{_fmt_pct(p.get('pnl_pct'))}</td></tr>"
        )

    if filas:
        posiciones_html = (
            "<table><thead><tr><th>Mkt</th><th>Símbolo</th><th>Qty</th>"
            "<th>Entrada</th><th>Actual</th><th>P&L $</th><th>P&L %</th>"
            "</tr></thead><tbody>" + "".join(filas) + "</tbody></table>"
        )
    else:
        posiciones_html = "<p style='color:var(--muted)'>Sin datos de posiciones</p>"

    # Último trade — BUG-05: motivo SIN truncar en el dashboard.
    # (Si viene cortado, la truncación es del log fuente o del hook de
    # registro en trading.py — el dashboard no recorta más.)
    ultimo_html = "<p style='color:var(--muted)'>Sin trades registrados</p>"
    if ultimo_trade:
        accion = ultimo_trade.get("accion", "")
        pill = f"<span class='pill {accion.lower()}'>{accion}</span>"
        motivo_completo = ultimo_trade.get("motivo") or ""
        ultimo_html = (
            f"<div style='font-size:14px'>{pill} "
            f"<strong>{ultimo_trade.get('simbolo','?')}</strong> · "
            f"{_fmt_money(ultimo_trade.get('precio'))} · "
            f"<span style='color:var(--muted)'>{ultimo_trade.get('timestamp','?')}</span>"
            f"<br><span style='color:var(--muted);font-size:12px'>"
            f"{motivo_completo}</span></div>"
        )

    # equity_actual ya calculado arriba (acciones live + cripto live)
    win_rate = resumen.get("win_rate")
    wr_str = f"{win_rate:.1f}%" if win_rate is not None else "—"

    body = f"""
    <div class='grid'>
      <div class='card'><h3>Equity Total</h3>
        <div class='value' id='live-equity'>{_fmt_money(equity_actual)}</div></div>
      <div class='card'><h3>P&amp;L Hoy</h3>
        <div class='value {_cls_pnl(pnl_hoy)}' id='live-pnl_hoy'>{_fmt_money(pnl_hoy, True)}</div></div>
      <div class='card'><h3>P&amp;L 7 días</h3>
        <div class='value {_cls_pnl(pnl_sem)}' id='live-pnl_sem'>{_fmt_money(pnl_sem, True)}</div></div>
      <div class='card'><h3>{pnl_largo_label}</h3>
        <div class='value {_cls_pnl(pnl_mes)}' id='live-pnl_mes'>{_fmt_money(pnl_mes, True)}</div></div>
      <div class='card'><h3>Win Rate</h3>
        <div class='value' id='live-win_rate'>{wr_str}</div></div>
      <div class='card'><h3>Trades Totales</h3>
        <div class='value' id='live-trades_total'>{resumen.get('trades_total', 0)}</div></div>
    </div>

    <div class='panel'><h2>Equity histórico</h2>{_plot_html(fig, 'eq_chart', 'Equity histórico acciones, cripto y total')}</div>

    <div class='panel'><h2>Posiciones abiertas</h2>{posiciones_html}</div>

    <div class='panel'><h2>Último trade</h2>{ultimo_html}</div>
    """
    return _page("Resumen", "home", body)


# ══════════════════════════════════════════════════════════════
#  PÁGINA 2 — Trades
# ══════════════════════════════════════════════════════════════

@app.route("/trades")
def trades():
    mercado = request.args.get("mercado") or None
    simbolo = (request.args.get("simbolo") or "").upper() or None
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None

    trs = db.get_trades(mercado=mercado, simbolo=simbolo,
                        desde=desde, hasta=hasta, limit=500)

    # P&L acumulado (ventas solo, cronológico)
    ventas = sorted([t for t in trs if t["accion"] == "SELL"
                     and (t.get("pnl_usd") is not None or t.get("pnl_pct") is not None)],
                    key=lambda t: t["timestamp"])
    acum = []
    total = 0.0
    for t in ventas:
        pnl = t.get("pnl_usd")
        if pnl is None:
            # Fallback: usar pnl_pct como proxy si no hay USD
            pnl = t.get("pnl_pct") or 0
        total += float(pnl or 0)
        acum.append(total)

    fig_acum = go.Figure()
    if ventas:
        fig_acum.add_trace(go.Scatter(
            x=[t["timestamp"] for t in ventas], y=acum, mode="lines+markers",
            line=dict(color="#42a5f5", width=2), name="P&L acumulado"))
    fig_acum.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=40))

    # P&L por trade (barras verde/rojo) — usa pnl_usd si está, si no pnl_pct
    fig_bar = go.Figure()
    if ventas:
        xs = [t["timestamp"][-8:] + " " + (t["simbolo"] or "") for t in ventas]
        ys = [t.get("pnl_usd") if t.get("pnl_usd") is not None
              else t.get("pnl_pct") for t in ventas]
        colores = ["#26a69a" if (y or 0) >= 0 else "#ef5350" for y in ys]
        fig_bar.add_trace(go.Bar(x=xs, y=ys, marker_color=colores))
    fig_bar.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=80),
                           xaxis=dict(tickangle=-45))

    # Tabla
    filas = []
    for t in trs[:200]:
        acc = t["accion"]
        pnl_pct = t.get("pnl_pct")
        pnl_usd = t.get("pnl_usd")
        qty_v = t.get("qty")
        # BUG-02: mostrar "—" si qty no disponible en vez de columna vacía
        qty_cell = (f"{qty_v:g}" if isinstance(qty_v, (int, float)) and qty_v else "—")
        score_v = t.get("score")
        score_cell = f"{score_v:+d}" if isinstance(score_v, int) else "—"
        # BUG-03/10: P&L N/A para BUYs (no hay trade cerrado aún)
        if acc == "BUY":
            pnl_usd_cell = "—"
            pnl_pct_cell = "—"
            pnl_usd_cls = pnl_pct_cls = ""
        else:
            pnl_usd_cell = (_fmt_money(pnl_usd, True)
                            if pnl_usd is not None else "—")
            pnl_pct_cell = _fmt_pct(pnl_pct) if pnl_pct is not None else "—"
            pnl_usd_cls = _cls_pnl(pnl_usd) if pnl_usd is not None else ""
            pnl_pct_cls = _cls_pnl(pnl_pct) if pnl_pct is not None else ""
        filas.append(
            f"<tr><td>{t['timestamp']}</td>"
            f"<td><span class='pill'>{t['mercado']}</span></td>"
            f"<td>{t['simbolo']}</td>"
            f"<td><span class='pill {acc.lower()}'>{acc}</span></td>"
            f"<td>{qty_cell}</td>"
            f"<td>{_fmt_money(t.get('precio'))}</td>"
            f"<td>{score_cell}</td>"
            f"<td>{(t.get('regla') or '')}</td>"
            f"<td class='{pnl_usd_cls}'>{pnl_usd_cell}</td>"
            f"<td class='{pnl_pct_cls}'>{pnl_pct_cell}</td>"
            f"</tr>"
        )

    filtros = f"""
    <form class='filter-bar' method='get'>
      <select name='mercado'>
        <option value=''>Todos</option>
        <option value='acciones' {'selected' if mercado=='acciones' else ''}>Acciones</option>
        <option value='cripto' {'selected' if mercado=='cripto' else ''}>Cripto</option>
      </select>
      <input name='simbolo' placeholder='Símbolo' value='{simbolo or ''}'>
      <input name='desde' type='date' value='{desde or ''}'>
      <input name='hasta' type='date' value='{hasta or ''}'>
      <button type='submit'>Filtrar</button>
    </form>
    """

    # BUG-11: título claro con mostrado vs total
    n_mostrados = len(filas)
    total_resumen = db.get_resumen_general().get("trades_total", len(trs))
    titulo_tabla = f"Trades (mostrando {n_mostrados} de {total_resumen} totales)"

    # Python 3.10 no permite backslashes dentro de expresiones f-string:
    # extraemos el fallback HTML a una variable simple.
    _empty_tr_trades = (
        '<tr><td colspan=10 style="color:var(--muted)">'
        'Sin trades en el rango</td></tr>'
    )
    _filas_o_vacio = "".join(filas) or _empty_tr_trades

    body = f"""
    {filtros}
    <div class='panel'><h2>P&amp;L acumulado (ventas cerradas)</h2>{_plot_html(fig_acum, 'acum', 'P&L acumulado de ventas cerradas')}</div>
    <div class='panel'><h2>P&amp;L por trade</h2>{_plot_html(fig_bar, 'bar', 'P&L por trade individual')}</div>
    <div class='panel'><h2>{titulo_tabla}</h2>
      <table><thead><tr>
        <th>Fecha</th><th>Mkt</th><th>Símbolo</th><th>Acción</th>
        <th>Qty</th><th>Precio</th><th>Score</th><th>Regla</th>
        <th>P&amp;L $</th><th>P&amp;L %</th>
      </tr></thead><tbody>{_filas_o_vacio}</tbody></table>
    </div>
    """
    return _page("Trades", "trades", body)


# ══════════════════════════════════════════════════════════════
#  PÁGINA 3 — Análisis
# ══════════════════════════════════════════════════════════════

@app.route("/analisis")
def analisis():
    ventas = [t for t in db.get_trades(limit=5000) if t["accion"] == "SELL"]

    # Win rate por regla
    por_regla = defaultdict(lambda: {"gana": 0, "pierde": 0, "total": 0})
    for t in ventas:
        regla = t.get("regla") or "(sin regla)"
        pnl = t.get("pnl_usd") if t.get("pnl_usd") is not None else t.get("pnl_pct")
        if pnl is None:
            continue
        por_regla[regla]["total"] += 1
        if pnl >= 0:
            por_regla[regla]["gana"] += 1
        else:
            por_regla[regla]["pierde"] += 1

    reglas = sorted(por_regla.keys())
    wr_por_regla = [(por_regla[r]["gana"] / por_regla[r]["total"] * 100)
                    if por_regla[r]["total"] else 0 for r in reglas]

    fig_wr = go.Figure()
    fig_wr.add_trace(go.Bar(x=reglas, y=wr_por_regla, marker_color="#42a5f5"))
    fig_wr.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=80),
                          yaxis_title="Win rate %", xaxis=dict(tickangle=-30))

    # BUG-04: el SELL casi nunca trae score. Para cada venta cerrada,
    # correlacionar con el score del BUY más reciente del mismo símbolo
    # previo a esa venta.
    all_trades_asc = sorted(db.get_trades(limit=10000),
                             key=lambda t: t.get("timestamp") or "")
    last_buy_score = {}  # (mercado, simbolo) -> score del último BUY
    scores_gana = []
    scores_pier = []
    for t in all_trades_asc:
        key = (t.get("mercado"), t.get("simbolo"))
        if t["accion"] == "BUY":
            s = t.get("score")
            if s is not None:
                last_buy_score[key] = s
        elif t["accion"] == "SELL":
            pnl = t.get("pnl_usd") if t.get("pnl_usd") is not None else t.get("pnl_pct")
            if pnl is None:
                continue
            buy_s = last_buy_score.get(key)
            if buy_s is None:
                continue
            (scores_gana if pnl >= 0 else scores_pier).append(buy_s)

    fig_scores = go.Figure()
    if scores_gana:
        fig_scores.add_trace(go.Box(y=scores_gana, name=f"Ganadoras (n={len(scores_gana)})",
                                     marker_color="#26a69a"))
    if scores_pier:
        fig_scores.add_trace(go.Box(y=scores_pier, name=f"Perdedoras (n={len(scores_pier)})",
                                     marker_color="#ef5350"))
    fig_scores.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=40),
                              yaxis_title="Score del BUY correlacionado")

    # Distribución P&L
    pnls = [t.get("pnl_pct") for t in ventas if t.get("pnl_pct") is not None]
    fig_dist = go.Figure()
    if pnls:
        fig_dist.add_trace(go.Histogram(x=pnls, nbinsx=40, marker_color="#42a5f5"))
    fig_dist.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=40),
                            xaxis_title="P&L %", yaxis_title="Trades")

    # BUG-12: sólo considerar snapshots con pnl_dia_total real. Los
    # migrados no tienen ese campo. Si hay un único día, no duplicar
    # "Mejor == Peor" — mostrar insuficiencia explícita.
    snaps_con_pnl = [s for s in db.get_snapshots()
                     if s.get("pnl_dia_total") is not None]
    if not snaps_con_pnl:
        mp_html = ("<p style='color:var(--muted)'>"
                   "Sin ventas cerradas con P&amp;L registrado todavía</p>")
    elif len(snaps_con_pnl) == 1 or (
            max(snaps_con_pnl, key=lambda s: s["pnl_dia_total"])["fecha"]
            == min(snaps_con_pnl, key=lambda s: s["pnl_dia_total"])["fecha"]):
        unico = snaps_con_pnl[0]
        clase = "pos" if unico["pnl_dia_total"] >= 0 else "neg"
        mp_html = (
            f"<p>Mejor día: <strong>{unico['fecha']}</strong> "
            f"<span class='{clase}'>{_fmt_money(unico['pnl_dia_total'], True)}</span></p>"
            f"<p>Peor día: <span style='color:var(--muted)'>"
            f"Datos insuficientes (1 día)</span></p>"
        )
    else:
        mejor = max(snaps_con_pnl, key=lambda s: s["pnl_dia_total"])
        peor = min(snaps_con_pnl, key=lambda s: s["pnl_dia_total"])
        mp_html = (
            f"<p>Mejor día: <strong>{mejor['fecha']}</strong> "
            f"<span class='pos'>{_fmt_money(mejor['pnl_dia_total'], True)}</span></p>"
            f"<p>Peor día: <strong>{peor['fecha']}</strong> "
            f"<span class='neg'>{_fmt_money(peor['pnl_dia_total'], True)}</span></p>"
        )

    # Tabla por regla
    regla_filas = []
    for r in reglas:
        pr = por_regla[r]
        wr = (pr["gana"] / pr["total"] * 100) if pr["total"] else 0
        regla_filas.append(
            f"<tr><td>{r}</td><td>{pr['total']}</td><td>{pr['gana']}</td>"
            f"<td>{pr['pierde']}</td><td>{wr:.1f}%</td></tr>"
        )

    # Python 3.10 no permite backslashes ni comillas escapadas dentro de
    # expresiones f-string; extraemos los fallbacks.
    _mp_fallback = (
        '<p style="color:var(--muted)">Sin datos suficientes</p>'
    )
    _mp_final = mp_html or _mp_fallback

    _empty_tr_regla = (
        '<tr><td colspan=5 style="color:var(--muted)">Sin datos</td></tr>'
    )
    _regla_filas_html = "".join(regla_filas) or _empty_tr_regla

    body = f"""
    <div class='panel'><h2>Win rate por regla</h2>{_plot_html(fig_wr, 'wr', 'Win rate por regla de trading')}</div>
    <div class='panel'><h2>Score: ganadoras vs perdedoras</h2>{_plot_html(fig_scores, 'sc', 'Score de trades ganadoras vs perdedoras')}</div>
    <div class='panel'><h2>Distribución P&amp;L %</h2>{_plot_html(fig_dist, 'dist', 'Distribución de P&L porcentual')}</div>
    <div class='panel'><h2>Mejor / peor día</h2>{_mp_final}</div>
    <div class='panel'><h2>Tabla por regla</h2>
      <table><thead><tr><th>Regla</th><th>Total</th><th>Ganadas</th><th>Perdidas</th><th>WR</th></tr></thead>
      <tbody>{_regla_filas_html}</tbody></table>
    </div>
    """
    return _page("Análisis", "analisis", body)


# ══════════════════════════════════════════════════════════════
#  PÁGINA 4 — Cripto
# ══════════════════════════════════════════════════════════════

@app.route("/cripto")
def cripto():
    # Trades cripto (dedup visual en ventana de 30 min)
    trs_raw = db.get_trades(mercado="cripto", limit=500)
    trs = _dedup_trades_30min(trs_raw)

    # Equity / posiciones LIVE (mismos helpers que Resumen)
    eq_actual, valor_pos, usdt_cash, fuente = _cripto_equity_live()
    posiciones = _cripto_posiciones_live()
    n_pos = len(posiciones)

    # Snapshots históricos con equity_cripto > 0 (evita eje Y raro)
    snaps = [s for s in db.get_snapshots()
             if s.get("equity_cripto") and s["equity_cripto"] > 0]
    fechas = [s["fecha"] for s in snaps]
    eq_cri = [s["equity_cripto"] for s in snaps]

    # BUG-08: el punto final siempre es el valor LIVE (consistente con la
    # tarjeta de arriba). Si el último snapshot ya es de hoy, lo reemplazamos;
    # si no, añadimos un nuevo punto.
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    if fechas and fechas[-1] == hoy_str:
        eq_cri[-1] = eq_actual
    else:
        fechas.append(hoy_str)
        eq_cri.append(eq_actual)

    # BUG-09: con <2 datapoints el eje X se ve absurdo. Mejor un mensaje.
    if len(fechas) < 2:
        grafico_html = ("<p style='color:var(--muted)'>"
                        "Acumulando datos históricos… "
                        "(se necesitan al menos 2 snapshots diarios)</p>")
    else:
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(x=fechas, y=eq_cri, mode="lines+markers",
                                     line=dict(color="#ffb020", width=2),
                                     name="Equity cripto"))
        fig_eq.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=40))
        grafico_html = _plot_html(fig_eq, "eq_cri",
                                    "Equity histórico cripto")

    # Tabla de posiciones live
    pos_html = "<p style='color:var(--muted)'>Sin posiciones</p>"
    if posiciones:
        filas = []
        for p in posiciones:
            sym = p.get("par") or p.get("simbolo", "?")
            filas.append(
                f"<tr><td>{sym}</td><td>{p.get('qty','')}</td>"
                f"<td>{_fmt_money(p.get('precio_entrada'))}</td>"
                f"<td>{_fmt_money(p.get('precio_actual'))}</td>"
                f"<td>{_fmt_money(p.get('valor_actual'))}</td>"
                f"<td class='{_cls_pnl(p.get('pnl_usd'))}'>{_fmt_money(p.get('pnl_usd'), True)}</td>"
                f"<td class='{_cls_pnl(p.get('pnl_pct'))}'>{_fmt_pct(p.get('pnl_pct'))}</td></tr>"
            )
        pos_html = (
            "<table><thead><tr><th>Par</th><th>Qty</th><th>Entrada</th>"
            "<th>Actual</th><th>Valor</th><th>P&L $</th><th>P&L %</th>"
            "</tr></thead><tbody>" + "".join(filas) + "</tbody></table>"
        )

    # Tabla de trades (ya deduplicados). BUG-03/10: P&L "—" para BUY.
    filas_tr = []
    for t in trs[:100]:
        acc = t["accion"]
        qty_v = t.get("qty")
        qty_cell = (f"{qty_v:g}" if isinstance(qty_v, (int, float)) and qty_v else "—")
        score_v = t.get("score")
        score_cell = f"{score_v:+d}" if isinstance(score_v, int) else "—"
        if acc == "BUY":
            pnl_usd_cell = pnl_pct_cell = "—"
            pnl_usd_cls = pnl_pct_cls = ""
        else:
            pnl_usd = t.get("pnl_usd")
            pnl_pct = t.get("pnl_pct")
            pnl_usd_cell = (_fmt_money(pnl_usd, True)
                            if pnl_usd is not None else "—")
            pnl_pct_cell = _fmt_pct(pnl_pct) if pnl_pct is not None else "—"
            pnl_usd_cls = _cls_pnl(pnl_usd) if pnl_usd is not None else ""
            pnl_pct_cls = _cls_pnl(pnl_pct) if pnl_pct is not None else ""
        filas_tr.append(
            f"<tr><td>{t['timestamp']}</td>"
            f"<td>{t['simbolo']}</td>"
            f"<td><span class='pill {acc.lower()}'>{acc}</span></td>"
            f"<td>{qty_cell}</td>"
            f"<td>{_fmt_money(t.get('precio'))}</td>"
            f"<td>{score_cell}</td>"
            f"<td class='{pnl_usd_cls}'>{pnl_usd_cell}</td>"
            f"<td class='{pnl_pct_cls}'>{pnl_pct_cell}</td>"
            f"</tr>"
        )
    # BUG-14: la nota de dedup ya no es visible. Se preserva como tooltip
    # (title=) sobre el contador para quien quiera inspeccionar.
    dedup_title = f"Dedup 30 min: {len(trs_raw)} trades brutos → {len(trs)} visibles"

    # Python 3.10 no permite backslashes en expresiones f-string — extraer.
    _empty_tr_cripto = (
        '<tr><td colspan=8 style="color:var(--muted)">Sin trades</td></tr>'
    )
    _filas_tr_html = "".join(filas_tr) or _empty_tr_cripto

    body = f"""
    <div class='grid'>
      <div class='card'><h3>Equity cripto</h3>
        <div class='value'>{_fmt_money(eq_actual)}</div>
        <div style='color:var(--muted);font-size:11px;margin-top:4px'>
          posiciones {_fmt_money(valor_pos)} + USDT {_fmt_money(usdt_cash)}
        </div></div>
      <div class='card'><h3>Posiciones</h3>
        <div class='value'>{n_pos}</div></div>
      <div class='card' title="{dedup_title}"><h3>Trades totales</h3>
        <div class='value'>{len(trs)}</div></div>
    </div>
    <div class='panel'><h2>Equity histórico (cripto)</h2>{grafico_html}</div>
    <div class='panel'><h2>Posiciones actuales</h2>{pos_html}</div>
    <div class='panel'><h2>Trades cripto ({len(trs)})</h2>
      <table><thead><tr>
        <th>Fecha</th><th>Par</th><th>Acción</th><th>Qty</th>
        <th>Precio</th><th>Score</th><th>P&amp;L $</th><th>P&amp;L %</th>
      </tr></thead><tbody>{_filas_tr_html}</tbody></table>
    </div>
    """
    return _page("Cripto", "cripto", body)


# ── Endpoint JSON ──────────────────────────────────────────
@app.route("/api/resumen")
def api_resumen():
    """Resumen DB + datos live para que el JS de la home page actualice sin
    hacer hard reload. BUG-16.
    """
    from flask import jsonify
    resumen = db.get_resumen_general()

    # Live equity / P&L (best-effort, sin fallar el endpoint)
    live = {}
    try:
        eq_acc, _, _ = _acciones_equity_live()
        eq_cri, _, _, _ = _cripto_equity_live()
        equity_total = round(eq_acc + eq_cri, 2)
        live["equity"] = _fmt_money(equity_total)
        live["trades_total"] = resumen.get("trades_total", 0)
        wr = resumen.get("win_rate")
        live["win_rate"] = f"{wr:.1f}%" if wr is not None else "—"

        # Recalcular pnl hoy/semana/total con la misma lógica de home()
        snaps = db.get_snapshots()
        from datetime import datetime as _dt, timedelta as _td

        def _eq_hasta(fecha):
            acc = cri = None
            for s in snaps:
                if s["fecha"] > fecha:
                    break
                if s.get("equity_acciones") and s["equity_acciones"] > 0:
                    acc = s["equity_acciones"]
                if s.get("equity_cripto") and s["equity_cripto"] > 0:
                    cri = s["equity_cripto"]
            if acc is None and cri is None:
                return None
            return (acc or 0) + (cri or 0)

        ayer = (_dt.now() - _td(days=1)).strftime("%Y-%m-%d")
        semana = (_dt.now() - _td(days=7)).strftime("%Y-%m-%d")
        mes = (_dt.now() - _td(days=30)).strftime("%Y-%m-%d")
        eq_ayer = _eq_hasta(ayer)
        eq_sem = _eq_hasta(semana)
        eq_mes = _eq_hasta(mes)
        pnl_hoy = round(equity_total - eq_ayer, 2) if eq_ayer else None
        pnl_sem = round(equity_total - eq_sem, 2) if eq_sem else None
        pnl_mes = round(equity_total - eq_mes, 2) if eq_mes else None
        if pnl_mes is None and snaps:
            primer_eq = ((snaps[0].get("equity_acciones") or 0)
                         + (snaps[0].get("equity_cripto") or 0))
            if primer_eq > 0:
                pnl_mes = round(equity_total - primer_eq, 2)
        live["pnl_hoy"] = _fmt_money(pnl_hoy, True) if pnl_hoy is not None else "—"
        live["pnl_sem"] = _fmt_money(pnl_sem, True) if pnl_sem is not None else "—"
        live["pnl_mes"] = _fmt_money(pnl_mes, True) if pnl_mes is not None else "—"
    except Exception as e:
        print(f"[dashboard] /api/resumen live falló: {e}")

    return jsonify({**resumen, "live": live})


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Dashboard — http://localhost:8050")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8050, debug=False)
