#!/home/hproano/asistente_env/bin/python
"""
Memoria persistente de JARVIS con ChromaDB.
Colecciones:
  - conversaciones: pregunta/respuesta de Telegram con timestamp y modelo
  - decisiones_trading: activo, accion, precio, razon, resultado posterior
Funciones:
  - guardar_conversacion(...)
  - guardar_decision_trading(...)
  - buscar_memoria(query, n=3) -> conversaciones similares
  - obtener_contexto_memoria(query, n=3) -> texto listo para inyectar en prompt
"""

import os
import json
from datetime import datetime

import chromadb

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MEMORIA_DIR = os.path.join(PROYECTO, "datos", "memoria")
os.makedirs(MEMORIA_DIR, exist_ok=True)

# ── Cliente ChromaDB persistente ───────────────────────────────

_client = chromadb.PersistentClient(path=MEMORIA_DIR)

_col_conversaciones = _client.get_or_create_collection(
    name="conversaciones",
    metadata={"hnsw:space": "cosine"},
)

_col_trading = _client.get_or_create_collection(
    name="decisiones_trading",
    metadata={"hnsw:space": "cosine"},
)


# ================================================================
#  1. Conversaciones de Telegram
# ================================================================

def guardar_conversacion(pregunta, respuesta, modelo="", nodo="", tiempo=0):
    """
    Guarda una conversacion de Telegram en ChromaDB.
    El documento indexado es pregunta + respuesta (para busqueda semantica).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc_id = f"conv_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    documento = f"Pregunta: {pregunta}\nRespuesta: {respuesta[:500]}"

    _col_conversaciones.add(
        ids=[doc_id],
        documents=[documento],
        metadatas=[{
            "timestamp": ts,
            "pregunta": pregunta[:500],
            "respuesta": respuesta[:2000],
            "modelo": modelo,
            "nodo": nodo,
            "tiempo": str(tiempo),
        }],
    )
    return doc_id


# ================================================================
#  2. Decisiones de trading
# ================================================================

def guardar_decision_trading(activo, accion, precio, razon,
                              ejecutada=False, order_id="", modelo=""):
    """
    Guarda una decision de trading en ChromaDB.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc_id = f"trade_{activo}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    documento = f"{accion} {activo} a ${precio} — {razon}"

    _col_trading.add(
        ids=[doc_id],
        documents=[documento],
        metadatas=[{
            "timestamp": ts,
            "activo": activo,
            "accion": accion,
            "precio": str(precio),
            "razon": razon[:500],
            "ejecutada": str(ejecutada),
            "order_id": order_id,
            "modelo": modelo,
            "resultado_posterior": "",
        }],
    )
    return doc_id


def actualizar_resultado_trading(doc_id, resultado):
    """Actualiza el resultado posterior de una decision de trading."""
    existente = _col_trading.get(ids=[doc_id], include=["metadatas", "documents"])
    if existente and existente["ids"]:
        meta = existente["metadatas"][0]
        meta["resultado_posterior"] = str(resultado)[:500]
        _col_trading.update(
            ids=[doc_id],
            metadatas=[meta],
        )


# ================================================================
#  3. Buscar en memoria (conversaciones similares)
# ================================================================

def buscar_memoria(query, n=3, coleccion="conversaciones"):
    """
    Busca en la memoria las N entradas mas similares a la query.
    Retorna lista de dicts con pregunta, respuesta, timestamp, distancia.
    """
    col = _col_conversaciones if coleccion == "conversaciones" else _col_trading
    total = col.count()
    if total == 0:
        return []

    n_buscar = min(n, total)
    resultados = col.query(
        query_texts=[query],
        n_results=n_buscar,
        include=["metadatas", "distances"],
    )

    salida = []
    for i, meta in enumerate(resultados["metadatas"][0]):
        distancia = resultados["distances"][0][i]
        entrada = dict(meta)
        entrada["distancia"] = round(distancia, 4)
        entrada["relevancia"] = round(1 - distancia, 4)
        salida.append(entrada)

    return salida


# ================================================================
#  4. Contexto de memoria para inyectar en prompt
# ================================================================

def obtener_contexto_memoria(query, n=3):
    """
    Busca conversaciones previas relevantes y retorna texto formateado
    listo para inyectar en el contexto de JARVIS.
    Filtra por relevancia > 0.3 para no inyectar ruido.
    """
    resultados = buscar_memoria(query, n=n, coleccion="conversaciones")
    if not resultados:
        return ""

    relevantes = [r for r in resultados if r["relevancia"] > 0.3]
    if not relevantes:
        return ""

    lineas = ["--- MEMORIA: Conversaciones previas relevantes ---"]
    for i, r in enumerate(relevantes, 1):
        lineas.append(
            f"  [{r['timestamp']}] (relevancia: {r['relevancia']:.0%})\n"
            f"  Hugo: {r['pregunta'][:200]}\n"
            f"  JARVIS: {r['respuesta'][:300]}"
        )
    lineas.append("--- Fin memoria ---")

    return "\n".join(lineas)


def obtener_contexto_memoria_trading(activo, n=3):
    """
    Busca decisiones de trading previas para un activo.
    Retorna texto formateado con historial de decisiones.
    """
    resultados = buscar_memoria(activo, n=n, coleccion="trading")
    if not resultados:
        return ""

    lineas = [f"--- MEMORIA: Decisiones previas sobre {activo} ---"]
    for r in resultados:
        resultado_post = r.get("resultado_posterior", "pendiente") or "pendiente"
        lineas.append(
            f"  [{r['timestamp']}] {r['accion']} {r['activo']} "
            f"@ ${r['precio']} — {r['razon'][:100]}"
            f" | Resultado: {resultado_post}"
        )
    lineas.append("--- Fin memoria trading ---")

    return "\n".join(lineas)


# ================================================================
#  5. Estadisticas
# ================================================================

def stats():
    """Retorna estadisticas de la memoria."""
    return {
        "conversaciones": _col_conversaciones.count(),
        "decisiones_trading": _col_trading.count(),
        "ruta": MEMORIA_DIR,
    }


# ================================================================
#  CLI — Test
# ================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  MEMORIA JARVIS — ChromaDB Test")
    print("=" * 60)

    s = stats()
    print(f"\n  Ruta: {s['ruta']}")
    print(f"  Conversaciones almacenadas: {s['conversaciones']}")
    print(f"  Decisiones trading almacenadas: {s['decisiones_trading']}")

    # Test: guardar conversaciones de ejemplo
    print("\n  Guardando conversaciones de prueba...")
    guardar_conversacion(
        "Como esta mi portafolio hoy?",
        "Tu portafolio tiene equity de $100,500. Posiciones: XOM +3.2%, JNJ -1.1%.",
        modelo="nemotron-3-super", nodo="jarvis-core", tiempo=2.1,
    )
    guardar_conversacion(
        "Que opinas de TSLA?",
        "TSLA muestra volatilidad alta. Los analistas estan divididos. Recomiendo cautela.",
        modelo="nemotron-3-super", nodo="jarvis-core", tiempo=1.8,
    )
    guardar_conversacion(
        "Deberia comprar oro?",
        "GLD ha subido 17% este anio. El VIX esta elevado, lo que favorece activos refugio.",
        modelo="nemotron-3-nano", nodo="jarvis-power", tiempo=1.2,
    )

    # Test: guardar decisiones de trading
    print("  Guardando decisiones de trading de prueba...")
    tid = guardar_decision_trading(
        "XOM", "COMPRAR", 118.50,
        "momentum alcista +2.3%, Sharpe alto",
        ejecutada=True, order_id="test-001", modelo="deepseek-r1:32b",
    )
    guardar_decision_trading(
        "TSLA", "MANTENER", 245.00,
        "volatilidad alta, sin senal clara",
        ejecutada=False, modelo="deepseek-r1:32b",
    )

    # Test: buscar memoria
    print("\n  Buscando 'portafolio posiciones' en conversaciones...")
    resultados = buscar_memoria("portafolio posiciones", n=3)
    for r in resultados:
        print(f"    [{r['timestamp']}] relevancia={r['relevancia']:.0%}")
        print(f"    P: {r['pregunta'][:80]}")
        print(f"    R: {r['respuesta'][:80]}")
        print()

    # Test: contexto para inyectar
    print("  Contexto de memoria para 'como van mis acciones':")
    ctx = obtener_contexto_memoria("como van mis acciones")
    print(ctx if ctx else "  (sin resultados relevantes)")

    # Test: memoria trading
    print(f"\n  Contexto trading para XOM:")
    ctx_t = obtener_contexto_memoria_trading("XOM")
    print(ctx_t if ctx_t else "  (sin resultados)")

    # Stats finales
    s = stats()
    print(f"\n  Total conversaciones: {s['conversaciones']}")
    print(f"  Total decisiones trading: {s['decisiones_trading']}")
    print(f"\n  ChromaDB persistido en: {s['ruta']}")
