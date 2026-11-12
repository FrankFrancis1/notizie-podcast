import asyncio
import base64
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
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
    "https://www.ilpost.it/politica/feed/",
    "https://www.ilpost.it/economia/feed/",
    "https://www.ilpost.it/cultura/feed/",
    "https://www.ilpost.it/scienza/feed/",
    "https://www.ilpost.it/tecnologia/feed/",
    "https://www.ilpost.it/sport/feed/",
]  # i feed che non esistono o sono vuoti vengono semplicemente saltati

# Quali notizie
POOL_MAX = 60        # notizie del giorno tra cui scegliere le più importanti
MIN_NOTIZIE = 10     # se oggi ce ne sono meno, aggiungo quelle del giorno prima
MAX_PAGINE_FEED = 4  # quante pagine di ogni feed leggere per risalire fino a inizio giornata

# Struttura della puntata (circa 10 minuti)
N_APPROFONDIMENTI = 5         # notizie raccontate in modo disteso
PAROLE_APPROFONDIMENTO = 170  # parole indicative per ciascuna
N_PUNTI_ELENCO = 5            # titoli mostrati come elenco puntato nella descrizione dell'episodio
N_BREVI = 8                   # notizie nella rassegna "in breve"
PAROLE_BREVE = 65             # parole indicative per ciascuna
# Durata indicativa: (5x170 + 8x65 + 100) = circa 1500 parole = 9-10 minuti di audio.
# Per accorciare o allungare, cambia questi quattro numeri.

MAX_CARATTERI_APPROF = 4000  # testo articolo passato a Gemini (approfondimenti)
MAX_CARATTERI_BREVI = 1800   # idem per le notizie brevi

# Voce
USA_GEMINI_TTS = True        # se False usa sempre Isabella
VOCE_GEMINI = "Aoede"         # altre: Zephyr, Puck, Charon, Fenrir, Leda, Orus, Aoede
MODELLI_TTS = [
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-2.5-flash-preview-tts",
]
# Voce: un nome della lista di 30 voci (es. "Aoede", "Charon", "Sulafat") oppure un ID personale
# "voice_..." creato con Voice design in Google AI Studio (vedi spiegazione).
# Stile: frase breve che NON viene letta (es. "calda e chiara, da conduttore radiofonico").
# Lasciare vuoto per la voce naturale: la documentazione consiglia di provare prima senza stile.
STILE_VOCE = ""
MAX_CARATTERI_TTS = 5000     # dimensione di ogni pezzo inviato alla voce (meno pezzi = meno richieste)
TENTATIVI_TTS = 3

# Email con il recap scritto. Servono i due "secrets" GMAIL_USER e GMAIL_APP_PASSWORD nel repo.
EMAIL_DESTINATARIO = "francescorosa.business@gmail.com"

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


def crea_client():
    # La libreria 3.x usa di default l'API stabile "v1", che non accetta la voce:
    # per questo forzo "v1beta". La chiave è letta da GEMINI_API_KEY.
    return genai.Client(http_options=types.HttpOptions(api_version="v1beta", timeout=300_000))


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


def data_voce(voce, fuso):
    """Data (fuso italiano) di pubblicazione di un articolo, o None se non è indicata."""
    t = voce.get("published_parsed") or voce.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).astimezone(fuso).date()


def leggi_feed(url, giorno_minimo, fuso):
    """Legge un feed pagina per pagina, finché arriva a notizie più vecchie di giorno_minimo."""
    voci, link_visti = [], set()
    pagine = 0
    for pagina in range(1, MAX_PAGINE_FEED + 1):
        indirizzo = url if pagina == 1 else f"{url}?paged={pagina}"
        try:
            feed = feedparser.parse(indirizzo)
            if not feed.entries and pagina == 1:
                time.sleep(5)
                feed = feedparser.parse(indirizzo)  # secondo tentativo
        except Exception as e:
            print(f"Feed non leggibile {indirizzo}: {e}")
            break
        if not feed.entries and pagina == 1:
            print(f"Feed vuoto {indirizzo}: stato {feed.get('status')}, errore {feed.get('bozo_exception')}")
        nuove = [v for v in feed.entries if v.get("link") and v.get("link") not in link_visti]
        if not nuove:
            break  # pagina vuota, oppure il sito ignora la paginazione
        pagine += 1
        for v in nuove:
            link_visti.add(v.get("link"))
            voci.append(v)
        date = [data_voce(v, fuso) for v in nuove]
        date = [d for d in date if d]
        if date and min(date) < giorno_minimo:
            break  # siamo già arrivati a notizie dei giorni scorsi
    print(f"Feed {url}: {len(voci)} articoli letti in {pagine} pagine")
    return voci


def raccogli_pool(giorno, fuso):
    """Notizie pubblicate nel giorno indicato (e, se sono poche, anche il giorno prima)."""
    ieri = giorno - timedelta(days=1)
    viste = set()
    di_oggi, di_ieri = [], []
    for url in FEEDS:
        for voce in leggi_feed(url, ieri, fuso):
            titolo = pulisci(voce.get("title"))
            if not titolo or titolo in viste:
                continue
            d = data_voce(voce, fuso)
            elemento = {
                "titolo": titolo,
                "riassunto": pulisci(voce.get("summary"))[:600],
                "link": voce.get("link"),
            }
            if d == giorno:
                viste.add(titolo)
                di_oggi.append(elemento)
            elif d == ieri:
                viste.add(titolo)
                di_ieri.append(elemento)
    print(f"Notizie pubblicate oggi: {len(di_oggi)}; ieri: {len(di_ieri)}")
    pool = di_oggi
    if len(pool) < MIN_NOTIZIE:
        print("Poche notizie di oggi: aggiungo quelle del giorno prima.")
        pool = di_oggi + di_ieri
    if not pool:
        raise SystemExit("Nessuna notizia trovata nei feed.")
    return pool[:POOL_MAX]


def scegli_importanti(client, pool, quante):
    """Chiede a Gemini quali notizie del pool sono le più rilevanti. In caso di problemi, ordine del feed."""
    if len(pool) <= quante:
        return pool
    elenco = "\n".join(f"{i}. {n['titolo']} - {n['riassunto'][:200]}" for i, n in enumerate(pool))
    prompt = (
        f"Qui sotto trovi le notizie pubblicate oggi da Il Post, numerate. "
        f"Scegli le {quante} più rilevanti per una rassegna serale: dai priorità a fatti "
        f"importanti e di interesse generale, e mantieni un buon equilibrio tra Italia, "
        f"mondo, economia e cultura; evita doppioni sullo stesso argomento. "
        f"Rispondi SOLO con i numeri scelti, separati da virgole, dal più importante "
        f"al meno importante.\n\n{elenco}"
    )
    risposta = chiedi_gemini(client, prompt)
    numeri = []
    for n in re.findall(r"\d+", risposta):
        i = int(n)
        if 0 <= i < len(pool) and i not in numeri:
            numeri.append(i)
    if len(numeri) < quante // 2:
        print("Selezione per importanza non utilizzabile: uso l'ordine del feed.")
        return pool[:quante]
    scelte = [pool[i] for i in numeri[:quante]]
    # se la selezione è più corta del previsto, completo con le altre notizie
    for i, n in enumerate(pool):
        if len(scelte) >= quante:
            break
        if i not in numeri:
            scelte.append(n)
    return scelte


def prendi_notizie(client, giorno, fuso):
    totale = N_APPROFONDIMENTI + N_BREVI
    pool = raccogli_pool(giorno, fuso)
    notizie = scegli_importanti(client, pool, totale)

    # scarico in parallelo il testo completo di tutte le notizie scelte
    with ThreadPoolExecutor(max_workers=8) as pool_thread:
        testi = list(pool_thread.map(lambda n: scarica_articolo(n["link"]), notizie))
    for notizia, testo in zip(notizie, testi):
        notizia["completo"] = testo
    completi = sum(1 for n in notizie if n["completo"])
    print(f"Notizie scelte: {len(notizie)} (con testo completo: {completi})")
    return notizie


def formatta(notizie, massimo):
    blocchi = []
    for i, n in enumerate(notizie, 1):
        corpo = n["completo"][:massimo] if n["completo"] else n["riassunto"]
        blocchi.append(f"{i}. {n['titolo']}.\n{corpo}")
    return "\n\n".join(blocchi)


def chiedi_gemini(client, prompt, json_out=False):
    for modello in MODELLI:
        for tentativo in range(1, TENTATIVI + 1):
            try:
                config = types.GenerateContentConfig(response_mime_type="application/json") if json_out else None
                risposta = client.models.generate_content(model=modello, contents=prompt, config=config)
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
    "- Rispetta le lunghezze indicate (non superarle di molto): sono importanti.\n"
)


def scrivi_testo(client, notizie, data_parlata):
    approfondimenti = notizie[:N_APPROFONDIMENTI]
    brevi = notizie[N_APPROFONDIMENTI:]

    prompt_a = (
        f"Sei il conduttore di un notiziario radiofonico in italiano. La puntata viene "
        f"preparata la sera e ascoltata la mattina dopo. Racconta le notizie di {data_parlata}: "
        f"non dire mai \"oggi\", \"stamattina\", \"ieri\" o \"stasera\", ma riferisciti al giorno "
        f"per nome o alla data. Stai scrivendo la PRIMA PARTE della puntata.\n\n"
        f"Scrivi: un saluto breve (per esempio \"Buongiorno\") con la data della rassegna e "
        f"una panoramica dei titoli del giorno "
        f"(3 o 4 frasi); poi un approfondimento per OGNUNA delle {len(approfondimenti)} "
        f"notizie qui sotto, nell'ordine dato. Ogni approfondimento deve essere un "
        f"racconto di circa {PAROLE_APPROFONDIMENTO} parole (mai più di "
        f"{int(PAROLE_APPROFONDIMENTO * 1.25)}): cosa è successo, "
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
            f"Sei il conduttore di un notiziario radiofonico in italiano. La puntata viene "
            f"ascoltata la mattina dopo e racconta le notizie di {data_parlata}: non dire mai "
            f"\"oggi\", \"stamattina\", \"ieri\" o \"stasera\". Stai scrivendo la SECONDA E "
            f"ULTIMA PARTE della puntata, che comincia dopo gli approfondimenti.\n\n"
            f"Apri con una breve frase di passaggio (per esempio che passiamo alle altre "
            f"notizie del giorno), senza salutare di nuovo. Poi la rassegna \"in breve\": "
            f"per OGNUNA delle {len(brevi)} notizie qui sotto scrivi un paragrafo di circa "
            f"{PAROLE_BREVE} parole (mai più di {int(PAROLE_BREVE * 1.25)}), di almeno due frasi: "
            f"cosa è successo, dove, chi è "
            f"coinvolto, e il contesto presente nel testo. Puoi raggruppare per tema "
            f"(Italia, mondo, economia, cultura...) con frasi di transizione. "
            f"Chiudi con un congedo breve.\n\n"
            f"{REGOLE_COMUNI}\nNOTIZIE:\n\n{formatta(brevi, MAX_CARATTERI_BREVI)}"
        )
        parte_b = chiedi_gemini(client, prompt_b)
        print(f"Seconda parte: {len(parte_b.split())} parole")
    if not brevi:
        print("Tutte le notizie disponibili sono negli approfondimenti: niente rassegna in breve.")
    if len(parte_b) < 300:
        if brevi:
            print("Seconda parte non disponibile, chiudo la puntata con un congedo.")
        parte_b = "Per questa rassegna è tutto. Buona giornata, e al prossimo notiziario."

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


def sintetizza_nuovo(client, modello, testo):
    """Modelli Gemini 3.8 TTS: usano la Interactions API; la voce va in generation_config.speech_config."""
    blocco = {"type": "text", "text": testo}
    if STILE_VOCE:
        blocco["annotations"] = [{"type": "speech_metadata", "style": STILE_VOCE}]
    risposta = client.interactions.create(
        model=modello,
        input=[{"type": "user_input", "content": [blocco]}],
        # PCM grezzo a 24 kHz: si possono incollare i pezzi uno dopo l'altro
        response_format={"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
        generation_config={"speech_config": [{"voice": VOCE_GEMINI}]},
    )
    dati = base64.b64decode(risposta.output_audio.data)
    if not dati:
        raise RuntimeError("audio vuoto")
    return dati


def sintetizza_vecchio(client, modello, testo):
    """Modelli TTS precedenti (2.5 / 3.1): vecchia interfaccia generate_content."""
    risposta = client.models.generate_content(
        model=modello,
        contents=testo,
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


def sintetizza(client, modello, testo):
    if modello.startswith("gemini-3.8"):
        return sintetizza_nuovo(client, modello, testo)
    return sintetizza_vecchio(client, modello, testo)


def audio_gemini(testo, percorso_mp3):
    """Voce Gemini. Solleva un errore se qualcosa non va, così si passa a Isabella."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg non installato")
    client = crea_client()
    pezzi = dividi_testo(testo, MAX_CARATTERI_TTS)
    print(f"Voce Gemini: {VOCE_GEMINI}, {len(pezzi)} pezzi di testo")
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
                    if "404" in messaggio or "PerDay" in messaggio or "INVALID_ARGUMENT" in messaggio:
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
            "guid": voce.get("id", nome),
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
      <guid isPermaLink="false">{escape(ep.get("guid", ep["nome"]))}</guid>
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


# ---------- EMAIL ----------

def leggi_json(testo):
    testo = re.sub(r"^```(?:json)?|```$", "", testo.strip(), flags=re.MULTILINE).strip()
    return json.loads(testo)


def scrivi_recap(client, notizie):
    """Un riassunto scritto per ogni notizia: più lungo per gli approfondimenti, breve per le altre."""
    blocchi = []
    for i, n in enumerate(notizie, 1):
        limite = 1500 if i <= N_APPROFONDIMENTI else 600
        corpo = n["completo"][:limite] if n.get("completo") else n["riassunto"]
        blocchi.append(f"{i}. {n['titolo']}.\n{corpo}")
    prompt = (
        "Scrivi il recap scritto (per una email) di un notiziario italiano. Per ciascuna delle "
        f"{len(notizie)} notizie numerate qui sotto scrivi un riassunto in italiano: "
        f"per le prime {min(N_APPROFONDIMENTI, len(notizie))} di 3 o 4 frasi (circa 60 parole), "
        "per le altre di 1 o 2 frasi (circa 30 parole). Stile chiaro e neutro; usa solo le "
        "informazioni fornite, senza inventare fatti, numeri o citazioni.\n"
        'Rispondi SOLO con un array JSON, per esempio [{"n": 1, "testo": "..."}, '
        '{"n": 2, "testo": "..."}], con un oggetto per ogni notizia.\n\n'
        + "\n\n".join(blocchi)
    )
    riassunti = {}
    try:
        risposta = chiedi_gemini(client, prompt, json_out=True)
        for voce in leggi_json(risposta):
            riassunti[int(voce["n"])] = str(voce["testo"]).strip()
    except Exception as e:
        print(f"Recap con Gemini non riuscito ({e}): uso gli estratti degli articoli.")
    # per le notizie senza riassunto uso l'estratto del feed
    return [riassunti.get(i) or n["riassunto"][:300] for i, n in enumerate(notizie, 1)]


def invia_email(titolo, notizie, riassunti, utente, password):
    principali = list(zip(notizie, riassunti))[:N_APPROFONDIMENTI]
    brevi = list(zip(notizie, riassunti))[N_APPROFONDIMENTI:]
    link_ascolto = f"{BASE_URL}/"

    testo = [titolo, f"Ascolta l'episodio: {link_ascolto}", "", "LE NOTIZIE PRINCIPALI", ""]
    html_righe = [
        f"<h2>{escape(titolo)}</h2>",
        f'<p><a href="{link_ascolto}">Ascolta l\'episodio</a></p>',
        "<h3>Le notizie principali</h3>",
    ]
    for n, r in principali:
        testo += [n["titolo"], r, n["link"] or "", ""]
        html_righe.append(
            f'<p><b><a href="{escape(n["link"] or "")}">{escape(n["titolo"])}</a></b><br>{escape(r)}</p>'
        )
    if brevi:
        testo += ["IN BREVE", ""]
        html_righe.append("<h3>In breve</h3>")
        for n, r in brevi:
            testo += [f"- {n['titolo']}: {r}", n["link"] or "", ""]
            html_righe.append(
                f'<p><a href="{escape(n["link"] or "")}">{escape(n["titolo"])}</a> &ndash; {escape(r)}</p>'
            )
    html_righe.append("<p><small>Riassunto generato automaticamente: può contenere imprecisioni.</small></p>")

    messaggio = EmailMessage()
    messaggio["Subject"] = titolo
    messaggio["From"] = utente
    messaggio["To"] = EMAIL_DESTINATARIO
    messaggio.set_content("\n".join(testo))
    messaggio.add_alternative("\n".join(html_righe), subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
        server.login(utente, password)
        server.send_message(messaggio)
    print(f"Email inviata a {EMAIL_DESTINATARIO}")


def descrizione_episodio(notizie):
    """Elenco puntato dei temi principali, mostrato nella descrizione dell'episodio."""
    voci = "".join(f"<li>{escape(n['titolo'])}</li>" for n in notizie[:N_PUNTI_ELENCO])
    return f"<p>Le notizie principali:</p><ul>{voci}</ul>"


def main():
    adesso = datetime.now(ZoneInfo("Europe/Rome"))
    data_parlata = f"{GIORNI[adesso.weekday()]} {adesso.day} {MESI[adesso.month - 1]} {adesso.year}"
    sigla_data = adesso.strftime("%Y-%m-%d")
    nome_file = f"notizie-{sigla_data}.mp3"
    titolo = f"Notizie di {data_parlata}"

    client = crea_client()
    notizie = prendi_notizie(client, adesso.date(), adesso.tzinfo)
    testo = scrivi_testo(client, notizie, data_parlata)
    parole = len(testo.split())
    print(f"Testo totale: {parole} parole (circa {parole / 155:.1f} minuti)")

    cartella = "site"
    os.makedirs(cartella, exist_ok=True)
    crea_audio(testo, os.path.join(cartella, nome_file))

    oggi = {
        "titolo": titolo,
        "descrizione": descrizione_episodio(notizie),
        "data": formatdate(adesso.timestamp(), usegmt=True),
        "nome": nome_file,
        # include l'ora: se rilanci il workflow lo stesso giorno, l'app vede un episodio nuovo
        "guid": f"{nome_file}-{adesso.strftime('%H%M')}",
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

    # recap scritto via email (un eventuale errore qui non blocca la pubblicazione)
    utente = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not utente or not password:
        print("Email non configurata (mancano GMAIL_USER e GMAIL_APP_PASSWORD): salto l'invio.")
        return
    try:
        riassunti = scrivi_recap(client, notizie)
        invia_email(titolo, notizie, riassunti, utente, password)
    except Exception as e:
        print(f"Email non inviata: {e}")


if __name__ == "__main__":
    main()
