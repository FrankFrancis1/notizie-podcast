import asyncio
import html
import os
import re
import time
from datetime import datetime
from email.utils import formatdate
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import edge_tts
import feedparser
import trafilatura
from google import genai

# ---------- IMPOSTAZIONI (modificabili) ----------
USER = "FrankFrancis1"
REPO = "notizie-podcast"
BASE_URL = f"https://{USER.lower()}.github.io/{REPO}"

FEEDS = [
    "https://www.ilpost.it/feed/",
    "https://www.ilpost.it/italia/feed/",
    "https://www.ilpost.it/mondo/feed/",
]
MAX_ARTICOLI = 30          # notizie totali date a Gemini
APPROFONDIMENTI = 8        # di quante scarico il testo completo (candidate agli approfondimenti)
MAX_CARATTERI_ARTICOLO = 3500

VOCE = "it-IT-IsabellaNeural"  # alternativa maschile: it-IT-DiegoNeural
VELOCITA = "+5%"
PAROLE_TARGET = 2200       # circa 14 minuti di audio (circa 155 parole al minuto)

# Prova i modelli in ordine e usa il primo che funziona
MODELLI = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
]
TENTATIVI = 3  # per ogni modello, con pausa crescente se Google è sovraccarico

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
# --------------------------------------------------


def pulisci(testo):
    testo = re.sub(r"<[^>]+>", " ", testo or "")
    return re.sub(r"\s+", " ", html.unescape(testo)).strip()


def scarica_articolo(link):
    """Prova a leggere il testo completo dell'articolo. Se non riesce, restituisce ''."""
    if not link:
        return ""
    try:
        pagina = trafilatura.fetch_url(link)
        testo = trafilatura.extract(pagina) if pagina else ""
    except Exception as e:
        print(f"Articolo non scaricabile {link}: {e}")
        return ""
    testo = pulisci(testo or "")
    # sotto i 400 caratteri di solito è un estratto o una pagina riservata agli abbonati
    return testo[:MAX_CARATTERI_ARTICOLO] if len(testo) > 400 else ""


def prendi_notizie():
    viste = set()
    blocchi = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"Feed non leggibile {url}: {e}")
            continue
        for voce in feed.entries:
            titolo = pulisci(voce.get("title"))
            if not titolo or titolo in viste:
                continue
            viste.add(titolo)
            riassunto = pulisci(voce.get("summary"))[:500]
            completo = ""
            if len(blocchi) < APPROFONDIMENTI:
                completo = scarica_articolo(voce.get("link"))
            if completo:
                blocchi.append(f"- {titolo}.\n  TESTO COMPLETO: {completo}")
            else:
                blocchi.append(f"- {titolo}. {riassunto}")
            if len(blocchi) >= MAX_ARTICOLI:
                break
        if len(blocchi) >= MAX_ARTICOLI:
            break
    if not blocchi:
        raise SystemExit("Nessuna notizia trovata nei feed.")
    completi = sum(1 for b in blocchi if "TESTO COMPLETO" in b)
    print(f"Notizie raccolte: {len(blocchi)} (con testo completo: {completi})")
    return "\n".join(blocchi)


def scrivi_testo(notizie, data_parlata):
    prompt = (
        f"Sei il conduttore di un notiziario radiofonico mattutino in italiano. "
        f"Oggi è {data_parlata}. Qui sotto trovi le notizie del giorno da Il Post: "
        f"alcune con TESTO COMPLETO, altre solo con titolo ed estratto.\n\n"
        f"Scrivi il testo parlato di una puntata di circa {PAROLE_TARGET} parole "
        f"(non meno di {int(PAROLE_TARGET * 0.85)}), da ascoltare in metro, "
        f"circa 14 minuti di lettura.\n"
        f"Struttura:\n"
        f"1. Saluto breve con la data e una panoramica di 3-4 titoli del giorno.\n"
        f"2. Approfondimenti: scegli le 5 o 6 notizie più importanti tra quelle con "
        f"TESTO COMPLETO e raccontale in modo disteso, circa 250-300 parole ciascuna: "
        f"cosa è successo, contesto, protagonisti, perché conta.\n"
        f"3. In breve: tutte le altre notizie, 2 o 3 frasi ciascuna, raggruppate per tema "
        f"(Italia, mondo, economia, cultura...).\n"
        f"4. Congedo breve.\n\n"
        f"Regole:\n"
        f"- Tono chiaro, scorrevole, naturale, come una radio di qualità; usa frasi "
        f"di transizione tra una notizia e l'altra.\n"
        f"- Usa solo le informazioni fornite, non inventare dettagli, citazioni o numeri.\n"
        f"- Solo testo da leggere ad alta voce: niente elenchi, titoli, markdown, "
        f"emoji, parentesi o URL.\n"
        f"- Scrivi per esteso numeri, sigle e simboli difficili da pronunciare.\n\n"
        f"NOTIZIE:\n{notizie}"
    )
    client = genai.Client()  # legge la chiave da GEMINI_API_KEY
    testo = ""
    for modello in MODELLI:
        for tentativo in range(1, TENTATIVI + 1):
            try:
                risposta = client.models.generate_content(model=modello, contents=prompt)
                testo = (risposta.text or "").strip()
                if testo:
                    print(f"Modello usato: {modello} (tentativo {tentativo})")
                    break
            except Exception as e:
                print(f"{modello}, tentativo {tentativo}/{TENTATIVI} fallito: {e}")
                if "404" in str(e):
                    break  # modello inesistente, inutile riprovare
                if tentativo < TENTATIVI:
                    time.sleep(30 * tentativo)
        if testo:
            break
    if len(testo) < 500:
        raise SystemExit("Il testo generato è troppo corto o assente, interrompo.")
    return testo


async def crea_audio(testo, percorso):
    comunicazione = edge_tts.Communicate(testo, VOCE, rate=VELOCITA)
    await comunicazione.save(percorso)


def scrivi_feed(cartella, nome_file, titolo, descrizione, adesso):
    dimensione = os.path.getsize(os.path.join(cartella, nome_file))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Notizie del mattino</title>
    <link>{BASE_URL}/</link>
    <language>it</language>
    <description>Rassegna quotidiana delle notizie de Il Post, letta da una voce AI.</description>
    <itunes:author>Notizie del mattino</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <item>
      <title>{escape(titolo)}</title>
      <description>{escape(descrizione)}</description>
      <pubDate>{formatdate(adesso.timestamp())}</pubDate>
      <guid isPermaLink="false">{escape(nome_file)}</guid>
      <enclosure url="{BASE_URL}/{nome_file}" length="{dimensione}" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""
    with open(os.path.join(cartella, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(xml)


def scrivi_pagina(cartella, nome_file, titolo):
    pagina = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notizie del mattino</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem">
<h1>Notizie del mattino</h1>
<p>{escape(titolo)}</p>
<audio controls src="{nome_file}" style="width:100%"></audio>
<p>Feed podcast: <code>{BASE_URL}/feed.xml</code></p>
</body></html>
"""
    with open(os.path.join(cartella, "index.html"), "w", encoding="utf-8") as f:
        f.write(pagina)


def main():
    adesso = datetime.now(ZoneInfo("Europe/Rome"))
    data_parlata = f"{GIORNI[adesso.weekday()]} {adesso.day} {MESI[adesso.month - 1]} {adesso.year}"
    sigla_data = adesso.strftime("%Y-%m-%d")
    nome_file = f"notizie-{sigla_data}.mp3"
    titolo = f"Notizie di {data_parlata}"

    notizie = prendi_notizie()
    testo = scrivi_testo(notizie, data_parlata)
    parole = len(testo.split())
    print(f"Testo generato: {parole} parole (circa {parole / 155:.1f} minuti)")
    if parole < PAROLE_TARGET * 0.6:
        print("Attenzione: testo più corto del previsto.")

    cartella = "site"
    os.makedirs(cartella, exist_ok=True)
    asyncio.run(crea_audio(testo, os.path.join(cartella, nome_file)))

    scrivi_feed(cartella, nome_file, titolo, testo[:300] + "...", adesso)
    scrivi_pagina(cartella, nome_file, titolo)
    print("Fatto:", nome_file)


if __name__ == "__main__":
    main()
