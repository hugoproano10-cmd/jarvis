#!/home/hproano/asistente_env/bin/python
"""
Señales sociales y alternativas — 3 fuentes de datos de alto impacto.

1) Reddit Sentiment (r/wallstreetbets + r/stocks) — reemplazo de StockTwits
2) Alpha Vantage Earnings Estimates — crowd vs Wall Street consensus
3) SEC EDGAR R&D — gasto en I+D como proxy de innovación (reemplazo de USPTO)
"""

import os
import sys
import importlib.util
import logging
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(PROYECTO, ".env"))

AV_KEY = os.getenv("ALPHA_VANTAGE_PREMIUM_KEY", "")

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS = _cfg.ACTIVOS_OPERABLES

log = logging.getLogger("senales-sociales")

REDDIT_HEADERS = {"User-Agent": "JARVIS-Trading/1.0"}


# ══════════════════════════════════════════════════════════════
#  1. REDDIT SENTIMENT (r/wallstreetbets + r/stocks)
# ══════════════════════════════════════════════════════════════

def get_reddit_signal(simbolo):
    """
    Busca menciones recientes en WSB y r/stocks.
    Analiza score y ratio bullish/bearish por títulos.
    """
    BULLISH = {"buy", "long", "calls", "bull", "moon", "rocket", "yolo", "gain", "up", "beat"}
    BEARISH = {"sell", "short", "puts", "bear", "crash", "dump", "loss", "down", "miss", "tank"}

    total_posts = 0
    total_score = 0
    bull_count = 0
    bear_count = 0
    top_posts = []

    for sub in ["wallstreetbets", "stocks"]:
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": simbolo, "sort": "new", "limit": 10, "restrict_sr": "1", "t": "week"}
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            posts = resp.json().get("data", {}).get("children", [])

            for p in posts:
                d = p["data"]
                titulo = d.get("title", "").lower()
                score = d.get("score", 0)
                total_posts += 1
                total_score += score

                words = set(titulo.split())
                if words & BULLISH:
                    bull_count += 1
                if words & BEARISH:
                    bear_count += 1

                top_posts.append({
                    "titulo": d.get("title", "")[:80],
                    "score": score,
                    "sub": sub,
                })
        except Exception as e:
            log.warning(f"Reddit r/{sub} error for {simbolo}: {e}")
        time.sleep(0.5)

    if total_posts == 0:
        return {"senal": "SIN_DATOS", "menciones": 0}

    bullish_pct = (bull_count / total_posts * 100) if total_posts > 0 else 50
    bearish_pct = (bear_count / total_posts * 100) if total_posts > 0 else 50

    if bullish_pct > 60 and bull_count > bear_count * 2:
        senal = "ALCISTA"
    elif bearish_pct > 60 and bear_count > bull_count * 2:
        senal = "BAJISTA"
    else:
        senal = "NEUTRAL"

    return {
        "senal": senal,
        "menciones": total_posts,
        "bullish_pct": round(bullish_pct, 1),
        "bearish_pct": round(bearish_pct, 1),
        "score_total": total_score,
        "top_posts": sorted(top_posts, key=lambda x: x["score"], reverse=True)[:3],
    }


# ══════════════════════════════════════════════════════════════
#  2. EARNINGS ESTIMATES (Alpha Vantage)
# ══════════════════════════════════════════════════════════════

def get_earnings_estimate_signal(simbolo):
    """
    Compara EPS real vs estimado de los últimos trimestres.
    Si consistentemente supera → BEAT (alcista).
    """
    if not AV_KEY:
        return {"senal": "N/D", "error": "Sin API key"}

    try:
        resp = requests.get("https://www.alphavantage.co/query", params={
            "function": "EARNINGS", "symbol": simbolo, "apikey": AV_KEY
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        quarterly = data.get("quarterlyEarnings", [])[:4]
        if not quarterly:
            return {"senal": "N/D", "error": "Sin datos de earnings"}

        beats = 0
        misses = 0
        sorpresas = []

        for q in quarterly:
            try:
                actual = float(q.get("reportedEPS", 0))
                estimado = float(q.get("estimatedEPS", 0))
            except (ValueError, TypeError):
                continue

            if estimado == 0:
                continue

            diff_pct = ((actual - estimado) / abs(estimado)) * 100
            sorpresas.append(diff_pct)

            if actual > estimado:
                beats += 1
            elif actual < estimado:
                misses += 1

        if not sorpresas:
            return {"senal": "N/D"}

        avg_sorpresa = sum(sorpresas) / len(sorpresas)

        if beats >= 3:
            senal = "BEAT"
        elif misses >= 3:
            senal = "MISS"
        else:
            senal = "INLINE"

        return {
            "senal": senal,
            "beats": beats,
            "misses": misses,
            "total": len(sorpresas),
            "sorpresa_avg_pct": round(avg_sorpresa, 2),
            "ultimo_eps": quarterly[0].get("reportedEPS"),
            "ultimo_estimado": quarterly[0].get("estimatedEPS"),
        }

    except Exception as e:
        return {"senal": "N/D", "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  3. SEC EDGAR R&D PROXY (filings recientes)
# ══════════════════════════════════════════════════════════════

EMPRESA_CIK = {
    "AAPL": "0000320193", "META": "0001326801", "IBM": "0000051143",
    "JNJ": "0000200406", "XOM": "0000034088", "TSLA": "0001318605",
    "KO": "0000021344", "MCD": "0000789019",
}

SEC_HEADERS = {"User-Agent": "JARVIS Trading research@jarvis-trading.local", "Accept": "application/json"}


def get_innovation_signal(simbolo):
    """
    Cuenta filings recientes en SEC EDGAR como proxy de actividad corporativa.
    Más filings recientes = empresa activa = señal positiva.
    """
    cik = EMPRESA_CIK.get(simbolo)
    if not cik:
        return {"senal": "N/D", "error": "Sin CIK"}

    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])

        # Contar filings últimos 90 días
        cutoff = datetime.now().strftime("%Y-%m-%d")
        from datetime import timedelta
        cutoff_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        filings_90d = sum(1 for d in dates if d >= cutoff_90)
        filings_total = len(forms)

        # Buscar 10-K, 10-Q, 8-K específicos
        n_10k = sum(1 for f in forms[:20] if "10-K" in f)
        n_10q = sum(1 for f in forms[:20] if "10-Q" in f)
        n_8k = sum(1 for f in forms[:20] if "8-K" in f)

        if filings_90d > 15:
            senal = "MUY_ACTIVA"
        elif filings_90d > 8:
            senal = "ACTIVA"
        else:
            senal = "NORMAL"

        return {
            "senal": senal,
            "filings_90d": filings_90d,
            "filings_total": filings_total,
            "empresa": data.get("name", ""),
            "10k": n_10k,
            "10q": n_10q,
            "8k": n_8k,
        }

    except Exception as e:
        return {"senal": "N/D", "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  FUNCIÓN COMBINADA
# ══════════════════════════════════════════════════════════════

def get_senales_sociales(simbolo):
    """Retorna las 3 señales sociales/alternativas para un símbolo."""
    return {
        "simbolo": simbolo,
        "reddit": get_reddit_signal(simbolo),
        "earnings_est": get_earnings_estimate_signal(simbolo),
        "sec_actividad": get_innovation_signal(simbolo),
    }


def get_senales_sociales_batch(simbolos=None):
    """Evalúa múltiples activos."""
    if simbolos is None:
        simbolos = ACTIVOS[:5]
    return {sym: get_senales_sociales(sym) for sym in simbolos}


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 80)
    print(f"  SEÑALES SOCIALES Y ALTERNATIVAS — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    test = ["IBM", "META", "JNJ"]

    for sym in test:
        print(f"\n{'─' * 60}")
        print(f"  {sym}")
        print(f"{'─' * 60}")
        r = get_senales_sociales(sym)

        # Reddit
        rd = r["reddit"]
        print(f"  Reddit: {rd['senal']} ({rd.get('menciones',0)} menciones, "
              f"bull:{rd.get('bullish_pct',0):.0f}% bear:{rd.get('bearish_pct',0):.0f}%)")
        for p in rd.get("top_posts", [])[:2]:
            print(f"    [{p['score']}pts r/{p['sub']}] {p['titulo']}")

        # Earnings estimates
        ee = r["earnings_est"]
        print(f"  Earnings: {ee['senal']} ({ee.get('beats',0)} beats / {ee.get('total',0)} trimestres, "
              f"sorpresa avg: {ee.get('sorpresa_avg_pct',0):+.1f}%)")

        # SEC actividad
        sec = r["sec_actividad"]
        print(f"  SEC: {sec['senal']} ({sec.get('filings_90d',0)} filings 90d, "
              f"10-K:{sec.get('10k',0)} 10-Q:{sec.get('10q',0)} 8-K:{sec.get('8k',0)})")

    print(f"\n{'=' * 80}")
