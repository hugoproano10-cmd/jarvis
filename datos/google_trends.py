#!/home/hproano/asistente_env/bin/python
"""
Google Trends — Señal contrarian basada en búsquedas del retail.
Si el retail busca "crash" en masa → oportunidad de compra (contrarian).
Si el retail busca "buy stocks" en masa → señal de techo.
"""

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


def _consultar_trends(keywords, timeframe="now 7-d", geo="US"):
    """Consulta Google Trends para una lista de keywords. Retorna score promedio 0-100."""
    from pytrends.request import TrendReq

    pytrends = TrendReq(hl="en-US", tz=300, timeout=(10, 25))

    # Google Trends acepta máximo 5 keywords por consulta
    batch = keywords[:5]
    try:
        pytrends.build_payload(batch, timeframe=timeframe, geo=geo)
        data = pytrends.interest_over_time()

        if data.empty:
            return 0, {}

        # Score promedio de la última observación
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
    except Exception as e:
        log.warning(f"Error Google Trends: {e}")
        return 0, {"error": str(e)}


def get_tendencias_mercado():
    """
    Consulta Google Trends y genera señal contrarian.

    Returns:
        dict con: senal, score, panico_score, euforia_score, cripto_score, detalle, timestamp
    """
    inicio = time.time()

    panico_score, panico_det = _consultar_trends(TERMINOS_PANICO)
    time.sleep(1)  # Rate limiting
    euforia_score, euforia_det = _consultar_trends(TERMINOS_EUFORIA)
    time.sleep(1)
    cripto_score, cripto_det = _consultar_trends(TERMINOS_CRIPTO)

    elapsed = round(time.time() - inicio, 2)

    # Lógica contrarian
    detalles = []

    if panico_score > 70:
        senal = "COMPRA"
        nota = (f"Pánico retail extremo ({panico_score}/100). "
                f"Históricamente zona de oportunidad de compra.")
        detalles.append(f"Pánico: {panico_score}/100 → COMPRA contrarian")
    elif panico_score > 50:
        senal = "COMPRA_LEVE"
        nota = f"Miedo retail elevado ({panico_score}/100). Posible oportunidad."
        detalles.append(f"Pánico: {panico_score}/100 → sesgo compra")
    elif euforia_score > 70:
        senal = "VENTA"
        nota = (f"Euforia retail extrema ({euforia_score}/100). "
                f"Señal de posible techo. Cautela con compras nuevas.")
        detalles.append(f"Euforia: {euforia_score}/100 → VENTA contrarian")
    elif euforia_score > 50:
        senal = "VENTA_LEVE"
        nota = f"Optimismo retail alto ({euforia_score}/100). Precaución."
        detalles.append(f"Euforia: {euforia_score}/100 → sesgo venta")
    else:
        senal = "NEUTRAL"
        nota = "Sin señal contrarian fuerte. Mercado en sentimiento normal."
        detalles.append(f"Pánico: {panico_score} | Euforia: {euforia_score} → neutral")

    # Cripto
    if cripto_score > 60:
        detalles.append(f"Cripto: interés alto ({cripto_score}/100)")
    elif cripto_score < 20:
        detalles.append(f"Cripto: interés bajo ({cripto_score}/100)")

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

    print(f"\n  SEÑAL: {r['senal']}")
    print(f"  {r['nota']}")
    print(f"\n  Scores:")
    print(f"    Pánico:  {r['panico_score']}/100")
    print(f"    Euforia: {r['euforia_score']}/100")
    print(f"    Cripto:  {r['cripto_score']}/100")

    print(f"\n  Detalle pánico:")
    for k, v in r["panico_detalle"].items():
        if k != "error":
            print(f"    \"{k}\": {v}/100")

    print(f"\n  Detalle euforia:")
    for k, v in r["euforia_detalle"].items():
        if k != "error":
            print(f"    \"{k}\": {v}/100")

    print(f"\n  Detalle cripto:")
    for k, v in r["cripto_detalle"].items():
        if k != "error":
            print(f"    \"{k}\": {v}/100")

    print(f"\n  Tiempo: {r['tiempo_consulta']}s")
    print(f"{'=' * 60}")
