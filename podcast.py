import asyncio
import html
import os
import re
from datetime import datetime
from email.utils import formatdate
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import edge_tts
import feedparser
from anthropic import Anthropic

# ---------- IMPOSTAZIONI (modificabili) ----------
USER = "FrankFrancis1"
REPO = "notizie-podcast"
BASE_URL = f"https://{USER.lower()}.github.io/{REPO}"

FEEDS = [
    "https://www.ilpost.it/feed/",
    "https://www.ilpost.it/italia/feed/",
    "https://www.ilpost.it/mondo/feed/",
]
MAX_ARTICOLI = 18
VOCE = "it-IT-IsabellaNeural"  # alternativa maschile: it-IT-DiegoNeural
VELOCITA = "+5%"
MODELLO = "claude-sonnet-5-5"
PAROLE_TARGET = 1100  # circa 7-8 minuti di audio

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
# --------------------------------------------------


def pulisci(testo):
    testo = re.sub(r"<[^>]+>", " ", testo or "")
    return re.sub(r"\s+", " ", html.unescape(testo)).strip()


def prendi_notizie():
    viste = set()
    righe = []
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
            righe.append(f"- {titolo}. {riassunto}")
            if len(righe) >= MAX_ARTICOLI:
                break
        if len(righe) >= MAX_ARTICOLI:
            break
    if not righe:
        raise SystemExit("Nessuna notizia trovata nei feed.")
    return "\n".join(righe)


def scrivi_testo(notizie, data_parlata):
    prompt = (
        f"Sei il conduttore di un notiziario radiofonico mattutino in italiano. "
        f"Oggi è {data_parlata}. Qui sotto trovi i titoli e gli estratti delle notizie "
        f"del giorno da Il Post.\n\n"
        f"Scrivi il testo parlato di una rassegna di circa {PAROLE_TARGET} parole, "
        f"da ascoltare in metro.\n"
        f"Regole:\n"
        f"- Apri con un saluto breve e la data, chiudi con una frase di congedo.\n"
        f"- Raggruppa le notizie per tema (Italia, mondo, economia, cultura...) "
        f"e dai più spazio alle più importanti.\n"
        f"- Tono chiaro, scorrevole, naturale, come una radio di qualità.\n"
        f"- Usa solo le informazioni fornite, non inventare dettagli o numeri.\n"
        f"- Solo testo da leggere ad alta voce: niente elenchi, titoli, markdown, "
        f"emoji, parentesi o URL.\n"
        f"- Scrivi per esteso numeri, sigle e simboli difficili da pronunciare.\n\n"
        f"NOTIZIE:\n{notizie}"
    )
    client = Anthropic()
    risposta = client.messages.create(
        model=MODELLO,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    testo = risposta.content[0].text.strip()
    if len(testo) < 500:
        raise SystemExit("Il testo generato è troppo corto, interrompo.")
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
    print(f"Testo generato: {len(testo.split())} parole")

    cartella = "site"
    os.makedirs(cartella, exist_ok=True)
    asyncio.run(crea_audio(testo, os.path.join(cartella, nome_file)))

    scrivi_feed(cartella, nome_file, titolo, testo[:300] + "...", adesso)
    scrivi_pagina(cartella, nome_file, titolo)
    print("Fatto:", nome_file)


if __name__ == "__main__":
    main()
  
