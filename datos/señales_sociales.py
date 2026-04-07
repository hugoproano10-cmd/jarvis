#!/home/hproano/asistente_env/bin/python
"""
Señales sociales para JARVIS — Job Postings + Reddit Sentiment.

1) JOB POSTINGS: Detecta expansión/contracción de empresas via Indeed RSS.
   - Si postings suben >30% en 7 días → EXPANSIÓN → señal alcista +1
   - Si postings bajan >30% → CONTRACCIÓN → señal bajista -1

2) REDDIT SENTIMENT: Sentimiento de comunidad inversora (WSB, investing, stocks).
   - Cuenta menciones, analiza sentimiento básico (palabras positivas/negativas)
   - Si menciones suben >50% vs semana anterior → señal de momentum retail
"""

import os
import re
import time
import json
import logging
import importlib.util
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from collections import defaultdict

import requests
import chromadb

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# ── Config ────────────────────────────────────────────────────
_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

log = logging.getLogger("señales-sociales")

# ── ChromaDB para historial semanal ───────────────────────────
MEMORIA_DIR = os.path.join(PROYECTO, "datos", "memoria")
os.makedirs(MEMORIA_DIR, exist_ok=True)

_client = chromadb.PersistentClient(path=MEMORIA_DIR)

_col_jobs = _client.get_or_create_collection(
    name="job_postings_weekly",
    metadata={"hnsw:space": "cosine"},
)

_col_reddit = _client.get_or_create_collection(
    name="reddit_sentiment_weekly",
    metadata={"hnsw:space": "cosine"},
)

# ── Mapeo símbolo → nombre empresa para búsquedas ────────────
EMPRESA_NOMBRES = {
    "XOM": "Exxon Mobil",
    "JNJ": "Johnson Johnson",
    "GLD": "SPDR Gold",
    "IBM": "IBM",
    "HYG": "iShares High Yield",
    "VZ": "Verizon",
    "XLU": "Utilities Select",
    "META": "Meta Platforms",
    "T": "AT&T",
    "SOXX": "iShares Semiconductor",
    "XLC": "Communication Services",
    "AGG": "iShares Core Bond",
    "MCD": "McDonalds",
    "D": "Dominion Energy",
    "EEM": "iShares Emerging Markets",
    "EFA": "iShares EAFE",
    "IEF": "iShares Treasury",
    "TSLA": "Tesla",
    "KO": "Coca Cola",
    "XLE": "Energy Select",
    "SPY": "SPDR S&P 500",
    "AAPL": "Apple",
}

# Solo empresas individuales tienen sentido para job postings
EMPRESAS_CON_JOBS = {
    "XOM", "JNJ", "IBM", "VZ", "META", "T", "MCD", "D",
    "TSLA", "KO", "AAPL",
}

# ── Headers comunes ───────────────────────────────────────────
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ================================================================
#  1. JOB POSTINGS — Señal de expansión / contracción
# ================================================================

def _fetch_indeed_rss(empresa: str) -> int:
    """Cuenta postings desde Indeed RSS feed para una empresa."""
    url = f"https://www.indeed.com/rss?q={requests.utils.quote(empresa)}&l="
    # Indeed bloquea bots; usar session con cookies
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        # Primero visitar la página principal para obtener cookies
        session.get("https://www.indeed.com/", timeout=10)
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")
        return len(items)
    except Exception as e:
        log.warning("Indeed RSS error para %s: %s", empresa, e)
        return 0


def _fetch_google_jobs(empresa: str) -> int:
    """Fallback: estimar postings via Google search site:indeed.com."""
    query = f'"{empresa}" jobs site:indeed.com'
    url = "https://www.google.com/search"
    params = {"q": query, "num": 10}
    headers = {**_HEADERS, "Referer": "https://www.google.com/"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            # Contar resultados de búsqueda como proxy de postings
            match = re.search(r'About\s+([\d,]+)\s+results', resp.text)
            if match:
                total = int(match.group(1).replace(",", ""))
                # Normalizar: Google muestra todos los resultados, escalar a postings recientes
                return min(total // 10, 500)
            # Contar links a indeed como alternativa
            indeed_links = re.findall(r'indeed\.com/(?:rc/|viewjob)', resp.text)
            return len(indeed_links)
        return 0
    except Exception as e:
        log.warning("Google Jobs fallback error para %s: %s", empresa, e)
        return 0


def _fetch_linkedin_jobs(empresa: str) -> int:
    """Cuenta postings desde LinkedIn jobs API pública (guest)."""
    # LinkedIn guest API — no requiere auth
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    params = {"keywords": empresa, "location": "United States",
              "f_TPR": "r604800", "start": 0}
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            # Contar tarjetas de empleo en el HTML
            cards = re.findall(r'class="base-card', resp.text)
            count = len(cards)
            if count == 0:
                # Alternativa: buscar data-entity-urn
                cards = re.findall(r'data-entity-urn', resp.text)
                count = len(cards)
            return count
        # Fallback a URL pública estándar
        url2 = "https://www.linkedin.com/jobs/search/"
        params2 = {"keywords": empresa, "location": "United States", "f_TPR": "r604800"}
        resp2 = requests.get(url2, params=params2, headers=_HEADERS, timeout=15)
        if resp2.status_code == 200:
            match = re.search(r'([\d,]+)\s+results?', resp2.text)
            if match:
                return int(match.group(1).replace(",", ""))
            cards = re.findall(r'class="base-card"', resp2.text)
            return len(cards)
        return 0
    except Exception as e:
        log.warning("LinkedIn jobs error para %s: %s", empresa, e)
        return 0


def _guardar_jobs_semanal(simbolo: str, conteo: int):
    """Guarda conteo de jobs en ChromaDB para comparación semanal."""
    semana = datetime.now().strftime("%Y-W%W")
    doc_id = f"jobs_{simbolo}_{semana}"
    ts = datetime.now().isoformat()

    _col_jobs.upsert(
        ids=[doc_id],
        documents=[f"{simbolo} job postings: {conteo} ({semana})"],
        metadatas=[{
            "simbolo": simbolo,
            "semana": semana,
            "conteo": conteo,
            "timestamp": ts,
        }],
    )


def _obtener_jobs_semana_anterior(simbolo: str) -> int | None:
    """Recupera conteo de jobs de la semana anterior desde ChromaDB."""
    semana_ant = (datetime.now() - timedelta(days=7)).strftime("%Y-W%W")
    doc_id = f"jobs_{simbolo}_{semana_ant}"
    try:
        result = _col_jobs.get(ids=[doc_id], include=["metadatas"])
        if result["metadatas"]:
            return result["metadatas"][0].get("conteo", 0)
    except Exception:
        pass
    return None


def get_job_signal(simbolo: str) -> dict:
    """
    Señal de expansión/contracción basada en job postings.

    Returns:
        {"señal": "EXPANSIÓN"/"NEUTRAL"/"CONTRACCIÓN",
         "cambio_pct": float,
         "postings_actual": int,
         "postings_anterior": int | None,
         "fuentes": {"indeed": int, "linkedin": int}}
    """
    if simbolo not in EMPRESA_NOMBRES:
        return {"señal": "N/D", "cambio_pct": 0, "postings_actual": 0,
                "postings_anterior": None, "fuentes": {},
                "nota": f"{simbolo} no mapeado a empresa"}

    if simbolo not in EMPRESAS_CON_JOBS:
        return {"señal": "N/A", "cambio_pct": 0, "postings_actual": 0,
                "postings_anterior": None, "fuentes": {},
                "nota": f"{simbolo} es ETF, no aplica job postings"}

    empresa = EMPRESA_NOMBRES[simbolo]
    log.info("Buscando job postings para %s (%s)...", simbolo, empresa)

    # Consultar fuentes (Indeed → Google fallback si Indeed falla)
    indeed = _fetch_indeed_rss(empresa)
    time.sleep(1)  # Rate limiting
    google_fb = 0
    if indeed == 0:
        google_fb = _fetch_google_jobs(empresa)
        time.sleep(1)
    linkedin = _fetch_linkedin_jobs(empresa)

    total = indeed + google_fb + linkedin

    # Guardar en ChromaDB
    _guardar_jobs_semanal(simbolo, total)

    # Comparar con semana anterior
    anterior = _obtener_jobs_semana_anterior(simbolo)

    if anterior is not None and anterior > 0:
        cambio_pct = ((total - anterior) / anterior) * 100
    else:
        cambio_pct = 0.0

    # Determinar señal
    if cambio_pct > 30:
        senal = "EXPANSIÓN"
    elif cambio_pct < -30:
        senal = "CONTRACCIÓN"
    else:
        senal = "NEUTRAL"

    return {
        "señal": senal,
        "cambio_pct": round(cambio_pct, 1),
        "postings_actual": total,
        "postings_anterior": anterior,
        "fuentes": {"indeed": indeed, "google_fallback": google_fb, "linkedin": linkedin},
    }


# ================================================================
#  2. REDDIT SENTIMENT — Comunidad inversora
# ================================================================

# Palabras para análisis de sentimiento básico
PALABRAS_POSITIVAS = {
    "buy", "long", "calls", "moon", "rocket", "bullish", "bull",
    "up", "rally", "breakout", "gains", "profit", "winner", "strong",
    "love", "great", "amazing", "undervalued", "cheap", "opportunity",
    "holding", "diamond", "hands", "squeeze", "gamma", "yolo",
    "tendies", "print", "money", "growth", "beat", "earnings",
    "upgrade", "outperform", "buy the dip",
}

PALABRAS_NEGATIVAS = {
    "sell", "short", "puts", "crash", "dump", "bearish", "bear",
    "down", "fall", "drop", "loss", "loser", "weak", "bag",
    "hate", "terrible", "overvalued", "expensive", "bubble",
    "paper", "panic", "fear", "recession", "bankrupt", "fraud",
    "scam", "rug", "worthless", "downgrade", "underperform",
    "dead", "rip", "guh", "loss porn",
}

SUBREDDITS = ["wallstreetbets", "investing", "stocks"]


def _fetch_reddit_posts(subreddit: str, simbolo: str, limit: int = 25) -> list[dict]:
    """Busca posts en un subreddit sobre un símbolo usando old.reddit.com."""
    # Reddit bloquea el dominio principal para bots; old.reddit.com + JSON es más permisivo
    urls_intentar = [
        f"https://old.reddit.com/r/{subreddit}/search.json",
        f"https://www.reddit.com/r/{subreddit}/search.json",
    ]
    params = {
        "q": simbolo,
        "sort": "new",
        "limit": limit,
        "restrict_sr": "on",
        "t": "week",
    }
    # Reddit requiere un User-Agent descriptivo; bots genéricos son bloqueados
    headers = {
        "User-Agent": f"linux:jarvis-trading-signals:v1.0 (by /u/jarvis_bot)",
        "Accept": "application/json",
    }

    for url in urls_intentar:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                # Rate limited — esperar y reintentar
                time.sleep(5)
                resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                posts.append({
                    "titulo": post.get("title", ""),
                    "texto": post.get("selftext", "")[:500],
                    "score": post.get("score", 0),
                    "comentarios": post.get("num_comments", 0),
                    "creado": post.get("created_utc", 0),
                    "subreddit": subreddit,
                })
            if posts:
                return posts
        except Exception as e:
            log.warning("Reddit error r/%s %s (%s): %s", subreddit, simbolo,
                        url.split("/")[2], e)
            continue

    # Fallback: scraping de la página HTML de old.reddit.com
    return _fetch_reddit_html_fallback(subreddit, simbolo)


def _fetch_reddit_html_fallback(subreddit: str, simbolo: str) -> list[dict]:
    """Fallback: extraer posts de Reddit via HTML si JSON falla."""
    url = f"https://old.reddit.com/r/{subreddit}/search"
    params = {"q": simbolo, "sort": "new", "restrict_sr": "on", "t": "week"}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Accept": "text/html",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []

        posts = []
        # Extraer títulos de posts
        titulos = re.findall(
            r'class="search-title[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>', resp.text)
        scores = re.findall(
            r'class="search-score"[^>]*>\s*([\d,]+)\s*points?', resp.text)
        comentarios = re.findall(
            r'class="search-comments"[^>]*>\s*<a[^>]*>\s*([\d,]+)\s*comments?', resp.text)

        for i, titulo in enumerate(titulos[:25]):
            score = int(scores[i].replace(",", "")) if i < len(scores) else 0
            ncom = int(comentarios[i].replace(",", "")) if i < len(comentarios) else 0
            posts.append({
                "titulo": titulo.strip(),
                "texto": "",
                "score": score,
                "comentarios": ncom,
                "creado": 0,
                "subreddit": subreddit,
            })
        return posts
    except Exception as e:
        log.warning("Reddit HTML fallback error r/%s %s: %s", subreddit, simbolo, e)
        return []


def _analizar_sentimiento_texto(texto: str) -> tuple[int, int]:
    """Cuenta palabras positivas y negativas en un texto."""
    texto_lower = texto.lower()
    palabras = re.findall(r'\b[a-z]+\b', texto_lower)

    positivas = sum(1 for p in palabras if p in PALABRAS_POSITIVAS)
    negativas = sum(1 for p in palabras if p in PALABRAS_NEGATIVAS)

    # También buscar frases compuestas
    for frase in PALABRAS_POSITIVAS:
        if " " in frase and frase in texto_lower:
            positivas += 1
    for frase in PALABRAS_NEGATIVAS:
        if " " in frase and frase in texto_lower:
            negativas += 1

    return positivas, negativas


def _guardar_reddit_semanal(simbolo: str, menciones: int, score_sentimiento: float):
    """Guarda datos de Reddit en ChromaDB para comparación semanal."""
    semana = datetime.now().strftime("%Y-W%W")
    doc_id = f"reddit_{simbolo}_{semana}"
    ts = datetime.now().isoformat()

    _col_reddit.upsert(
        ids=[doc_id],
        documents=[f"{simbolo} Reddit: {menciones} menciones, sent={score_sentimiento:.2f} ({semana})"],
        metadatas=[{
            "simbolo": simbolo,
            "semana": semana,
            "menciones": menciones,
            "sentimiento_score": round(score_sentimiento, 3),
            "timestamp": ts,
        }],
    )


def _obtener_reddit_semana_anterior(simbolo: str) -> dict | None:
    """Recupera datos de Reddit de la semana anterior desde ChromaDB."""
    semana_ant = (datetime.now() - timedelta(days=7)).strftime("%Y-W%W")
    doc_id = f"reddit_{simbolo}_{semana_ant}"
    try:
        result = _col_reddit.get(ids=[doc_id], include=["metadatas"])
        if result["metadatas"]:
            return result["metadatas"][0]
    except Exception:
        pass
    return None


def get_reddit_signal(simbolo: str) -> dict:
    """
    Señal de sentimiento de Reddit (WSB, investing, stocks).

    Returns:
        {"menciones": int,
         "sentimiento": "positivo"/"negativo"/"neutral",
         "sentimiento_score": float (-1 a 1),
         "cambio": float (% cambio menciones vs semana anterior),
         "momentum_retail": bool,
         "por_subreddit": dict,
         "top_posts": list}
    """
    log.info("Analizando Reddit para %s...", simbolo)

    todos_posts = []
    por_subreddit = {}

    for sub in SUBREDDITS:
        posts = _fetch_reddit_posts(sub, simbolo)
        por_subreddit[sub] = len(posts)
        todos_posts.extend(posts)
        time.sleep(2)  # Rate limiting Reddit

    menciones = len(todos_posts)

    # Análisis de sentimiento agregado
    total_pos = 0
    total_neg = 0
    for post in todos_posts:
        texto_completo = f"{post['titulo']} {post['texto']}"
        pos, neg = _analizar_sentimiento_texto(texto_completo)
        # Ponderar por score del post (más upvotes = más peso)
        peso = max(1, post["score"])
        total_pos += pos * peso
        total_neg += neg * peso

    # Score normalizado -1 a 1
    total_palabras = total_pos + total_neg
    if total_palabras > 0:
        score = (total_pos - total_neg) / total_palabras
    else:
        score = 0.0

    if score > 0.15:
        sentimiento = "positivo"
    elif score < -0.15:
        sentimiento = "negativo"
    else:
        sentimiento = "neutral"

    # Guardar en ChromaDB
    _guardar_reddit_semanal(simbolo, menciones, score)

    # Comparar con semana anterior
    anterior = _obtener_reddit_semana_anterior(simbolo)
    if anterior and anterior.get("menciones", 0) > 0:
        cambio_menciones = ((menciones - anterior["menciones"]) / anterior["menciones"]) * 100
    else:
        cambio_menciones = 0.0

    momentum_retail = cambio_menciones > 50

    # Top posts por score
    top_posts = sorted(todos_posts, key=lambda p: p["score"], reverse=True)[:5]
    top_posts_resumen = [
        {"titulo": p["titulo"][:100], "score": p["score"],
         "comentarios": p["comentarios"], "subreddit": p["subreddit"]}
        for p in top_posts
    ]

    return {
        "menciones": menciones,
        "sentimiento": sentimiento,
        "sentimiento_score": round(score, 3),
        "cambio": round(cambio_menciones, 1),
        "momentum_retail": momentum_retail,
        "por_subreddit": por_subreddit,
        "top_posts": top_posts_resumen,
    }


# ================================================================
#  3. Señal combinada social
# ================================================================

def get_señales_sociales(activos: list[str] | None = None) -> dict:
    """
    Combina Job Postings + Reddit Sentiment para una lista de activos.

    Returns:
        {simbolo: {"jobs": {...}, "reddit": {...}, "senal_social": int}}
        senal_social: +2 muy alcista, +1 alcista, 0 neutral, -1 bajista, -2 muy bajista
    """
    if activos is None:
        activos = ACTIVOS_OPERABLES[:5]

    resultado = {}
    for sym in activos:
        jobs = get_job_signal(sym)
        time.sleep(1)
        reddit = get_reddit_signal(sym)

        # Calcular señal combinada
        puntos = 0

        # Jobs: expansión +1, contracción -1
        if jobs["señal"] == "EXPANSIÓN":
            puntos += 1
        elif jobs["señal"] == "CONTRACCIÓN":
            puntos -= 1

        # Reddit: sentimiento positivo +1, negativo -1
        if reddit["sentimiento"] == "positivo":
            puntos += 1
        elif reddit["sentimiento"] == "negativo":
            puntos -= 1

        # Bonus: momentum retail con sentimiento positivo
        if reddit["momentum_retail"] and reddit["sentimiento"] == "positivo":
            puntos += 1
        elif reddit["momentum_retail"] and reddit["sentimiento"] == "negativo":
            puntos -= 1

        resultado[sym] = {
            "jobs": jobs,
            "reddit": reddit,
            "senal_social": puntos,
        }

    return resultado


# ================================================================
#  4. CLI — Prueba con TSLA, META, AAPL
# ================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    TEST = ["TSLA", "META", "AAPL"]

    print("=" * 60)
    print("  SEÑALES SOCIALES — Test (TSLA, META, AAPL)")
    print("=" * 60)

    # ── Test 1: Job Postings ──
    print("\n" + "-" * 50)
    print("  [1/3] JOB POSTINGS")
    print("-" * 50)
    for sym in TEST:
        js = get_job_signal(sym)
        print(f"\n  {sym}: {js['señal']} ({js['cambio_pct']:+.1f}%)")
        f = js['fuentes']
        print(f"    Postings actuales: {js['postings_actual']} "
              f"(Indeed: {f.get('indeed', 0)}, "
              f"Google: {f.get('google_fallback', 0)}, "
              f"LinkedIn: {f.get('linkedin', 0)})")
        if js["postings_anterior"] is not None:
            print(f"    Semana anterior: {js['postings_anterior']}")

    # ── Test 2: Reddit Sentiment ──
    print("\n" + "-" * 50)
    print("  [2/3] REDDIT SENTIMENT")
    print("-" * 50)
    for sym in TEST:
        rs = get_reddit_signal(sym)
        print(f"\n  {sym}: {rs['sentimiento']} (score: {rs['sentimiento_score']:+.3f})")
        print(f"    Menciones: {rs['menciones']} (cambio: {rs['cambio']:+.1f}%)")
        print(f"    Momentum retail: {'SI' if rs['momentum_retail'] else 'NO'}")
        print(f"    Por subreddit: {rs['por_subreddit']}")
        if rs["top_posts"]:
            print(f"    Top post: {rs['top_posts'][0]['titulo'][:80]}... "
                  f"(score: {rs['top_posts'][0]['score']})")

    # ── Test 3: Señal combinada ──
    print("\n" + "-" * 50)
    print("  [3/3] SEÑAL SOCIAL COMBINADA")
    print("-" * 50)
    combinada = get_señales_sociales(TEST)
    for sym in TEST:
        c = combinada[sym]
        etiqueta = {2: "MUY ALCISTA", 1: "ALCISTA", 0: "NEUTRAL",
                    -1: "BAJISTA", -2: "MUY BAJISTA"}.get(c["senal_social"], "?")
        print(f"  {sym}: {etiqueta} (puntos: {c['senal_social']:+d})")

    print("\n" + "=" * 60)
    print("  Datos guardados en ChromaDB para comparación semanal")
    print("=" * 60)
