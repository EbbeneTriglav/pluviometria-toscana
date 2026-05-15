"""
SIR Toscana – Scraper per GitHub Actions
Gira automaticamente ogni ora, salva i dati in data/
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
BASE_SIR   = "https://www.sir.toscana.it"
DATA_DIR   = Path("data")
DATA_DIR.mkdir(exist_ok=True)

TIPI_DA_SCRAPARE = [
    "pluvio_men",   # precipitazioni giornaliere (ultime settimane)
    "pluvio",       # cumulate in corso
]

# ── Browser helper ────────────────────────────────────────────────────────────
async def fetch_page(browser, url, wait_selector="table", timeout=25000):
    page = await browser.new_page()
    html = ""
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout)
        try:
            await page.wait_for_selector(wait_selector, timeout=12000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    except Exception as e:
        print(f"  ⚠️  Errore caricamento {url}: {e}")
    finally:
        await page.close()
    return html

# ── Parsing tabella ───────────────────────────────────────────────────────────
def parsa_tabella(html, tipo):
    soup = BeautifulSoup(html, "html.parser")
    risultati = []

    # Estrai riferimento temporale dalla pagina (es. "riferiti al 15/05/2026")
    riferimento = ""
    for tag in soup.find_all(["h4","h3","p","div"]):
        testo = tag.get_text()
        if "riferit" in testo.lower() or "precipitazioni" in testo.lower():
            riferimento = testo.strip()[:120]
            break

    for tabella in soup.find_all("table"):
        righe = tabella.find_all("tr")
        if len(righe) < 3:
            continue

        # Header
        header_els = righe[0].find_all(["th", "td"])
        header = [h.get_text(strip=True) for h in header_els]
        if len(header) < 2:
            continue

        for riga in righe[1:]:
            celle = riga.find_all(["td", "th"])
            if not celle:
                continue
            valori = [c.get_text(strip=True) for c in celle]
            if not any(valori):
                continue

            row = {}
            for j, val in enumerate(valori):
                chiave = header[j] if j < len(header) else f"col_{j}"
                row[chiave] = val

            # Estrai codice stazione e URL dettaglio dai link
            for cella in celle:
                link = cella.find("a", href=True)
                if link:
                    href = link["href"]
                    import re
                    m = re.search(r'id=(TOS[^&\s"\']+)', href)
                    if m:
                        row["codice"] = m.group(1)
                        row["url_dettaglio"] = BASE_SIR + "/monitoraggio/" + href \
                            if not href.startswith("http") else href
                    if not row.get("nome"):
                        row["nome"] = link.get_text(strip=True)

            row["tipo"]        = tipo
            row["riferimento"] = riferimento
            risultati.append(row)

    return risultati

# ── Stazioni SIR (GeoJSON) ────────────────────────────────────────────────────
async def scarica_stazioni(session):
    """Scarica lista stazioni attive dal GeoJSON SIR."""
    import urllib.request
    url = "http://www.sir.toscana.it/archivio/dati.php?D=json_stations"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (GitHub Actions; SIR scraper)"
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            geojson = json.loads(r.read().decode("utf-8"))

        stazioni = []
        for f in geojson.get("features", []):
            props = f.get("properties", {})
            geom  = f.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            consist = props.get("Consistenza", {})
            anni = []
            for k, v in consist.items():
                if "PLUVIOMETRIA" in k and "9-9" in k:
                    anni = [a for grp in (v.get("Anni") or []) for a in grp]
                    break
            if not anni or ("2026" not in anni and "2025" not in anni):
                continue
            lng, lat = geom["coordinates"]
            stazioni.append({
                "codice":    props.get("Codice", ""),
                "nome":      props.get("Nome", ""),
                "comune":    props.get("Comune", ""),
                "provincia": props.get("Provincia", ""),
                "quota":     props.get("Quota mslm", ""),
                "lat":       round(float(lat), 6),
                "lng":       round(float(lng), 6),
            })
        print(f"  ✅ {len(stazioni)} stazioni pluviometriche attive")
        return stazioni
    except Exception as e:
        print(f"  ⚠️  Stazioni non scaricabili: {e}")
        return []

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    ts_utc = datetime.now(timezone.utc).isoformat()
    print(f"═══ SIR Scraper – {ts_utc} ═══\n")

    # 1. Scarica lista stazioni
    print("1. Lista stazioni SIR...")
    stazioni = await scarica_stazioni(None)
    if stazioni:
        out = {
            "aggiornato": ts_utc,
            "totale": len(stazioni),
            "stazioni": stazioni
        }
        (DATA_DIR / "stazioni.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"   💾 data/stazioni.json ({len(stazioni)} stazioni)")

    # 2. Scraping tabelle con Playwright
    print("\n2. Scraping tabelle SIR con Playwright...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-extensions"]
        )

        for tipo in TIPI_DA_SCRAPARE:
            url = f"{BASE_SIR}/monitoraggio/stazioni.php?type={tipo}"
            print(f"\n   → {tipo}: {url}")
            html = await fetch_page(browser, url)

            if not html:
                print(f"   ❌ Nessun HTML per {tipo}")
                continue

            dati = parsa_tabella(html, tipo)
            print(f"   ✅ {len(dati)} righe estratte")

            # Arricchisci con coordinate dalle stazioni
            staz_map = {s["codice"]: s for s in stazioni}
            for row in dati:
                cod = row.get("codice", "")
                if cod in staz_map:
                    row["lat"]    = staz_map[cod]["lat"]
                    row["lng"]    = staz_map[cod]["lng"]
                    row["quota"]  = staz_map[cod]["quota"]
                    row["comune"] = staz_map[cod]["comune"]
                    row["provincia"] = staz_map[cod]["provincia"]

            out = {
                "aggiornato": ts_utc,
                "tipo": tipo,
                "totale": len(dati),
                "dati": dati
            }
            fname = DATA_DIR / f"{tipo}.json"
            fname.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"   💾 data/{tipo}.json")

        await browser.close()

    # 3. Crea file di stato per il sito
    stato = {
        "ultimo_aggiornamento": ts_utc,
        "files_disponibili": [f.name for f in DATA_DIR.glob("*.json")],
        "stazioni_totali": len(stazioni),
    }
    (DATA_DIR / "stato.json").write_text(
        json.dumps(stato, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n💾 data/stato.json")
    print(f"\n✅ Completato: {datetime.now(timezone.utc).isoformat()}")

if __name__ == "__main__":
    asyncio.run(main())
