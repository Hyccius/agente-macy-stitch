#!/usr/bin/env python3
"""
Agente Instagram di Macy & Stitch.

Ogni volta che parte:
  1. legge le foto/video nuovi dalla cartella Google Drive;
  2. chiede a Claude che post fare oggi (foto, carosello, reel o immagine IA) e gli fa scrivere la didascalia;
  3. se serve, genera un'immagine IA con Gemini usando le foto di riferimento dei cani;
  4. pubblica su Instagram e aggiorna registro.json.

Variabili d'ambiente (impostate come "Secrets" su GitHub):
  ANTHROPIC_API_KEY, GEMINI_API_KEY, IG_TOKEN, IG_USER_ID, GOOGLE_SA_JSON, DRIVE_FOLDER_ID
Opzionali:
  PROVA=true  -> fa tutto tranne pubblicare (anteprima in _anteprima/)
  FORZA=auto|foto|carosello|reel|ia
"""
import base64
import datetime
import io
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import anthropic
import requests
from PIL import Image, ImageOps

try:  # foto HEIC dei telefoni
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------- impostazioni
ROOT = pathlib.Path(__file__).resolve().parent
REGISTRO = ROOT / "registro.json"
CANI_MD = ROOT / "cani.md"
LAVORO = ROOT / "_lavoro"
ANTEPRIMA = ROOT / "_anteprima"
MEDIA = ROOT / "media"

PROVA = os.getenv("PROVA", "false").strip().lower() == "true"
FORZA = (os.getenv("FORZA", "auto") or "auto").strip().lower()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.1-flash-image-preview"), "gemini-2.5-flash-image"]
API_VER = os.getenv("IG_API_VERSION", "v23.0")
FUSO = ZoneInfo("Europe/Rome")

NOMI_CANI = ["macy", "stitch"]
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MAX_FOTO = 15          # foto mostrate a Claude per scegliere
MAX_VIDEO = 4          # video mostrati a Claude per scegliere
MAX_VIDEO_MB = 500
ETICHETTA_IA = "✨ Immagine creata con l'IA partendo dalle foto vere di Macy e Stitch."


def log(*a):
    print(*a, flush=True)


def riepilogo(testo):
    """Scrive nel riepilogo visibile su GitHub Actions."""
    p = os.getenv("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(testo + "\n")


# ---------------------------------------------------------------- registro
def carica_registro():
    if REGISTRO.exists():
        return json.loads(REGISTRO.read_text(encoding="utf-8"))
    return {"usati": [], "post": []}


def salva_registro(reg):
    REGISTRO.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- Google Drive
def drive():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def lista(svc, folder_id):
    files, token = [], None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,createdTime,size)",
            pageSize=200,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return files


def esplora(svc, folder_id, percorso=""):
    """Restituisce (materiali, riferimenti). La sottocartella 'riferimenti' contiene le foto modello dei cani."""
    materiali, riferimenti = [], []
    for f in lista(svc, folder_id):
        if f["mimeType"] == "application/vnd.google-apps.folder":
            m, r = esplora(svc, f["id"], f"{percorso}{f['name']}/")
            if f["name"].strip().lower().startswith("riferiment"):
                riferimenti += m + r
            else:
                materiali += m
                riferimenti += r
        elif f["mimeType"].startswith(("image/", "video/")):
            f["percorso"] = percorso + f["name"]
            materiali.append(f)
    return materiali, riferimenti


def scarica(svc, file_id, dest):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=20 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest


# ---------------------------------------------------------------- immagini e video
def apri_foto(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def jpeg_b64(im, lato=512):
    im = im.copy()
    im.thumbnail((lato, lato))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def prepara_foto(src, dest, solo_4_5=False, max_w=1440):
    """Instagram accetta proporzioni tra 4:5 (verticale) e 1.91:1 (orizzontale): ritaglia se serve."""
    im = apri_foto(src)
    w, h = im.size
    r = w / h
    lim_min, lim_max = 0.8, (0.8 if solo_4_5 else 1.91)
    if r < lim_min:  # troppo alta: taglia soprattutto sotto, per non tagliare le teste
        nh = int(w / lim_min)
        top = int((h - nh) * 0.3)
        im = im.crop((0, top, w, top + nh))
    elif r > lim_max:  # troppo larga: taglia ai lati
        nw = int(h * lim_max)
        left = (w - nw) // 2
        im = im.crop((left, 0, left + nw, h))
    if im.width > max_w:
        nuova_h = im.height * max_w / im.width
        # arrotonda in modo da restare sempre dentro i limiti di Instagram
        nuova_h = int(nuova_h) if im.width < im.height else -int(-nuova_h // 1)
        im = im.resize((max_w, nuova_h), Image.LANCZOS)
    im.save(dest, "JPEG", quality=90)
    return dest


def durata_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def fotogrammi(path, n=3):
    d = durata_video(path) or 3
    immagini = []
    for i in range(n):
        t = d * (i + 1) / (n + 1)
        dest = LAVORO / f"fotogramma_{path.stem}_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=512:-2", str(dest)],
            check=False,
        )
        if dest.exists():
            immagini.append(apri_foto(dest))
    return immagini, d


def prepara_video(src, dest):
    """Converte il video in un formato sicuro per i Reel (H.264 + AAC, max 90 s)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", "90",
         "-vf", "scale='trunc(min(1080,iw)/2)*2':-2,fps=30",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart", str(dest)],
        check=True,
    )
    return dest


# ---------------------------------------------------------------- Claude
def claude(system, content, max_tokens=2000):
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in r.content if b.type == "text")


def estrai_json(testo):
    m = re.search(r"\{.*\}", testo, re.S)
    if not m:
        raise ValueError(f"Claude non ha risposto in JSON:\n{testo}")
    return json.loads(m.group(0))


def img_block(b64):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def decidi_post(candidati, reg, ia_per, oggi):
    system = (
        "Sei il social media manager del profilo Instagram di due cani, Macy e Stitch, "
        "e lo gestisci in completa autonomia.\n\n" + CANI_MD.read_text(encoding="utf-8")
    )
    storia = reg["post"][-20:][::-1]
    righe_storia = "\n".join(
        f"- {p['data']} · {p['tipo']} · {p.get('didascalia', '')[:140]!r}" for p in storia
    ) or "nessuno: questo è il primo post del profilo!"

    content = [{"type": "text", "text": (
        f"Oggi è {GIORNI[oggi.weekday()]} {oggi.strftime('%d/%m/%Y')}.\n\n"
        f"Ultimi post pubblicati (dal più recente):\n{righe_storia}\n\n"
        "Materiali nuovi disponibili (foto e video veri dei cani). Ognuno ha un codice."
    )}]
    for codice, c in candidati.items():
        if c["tipo"] == "foto":
            content.append({"type": "text", "text": f"{codice} (foto, caricata il {c['data']}):"})
            content.append(img_block(c["anteprima"][0]))
        else:
            content.append({"type": "text", "text": (
                f"{codice} (video di {c['durata']:.0f} secondi, caricato il {c['data']}) – fotogrammi:"
            )})
            for b in c["anteprima"]:
                content.append(img_block(b))
    if not candidati:
        content.append({"type": "text", "text": "(nessun materiale nuovo disponibile)"})

    if ia_per:
        ia_txt = "Immagini IA: possibili con " + " e ".join(n.capitalize() for n in ia_per) + "."
    else:
        ia_txt = "Immagini IA: NON disponibili (mancano le foto di riferimento)."

    vincolo = ""
    if FORZA != "auto":
        vincolo = f"\nOBBLIGATORIO: oggi il tipo di post deve essere '{FORZA}'.\n"

    content.append({"type": "text", "text": f"""
{ia_txt}
{vincolo}
Regole per scegliere il post di oggi:
- Varia i tipi: foto singola, carosello (da 2 a 10 foto dello stesso momento o tema), reel (da un video), immagine IA.
- Le immagini IA devono essere circa 1 post ogni 3-4, mai due di fila. Se non ci sono materiali nuovi, usa l'IA.
- Scegli i materiali migliori: nitidi, cani ben visibili, espressivi, buffi o teneri.
- Per l'IA inventa una scena divertente, tenera o fantasiosa, coerente con il carattere dei cani e con il periodo
  dell'anno (stagioni, feste, ricorrenze). Scrivila in INGLESE, concreta e visiva: luogo, azione, espressioni, luce.
  Nessun testo dentro l'immagine.
- Nella didascalia NON scrivere che l'immagine è fatta con l'IA (lo aggiunge il sistema) e NON mettere hashtag
  (vanno nel campo separato).

Rispondi SOLO con un oggetto JSON, senza altro testo:
{{"tipo": "foto|carosello|reel|ia",
  "materiali": ["codici scelti, vuoto se ia"],
  "cani_ia": ["macy", "stitch"],
  "scena_ia": "scena in inglese, solo se ia",
  "didascalia": "testo del post in italiano",
  "hashtag": "#uno #due ...",
  "motivo": "una riga sul perché di questa scelta"}}"""})

    return estrai_json(claude(system, content))


def controlla_ia(path, cani, riferimenti):
    """Claude controlla che l'immagine IA sia credibile e che i cani siano riconoscibili."""
    content = [{"type": "text", "text": "Foto vere di riferimento:"}]
    for n in cani:
        for im in riferimenti.get(n, [])[:2]:
            content.append({"type": "text", "text": n.capitalize()})
            content.append(img_block(jpeg_b64(im)))
    content.append({"type": "text", "text": "Immagine generata:"})
    content.append(img_block(jpeg_b64(apri_foto(path), 1024)))
    content.append({"type": "text", "text": (
        "L'immagine generata è adatta a Instagram? Controlla: i cani sono riconoscibili rispetto alle foto vere, "
        "nessun errore evidente (zampe in più, occhi strani, corpi deformati), nessuna scritta strana. "
        'Rispondi SOLO in JSON: {"ok": true/false, "problema": "breve, in inglese, se ok è false"}'
    )})
    try:
        return estrai_json(claude("Sei un attento controllore di qualità di immagini.", content, 300))
    except Exception as e:
        log("Controllo IA non riuscito, accetto l'immagine:", e)
        return {"ok": True}


# ---------------------------------------------------------------- Gemini (immagini IA)
def aspetto_cani():
    """Legge da cani.md le righe 'macy: ...' / 'stitch: ...' della sezione sull'aspetto."""
    descr = {}
    for riga in CANI_MD.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(macy|stitch)\s*:\s*(.+?)\s*$", riga, re.I)
        if m:
            descr[m.group(1).lower()] = m.group(2)
    return descr


def genera_ia(scena, cani, riferimenti, dest, correzione=""):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    contents = []
    for n in cani:
        for im in riferimenti.get(n, [])[:3]:
            contents.append(f"Reference photo of {n.capitalize()}:")
            contents.append(im)
    nomi = " and ".join(n.capitalize() for n in cani)
    descr = aspetto_cani()
    dettagli = " ".join(f"{n.capitalize()} is a {descr[n]}" for n in cani if descr.get(n))
    prompt = (
        f"Create a brand new photograph featuring {nomi}, the dog(s) shown in the reference photos. "
        "Keep each dog exactly recognizable: same breed, size, fur colors and markings, face and eyes. "
        + (f"Appearance details (follow them strictly): {dettagli} " if dettagli else "")
        + f"Scene: {scena} "
        "Style: warm, high-quality, cinematic photo for Instagram, natural lighting, realistic anatomy. "
        "No text, no letters, no watermark."
    )
    if correzione:
        prompt += f" Important: avoid this problem from a previous attempt: {correzione}"
    contents.append(prompt)

    ultimo_errore = None
    for model in GEMINI_MODELS:
        try:
            log(f"Genero l'immagine con {model}…")
            r = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="4:5"),
                ),
            )
            for cand in r.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    dati = getattr(getattr(part, "inline_data", None), "data", None)
                    if dati:
                        Image.open(io.BytesIO(dati)).convert("RGB").save(dest, "JPEG", quality=92)
                        return prepara_foto(dest, dest, solo_4_5=True)
            ultimo_errore = RuntimeError("nessuna immagine nella risposta")
        except Exception as e:
            ultimo_errore = e
            log(f"  {model} non ha funzionato: {e}")
    raise RuntimeError(f"Generazione IA fallita: {ultimo_errore}")


# ---------------------------------------------------------------- Instagram
TOKEN = os.getenv("IG_TOKEN", "").strip()
IG_USER = os.getenv("IG_USER_ID", "").strip()
HOST = "graph.instagram.com" if TOKEN.startswith("IG") else "graph.facebook.com"
BASE = f"https://{HOST}/{API_VER}"


def ig_post(path, **params):
    params["access_token"] = TOKEN
    r = requests.post(f"{BASE}/{path}", data=params, timeout=180)
    if not r.ok:
        raise RuntimeError(f"Errore Instagram ({path}): {r.text}")
    return r.json()


def ig_get(path, **params):
    params["access_token"] = TOKEN
    r = requests.get(f"{BASE}/{path}", params=params, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Errore Instagram ({path}): {r.text}")
    return r.json()


def attendi_container(cid, max_s=900):
    t0 = time.time()
    while time.time() - t0 < max_s:
        stato = ig_get(cid, fields="status_code,status").get("status_code")
        if stato == "FINISHED":
            return
        if stato in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram ha rifiutato il contenuto: {ig_get(cid, fields='status')}")
        time.sleep(8)
    raise RuntimeError("Instagram ci mette troppo a elaborare il contenuto")


def ig_pubblica(cid):
    return ig_post(f"{IG_USER}/media_publish", creation_id=cid)["id"]


def pubblica_foto(url, didascalia):
    cid = ig_post(f"{IG_USER}/media", image_url=url, caption=didascalia)["id"]
    attendi_container(cid)
    return ig_pubblica(cid)


def pubblica_carosello(urls, didascalia):
    figli = []
    for u in urls:
        c = ig_post(f"{IG_USER}/media", image_url=u, is_carousel_item="true")["id"]
        attendi_container(c)
        figli.append(c)
    cid = ig_post(f"{IG_USER}/media", media_type="CAROUSEL", children=",".join(figli), caption=didascalia)["id"]
    attendi_container(cid)
    return ig_pubblica(cid)


def pubblica_reel(path, didascalia):
    c = ig_post(f"{IG_USER}/media", media_type="REELS", upload_type="resumable",
                caption=didascalia, share_to_feed="true")
    cid = c["id"]
    uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API_VER}/{cid}"
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        r = requests.post(uri, data=fh, timeout=900, headers={
            "Authorization": f"OAuth {TOKEN}", "offset": "0", "file_size": str(size),
        })
    if not r.ok:
        raise RuntimeError(f"Caricamento video fallito: {r.text}")
    attendi_container(cid)
    return ig_pubblica(cid)


# ---------------------------------------------------------------- GitHub (le foto devono avere un link pubblico)
def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def carica_su_github(files):
    for f in files:
        git("add", "-f", str(f.relative_to(ROOT)))
    git("commit", "-m", "Media per il post di oggi")
    git("push")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    repo = os.environ["GITHUB_REPOSITORY"]
    urls = [f"https://raw.githubusercontent.com/{repo}/{sha}/{f.relative_to(ROOT).as_posix()}" for f in files]
    for u in urls:  # aspetta che il link funzioni
        for _ in range(20):
            if requests.head(u, timeout=30).status_code == 200:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"Il link {u} non risponde: il repository è pubblico?")
    return urls


def chiudi_e_salva(reg):
    salva_registro(reg)
    if MEDIA.exists():
        shutil.rmtree(MEDIA)
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode != 0:
        git("commit", "-m", "Aggiorno il registro dei post")
        git("push")


# ---------------------------------------------------------------- programma principale
def main():
    oggi = datetime.datetime.now(FUSO)
    stamp = oggi.strftime("%Y%m%d_%H%M")
    reg = carica_registro()
    for d in (LAVORO, ANTEPRIMA, MEDIA):
        d.mkdir(exist_ok=True)
    log(f"Avvio agente · {oggi:%d/%m/%Y %H:%M} · prova={PROVA} · tipo={FORZA}")

    # 1) Drive
    svc = drive()
    materiali, rif_drive = esplora(svc, os.environ["DRIVE_FOLDER_ID"])
    usati = set(reg["usati"])
    nuovi = [m for m in materiali if m["id"] not in usati]
    log(f"Su Drive: {len(materiali)} file, di cui {len(nuovi)} mai usati; {len(rif_drive)} foto di riferimento")

    def scegli(lista_file, n):
        lista_file = sorted(lista_file, key=lambda m: m.get("createdTime", ""), reverse=True)
        recenti, altri = lista_file[: n - n // 3], lista_file[n - n // 3:]
        return recenti + random.sample(altri, min(len(altri), n // 3))

    foto = scegli([m for m in nuovi if m["mimeType"].startswith("image/")], MAX_FOTO)
    video = scegli([m for m in nuovi if m["mimeType"].startswith("video/")
                    and int(m.get("size", 0)) < MAX_VIDEO_MB * 1024 * 1024], MAX_VIDEO)

    candidati = {}
    for i, m in enumerate(foto, 1):
        try:
            p = scarica(svc, m["id"], LAVORO / f"F{i}_{m['id']}{pathlib.Path(m['name']).suffix}")
            candidati[f"F{i}"] = {"tipo": "foto", "file": m, "path": p,
                                  "data": m.get("createdTime", "")[:10],
                                  "anteprima": [jpeg_b64(apri_foto(p))]}
        except Exception as e:
            log(f"Salto {m['percorso']}: {e}")
    for i, m in enumerate(video, 1):
        try:
            p = scarica(svc, m["id"], LAVORO / f"V{i}_{m['id']}{pathlib.Path(m['name']).suffix}")
            frames, d = fotogrammi(p)
            if not frames or d < 3:
                continue
            candidati[f"V{i}"] = {"tipo": "video", "file": m, "path": p, "durata": d,
                                  "data": m.get("createdTime", "")[:10],
                                  "anteprima": [jpeg_b64(f) for f in frames]}
        except Exception as e:
            log(f"Salto {m['percorso']}: {e}")

    riferimenti = {n: [] for n in NOMI_CANI}
    for m in rif_drive:
        nome_file = m["name"].lower()
        cani_nel_file = [n for n in NOMI_CANI if n in nome_file]
        if not cani_nel_file or not m["mimeType"].startswith("image/"):
            continue
        if all(len(riferimenti[n]) >= 3 for n in cani_nel_file):
            continue
        try:
            im = apri_foto(scarica(svc, m["id"], LAVORO / f"rif_{m['id']}"))
            im.thumbnail((1024, 1024))
            for n in cani_nel_file:
                riferimenti[n].append(im)
        except Exception as e:
            log(f"Riferimento {m['name']} non leggibile: {e}")
    ia_per = [n for n in NOMI_CANI if riferimenti[n]]

    # 2) Claude decide
    scelta = decidi_post(candidati, reg, ia_per, oggi)
    log("Scelta di Claude:", json.dumps(scelta, ensure_ascii=False, indent=2))
    tipo = FORZA if FORZA != "auto" else str(scelta.get("tipo", "")).lower()
    sel = [candidati[c] for c in scelta.get("materiali") or [] if c in candidati]
    if FORZA == "auto" and tipo != "ia" and not sel and ia_per:
        log("Nessun materiale nuovo utilizzabile: creo un contenuto con l'IA")
        tipo = "ia"
        if not scelta.get("scena_ia"):
            scelta = decidi_post({}, reg, ia_per, oggi)

    if tipo in ("foto", "carosello"):
        sel = [s for s in sel if s["tipo"] == "foto"]
        if tipo == "carosello" and len(sel) < 2:
            tipo = "foto"
        sel = sel[:1] if tipo == "foto" else sel[:10]
    elif tipo == "reel":
        sel = [s for s in sel if s["tipo"] == "video"][:1]
    elif tipo != "ia":
        raise RuntimeError(f"Tipo di post sconosciuto: {tipo}")
    if tipo != "ia" and not sel:
        raise RuntimeError("Claude non ha scelto materiali validi per il post di oggi")
    if tipo == "ia" and not ia_per:
        raise RuntimeError("Per le immagini IA servono le foto nella cartella Drive 'riferimenti'")

    didascalia = (scelta.get("didascalia") or "").strip()
    if tipo == "ia":
        didascalia += "\n\n" + ETICHETTA_IA
    hashtag = (scelta.get("hashtag") or "").strip()
    testo = (didascalia + ("\n\n" + hashtag if hashtag else ""))[:2200]

    # 3) Preparo i file
    files = []
    if tipo == "foto":
        files = [prepara_foto(sel[0]["path"], MEDIA / f"{stamp}_1.jpg")]
    elif tipo == "carosello":
        files = [prepara_foto(s["path"], MEDIA / f"{stamp}_{i}.jpg", solo_4_5=True) for i, s in enumerate(sel, 1)]
    elif tipo == "reel":
        files = [prepara_video(sel[0]["path"], LAVORO / f"{stamp}_reel.mp4")]
    else:
        cani = [n for n in (scelta.get("cani_ia") or []) if n in ia_per] or ia_per
        scena = scelta.get("scena_ia") or "the dogs playing happily together in a sunny park"
        dest = MEDIA / f"{stamp}_ia.jpg"
        genera_ia(scena, cani, riferimenti, dest)
        verdetto = controlla_ia(dest, cani, riferimenti)
        if not verdetto.get("ok", True):
            log("Il controllo qualità chiede di rifarla:", verdetto.get("problema"))
            genera_ia(scena, cani, riferimenti, dest, correzione=verdetto.get("problema", ""))
        files = [dest]

    riepilogo(f"## Post di oggi: {tipo}\n\n**Perché:** {scelta.get('motivo', '')}\n\n"
              + (f"**Scena IA:** {scelta.get('scena_ia', '')}\n\n" if tipo == "ia" else "")
              + "**Didascalia:**\n\n" + testo.replace("\n", "  \n"))

    # 4) Prova o pubblicazione
    if PROVA:
        for f in files:
            shutil.copy(f, ANTEPRIMA / f.name)
        (ANTEPRIMA / "didascalia.txt").write_text(testo, encoding="utf-8-sig")
        shutil.rmtree(MEDIA, ignore_errors=True)
        log("\n--- MODALITÀ PROVA: niente pubblicato. Didascalia:\n" + testo)
        return

    try:
        if tipo == "reel":
            ig_id = pubblica_reel(files[0], testo)
        else:
            urls = carica_su_github(files)
            ig_id = pubblica_foto(urls[0], testo) if tipo != "carosello" else pubblica_carosello(urls, testo)
        log(f"Pubblicato! ID Instagram: {ig_id}")
        reg["usati"] += [s["file"]["id"] for s in sel]
        reg["post"].append({"data": oggi.strftime("%Y-%m-%d %H:%M"), "tipo": tipo, "ig_id": ig_id,
                            "didascalia": didascalia, "file": [s["file"]["percorso"] for s in sel],
                            "scena_ia": scelta.get("scena_ia") if tipo == "ia" else None})
        riepilogo(f"\n✅ Pubblicato su Instagram (ID {ig_id})")
    finally:
        chiudi_e_salva(reg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n❌ ERRORE: {e}")
        riepilogo(f"\n❌ **Errore:** {e}")
        sys.exit(1)
