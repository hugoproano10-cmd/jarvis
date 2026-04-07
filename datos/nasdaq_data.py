#!/home/hproano/asistente_env/bin/python
"""
NASDAQ Data + Alpha Vantage Fundamentals para JARVIS.

Fuentes:
  - NASDAQ Data Link (SHARADAR/SF1): fundamentales históricos anuales
  - Alpha Vantage OVERVIEW: fundamentales actuales (P/E, Revenue, ROE)

Señal: si P/E razonable + Revenue creciendo + ROE alto → VALOR_CRECIMIENTO (+1 score)
"""

import os
import sys
import importlib.util
import logging
from datetime import datetime

import requests
import nasdaqdatalink
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

NASDAQ_KEY = os.getenv("NASDAQ_API_KEY", "")
AV_KEY = os.getenv("ALPHA_VANTAGE_PREMIUM_KEY", "")

nasdaqdatalink.ApiConfig.api_key = NASDAQ_KEY

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS = _cfg.ACTIVOS_OPERABLES

log = logging.getLogger("nasdaq-data")

# Promedios sectoriales de P/E para comparación
PE_SECTOR = {
    "energy": 15, "healthcare": 25, "tech": 30, "consumer": 22,
    "financial": 14, "industrial": 20, "utilities": 18, "telecom": 16,
    "bonds": 0, "international": 18, "materials": 17,
}

ACTIVO_SECTOR = {
    "XOM": "energy", "XLE": "energy",
    "JNJ": "healthcare", "PFE": "healthcare", "ABT": "healthcare",
    "META": "tech", "AAPL": "tech", "IBM": "tech", "SOXX": "tech", "XLK": "tech",
    "TSLA": "tech",
    "KO": "consumer", "MCD": "consumer", "PG": "consumer",
    "VZ": "telecom", "T": "telecom", "XLC": "telecom",
    "GLD": "materials", "XLB": "materials",
    "XLF": "financial",
    "D": "utilities", "XLU": "utilities",
    "HYG": "bonds", "AGG": "bonds", "IEF": "bonds", "TLT": "bonds",
    "EEM": "international", "EFA": "international", "VWO": "international",
    "FXI": "international",
    "SPY": "tech",  # S&P dominated by tech
}


# ══════════════════════════════════════════════════════════════
#  1. NASDAQ DATA LINK — Fundamentales históricos
# ══════════════════════════════════════════════════════════════

def _get_sharadar(simbolo, dimension="ARQ", trimestres=8):
    """Obtiene fundamentales trimestrales de SHARADAR/SF1."""
    try:
        df = nasdaqdatalink.get_table("SHARADAR/SF1", ticker=simbolo, paginate=True,
                                       dimension=dimension, calendardate={"gte": "2023-01-01"})
        if df.empty:
            return []
        cols = ["calendardate", "revenue", "netinc", "pe", "pe1", "roe", "eps",
                "epsdil", "grossmargin", "netmargin", "de", "fcf", "debt"]
        avail = [c for c in cols if c in df.columns]
        records = df[["ticker"] + avail].head(trimestres).to_dict("records")
        return records
    except Exception as e:
        log.debug(f"SHARADAR {simbolo}: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  2. ALPHA VANTAGE — Fundamentales actuales
# ══════════════════════════════════════════════════════════════

def _get_av_overview(simbolo):
    """Obtiene overview actual de Alpha Vantage."""
    if not AV_KEY:
        return None
    try:
        resp = requests.get("https://www.alphavantage.co/query", params={
            "function": "OVERVIEW", "symbol": simbolo, "apikey": AV_KEY
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "Symbol" not in data:
            return None
        return data
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  3. ANÁLISIS DE FUNDAMENTALES
# ══════════════════════════════════════════════════════════════

def _analizar_tendencia_trimestral(trimestres):
    """
    Analiza tendencia de revenue y P/E en últimos trimestres.
    Retorna dict con tendencias y señales.
    """
    if len(trimestres) < 2:
        return {"tendencia_revenue": "N/D", "tendencia_pe": "N/D"}

    # Revenue: crecimiento consecutivo
    revs = [q.get("revenue") for q in trimestres[:4] if q.get("revenue")]
    rev_creciendo = 0
    if len(revs) >= 2:
        for i in range(len(revs) - 1):
            if revs[i] > revs[i + 1]:
                rev_creciendo += 1
            else:
                break

    # P/E: compresión (bajando = bueno)
    pes = [q.get("pe") for q in trimestres[:4] if q.get("pe") and q["pe"] > 0]
    pe_comprimiendo = 0
    if len(pes) >= 2:
        for i in range(len(pes) - 1):
            if pes[i] < pes[i + 1]:
                pe_comprimiendo += 1
            else:
                break

    # EPS tendencia
    epss = [q.get("eps") for q in trimestres[:4] if q.get("eps")]
    eps_creciendo = 0
    if len(epss) >= 2:
        for i in range(len(epss) - 1):
            if epss[i] > epss[i + 1]:
                eps_creciendo += 1
            else:
                break

    return {
        "rev_creciendo_consecutivo": rev_creciendo,
        "pe_comprimiendo_consecutivo": pe_comprimiendo,
        "eps_creciendo_consecutivo": eps_creciendo,
        "revs": [round(r / 1e9, 2) for r in revs] if revs else [],
        "pes": [round(p, 2) for p in pes] if pes else [],
        "epss": [round(e, 2) for e in epss] if epss else [],
    }


def get_fundamentals(simbolo):
    """
    Obtiene fundamentales y genera señal de valor.
    Fuentes: AV OVERVIEW (actual) + SHARADAR ARQ (trimestral histórico).
    """
    av = _get_av_overview(simbolo)
    trimestres = _get_sharadar(simbolo, dimension="ARQ", trimestres=8)

    if not av:
        return {"simbolo": simbolo, "senal": "N/D", "error": "Sin datos fundamentales"}

    def _f(key, default=None):
        val = av.get(key, "None")
        if val in ("None", "-", ""):
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    pe = _f("PERatio")
    rev_growth = _f("QuarterlyRevenueGrowthYOY")
    roe = _f("ReturnOnEquityTTM")
    eps = _f("EPS")
    div_yield = _f("DividendYield")
    market_cap = _f("MarketCapitalization")
    profit_margin = _f("ProfitMargin")

    sector = ACTIVO_SECTOR.get(simbolo, "tech")
    pe_sector = PE_SECTOR.get(sector, 20)

    # Tendencia trimestral (SHARADAR)
    tendencia = _analizar_tendencia_trimestral(trimestres)

    # Señal de valor
    score_fund = 0
    notas = []

    # P/E vs sector
    if pe is not None and pe > 0:
        if pe < pe_sector * 0.8:
            score_fund += 1
            notas.append(f"P/E {pe:.1f} < sector {pe_sector} (barato)")
        elif pe > pe_sector * 1.5:
            score_fund -= 1
            notas.append(f"P/E {pe:.1f} > sector {pe_sector}x1.5 (caro)")

    # Revenue growth YoY
    if rev_growth is not None:
        if rev_growth > 0.10:
            score_fund += 1
            notas.append(f"Rev growth +{rev_growth*100:.0f}% YoY (fuerte)")
        elif rev_growth < -0.05:
            score_fund -= 1
            notas.append(f"Rev growth {rev_growth*100:.0f}% YoY (contracción)")

    # ROE
    if roe is not None:
        if roe > 0.20:
            score_fund += 1
            notas.append(f"ROE {roe*100:.0f}% (excelente)")
        elif roe < 0.05:
            score_fund -= 1
            notas.append(f"ROE {roe*100:.0f}% (débil)")

    # Tendencia trimestral: MOMENTUM_FUNDAMENTAL
    if tendencia.get("rev_creciendo_consecutivo", 0) >= 2:
        score_fund += 1
        notas.append(f"Revenue creciendo {tendencia['rev_creciendo_consecutivo']}Q consecutivos (momentum)")

    # P/E comprimiéndose: VALOR_MEJORANDO
    if tendencia.get("pe_comprimiendo_consecutivo", 0) >= 2:
        score_fund += 1
        notas.append(f"P/E bajando {tendencia['pe_comprimiendo_consecutivo']}Q consecutivos (valor mejorando)")

    # P/E subiendo: precaución
    pes = tendencia.get("pes", [])
    if len(pes) >= 2 and pes[0] > pes[1] * 1.2:
        score_fund -= 1
        notas.append(f"P/E subiendo ({pes[1]:.1f}→{pes[0]:.1f}, +{((pes[0]/pes[1])-1)*100:.0f}%)")

    # Clasificar
    if score_fund >= 3:
        senal = "VALOR_CRECIMIENTO_FUERTE"
    elif score_fund >= 2:
        senal = "VALOR_CRECIMIENTO"
    elif score_fund >= 1:
        senal = "VALOR"
    elif score_fund <= -2:
        senal = "SOBREVALORADO"
    elif score_fund <= -1:
        senal = "PRECAUCION"
    else:
        senal = "NEUTRAL"

    return {
        "simbolo": simbolo,
        "senal": senal,
        "score_fundamental": score_fund,
        "notas": notas,
        "pe": pe,
        "pe_sector": pe_sector,
        "sector": sector,
        "revenue_growth_yoy": round(rev_growth * 100, 1) if rev_growth else None,
        "roe": round(roe * 100, 1) if roe else None,
        "eps": eps,
        "dividend_yield": round(div_yield * 100, 2) if div_yield else None,
        "market_cap_b": round(market_cap / 1e9, 1) if market_cap else None,
        "profit_margin": round(profit_margin * 100, 1) if profit_margin else None,
        "nombre": av.get("Name", ""),
        "trimestres": len(trimestres),
        "tendencia": tendencia,
    }


def get_fundamentals_batch(simbolos=None):
    """Evalúa fundamentales de múltiples activos."""
    if simbolos is None:
        simbolos = ACTIVOS[:5]
    return {sym: get_fundamentals(sym) for sym in simbolos}


# ══════════════════════════════════════════════════════════════
#  4. DATASETS DISPONIBLES
# ══════════════════════════════════════════════════════════════

def listar_datasets_disponibles():
    """Lista qué datasets de NASDAQ Data Link están accesibles con la key actual."""
    datasets_test = [
        ("SHARADAR/SF1", "Fundamentals"),
        ("SHARADAR/SEP", "Daily Prices"),
        ("SHARADAR/TICKERS", "Ticker Info"),
        ("SHARADAR/SF3", "Institutional Holdings"),
        ("SHARADAR/ACTIONS", "Corporate Actions"),
        ("FRED/DFF", "Fed Funds Rate"),
        ("WIKI/AAPL", "Wiki Prices"),
        ("EOD/AAPL", "End of Day"),
        ("MULTPL/SP500_PE_RATIO_MONTH", "S&P 500 PE"),
    ]

    resultados = []
    for code, desc in datasets_test:
        try:
            if "/" in code and code.count("/") == 1 and not code.startswith("SHARADAR"):
                nasdaqdatalink.get(code, rows=1)
            else:
                nasdaqdatalink.get_table(code.split("/")[0] + "/" + code.split("/")[1],
                                          paginate=True, ticker="XOM",
                                          calendardate={"gte": "2023-01-01"})
            resultados.append((code, desc, "OK"))
        except Exception as e:
            status = "403 PREMIUM" if "403" in str(e) else f"ERROR: {str(e)[:40]}"
            resultados.append((code, desc, status))

    return resultados


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 80)
    print(f"  NASDAQ DATA + FUNDAMENTALES — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    # Datasets disponibles
    print("\n--- Datasets NASDAQ Data Link ---")
    for code, desc, status in listar_datasets_disponibles():
        icono = "OK" if status == "OK" else "NO"
        print(f"  [{icono:>3}] {code:<35} {desc:<25} {status}")

    # Fundamentales de test
    print(f"\n--- Fundamentales (AV actual + SHARADAR trimestral) ---")
    test = ["XOM", "JNJ", "IBM", "META"]

    for sym in test:
        r = get_fundamentals(sym)
        if r.get("error"):
            print(f"\n  {sym}: {r.get('error','?')}")
            continue

        pe_s = f"{r['pe']:.1f}" if r.get("pe") else "N/D"
        rg = f"{r['revenue_growth_yoy']:+.0f}%" if r.get("revenue_growth_yoy") is not None else "N/D"
        roe_s = f"{r['roe']:.0f}%" if r.get("roe") is not None else "N/D"
        mcap = f"${r['market_cap_b']:.0f}B" if r.get("market_cap_b") else "N/D"

        print(f"\n  {sym} — {r.get('nombre','')} ({r['sector']})")
        print(f"  Señal: {r['senal']} (score {r['score_fundamental']:+d})")
        print(f"  P/E: {pe_s} (sector: {r['pe_sector']}) | RevGr: {rg} | ROE: {roe_s} | MCap: {mcap}")

        # Tendencia trimestral
        t = r.get("tendencia", {})
        if t.get("revs"):
            rev_str = " → ".join(f"${v:.1f}B" for v in t["revs"])
            print(f"  Revenue Q: {rev_str} ({t['rev_creciendo_consecutivo']}Q creciendo)")
        if t.get("pes"):
            pe_str = " → ".join(f"{v:.1f}" for v in t["pes"])
            print(f"  P/E Q:     {pe_str} ({t['pe_comprimiendo_consecutivo']}Q comprimiendo)")
        if t.get("epss"):
            eps_str = " → ".join(f"${v:.2f}" for v in t["epss"])
            print(f"  EPS Q:     {eps_str} ({t['eps_creciendo_consecutivo']}Q creciendo)")

        for nota in r.get("notas", []):
            print(f"    → {nota}")

    print(f"\n{'=' * 80}")
