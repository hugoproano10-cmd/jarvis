#!/home/hproano/asistente_env/bin/python
"""
Wikipedia Signals — Atención inusual como señal de movimiento inminente.
Si el retail busca masivamente un activo en Wikipedia → algo está pasando.
API gratuita de Wikimedia, sin API key.
"""

import os
import sys
import importlib.util
import logging
from datetime import datetime, timedelta

import requests
import numpy as np

log = logging.getLogger("wiki-signals")

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS = _cfg.ACTIVOS_OPERABLES

# ── Mapeo símbolo → artículo Wikipedia ───────────────────────

WIKI_MAP = {
    "XOM": "ExxonMobil",
    "JNJ": "Johnson_%26_Johnson",
    "GLD": "SPDR_Gold_Shares",
    "IBM": "IBM",
    "HYG": "High-yield_debt",
    "VZ": "Verizon_Communications",
    "XLU": "Utilities_Select_Sector_SPDR_Fund",
    "META": "Meta_Platforms",
    "T": "AT%26T",
    "SOXX": "PHLX_Semiconductor_Sector",
    "XLC": "Communication_Services_Select_Sector_SPDR_Fund",
    "AGG": "Bond_market_index",
    "MCD": "McDonald%27s",
    "D": "Dominion_Energy",
    "EEM": "MSCI_Emerging_Markets_Index",
    "EFA": "MSCI_EAFE",
    "IEF": "United_States_Treasury_security",
    "TSLA": "Tesla,_Inc.",
    "KO": "The_Coca-Cola_Company",
    "XLE": "Energy_Select_Sector_SPDR_Fund",
    "SPY": "S%26P_500",
    "AAPL": "Apple_Inc.",
}

WIKI_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents"


def _get_pageviews(articulo, dias=90):
    """Obtiene pageviews diarios de un artículo de Wikipedia."""
    hoy = datetime.now()
    inicio = hoy - timedelta(days=dias)
    url = f"{WIKI_BASE}/{articulo}/daily/{inicio.strftime('%Y%m%d')}/{hoy.strftime('%Y%m%d')}"

    try:
        resp = requests.get(url, headers={"User-Agent": "JARVIS-Trading/1.0"}, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [item["views"] for item in items]
    except Exception as e:
        log.warning(f"Wiki error {articulo}: {e}")
        return []


def get_wikipedia_signal(simbolo):
    """
    Analiza pageviews de Wikipedia para detectar atención inusual.
    >2x promedio → ALERTA. >3x promedio → FUERTE.
    """
    articulo = WIKI_MAP.get(simbolo)
    if not articulo:
        return {"senal": "N/D", "error": f"Sin artículo para {simbolo}"}

    views = _get_pageviews(articulo, dias=90)
    if len(views) < 10:
        return {"senal": "N/D", "error": "datos insuficientes"}

    # Últimos 7 días vs promedio de los 90
    recientes = views[-7:] if len(views) >= 7 else views[-3:]
    historico = views[:-7] if len(views) > 7 else views[:len(views)//2]

    vistas_recientes = np.mean(recientes)
    promedio_hist = np.mean(historico) if historico else 1
    vistas_hoy = views[-1] if views else 0

    if promedio_hist <= 0:
        promedio_hist = 1

    ratio = vistas_recientes / promedio_hist
    ratio_hoy = vistas_hoy / promedio_hist

    # Score 0-100
    score = min(100, int(ratio * 33))

    if ratio_hoy > 3:
        senal = "FUERTE"
    elif ratio_hoy > 2:
        senal = "ALERTA"
    elif ratio > 2:
        senal = "ALERTA"
    else:
        senal = "NORMAL"

    return {
        "simbolo": simbolo,
        "articulo": articulo,
        "senal": senal,
        "score": score,
        "vistas_hoy": vistas_hoy,
        "promedio_7d": round(vistas_recientes),
        "promedio_90d": round(promedio_hist),
        "ratio_hoy": round(ratio_hoy, 2),
        "ratio_7d": round(ratio, 2),
        "vs_promedio": f"{(ratio - 1) * 100:+.0f}%",
    }


def get_wikipedia_signals(activos=None):
    """Evalúa todos los activos y retorna dict con señales Wikipedia."""
    if activos is None:
        activos = ACTIVOS
    resultados = {}
    for sym in activos:
        resultados[sym] = get_wikipedia_signal(sym)
    return resultados


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 80)
    print(f"  WIKIPEDIA SIGNALS — Atención inusual")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    hdr = f"  {'Sym':<6} {'Señal':<8} {'Score':>5} {'Hoy':>7} {'7d avg':>7} {'90d avg':>7} {'Ratio':>6} {'vs prom':>8}"
    print(hdr)
    print(f"  {'─' * 72}")

    alertas = []
    for sym in ACTIVOS:
        r = get_wikipedia_signal(sym)
        if r.get("error"):
            print(f"  {sym:<6} {'N/D':<8} {'-':>5} {'-':>7} {'-':>7} {'-':>7} {'-':>6} {r['error'][:20]}")
            continue

        marca = ""
        if r["senal"] == "FUERTE":
            marca = " <<<"
            alertas.append(sym)
        elif r["senal"] == "ALERTA":
            marca = " <<"
            alertas.append(sym)

        print(f"  {sym:<6} {r['senal']:<8} {r['score']:>5} {r['vistas_hoy']:>7,} "
              f"{r['promedio_7d']:>7,} {r['promedio_90d']:>7,} "
              f"{r['ratio_hoy']:>5.1f}x {r['vs_promedio']:>8}{marca}")

    print(f"\n{'=' * 80}")
    if alertas:
        print(f"  ATENCIÓN INUSUAL: {', '.join(alertas)}")
    else:
        print(f"  Sin atención inusual detectada. Mercado en modo normal.")
    print(f"{'=' * 80}")
