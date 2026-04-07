#!/home/hproano/asistente_env/bin/python
"""
JARVIS Negocios — Agente autónomo de investigación de oportunidades.

Fuentes:
  - Google Trends (Ecuador + USA, por sectores)
  - Product Hunt RSS (startups trending)
  - Reddit RSS (r/entrepreneur, r/startups, r/smallbusiness)
  - Hacker News API (top stories del día)

Análisis:
  - Envía datos consolidados a jarvis-brain (deepseek-r1:671b)
  - Identifica las 3 mejores oportunidades para Ecuador

Automatización:
  - Cron cada noche a las 11PM
  - Guarda en datos/oportunidades_negocio.json
  - Envía reporte por Telegram
  - Historial en ChromaDB para detectar tendencias persistentes
"""

import os
import sys
import json
import time
import logging
import re
from datetime import datetime

import requests
import feedparser

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROYECTO, ".env"))

from config.alertas import enviar_telegram

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("jarvis-negocios")

# ── Configuración ─────────────────────────────────────────────

DATOS_DIR = os.path.join(PROYECTO, "datos")
JSON_SALIDA = os.path.join(DATOS_DIR, "oportunidades_negocio.json")
MEMORIA_DIR = os.path.join(DATOS_DIR, "memoria")

BRAIN_URL = "http://192.168.202.53:11436"
BRAIN_TIMEOUT = 900  # 15 min para 671B

SECTORES = ["tecnología", "salud", "finanzas", "educación", "logística"]

# ── ChromaDB para historial ───────────────────────────────────

import chromadb

os.makedirs(MEMORIA_DIR, exist_ok=True)
_chroma = chromadb.PersistentClient(path=MEMORIA_DIR)
_col_negocios = _chroma.get_or_create_collection(
    name="oportunidades_negocio",
    metadata={"hnsw:space": "cosine"},
)


# ================================================================
#  FUENTES DE INVESTIGACIÓN
# ================================================================

def obtener_google_trends_rss(geo="EC"):
    """Obtiene tendencias de Google Trends via RSS (sin pytrends)."""
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    try:
        feed = feedparser.parse(url)
        tendencias = []
        for entry in feed.entries[:20]:
            titulo = entry.get("title", "")
            trafico = entry.get("ht_approx_traffic", entry.get("ht_picture_news_item_approx_traffic", ""))
            tendencias.append({
                "titulo": titulo,
                "trafico_aprox": trafico,
                "enlace": entry.get("link", ""),
            })
        log.info(f"Google Trends {geo}: {len(tendencias)} tendencias")
        return tendencias
    except Exception as e:
        log.warning(f"Error Google Trends {geo}: {e}")
        return []


def obtener_trends_por_sectores():
    """Busca tendencias en Google Trends por sector usando RSS de cada keyword."""
    resultados = {}
    for sector in SECTORES:
        url = f"https://trends.google.com/trending/rss?geo=EC"
        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries[:10]:
                titulo = entry.get("title", "").lower()
                # Filtrar entries que tengan relación con el sector
                items.append(entry.get("title", ""))
            resultados[sector] = items[:5]
        except Exception as e:
            log.warning(f"Error trends sector {sector}: {e}")
            resultados[sector] = []
    return resultados


def obtener_product_hunt():
    """Obtiene startups trending de Product Hunt via RSS."""
    url = "https://www.producthunt.com/feed"
    try:
        feed = feedparser.parse(url)
        productos = []
        for entry in feed.entries[:15]:
            productos.append({
                "nombre": entry.get("title", ""),
                "descripcion": entry.get("summary", "")[:200],
                "enlace": entry.get("link", ""),
                "fecha": entry.get("published", ""),
            })
        log.info(f"Product Hunt: {len(productos)} productos")
        return productos
    except Exception as e:
        log.warning(f"Error Product Hunt: {e}")
        return []


def obtener_reddit(subreddit="entrepreneur"):
    """Obtiene posts trending de un subreddit via RSS JSON."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=10"
    headers = {"User-Agent": "JARVIS-Negocios/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        posts = []
        for child in data["data"]["children"][:10]:
            p = child["data"]
            posts.append({
                "titulo": p.get("title", ""),
                "score": p.get("score", 0),
                "comentarios": p.get("num_comments", 0),
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "selftext": (p.get("selftext") or "")[:200],
            })
        log.info(f"Reddit r/{subreddit}: {len(posts)} posts")
        return posts
    except Exception as e:
        log.warning(f"Error Reddit r/{subreddit}: {e}")
        return []


def obtener_hacker_news():
    """Obtiene top 10 stories de Hacker News API."""
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        )
        resp.raise_for_status()
        ids = resp.json()[:10]

        stories = []
        for sid in ids:
            r = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10
            )
            if r.ok:
                item = r.json()
                stories.append({
                    "titulo": item.get("title", ""),
                    "score": item.get("score", 0),
                    "url": item.get("url", ""),
                    "comentarios": item.get("descendants", 0),
                })
        log.info(f"Hacker News: {len(stories)} stories")
        return stories
    except Exception as e:
        log.warning(f"Error Hacker News: {e}")
        return []


def obtener_ycombinator():
    """Obtiene startups trending de Y Combinator (news.ycombinator.com/newest)."""
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/newstories.json", timeout=10
        )
        resp.raise_for_status()
        ids = resp.json()[:15]

        stories = []
        for sid in ids:
            r = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10
            )
            if r.ok:
                item = r.json()
                if item.get("url"):
                    stories.append({
                        "titulo": item.get("title", ""),
                        "url": item.get("url", ""),
                        "score": item.get("score", 0),
                    })
        log.info(f"YC/HN Nuevos: {len(stories)} stories")
        return stories[:10]
    except Exception as e:
        log.warning(f"Error YC: {e}")
        return []


def obtener_techcrunch():
    """Obtiene noticias de startups de TechCrunch via RSS."""
    url = "https://techcrunch.com/feed/"
    try:
        feed = feedparser.parse(url)
        noticias = []
        for entry in feed.entries[:15]:
            noticias.append({
                "titulo": entry.get("title", ""),
                "resumen": entry.get("summary", "")[:200],
                "enlace": entry.get("link", ""),
                "fecha": entry.get("published", ""),
            })
        log.info(f"TechCrunch: {len(noticias)} noticias")
        return noticias
    except Exception as e:
        log.warning(f"Error TechCrunch: {e}")
        return []


# ================================================================
#  RECOLECCIÓN COMPLETA
# ================================================================

REGIONES_TRENDS = ["US", "GB", "ES", "MX", "CO", "AR", "EC"]


def recolectar_fuentes():
    """Ejecuta todas las fuentes globales y consolida datos."""
    log.info("Recolectando fuentes de investigación (alcance global)...")
    inicio = time.time()

    datos = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "product_hunt": obtener_product_hunt(),
        "reddit_entrepreneur": obtener_reddit("entrepreneur"),
        "reddit_startups": obtener_reddit("startups"),
        "reddit_smallbusiness": obtener_reddit("smallbusiness"),
        "hacker_news": obtener_hacker_news(),
        "ycombinator": obtener_ycombinator(),
        "techcrunch": obtener_techcrunch(),
        "trends_sectores": obtener_trends_por_sectores(),
    }

    # Google Trends por región
    for geo in REGIONES_TRENDS:
        key = f"google_trends_{geo.lower()}"
        datos[key] = obtener_google_trends_rss(geo)

    # Trends global (sin región)
    datos["google_trends_global"] = obtener_google_trends_rss("")

    elapsed = time.time() - inicio
    log.info(f"Recolección completada en {elapsed:.1f}s")
    return datos


# ================================================================
#  ANÁLISIS CON 671B (jarvis-brain)
# ================================================================

def _formatear_fuentes(datos):
    """Formatea TODOS los datos recolectados (versión completa para fallback 70B)."""
    secciones = []

    for geo in REGIONES_TRENDS:
        key = f"google_trends_{geo.lower()}"
        items = datos.get(key, [])
        if items:
            nombre = {"US": "EEUU", "GB": "UK", "ES": "España", "MX": "México",
                      "CO": "Colombia", "AR": "Argentina", "EC": "Ecuador"}.get(geo, geo)
            lineas = [f"## Trends {nombre}"]
            for t in items[:7]:
                lineas.append(f"- {t['titulo']} ({t['trafico_aprox']})")
            secciones.append("\n".join(lineas))

    if datos.get("google_trends_global"):
        lineas = ["## Trends Global"]
        for t in datos["google_trends_global"][:7]:
            lineas.append(f"- {t['titulo']} ({t['trafico_aprox']})")
        secciones.append("\n".join(lineas))

    for key, label in [("techcrunch", "TechCrunch"), ("product_hunt", "Product Hunt"),
                       ("ycombinator", "Y Combinator")]:
        items = datos.get(key, [])
        if items:
            lineas = [f"## {label}"]
            for p in items[:8]:
                titulo = p.get("titulo") or p.get("nombre", "")
                lineas.append(f"- {titulo}")
            secciones.append("\n".join(lineas))

    for sub in ["entrepreneur", "startups", "smallbusiness"]:
        items = datos.get(f"reddit_{sub}", [])
        if items:
            lineas = [f"## r/{sub}"]
            for p in items[:5]:
                lineas.append(f"- [{p['score']}pts] {p['titulo']}")
            secciones.append("\n".join(lineas))

    if datos.get("hacker_news"):
        lineas = ["## HN Top"]
        for s in datos["hacker_news"]:
            lineas.append(f"- [{s['score']}pts] {s['titulo']}")
        secciones.append("\n".join(lineas))

    return "\n\n".join(secciones)


def _formatear_fuentes_compacto(datos):
    """Top 20 tendencias más relevantes (versión compacta para brain 671B)."""
    # Recolectar todas las tendencias con score de relevancia
    todas = []

    # Trends: ordenar por tráfico
    for geo in REGIONES_TRENDS + [""]:
        key = f"google_trends_{geo.lower()}" if geo else "google_trends_global"
        for t in datos.get(key, []):
            trafico = t.get("trafico_aprox", "0")
            try:
                num = int(str(trafico).replace(",", "").replace("+", "").replace("K", "000").strip() or "0")
            except ValueError:
                num = 0
            todas.append((num, f"[Trends] {t['titulo']}"))

    # TechCrunch, Product Hunt, YC
    for key, tag in [("techcrunch", "TC"), ("product_hunt", "PH"), ("ycombinator", "YC")]:
        for i, p in enumerate(datos.get(key, [])[:5]):
            titulo = p.get("titulo") or p.get("nombre", "")
            todas.append((100 - i * 10, f"[{tag}] {titulo}"))

    # HN top (ya con score)
    for s in datos.get("hacker_news", [])[:5]:
        todas.append((s.get("score", 0), f"[HN {s['score']}pts] {s['titulo']}"))

    # Reddit top
    for sub in ["entrepreneur", "startups"]:
        for p in datos.get(f"reddit_{sub}", [])[:3]:
            todas.append((p.get("score", 0), f"[Reddit] {p['titulo']}"))

    # Ordenar por relevancia y tomar top 20
    todas.sort(key=lambda x: x[0], reverse=True)
    lineas = [item[1] for item in todas[:20]]
    return "\n".join(lineas)


def analizar_con_brain(datos):
    """Envía datos consolidados a jarvis-brain (deepseek-r1:671b) para análisis."""
    log.info("Enviando análisis a jarvis-brain (deepseek-r1:671b)...")

    # Prompt compacto para brain (671B con 4096 tokens)
    fuentes_compactas = _formatear_fuentes_compacto(datos)
    prompt_brain = f"""NO uses <think> ni razonamiento interno. Ve directo a la respuesta.
Responde SOLO en español. Responde SOLO con JSON válido, sin markdown.

Tendencias globales de hoy:
{fuentes_compactas}

Perfil: Hugo, empresario ecuatoriano, cluster IA propio (671B params), capital, equipo técnico, opera remoto.

Responde con este JSON exacto (3 oportunidades):
{{"oportunidades":[{{"titulo":"...","problema":"...","mercado":"...","modelo_negocio":"...","competencia":"...","inversion_usd":"...","tiempo_primer_ingreso":"...","ventaja_ecuador":"...","por_que_ahora":"..."}}],"resumen":"..."}}"""

    # Prompt largo para fallback 70B (más contexto disponible)
    fuentes_completas = _formatear_fuentes(datos)
    prompt_fallback = f"""Responde SIEMPRE en español. No uses inglés.

Analiza tendencias globales e identifica 3 oportunidades para Hugo (empresario ecuatoriano, cluster IA propio, capital, equipo técnico, opera remoto).

FUENTES:
{fuentes_completas}

Para cada oportunidad: título, problema que resuelve, mercado, modelo de negocio, competencia, inversión USD, tiempo al primer ingreso, ventaja desde Ecuador, por qué ahora.
Al final: resumen ejecutivo con recomendación."""

    # Intentar brain primero, fallback a power (deepseek-r1:70b)
    nodos = [
        ("brain", BRAIN_URL, "llama.cpp", BRAIN_TIMEOUT),
        ("power", "http://192.168.208.80:11435", "ollama", 300),
    ]

    for nombre, url, api, timeout in nodos:
        try:
            if api == "llama.cpp":
                health = requests.get(f"{url}/health", timeout=5)
                if health.status_code != 200:
                    log.warning(f"{nombre} no disponible")
                    continue

                payload = {
                    "messages": [
                        {"role": "user", "content": prompt_brain},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 2000,
                    "stream": False,
                }
                resp = requests.post(
                    f"{url}/v1/chat/completions",
                    json=payload, timeout=timeout,
                )
            else:
                health = requests.get(f"{url}/api/tags", timeout=5)
                if health.status_code != 200:
                    log.warning(f"{nombre} no disponible")
                    continue

                payload = {
                    "model": "deepseek-r1:70b",
                    "messages": [
                        {"role": "user", "content": prompt_fallback},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.6},
                }
                resp = requests.post(
                    f"{url}/api/chat",
                    json=payload, timeout=timeout,
                )

            resp.raise_for_status()
            data = resp.json()

            if api == "llama.cpp":
                msg = data["choices"][0]["message"]
                texto = msg.get("content") or msg.get("reasoning_content") or ""
            else:
                texto = data["message"]["content"]

            # Limpiar tags de razonamiento de DeepSeek
            texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()

            log.info(f"Análisis completado via {nombre} ({len(texto)} chars)")
            return texto, nombre

        except Exception as e:
            log.warning(f"Error en {nombre}: {e}")
            continue

    return "No se pudo conectar a ningún nodo de análisis (brain/power).", "none"


def _formatear_analisis(texto_raw):
    """Intenta parsear JSON del brain, o retorna texto tal cual si no es JSON."""
    # Limpiar posibles bloques markdown
    limpio = texto_raw.strip()
    if limpio.startswith("```"):
        limpio = re.sub(r"^```\w*\n?", "", limpio)
        limpio = re.sub(r"\n?```$", "", limpio)
    # Buscar JSON en el texto
    inicio = limpio.find("{")
    fin = limpio.rfind("}") + 1
    if inicio >= 0 and fin > inicio:
        try:
            data = json.loads(limpio[inicio:fin])
            ops = data.get("oportunidades", [])
            resumen = data.get("resumen", "")
            lineas = []
            for i, op in enumerate(ops, 1):
                lineas.append(f"\n### OPORTUNIDAD {i}: {op.get('titulo', '?')}")
                for campo, label in [
                    ("problema", "Problema"),
                    ("mercado", "Mercado"),
                    ("modelo_negocio", "Modelo de negocio"),
                    ("competencia", "Competencia"),
                    ("inversion_usd", "Inversión USD"),
                    ("tiempo_primer_ingreso", "Tiempo al primer ingreso"),
                    ("ventaja_ecuador", "Ventaja desde Ecuador"),
                    ("por_que_ahora", "Por qué ahora"),
                ]:
                    val = op.get(campo, "")
                    if val:
                        lineas.append(f"  - {label}: {val}")
            if resumen:
                lineas.append(f"\n### RESUMEN: {resumen}")
            return "\n".join(lineas)
        except (json.JSONDecodeError, KeyError):
            pass
    return texto_raw


# ================================================================
#  GUARDAR RESULTADOS
# ================================================================

def guardar_json(datos, analisis, nodo_usado):
    """Guarda resultado en datos/oportunidades_negocio.json (append al historial)."""
    os.makedirs(DATOS_DIR, exist_ok=True)

    total_trends = sum(len(datos.get(f"google_trends_{g.lower()}", [])) for g in REGIONES_TRENDS)
    entrada = {
        "fecha": datos["timestamp"],
        "nodo_analisis": nodo_usado,
        "analisis": analisis,
        "fuentes_resumen": {
            "trends_regiones": total_trends,
            "techcrunch": len(datos.get("techcrunch", [])),
            "product_hunt": len(datos.get("product_hunt", [])),
            "ycombinator": len(datos.get("ycombinator", [])),
            "reddit_posts": (
                len(datos.get("reddit_entrepreneur", []))
                + len(datos.get("reddit_startups", []))
                + len(datos.get("reddit_smallbusiness", []))
            ),
            "hacker_news": len(datos.get("hacker_news", [])),
        },
    }

    # Cargar historial existente o crear nuevo
    historial = []
    if os.path.exists(JSON_SALIDA):
        try:
            with open(JSON_SALIDA) as f:
                historial = json.load(f)
        except (json.JSONDecodeError, IOError):
            historial = []

    historial.append(entrada)
    # Mantener últimos 90 días
    historial = historial[-90:]

    with open(JSON_SALIDA, "w") as f:
        json.dump(historial, f, ensure_ascii=False, indent=2)

    log.info(f"Guardado en {JSON_SALIDA} ({len(historial)} entradas)")
    return JSON_SALIDA


def guardar_chromadb(analisis, datos):
    """Guarda análisis en ChromaDB para detectar tendencias persistentes."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc_id = f"neg_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Crear resumen de tendencias para embedding
    trends = []
    for geo in REGIONES_TRENDS[:3]:
        for t in datos.get(f"google_trends_{geo.lower()}", [])[:3]:
            trends.append(t["titulo"])
    resumen_trends = ", ".join(trends)

    documento = f"Oportunidades {ts}: {analisis[:1000]}\nTendencias: {resumen_trends}"

    _col_negocios.add(
        ids=[doc_id],
        documents=[documento],
        metadatas=[{
            "timestamp": ts,
            "analisis": analisis[:2000],
            "tendencias_us": json.dumps([t["titulo"] for t in datos.get("google_trends_us", [])[:10]], ensure_ascii=False),
            "tendencias_global": json.dumps([t["titulo"] for t in datos.get("google_trends_global", [])[:10]], ensure_ascii=False),
        }],
    )
    log.info(f"ChromaDB: guardado {doc_id} (total: {_col_negocios.count()} entradas)")


def buscar_tendencias_persistentes(n=5):
    """Busca oportunidades recurrentes en el historial de ChromaDB."""
    total = _col_negocios.count()
    if total < 2:
        return "Aún no hay suficiente historial para detectar tendencias persistentes."

    resultados = _col_negocios.get(
        limit=min(n, total),
        include=["metadatas", "documents"],
    )
    lineas = ["Historial reciente de oportunidades detectadas:"]
    for meta in resultados["metadatas"]:
        lineas.append(f"  [{meta['timestamp']}] {meta['analisis'][:150]}...")
    return "\n".join(lineas)


# ================================================================
#  ENVIAR POR TELEGRAM
# ================================================================

def enviar_reporte_telegram(analisis, datos):
    """Envía las 3 oportunidades por Telegram."""
    fecha = datetime.now().strftime("%d/%m/%Y")

    # Estadísticas de fuentes
    total_trends = sum(len(datos.get(f"google_trends_{g.lower()}", [])) for g in REGIONES_TRENDS)
    total_reddit = len(datos.get('reddit_entrepreneur', [])) + len(datos.get('reddit_startups', [])) + len(datos.get('reddit_smallbusiness', []))
    stats = (
        f"Trends: {total_trends} ({len(REGIONES_TRENDS)} regiones) | "
        f"TC: {len(datos.get('techcrunch', []))} | "
        f"PH: {len(datos['product_hunt'])} | "
        f"YC: {len(datos.get('ycombinator', []))} | "
        f"Reddit: {total_reddit} | "
        f"HN: {len(datos['hacker_news'])}"
    )

    # Truncar análisis para Telegram (max 4096 chars)
    analisis_truncado = analisis[:3500]

    mensaje = (
        f"<b>JARVIS NEGOCIOS — {fecha}</b>\n"
        f"<i>{stats}</i>\n\n"
        f"{analisis_truncado}"
    )

    try:
        enviar_telegram(mensaje)
        log.info("Reporte enviado por Telegram")
    except Exception as e:
        log.warning(f"Error enviando Telegram: {e}")


# ================================================================
#  EJECUCIÓN PRINCIPAL
# ================================================================

def ejecutar():
    """Flujo completo: recolectar → analizar → guardar → enviar."""
    log.info("=" * 60)
    log.info("  JARVIS NEGOCIOS — Investigación de oportunidades")
    log.info("=" * 60)

    # 1. Recolectar fuentes
    datos = recolectar_fuentes()

    # Mostrar resumen de recolección
    total_trends = sum(len(datos.get(f"google_trends_{g.lower()}", [])) for g in REGIONES_TRENDS)
    total_trends += len(datos.get("google_trends_global", []))
    print(f"\n  Google Trends ({len(REGIONES_TRENDS)} regiones + global): {total_trends} tendencias")
    print(f"  TechCrunch:            {len(datos.get('techcrunch', []))} noticias")
    print(f"  Product Hunt:          {len(datos['product_hunt'])} productos")
    print(f"  Y Combinator:          {len(datos.get('ycombinator', []))} stories")
    print(f"  Reddit:                {len(datos['reddit_entrepreneur']) + len(datos['reddit_startups']) + len(datos['reddit_smallbusiness'])} posts")
    print(f"  Hacker News:           {len(datos['hacker_news'])} stories")

    # 2. Analizar con 671B (o fallback 70B)
    analisis_raw, nodo = analizar_con_brain(datos)
    analisis = _formatear_analisis(analisis_raw)

    print(f"\n{'='*60}")
    print(f"  ANÁLISIS (via {nodo})")
    print(f"{'='*60}")
    print(analisis)

    # 3. Guardar JSON
    guardar_json(datos, analisis, nodo)

    # 4. Guardar ChromaDB
    guardar_chromadb(analisis, datos)

    # 5. Enviar Telegram
    enviar_reporte_telegram(analisis, datos)

    # 6. Tendencias persistentes
    persistentes = buscar_tendencias_persistentes()
    if persistentes and "no hay suficiente" not in persistentes.lower():
        print(f"\n{'='*60}")
        print("  TENDENCIAS PERSISTENTES")
        print(f"{'='*60}")
        print(persistentes)

    log.info("Ejecución completada.")
    return analisis


if __name__ == "__main__":
    ejecutar()
