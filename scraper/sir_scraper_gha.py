"""
SIR Toscana – Scraper per GitHub Actions
Aggiornato per leggere correttamente la tabella pluvio_men con colonne:
Codice, Stazione, Comune, Provincia, Zona, Quota, Pcum, Ultimi dati,
1g, 2g, 5g, 7g, 10g, 15g, 30g
"""
import asyncio, json, re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

BASE_SIR   = "https://www.sir.toscana.it"
WORKER_URL = "https://sir-proxy.riccardo-giusti-gst.workers.dev"
DATA_DIR   = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Mapping intestazioni tabella SIR → chiavi JSON
HEADER_MAP = {
    "codice":       "codice",
    "stazione":     "stazione",
    "comune":       "comune",
    "provincia":    "provincia",
    "zona":         "zona",
    "quota":        "quota",
    "pcum":         "pcum",
    "ultimi dati":  "ultimo_dato",
    "1 g":          "1g",
    "2 g":          "2g",
    "5 g":          "5g",
    "7 g":          "7g",
    "10 g":         "10g",
    "15 g":         "15g",
    "30 g":         "30g",
}

def fetch_url(url, timeout=25):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")

async def fetch_page(browser, url, timeout=30000):
    page = await browser.new_page()
    html = ""
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout)
        # Aspetta la tabella con le stazioni
        try:
            await page.wait_for_selector("table tbody tr", timeout=15000)
        except PWTimeout:
            pass
        await asyncio.sleep(3)
        html = await page.content()
    except Exception as e:
        print(f"  ⚠️  Errore: {e}")
    finally:
        await page.close()
    return html

def parsa_tabella_pluvio(html, tipo):
    """
    Parsing specifico per la tabella SIR stazioni.php?type=pluvio_men
    Estrae: codice, stazione, comune, provincia, zona, quota,
            pcum, ultimo_dato, 1g, 2g, 5g, 7g, 10g, 15g, 30g
    """
    soup = BeautifulSoup(html, "html.parser")
    risultati = []

    # Estrai riferimento temporale (es. "riferite al 15/05/2026 22.00")
    riferimento = ""
    for tag in soup.find_all(["h3", "h4", "p", "div"]):
        t = tag.get_text()
        if "riferit" in t.lower() and "precipitazioni" in t.lower():
            riferimento = " ".join(t.split())[:200]
            break

    for tabella in soup.find_all("table"):
        righe = tabella.find_all("tr")
        if len(righe) < 3:
            continue

        # Cerca riga header con "Codice" o "Stazione"
        header = []
        header_idx = 0
        for i, riga in enumerate(righe[:5]):
            celle = riga.find_all(["th", "td"])
            testi = [c.get_text(strip=True).lower() for c in celle]
            if any(t in testi for t in ["codice", "stazione", "pcum"]):
                header = testi
                header_idx = i
                break

        if not header:
            continue

        print(f"  Header trovato: {header[:10]}...")

        for riga in righe[header_idx + 1:]:
            celle = riga.find_all(["td", "th"])
            if len(celle) < 5:
                continue

            valori = [c.get_text(strip=True) for c in celle]
            if not any(valori):
                continue

            row = {}
            for j, val in enumerate(valori):
                if j >= len(header):
                    break
                chiave_raw = header[j].strip().lower()
                chiave = HEADER_MAP.get(chiave_raw, chiave_raw)
                if chiave and chiave != "col_15":  # escludi ultima colonna confusa
                    row[chiave] = val

            # Estrai codice stazione dal link (più affidabile del testo)
            for cella in celle:
                link = cella.find("a", href=True)
                if link:
                    href = link["href"]
                    m = re.search(r'id=(TOS[^&\s"\']+)', href)
                    if m:
                        row["codice"] = m.group(1)
                        row["url_sir"] = (BASE_SIR + "/monitoraggio/" + href
                                         if not href.startswith("http") else href)
                    break

            # Salta righe senza codice o senza dati utili
            if not row.get("codice") and not row.get("stazione"):
                continue

            # Normalizza numeri (virgola → punto)
            for k in ["pcum", "1g", "2g", "5g", "7g", "10g", "15g", "30g"]:
                if k in row:
                    row[k] = row[k].replace(",", ".")
                    try:
                        row[k] = float(row[k])
                    except ValueError:
                        row[k] = None

            row["tipo"] = tipo
            row["riferimento"] = riferimento
            risultati.append(row)

    print(f"  ✅ {len(risultati)} stazioni estratte")
    return risultati, riferimento

def scarica_stazioni():
    urls = [
        "http://www.sir.toscana.it/archivio/dati.php?D=json_stations",
        f"{WORKER_URL}?url={quote('http://www.sir.toscana.it/archivio/dati.php?D=json_stations')}",
    ]
    for url in urls:
        try:
            print(f"  Stazioni: {url[:60]}...")
            testo = fetch_url(url)
            geojson = json.loads(testo)
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
            if stazioni:
                print(f"  ✅ {len(stazioni)} stazioni")
                return stazioni
        except Exception as e:
            print(f"  ⚠️  {e}")
    return []

async def main():
    ts_utc = datetime.now(timezone.utc).isoformat()
    print(f"═══ SIR Scraper – {ts_utc} ═══\n")

    print("1. Lista stazioni SIR...")
    stazioni = scarica_stazioni()
    out = {"aggiornato": ts_utc, "totale": len(stazioni), "stazioni": stazioni}
    (DATA_DIR / "stazioni.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   💾 stazioni.json ({len(stazioni)})\n")

    print("2. Scraping tabella pluvio_men...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

        url = f"{BASE_SIR}/monitoraggio/stazioni.php?type=pluvio_men"
        print(f"   URL: {url}")
        html = await fetch_page(browser, url)
        dati, riferimento = parsa_tabella_pluvio(html, "pluvio_men")

        # Arricchisci con lat/lng dalle stazioni
        staz_map = {s["codice"]: s for s in stazioni}
        for row in dati:
            cod = row.get("codice", "")
            if cod in staz_map:
                s = staz_map[cod]
                row["lat"]      = s["lat"]
                row["lng"]      = s["lng"]
                if not row.get("quota"):
                    row["quota"] = s["quota"]

        out = {
            "aggiornato":  ts_utc,
            "tipo":        "pluvio_men",
            "riferimento": riferimento,
            "totale":      len(dati),
            "dati":        dati
        }
        (DATA_DIR / "pluvio_men.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   💾 pluvio_men.json ({len(dati)} stazioni)")

        await browser.close()

    stato = {
        "ultimo_aggiornamento": ts_utc,
        "stazioni_totali": len(stazioni),
        "pluvio_men": len(dati),
        "riferimento": riferimento,
    }
    (DATA_DIR / "stato.json").write_text(
        json.dumps(stato, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Completato: {datetime.now(timezone.utc).isoformat()}")

if __name__ == "__main__":
    asyncio.run(main())
