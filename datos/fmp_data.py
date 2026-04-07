#!/home/hproano/asistente_env/bin/python
"""
Financial Modeling Prep — DCF valuation + Price Target consensus.

Señales:
  - DCF > precio * 1.2 → INFRAVALORADO (+2 score)
  - DCF < precio * 0.8 → SOBREVALORADO (-2 score)
  - Price target consensus > precio * 1.1 → analistas ven upside
"""

import os
import logging
from datetime import datetime

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

FMP_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE = "https://financialmodelingprep.com/stable"

log = logging.getLogger("fmp-data")


def _fmp_get(endpoint, params=None):
    """Helper para llamadas a FMP stable API."""
    if not FMP_KEY:
        return {"error": "FMP_API_KEY no configurada"}
    if params is None:
        params = {}
    params["apikey"] = FMP_KEY
    try:
        resp = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "Error Message" in data:
            return {"error": data["Error Message"][:80]}
        return data
    except Exception as e:
        return {"error": str(e)[:80]}


# ══════════════════════════════════════════════════════════════
#  1. DCF VALUATION
# ══════════════════════════════════════════════════════════════

def get_dcf_valuation(simbolo):
    """
    Obtiene DCF (Discounted Cash Flow) vs precio actual.
    DCF > precio * 1.2 → INFRAVALORADO. DCF < precio * 0.8 → SOBREVALORADO.
    """
    data = _fmp_get("discounted-cash-flow", {"symbol": simbolo})

    if isinstance(data, dict) and "error" in data:
        return {"simbolo": simbolo, "senal": "N/D", "error": data["error"]}
    if not data or not isinstance(data, list) or not data[0]:
        return {"simbolo": simbolo, "senal": "N/D", "error": "Sin datos DCF"}

    d = data[0]
    dcf = d.get("dcf", 0)
    precio = d.get("Stock Price", 0)

    if not dcf or not precio or precio <= 0:
        return {"simbolo": simbolo, "senal": "N/D", "error": "Datos incompletos"}

    ratio = dcf / precio
    upside_pct = (ratio - 1) * 100

    if ratio > 1.2:
        senal = "INFRAVALORADO"
        score = 2
    elif ratio > 1.05:
        senal = "LIGERAMENTE_INFRAVALORADO"
        score = 1
    elif ratio < 0.8:
        senal = "SOBREVALORADO"
        score = -2
    elif ratio < 0.95:
        senal = "LIGERAMENTE_SOBREVALORADO"
        score = -1
    else:
        senal = "FAIR_VALUE"
        score = 0

    return {
        "simbolo": simbolo,
        "senal": senal,
        "score_dcf": score,
        "dcf": round(dcf, 2),
        "precio": round(precio, 2),
        "upside_pct": round(upside_pct, 1),
        "ratio": round(ratio, 3),
        "fecha": d.get("date", ""),
    }


# ══════════════════════════════════════════════════════════════
#  2. PRICE TARGET CONSENSUS
# ══════════════════════════════════════════════════════════════

def get_price_target(simbolo):
    """
    Obtiene consenso de precio objetivo de analistas.
    """
    data = _fmp_get("price-target-consensus", {"symbol": simbolo})

    if isinstance(data, dict) and "error" in data:
        return {"simbolo": simbolo, "senal": "N/D", "error": data["error"]}
    if not data or not isinstance(data, list) or not data[0]:
        return {"simbolo": simbolo, "senal": "N/D", "error": "Sin price target"}

    d = data[0]
    high = d.get("targetHigh", 0)
    low = d.get("targetLow", 0)
    consensus = d.get("targetConsensus", 0)
    median = d.get("targetMedian", 0)

    return {
        "simbolo": simbolo,
        "target_high": high,
        "target_low": low,
        "target_consensus": consensus,
        "target_median": median,
    }


# ══════════════════════════════════════════════════════════════
#  3. SEÑAL COMBINADA FMP
# ══════════════════════════════════════════════════════════════

def get_fmp_signal(simbolo):
    """Combina DCF + Price Target en una señal unificada."""
    dcf = get_dcf_valuation(simbolo)
    pt = get_price_target(simbolo)

    score_total = dcf.get("score_dcf", 0)
    notas = []

    # DCF
    if dcf.get("dcf") and dcf.get("precio"):
        notas.append(f"DCF: ${dcf['dcf']:.0f} vs precio ${dcf['precio']:.0f} "
                     f"({dcf['upside_pct']:+.0f}% → {dcf['senal']})")

    # Price target vs precio actual
    precio = dcf.get("precio", 0)
    consensus = pt.get("target_consensus", 0)
    if precio > 0 and consensus > 0:
        pt_upside = ((consensus / precio) - 1) * 100
        if pt_upside > 15:
            score_total += 1
            notas.append(f"PT consenso: ${consensus:.0f} ({pt_upside:+.0f}% upside)")
        elif pt_upside < -10:
            score_total -= 1
            notas.append(f"PT consenso: ${consensus:.0f} ({pt_upside:+.0f}% downside)")
        else:
            notas.append(f"PT consenso: ${consensus:.0f} ({pt_upside:+.0f}%)")

    # Señal final
    if score_total >= 2:
        senal = "MUY_INFRAVALORADO"
    elif score_total >= 1:
        senal = "INFRAVALORADO"
    elif score_total <= -2:
        senal = "MUY_SOBREVALORADO"
    elif score_total <= -1:
        senal = "SOBREVALORADO"
    else:
        senal = "FAIR_VALUE"

    return {
        "simbolo": simbolo,
        "senal": senal,
        "score": score_total,
        "notas": notas,
        "dcf": dcf,
        "price_target": pt,
    }


def get_fmp_signals_batch(simbolos):
    """Evalúa múltiples activos."""
    return {sym: get_fmp_signal(sym) for sym in simbolos}


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 80)
    print(f"  FMP VALUATION — DCF + Price Target")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    test = ["IBM", "JNJ", "META", "XOM"]

    hdr = f"  {'Sym':<6} {'Señal':<22} {'DCF':>8} {'Precio':>8} {'Upside':>8} {'PT Cons':>8} {'PT Up':>7} {'Score':>5}"
    print(hdr)
    print(f"  {'─' * 76}")

    for sym in test:
        r = get_fmp_signal(sym)
        dcf_v = r["dcf"]
        pt_v = r["price_target"]

        dcf_s = f"${dcf_v['dcf']:.0f}" if dcf_v.get("dcf") else "N/D"
        precio_s = f"${dcf_v['precio']:.0f}" if dcf_v.get("precio") else "N/D"
        up_s = f"{dcf_v['upside_pct']:+.0f}%" if dcf_v.get("upside_pct") is not None else "N/D"
        pt_s = f"${pt_v['target_consensus']:.0f}" if pt_v.get("target_consensus") else "N/D"
        pt_up = ""
        if dcf_v.get("precio") and pt_v.get("target_consensus"):
            pt_up_pct = ((pt_v["target_consensus"] / dcf_v["precio"]) - 1) * 100
            pt_up = f"{pt_up_pct:+.0f}%"

        print(f"  {sym:<6} {r['senal']:<22} {dcf_s:>8} {precio_s:>8} {up_s:>8} {pt_s:>8} {pt_up:>7} {r['score']:>+5d}")
        for nota in r.get("notas", []):
            print(f"    → {nota}")

    print(f"\n{'=' * 80}")
