#!/home/hproano/asistente_env/bin/python
"""
SEC EDGAR — Posiciones institucionales de los grandes fondos.
Descarga 13F filings de Berkshire Hathaway, Renaissance Technologies y Bridgewater.
Detecta posiciones en nuestros 22 activos operables.
"""

import os
import sys
import json
import time
import importlib.util
from datetime import datetime

import requests

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

_cfg_spec = importlib.util.spec_from_file_location(
    "trading_config", os.path.join(PROYECTO, "trading", "config.py"))
_cfg = importlib.util.module_from_spec(_cfg_spec)
_cfg_spec.loader.exec_module(_cfg)
ACTIVOS_OPERABLES = _cfg.ACTIVOS_OPERABLES

# ── Fondos a rastrear ─────────────────────────────────────────
FONDOS = {
    "Berkshire Hathaway": "0001067983",
    "Renaissance Technologies": "0001037389",
    "Bridgewater Associates": "0001350694",
}

# Mapeo: símbolo de trading → posibles nombres/tickers en 13F filings
# Los 13F reportan por nombre de la compañía, no por ticker
SIMBOLO_A_NOMBRE = {
    "XOM": ["EXXON MOBIL", "EXXON"],
    "JNJ": ["JOHNSON & JOHNSON", "JOHNSON AND JOHNSON"],
    "GLD": ["SPDR GOLD", "GLD"],
    "VZ": ["VERIZON"],
    "META": ["META PLATFORMS", "META"],
    "SOXX": ["ISHARES SEMICONDUCTOR", "SOXX"],
    "MCD": ["MCDONALDS", "MCDONALD"],
    "KO": ["COCA-COLA", "COCA COLA"],
    "XLE": ["ENERGY SELECT", "XLE"],
    "SPY": ["SPDR S&P 500", "S&P 500 ETF", "SPY"],
    "TSLA": ["TESLA"],
    "AAPL": ["APPLE"],
    "IBM": ["INTERNATIONAL BUSINESS MACHINES", "IBM"],
    "HYG": ["ISHARES IBOXX HIGH YIELD", "HYG"],
    "XLU": ["UTILITIES SELECT", "XLU"],
    "T": ["AT&T", "ATT INC"],
    "XLC": ["COMMUNICATION SERVICES SELECT", "XLC"],
    "AGG": ["ISHARES CORE US AGGREGATE", "AGG"],
    "D": ["DOMINION ENERGY", "DOMINION"],
    "EEM": ["ISHARES MSCI EMERGING", "EEM"],
    "EFA": ["ISHARES MSCI EAFE", "EFA"],
    "IEF": ["ISHARES 7-10 YEAR TREASURY", "IEF"],
}

SEC_BASE = "https://data.sec.gov"
HEADERS = {
    "User-Agent": "JARVIS Trading Bot research@jarvis-trading.local",
    "Accept": "application/json",
}

# Cache local para no repetir llamadas a SEC
_cache = {}
_CACHE_TTL = 3600  # 1 hora


# ── API SEC EDGAR ─────────────────────────────────────────────

def _get_sec(url, intentos=2):
    """GET a SEC EDGAR con rate limiting y reintentos."""
    for i in range(intentos):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            if i < intentos - 1:
                time.sleep(1)
            else:
                raise
    return None


def obtener_filings_recientes(cik):
    """Obtiene los filings recientes de un CIK desde SEC EDGAR."""
    cache_key = f"filings_{cik}"
    if cache_key in _cache:
        cached_at, data = _cache[cache_key]
        if time.time() - cached_at < _CACHE_TTL:
            return data

    url = f"{SEC_BASE}/submissions/CIK{cik}.json"
    data = _get_sec(url)
    if data:
        _cache[cache_key] = (time.time(), data)
    return data


def obtener_ultimo_13f(cik):
    """
    Busca el 13F más reciente en los filings del fondo.
    Retorna el accession number y fecha del filing.
    """
    data = obtener_filings_recientes(cik)
    if not data:
        return None, None

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    for i, form in enumerate(forms):
        if "13F" in form.upper():
            return {
                "accession": accessions[i] if i < len(accessions) else None,
                "fecha": dates[i] if i < len(dates) else None,
                "form": form,
                "doc": primary_docs[i] if i < len(primary_docs) else None,
            }, data.get("name", "")

    return None, data.get("name", "")


def obtener_holdings_13f(cik):
    """
    Obtiene las posiciones del 13F más reciente.
    Scrapes the HTML filing directory to find the infotable XML.
    """
    import re

    cache_key = f"holdings_{cik}"
    if cache_key in _cache:
        cached_at, data = _cache[cache_key]
        if time.time() - cached_at < _CACHE_TTL:
            return data

    filing_info, nombre_fondo = obtener_ultimo_13f(cik)
    if not filing_info or not filing_info.get("accession"):
        return {"fondo": nombre_fondo, "error": "Sin 13F reciente", "holdings": []}

    accession = filing_info["accession"]
    accession_clean = accession.replace("-", "")
    cik_clean = cik.lstrip("0")

    holdings = []

    # Scrape the HTML directory listing to find XML files
    dir_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_clean}/"
    try:
        resp = requests.get(dir_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            # Find all XML links (exclude primary_doc.xml which is the cover page)
            xml_links = re.findall(r'href="([^"]+\.xml)"', resp.text)
            infotable_url = None
            for link in xml_links:
                fname = link.split("/")[-1].lower()
                if "primary" not in fname:
                    infotable_url = f"https://www.sec.gov{link}" if link.startswith("/") else link
                    break

            if infotable_url:
                xml_resp = requests.get(infotable_url, headers=HEADERS, timeout=20)
                if xml_resp.status_code == 200:
                    holdings = _parsear_13f_xml(xml_resp.text)
    except Exception:
        pass

    result = {
        "fondo": nombre_fondo,
        "fecha_filing": filing_info.get("fecha"),
        "accession": accession,
        "total_holdings": len(holdings),
        "holdings": holdings,
    }
    _cache[cache_key] = (time.time(), result)
    return result


def _parsear_13f_xml(xml_text):
    """Parsea el XML de infotable del 13F y extrae holdings."""
    import re
    import xml.etree.ElementTree as ET

    holdings = []
    try:
        # Limpiar todos los namespaces para facilitar el parsing
        xml_clean = re.sub(r'\s+xmlns[^"]*"[^"]*"', '', xml_text)
        xml_clean = re.sub(r'\s+xsi:[^"]*"[^"]*"', '', xml_clean)
        for ns_prefix in ["ns1:", "ns2:", "ns3:"]:
            xml_clean = xml_clean.replace(ns_prefix, "")

        root = ET.fromstring(xml_clean)

        # Buscar infoTable entries (varios posibles tags)
        entries = (root.findall(".//infoTable") or
                   root.findall(".//{*}infoTable") or
                   root.findall(".//InfoTable"))

        if not entries:
            # Buscar directamente por shrsOrPrnAmt o nameOfIssuer
            all_elements = list(root.iter())
            current_entry = {}
            for elem in all_elements:
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                tag_lower = tag.lower()
                if tag_lower == "nameofissuer" and elem.text:
                    if current_entry.get("nombre"):
                        holdings.append(current_entry)
                    current_entry = {"nombre": elem.text.strip().upper()}
                elif tag_lower == "titleofclass" and elem.text:
                    current_entry["clase"] = elem.text.strip()
                elif tag_lower == "cusip" and elem.text:
                    current_entry["cusip"] = elem.text.strip()
                elif tag_lower == "value" and elem.text:
                    try:
                        current_entry["valor_miles"] = int(elem.text.strip())
                    except (ValueError, TypeError):
                        pass
                elif tag_lower in ("sshprnamt", "shrsorprnamt") and elem.text:
                    try:
                        current_entry["shares"] = int(elem.text.strip())
                    except (ValueError, TypeError):
                        pass
            if current_entry.get("nombre"):
                holdings.append(current_entry)
        else:
            for entry in entries:
                h = {}
                for child in entry:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    tag_lower = tag.lower()
                    if tag_lower == "nameofissuer" and child.text:
                        h["nombre"] = child.text.strip().upper()
                    elif tag_lower == "titleofclass" and child.text:
                        h["clase"] = child.text.strip()
                    elif tag_lower == "cusip" and child.text:
                        h["cusip"] = child.text.strip()
                    elif tag_lower == "value" and child.text:
                        try:
                            h["valor_miles"] = int(child.text.strip())
                        except (ValueError, TypeError):
                            pass
                    elif tag_lower == "shrsorprnamt":
                        for sub in child:
                            sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                            if sub_tag.lower() == "sshprnamt" and sub.text:
                                try:
                                    h["shares"] = int(sub.text.strip())
                                except (ValueError, TypeError):
                                    pass
                if h.get("nombre"):
                    holdings.append(h)
    except ET.ParseError:
        pass

    return holdings


# ── Funciones principales ─────────────────────────────────────

def buscar_activo_en_holdings(simbolo, holdings):
    """
    Busca un símbolo en la lista de holdings por nombre.
    Retorna el holding encontrado o None.
    """
    nombres_buscar = SIMBOLO_A_NOMBRE.get(simbolo, [simbolo])
    for h in holdings:
        nombre_holding = h.get("nombre", "")
        for nombre in nombres_buscar:
            if nombre.upper() in nombre_holding:
                return h
    return None


def get_posiciones_institucionales(simbolo):
    """
    Consulta los 13F de los 3 fondos y detecta si tienen posición en el símbolo.
    Retorna dict con señal consolidada: AUMENTÓ/REDUJO/PRESENTE/SIN_DATOS.
    """
    resultados = {}
    fondos_con_posicion = 0
    total_valor_miles = 0

    for nombre_fondo, cik in FONDOS.items():
        try:
            data = obtener_holdings_13f(cik)
            if data.get("error"):
                resultados[nombre_fondo] = {
                    "senal": "SIN_DATOS",
                    "error": data["error"],
                }
                continue

            holding = buscar_activo_en_holdings(simbolo, data.get("holdings", []))
            if holding:
                fondos_con_posicion += 1
                valor = holding.get("valor_miles", 0)
                total_valor_miles += valor
                resultados[nombre_fondo] = {
                    "senal": "PRESENTE",
                    "valor_miles": valor,
                    "shares": holding.get("shares", 0),
                    "fecha_filing": data.get("fecha_filing"),
                }
            else:
                resultados[nombre_fondo] = {
                    "senal": "SIN_POSICION",
                    "fecha_filing": data.get("fecha_filing"),
                    "total_holdings": data.get("total_holdings", 0),
                }
        except Exception as e:
            resultados[nombre_fondo] = {"senal": "ERROR", "error": str(e)}

    # Señal consolidada
    if fondos_con_posicion >= 2:
        senal = "FUERTE"
    elif fondos_con_posicion == 1:
        senal = "PRESENTE"
    else:
        senal = "SIN_DATOS"

    return {
        "simbolo": simbolo,
        "senal": senal,
        "fondos_con_posicion": fondos_con_posicion,
        "total_fondos": len(FONDOS),
        "valor_total_miles": total_valor_miles,
        "detalle": resultados,
    }


def get_resumen_13f():
    """
    Resumen completo: posiciones de los 3 fondos en nuestros 22 activos.
    Retorna dict con todos los resultados.
    """
    print(f"Descargando 13F filings de {len(FONDOS)} fondos...")
    resultados = {}

    # Pre-cargar todos los holdings
    holdings_por_fondo = {}
    for nombre_fondo, cik in FONDOS.items():
        print(f"  {nombre_fondo} (CIK: {cik})...", end=" ", flush=True)
        try:
            data = obtener_holdings_13f(cik)
            holdings_por_fondo[nombre_fondo] = data
            total = data.get("total_holdings", 0)
            fecha = data.get("fecha_filing", "?")
            print(f"OK — {total} posiciones, filing: {fecha}")
        except Exception as e:
            holdings_por_fondo[nombre_fondo] = {"error": str(e), "holdings": []}
            print(f"ERROR: {e}")
        time.sleep(0.2)  # Rate limiting SEC

    # Buscar nuestros activos
    print(f"\nBuscando {len(ACTIVOS_OPERABLES)} activos en los filings...")
    for simbolo in ACTIVOS_OPERABLES:
        fondos_con = 0
        detalle = {}
        for nombre_fondo, data in holdings_por_fondo.items():
            holding = buscar_activo_en_holdings(simbolo, data.get("holdings", []))
            if holding:
                fondos_con += 1
                detalle[nombre_fondo] = {
                    "valor_miles": holding.get("valor_miles", 0),
                    "shares": holding.get("shares", 0),
                }
        resultados[simbolo] = {
            "fondos_con_posicion": fondos_con,
            "detalle": detalle,
        }

    return resultados, holdings_por_fondo


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 80)
    print(f"  SEC EDGAR — Posiciones Institucionales (13F)")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 80)

    resultados, holdings = get_resumen_13f()

    # Tabla de resultados
    print(f"\n{'=' * 80}")
    print(f"  POSICIONES EN NUESTROS ACTIVOS")
    print(f"{'=' * 80}")
    header = f"  {'Activo':<8}"
    for nombre in FONDOS:
        abrev = nombre.split()[0][:10]
        header += f" {abrev:>12}"
    header += f" {'Total':>6}"
    print(header)
    print(f"  {'─' * 74}")

    activos_presentes = []
    for simbolo in ACTIVOS_OPERABLES:
        r = resultados[simbolo]
        linea = f"  {simbolo:<8}"
        for nombre_fondo in FONDOS:
            d = r["detalle"].get(nombre_fondo)
            if d:
                val = d["valor_miles"]
                if val >= 1000:
                    linea += f" ${val/1000:>9.1f}M"
                elif val > 0:
                    linea += f" ${val:>9,}K"
                else:
                    linea += f" {'presente':>12}"
            else:
                linea += f" {'—':>12}"
        linea += f" {r['fondos_con_posicion']:>4}/{len(FONDOS)}"
        if r["fondos_con_posicion"] > 0:
            linea += " *"
            activos_presentes.append(simbolo)
        print(linea)

    print(f"\n  Activos con presencia institucional: {len(activos_presentes)}/{len(ACTIVOS_OPERABLES)}")
    if activos_presentes:
        print(f"  {', '.join(activos_presentes)}")

    # Resumen por fondo
    print(f"\n{'=' * 80}")
    print(f"  RESUMEN POR FONDO")
    print(f"{'=' * 80}")
    for nombre_fondo in FONDOS:
        data = holdings.get(nombre_fondo, {})
        total = data.get("total_holdings", 0)
        fecha = data.get("fecha_filing", "?")
        nuestros = sum(1 for r in resultados.values() if nombre_fondo in r["detalle"])
        print(f"  {nombre_fondo}")
        print(f"    Filing: {fecha} | Total posiciones: {total} | En nuestros activos: {nuestros}")

    print(f"\n{'=' * 80}")
