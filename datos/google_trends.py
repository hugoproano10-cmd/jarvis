#!/home/hproano/asistente_env/bin/python
"""
Google Trends — Señal contrarian basada en búsquedas del retail.
Si el retail busca "crash" en masa → oportunidad de compra (contrarian).
Si el retail busca "buy stocks" en masa → señal de techo.

Diseño resiliente al rate limit (HTTP 429) de Google:
  - Cache en disco con TTL de 4 horas.
  - Retry con backoff exponencial (5s / 15s / 30s, máx 3 intentos).
  - Si todas las llamadas fallan, se usa la cache de cualquier edad como
    fallback. Si tampoco hay cache, se retornan scores None (la ausencia
    se señaliza con `disponible: False`) — los callers deben interpretar
    esto como NEUTRO (0), no como señal contrarian.
"""

import os
import json
import time
import logging
from datetime import datetime

log = logging.getLogger("google-trends")

# ── Términos de búsqueda ─────────────────────────────────────

TERMINOS_PANICO = [
    "stock market crash",
    "sell stocks",
    "recession 2026",
    "market crash",
]

TERMINOS_EUFORIA = [
    "buy stocks",
    "stock market boom",
    "invest now",
]

TERMINOS_CRIPTO = [
    "buy bitcoin",
    "crypto crash",
    "bitcoin price",
]

# ── Cache ────────────────────────────────────────────────────

CACHE_PATH = "/tmp/jarvis_google_trends_cache.json"
CACHE_TTL_SEG = 4 * 3600          # 4 horas
BACKOFFS_SEG = [5, 15, 30]         # reintentos ante 429


def _cache_leer():
    """Retorna (payload, edad_segundos) o (None, None)."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("_cache_ts")
        if not ts:
            return None, None
        edad = time.time() - float(ts)
        return data, edad
    except Exception:
        return None, None


def _cache_guardar(payload):
    try:
        payload = dict(payload)
        payload["_cache_ts"] = time.time()
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"no pude guardar cache trends: {e}")


# ── Consulta con retry ───────────────────────────────────────

def _consultar_trends(keywords, timeframe="now 7-d", geo="US"):
    """Una sola consulta (sin retry). Retorna (score_promedio, detalles).
    Ante error propaga la excepción para que el caller decida retry.
    """
    from pytrends.request import TrendReq
    pytrends = TrendReq(hl="en-US", tz=300, timeout=(10, 25))
    batch = keywords[:5]
    pytrends.build_payload(batch, timeframe=timeframe, geo=geo)
    data = pytrends.interest_over_time()
    if data.empty:
        return 0, {}
    ultimo = data.iloc[-1]
    scores = {}
    total = 0
    for kw in batch:
        if kw in ultimo:
            val = int(ultimo[kw])
            scores[kw] = val
            total += val
    promedio = total // len(batch) if batch else 0
    return promedio, scores


def _consultar_con_retry(keywords, etiqueta):
    """Retry con backoff 5/15/30s ante cualquier excepción (429 incluido).
    Retorna (score, detalles, exitoso: bool). Si ningún intento pasa,
    exitoso=False y detalles contiene la última excepción.
    """
    ultimo_err = None
    intentos = len(BACKOFFS_SEG) + 1  # primer intento + 3 retries
    for i in range(intentos):
        try:
            score, det = _consultar_trends(keywords)
            if i > 0:
                log.info(f"Google Trends {etiqueta}: éxito en reintento {i}")
            return score, det, True
        except Exception as e:
            ultimo_err = e
            msg = str(e)[:120]
            if i < len(BACKOFFS_SEG):
                espera = BACKOFFS_SEG[i]
                log.warning(f"Google Trends {etiqueta} falló (intento {i+1}): "
                            f"{msg} — esperando {espera}s antes del retry")
                time.sleep(espera)
            else:
                log.warning(f"Google Trends {etiqueta} agotó reintentos: {msg}")
    return None, {"error": str(ultimo_err) if ultimo_err else "unknown"}, False


# ── API pública ──────────────────────────────────────────────

def get_tendencias_mercado(max_edad_cache=CACHE_TTL_SEG):
    """Retorna dict con senal + scores. Incluye `disponible` (bool).

    Orden de resolución:
      1. Cache fresca (<4h): retorna sin llamar a Google.
      2. Consulta a Google con retry + backoff.
      3. Si Google falla, cache de cualquier edad.
      4. Si no hay cache, retorna dict con disponible=False y scores=None.
    """
    # 1) Cache fresca
    cache, edad = _cache_leer()
    if cache and edad is not None and edad < max_edad_cache:
        cache["fuente"] = f"cache-fresca ({int(edad)}s)"
        cache["disponible"] = True
        return cache

    # 2) Consulta fresca con retry
    inicio = time.time()
    panico_s, panico_d, p_ok = _consultar_con_retry(TERMINOS_PANICO, "pánico")
    time.sleep(1)
    euforia_s, euforia_d, e_ok = _consultar_con_retry(TERMINOS_EUFORIA, "euforia")
    time.sleep(1)
    cripto_s, cripto_d, c_ok = _consultar_con_retry(TERMINOS_CRIPTO, "cripto")
    elapsed = round(time.time() - inicio, 2)

    # 3) Si al menos uno tuvo éxito, armar resultado y cachear
    if p_ok or e_ok or c_ok:
        payload = _armar_resultado(
            panico_s, panico_d, euforia_s, euforia_d,
            cripto_s, cripto_d, elapsed,
        )
        payload["disponible"] = True
        payload["fuente"] = "api-live"
        # Sólo cachear si los 3 tuvieron éxito (cache completa)
        if p_ok and e_ok and c_ok:
            _cache_guardar(payload)
        return payload

    # 4) Todos fallaron — usar cache stale si existe
    if cache:
        cache["fuente"] = f"cache-stale ({int(edad)}s)" if edad else "cache-stale"
        cache["disponible"] = True   # cache sirve, aunque sea vieja
        log.warning(f"Google Trends usando cache stale de {int(edad) if edad else '?'}s")
        return cache

    # 5) Sin datos reales — señalizar indisponibilidad. Los callers deben
    #    tratar esto como NEUTRO (0), NO como cripto_score=0.
    log.warning("Google Trends: sin datos frescos ni cache — retornando indisponible")
    return {
        "senal": "NEUTRAL",
        "nota": "Google Trends no disponible (rate limit sin cache)",
        "panico_score": None,
        "euforia_score": None,
        "cripto_score": None,
        "panico_detalle": {},
        "euforia_detalle": {},
        "cripto_detalle": {},
        "detalles": ["Google Trends no disponible"],
        "tiempo_consulta": elapsed,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disponible": False,
        "fuente": "sin-datos",
    }


def _armar_resultado(panico_score, panico_det, euforia_score, euforia_det,
                     cripto_score, cripto_det, elapsed):
    """Construye el dict canónico. Scores None se traducen a 0 solo para
    la LÓGICA de señal (comparaciones). Pero `*_score` devuelto preserva None.
    """
    # Para comparar, usar midpoint cuando no hay dato
    ps = panico_score if panico_score is not None else 0
    es = euforia_score if euforia_score is not None else 0

    detalles = []
    if ps > 70:
        senal = "COMPRA"
        nota = (f"Pánico retail extremo ({ps}/100). "
                f"Históricamente zona de oportunidad de compra.")
        detalles.append(f"Pánico: {ps}/100 → COMPRA contrarian")
    elif ps > 50:
        senal = "COMPRA_LEVE"
        nota = f"Miedo retail elevado ({ps}/100). Posible oportunidad."
        detalles.append(f"Pánico: {ps}/100 → sesgo compra")
    elif es > 70:
        senal = "VENTA"
        nota = (f"Euforia retail extrema ({es}/100). "
                f"Señal de posible techo. Cautela con compras nuevas.")
        detalles.append(f"Euforia: {es}/100 → VENTA contrarian")
    elif es > 50:
        senal = "VENTA_LEVE"
        nota = f"Optimismo retail alto ({es}/100). Precaución."
        detalles.append(f"Euforia: {es}/100 → sesgo venta")
    else:
        senal = "NEUTRAL"
        nota = "Sin señal contrarian fuerte. Mercado en sentimiento normal."
        detalles.append(f"Pánico: {ps} | Euforia: {es} → neutral")

    cs = cripto_score if cripto_score is not None else 50
    if cs > 60:
        detalles.append(f"Cripto: interés alto ({cs}/100)")
    elif cs < 20:
        detalles.append(f"Cripto: interés bajo ({cs}/100)")

    return {
        "senal": senal,
        "nota": nota,
        "panico_score": panico_score,
        "euforia_score": euforia_score,
        "cripto_score": cripto_score,
        "panico_detalle": panico_det,
        "euforia_detalle": euforia_det,
        "cripto_detalle": cripto_det,
        "detalles": detalles,
        "tiempo_consulta": elapsed,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)

    print("=" * 60)
    print(f"  GOOGLE TRENDS — Señal Contrarian")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    r = get_tendencias_mercado()

    print(f"\n  SEÑAL: {r['senal']}  (fuente: {r.get('fuente','?')}, "
          f"disponible: {r.get('disponible')})")
    print(f"  {r['nota']}")
    print(f"\n  Scores:")
    print(f"    Pánico:  {r['panico_score']}/100")
    print(f"    Euforia: {r['euforia_score']}/100")
    print(f"    Cripto:  {r['cripto_score']}/100")
    print(f"\n  Tiempo: {r['tiempo_consulta']}s")
    print("=" * 60)
