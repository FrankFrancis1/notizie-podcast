import asyncio
import html
import os
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import formatdate
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import edge_tts
import feedparser
import trafilatura
from google import genai
from google.genai import types

# ---------- IMPOSTAZIONI (modificabili) ----------
USER = "FrankFrancis1"
REPO = "notizie-podcast"
BASE_URL = f"https://{USER.lower()}.github.io/{REPO}"

FEEDS = [
    "https://www.ilpost.it/feed/",
    "https://www.ilpost.it/italia/feed/",
    "https://www.ilpost.it/mondo/feed/",
]

# Struttura della puntata
N_APPROFONDIMENTI = 8        # notizie raccontate in modo disteso
PAROLE_APPROFONDIMENTO = 200  # parole minime per ciascuna
N_BREVI = 16                 # notizie nella rassegna "in breve"
PAROLE_BREVE = 80            # parole minime per ciascuna
# Durata indicativa: (8x200 + 16x80 + 150) parole = circa 3000 parole = 17-19 minuti
# Per accorciare o allungare, cambia questi quattro numeri.

MAX_CARATTERI_APPROF = 4000  # testo articolo passato a Gemini (approfondimenti)
MAX_CARATTERI_BREVI = 1800   # idem per le notizie brevi

# Voce
USA_GEMINI_TTS = True        # se False usa sempre Isabella
VOCE_GEMINI = "Kore"         # altre: Zephyr, Puck, Charon, Fenrir, Leda, Orus, Aoede
MODELLI_TTS = [
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-2.5-flash-preview-tts",
]
ISTRUZIONE_VOCE = "Leggi ad alta voce in italiano, con tono da conduttore radiofonico, caldo e chiaro:"
MAX_CARATTERI_TTS = 5000     # dimensione di ogni pezzo inviato alla voce (meno pezzi = meno richieste)
TENTATIVI_TTS = 3

VOCE_EDGE = "it-IT-IsabellaNeural"  # voce di riserva; maschile: it-IT-DiegoNeural
VELOCITA_EDGE = "+5%"

# Modelli di testo, provati in ordine
MODELLI = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
]
TENTATIVI = 3  # per ogni modello, con pausa crescente se Google è sovraccarico

# Archivio e copertina
GIORNI_ARCHIVIO = 7  # quanti episodi restano disponibili nel feed
COPERTINE = ("cover.png", "cover.jpg", "cover.jpeg")  # file da mettere nella radice del repo

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
    return testo if len(testo) > 400 else ""


def prendi_notizie():
    totale = N_APPROFONDIMENTI + N_BREVI
    viste = set()
    notizie = []
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
            notizie.append({
                "titolo": titolo,
                "riassunto": pulisci(voce.get("summary"))[:600],
                "link": voce.get("link"),
            })
            if len(notizie) >= totale:
                break
        if len(notizie) >= totale:
            break
    if not notizie:
        raise SystemExit("Nessuna notizia trovata nei feed.")

    # scarico in parallelo il testo completo di tutte le notizie
    with ThreadPoolExecutor(max_workers=8) as pool:
        testi = list(pool.map(lambda n: scarica_articolo(n["link"]), notizie))
    for notizia, testo in zip(notizie, testi):
        notizia["completo"] = testo
    completi = sum(1 for n in notizie if n["completo"])
    print(f"Notizie raccolte: {len(notizie)} (con testo completo: {completi})")
    return notizie


def formatta(notizie, massimo):
    blocchi = []
    for i, n in enumerate(notizie, 1):
        corpo = n["completo"][:massimo] if n["completo"] else n["riassunto"]
        blocchi.append(f"{i}. {n['titolo']}.\n{corpo}")
    return "\n\n".join(blocchi)


def chiedi_gemini(client, prompt):
    for modello in MODELLI:
        for tentativo in range(1, TENTATIVI + 1):
            try:
                risposta = client.models.generate_content(model=modello, contents=prompt)
                testo = (risposta.text or "").strip()
                if testo:
                    print(f"Modello usato: {modello} (tentativo {tentativo})")
                    return testo
            except Exception as e:
                print(f"{modello}, tentativo {tentativo}/{TENTATIVI} fallito: {e}")
                if "404" in str(e):
                    break  # modello inesistente, inutile riprovare
                if tentativo < TENTATIVI:
                    time.sleep(30 * tentativo)
    return ""


REGOLE_COMUNI = (
    "Regole:\n"
    "- Tono chiaro, scorrevole, naturale, come una radio di qualità; usa frasi di "
    "transizione tra una notizia e l'altra.\n"
    "- Basati sulle informazioni fornite. Puoi spiegare il contesto generale, ma non "
    "inventare fatti specifici, citazioni o numeri.\n"
    "- Solo testo da leggere ad alta voce: niente elenchi, numeri di punto, titoli, "
    "markdown, emoji, parentesi o URL.\n"
    "- Scrivi per esteso numeri, sigle e simboli difficili da pronunciare.\n"
    "- Rispetta le lunghezze minime richieste: sono importanti.\n"
)


def scrivi_testo(notizie, data_parlata):
    client = genai.Client()  # legge la chiave da GEMINI_API_KEY
    approfondimenti = notizie[:N_APPROFONDIMENTI]
    brevi = notizie[N_APPROFONDIMENTI:]

    prompt_a = (
        f"Sei il conduttore di un notiziario radiofonico mattutino in italiano. "
        f"Oggi è {data_parlata}. Stai scrivendo la PRIMA PARTE della puntata.\n\n"
        f"Scrivi: un saluto breve con la data e una panoramica dei titoli del giorno "
        f"(3 o 4 frasi); poi un approfondimento per OGNUNA delle {len(approfondimenti)} "
        f"notizie qui sotto, nell'ordine dato. Ogni approfondimento deve essere un "
        f"racconto disteso di almeno {PAROLE_APPROFONDIMENTO} parole: cosa è successo, "
        f"dove, chi è coinvolto, contesto, perché conta. "
        f"Non chiudere la puntata e non salutare alla fine: seguirà una seconda parte.\n\n"
        f"{REGOLE_COMUNI}\nNOTIZIE:\n\n{formatta(approfondimenti, MAX_CARATTERI_APPROF)}"
    )
    parte_a = chiedi_gemini(client, prompt_a)
    if len(parte_a) < 500:
        raise SystemExit("Prima parte assente o troppo corta, interrompo.")
    print(f"Prima parte: {len(parte_a.split())} parole")

    parte_b = ""
    if brevi:
        prompt_b = (
            f"Sei il conduttore di un notiziario radiofonico mattutino in italiano. "
            f"Oggi è {data_parlata}. Stai scrivendo la SECONDA E ULTIMA PARTE della puntata, "
            f"che comincia dopo gli approfondimenti.\n\n"
            f"Apri con una breve frase di passaggio (per esempio che passiamo alle altre "
            f"notizie del giorno), senza salutare di nuovo. Poi la rassegna \"in breve\": "
            f"per OGNUNA delle {len(brevi)} notizie qui sotto scrivi un paragrafo di almeno "
            f"{PAROLE_BREVE} parole, non una sola frase: cosa è successo, dove, chi è "
            f"coinvolto, e il contesto presente nel testo. Puoi raggruppare per tema "
            f"(Italia, mondo, economia, cultura...) con frasi di transizione. "
            f"Chiudi con un congedo breve.\n\n"
            f"{REGOLE_COMUNI}\nNOTIZIE:\n\n{formatta(brevi, MAX_CARATTERI_BREVI)}"
        )
        parte_b = chiedi_gemini(client, prompt_b)
        print(f"Seconda parte: {len(parte_b.split())} parole")
    if len(parte_b) < 300:
        print("Seconda parte non disponibile, chiudo la puntata con un congedo.")
        parte_b = "Per oggi è tutto. Buona giornata, e al prossimo notiziario."

    return f"{parte_a}\n\n{parte_b}"


# ---------- AUDIO ----------

def dividi_testo(testo, massimo):
    frasi = re.split(r"(?<=[.!?])\s+", testo.replace("\n", " "))
    pezzi, corrente = [], ""
    for frase in frasi:
        if corrente and len(corrente) + len(frase) + 1 > massimo:
            pezzi.append(corrente)
            corrente = frase
        else:
            corrente = f"{corrente} {frase}".strip()
    if corrente:
        pezzi.append(corrente)
    return pezzi


def sintetizza(client, modello, testo):
    risposta = client.models.generate_content(
        model=modello,
        contents=f"{ISTRUZIONE_VOCE}\n\n{testo}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOCE_GEMINI)
                )
            ),
        ),
    )
    dati = risposta.candidates[0].content.parts[0].inline_data.data
    if not dati:
        raise RuntimeError("audio vuoto")
    return dati


def audio_gemini(testo, percorso_mp3):
    """Voce Gemini. Solleva un errore se qualcosa non va, così si passa a Isabella."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg non installato")
    client = genai.Client()
    pezzi = dividi_testo(testo, MAX_CARATTERI_TTS)
    print(f"Voce Gemini: {len(pezzi)} pezzi di testo")
    pcm = bytearray()
    modelli_ok = list(MODELLI_TTS)  # modelli ancora utilizzabili, il primo è quello in uso
    for numero, pezzo in enumerate(pezzi, 1):
        dati = None
        modello = None
        while modelli_ok and not dati:
            modello = modelli_ok[0]
            for tentativo in range(1, TENTATIVI_TTS + 1):
                try:
                    dati = sintetizza(client, modello, pezzo)
                    break
                except Exception as e:
                    messaggio = str(e)
                    print(f"TTS {modello}, pezzo {numero}, tentativo {tentativo}: {messaggio[:300]}")
                    # modello inesistente o quota giornaliera finita: inutile riprovare
                    if "404" in messaggio or "PerDay" in messaggio:
                        break
                    if tentativo < TENTATIVI_TTS:
                        time.sleep(30 * tentativo)
            if not dati:
                print(f"Escludo {modello} per questa puntata")
                modelli_ok.pop(0)
        if not dati:
            raise RuntimeError(f"Voce Gemini non riuscita al pezzo {numero}")
        pcm.extend(dati)
        print(f"Pezzo {numero}/{len(pezzi)} ok ({modello})")

    # l'audio Gemini è PCM grezzo a 24 kHz, 16 bit, mono: lo converto in mp3
    percorso_pcm = percorso_mp3 + ".pcm"
    with open(percorso_pcm, "wb") as f:
        f.write(pcm)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
             "-i", percorso_pcm, "-b:a", "64k", percorso_mp3],
            check=True, capture_output=True,
        )
    finally:
        if os.path.exists(percorso_pcm):
            os.remove(percorso_pcm)
    print(f"Audio Gemini: circa {len(pcm) / (24000 * 2) / 60:.1f} minuti")


async def audio_edge(testo, percorso):
    comunicazione = edge_tts.Communicate(testo, VOCE_EDGE, rate=VELOCITA_EDGE)
    await comunicazione.save(percorso)


def crea_audio(testo, percorso):
    if USA_GEMINI_TTS:
        try:
            audio_gemini(testo, percorso)
            return
        except Exception as e:
            print(f"Voce Gemini non disponibile ({e}). Uso Isabella.")
            if os.path.exists(percorso):
                os.remove(percorso)
    asyncio.run(audio_edge(testo, percorso))
    print("Audio creato con Isabella (Edge TTS)")


# ---------- FEED E ARCHIVIO ----------

NOME_VALIDO = re.compile(r"^notizie-\d{4}-\d{2}-\d{2}\.mp3$")


def recupera_archivio(cartella, nome_file_oggi):
    """Scarica dal sito già pubblicato gli episodi dei giorni scorsi, così restano online."""
    try:
        feed = feedparser.parse(f"{BASE_URL}/feed.xml")
    except Exception as e:
        print(f"Archivio non leggibile: {e}")
        return []
    candidati = []
    for voce in feed.entries:
        if not voce.get("enclosures"):
            continue
        url = voce.enclosures[0].get("href", "")
        nome = url.rsplit("/", 1)[-1]
        if not NOME_VALIDO.match(nome) or nome == nome_file_oggi:
            continue
        candidati.append({
            "titolo": voce.get("title", nome),
            "descrizione": voce.get("summary", ""),
            "data": voce.get("published", formatdate()),
            "nome": nome,
            "url": url,
        })
    candidati.sort(key=lambda e: e["nome"], reverse=True)  # il nome contiene la data
    recuperati = []
    for ep in candidati[:GIORNI_ARCHIVIO - 1]:
        try:
            with urllib.request.urlopen(ep["url"], timeout=120) as risposta:
                with open(os.path.join(cartella, ep["nome"]), "wb") as f:
                    shutil.copyfileobj(risposta, f)
            recuperati.append(ep)
        except Exception as e:
            print(f"Episodio {ep['nome']} non recuperabile, lo salto: {e}")
    print(f"Episodi recuperati dall'archivio: {len(recuperati)}")
    return recuperati


def scrivi_feed(cartella, episodi, copertina):
    immagine = ""
    if copertina:
        url_img = f"{BASE_URL}/{copertina}"
        immagine = (
            f'    <itunes:image href="{url_img}"/>\n'
            f"    <image><url>{url_img}</url><title>Notizie del mattino</title>"
            f"<link>{BASE_URL}/</link></image>\n"
        )
    voci = ""
    for ep in episodi:
        dimensione = os.path.getsize(os.path.join(cartella, ep["nome"]))
        voci += f"""    <item>
      <title>{escape(ep["titolo"])}</title>
      <description>{escape(ep["descrizione"])}</description>
      <pubDate>{ep["data"]}</pubDate>
      <guid isPermaLink="false">{escape(ep["nome"])}</guid>
      <enclosure url="{BASE_URL}/{ep["nome"]}" length="{dimensione}" type="audio/mpeg"/>
    </item>
"""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Notizie del mattino</title>
    <link>{BASE_URL}/</link>
    <language>it</language>
    <description>Rassegna quotidiana delle notizie de Il Post, letta da una voce AI.</description>
    <itunes:author>Notizie del mattino</itunes:author>
    <itunes:explicit>false</itunes:explicit>
{immagine}{voci}  </channel>
</rss>
"""
    with open(os.path.join(cartella, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(xml)


def scrivi_pagina(cartella, episodi):
    righe = "".join(
        f'<h3>{escape(ep["titolo"])}</h3>\n'
        f'<audio controls preload="none" src="{ep["nome"]}" style="width:100%"></audio>\n'
        for ep in episodi
    )
    pagina = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notizie del mattino</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem">
<h1>Notizie del mattino</h1>
{righe}<p>Feed podcast: <code>{BASE_URL}/feed.xml</code></p>
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
    print(f"Testo totale: {parole} parole (circa {parole / 155:.1f} minuti)")

    cartella = "site"
    os.makedirs(cartella, exist_ok=True)
    crea_audio(testo, os.path.join(cartella, nome_file))

    oggi = {
        "titolo": titolo,
        "descrizione": testo[:300] + "...",
        "data": formatdate(adesso.timestamp(), usegmt=True),
        "nome": nome_file,
    }
    episodi = [oggi] + recupera_archivio(cartella, nome_file)

    copertina = None
    for nome in COPERTINE:
        if os.path.exists(nome):
            shutil.copy(nome, os.path.join(cartella, nome))
            copertina = nome
            break
    if not copertina:
        print("Nessuna copertina trovata (cover.png o cover.jpg nella radice del repo).")

    scrivi_feed(cartella, episodi, copertina)
    scrivi_pagina(cartella, episodi)
    print(f"Fatto: {nome_file} ({len(episodi)} episodi nel feed)")


if __name__ == "__main__":
    main()
