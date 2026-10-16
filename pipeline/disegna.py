"""Passo 4 · Le slide: template HTML (templates/) fotografati da Chromium headless.

Un ritocco grafico si fa nel CSS dei template, non qui.
Requisito una tantum: python -m playwright install chromium
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import sync_playwright

from brand import theme
from brand.theme import font_path
from config import ROOT

W, H = 1080, 1350  # 4:5: il formato più alto che accetta Meta Business Suite
# I formati prodotti a ogni render: stesse slide, stessi dati, due tele.
# Il reel è 9:16; i margini lasciano libere le zone coperte dall'interfaccia di Instagram
# (nome e didascalia in basso, intestazione in alto) e tengono tutto dentro il ritaglio 3:4
# che il profilo mostra in griglia.
FORMATI = {
    "post": {"larghezza": W, "altezza": H, "suffisso": ""},
    "reel": {"larghezza": 1080, "altezza": 1920, "suffisso": "_reel"},
}
OUTPUT_DIR = ROOT / "output"
# output/AAAA/MM/GG/ con i file divisi per uso. Il resto del codice chiama i giorni "AAAA-MM-GG" e usa solo
# queste funzioni per arrivare ai file.
POST, REEL, VIDEO, ALTRI = "post", "reel", "video", "altri"  # slide 4:5 · slide 9:16 · video · rapporto e testi


def cartella_giorno(giorno) -> Path:
    """'2026-10-19' (o una date) → output/2026/10/19."""
    anno, mese, gg = str(giorno).split("-")
    return OUTPUT_DIR / anno / mese / gg


def giorno_di(path: Path) -> str:
    """Il giorno 'AAAA-MM-GG' di un file dentro output/AAAA/MM/GG/<sottocartella>/."""
    gg, mese, anno = path.parent.parent.parts[-1], path.parent.parent.parts[-2], path.parent.parent.parts[-3]
    return f"{anno}-{mese}-{gg}"


def giorni_in_output() -> list[str]:
    """Tutti i giorni che hanno almeno un post, dal più vecchio."""
    return sorted(f"{d.parent.parent.name}-{d.parent.name}-{d.name}" for d in OUTPUT_DIR.glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9]")
                  if any((d / ALTRI).glob("*_rapporto.json")))
TEMPLATES = ROOT / "templates"

_env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, autoescape=True)


# --- Numeri --------------------------------------------------------------------------
def fmt_it(value: float, decimals: int = 0) -> str:
    """Numero all'italiana: 1950.5 -> '1.950,5', -7 -> '−7'."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "§").replace(".", ",").replace("§", ".").replace("-", "−")


def _valid(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


# --- Dati per i template ---------------------------------------------------------------
def _etichetta(v: float, decimali: int, unita: str, forma: str, dec_compatto: int = 1) -> str:
    """Il valore sopra una colonna o un punto: sopra il milione in forma compatta ("139,6 mln"),
    altrimenti un numero a otto cifre diventa illeggibile nello spazio di una colonna."""
    if forma != "orizzontale" and abs(v) >= 1e6:
        for soglia, nome in ((1e9, "mld"), (1e6, "mln")):
            if abs(v) >= soglia:
                return f"{fmt_it(v / soglia, dec_compatto)} {nome}"
    return f"{fmt_it(v, decimali)}{'' if unita in ('', '%') else ' '}{unita}"


def _avvallamento(valori: list, i: int) -> bool:
    """True se il punto i è più basso di tutti i suoi vicini con un valore."""
    vicini = [valori[j] for j in (i - 1, i + 1) if 0 <= j < len(valori) and _valid(valori[j])]
    return bool(vicini) and all(v > valori[i] for v in vicini)


def grafico(
    etichette: list[str],
    valori: list[float | None],
    *,
    occhiello: str,
    titolo: str,
    fonte: str,
    nota: str = "",
    pagina: str = "",
    forma: str = "colonne",
    evidenzia: int | None = None,
    riferimento: float | None = None,
    riferimento_testo: str = "",
    decimali: int = 0,
    unita: str = "",
    testi_valori: list[str] | None = None,
) -> tuple[str, dict]:
    """Prepara una slide 'grafico': calcola le proporzioni, il template le disegna.

    forma: "colonne" o "linea" per le serie nel tempo, "orizzontale" per confrontare categorie (max 8).
    evidenzia: indice della barra (o del punto) in arancio (di default l'ultimo valore disponibile).
    Le colonne partono sempre da zero; la linea usa una scala ristretta sui valori, così anche
    le differenze piccole si vedono.
    """
    if forma == "barre":
        forma = "orizzontale"
    if forma not in ("colonne", "linea", "area", "orizzontale"):
        raise ValueError(f"forma sconosciuta: {forma}")
    if len(etichette) != len(valori):
        raise ValueError("etichette e valori devono avere la stessa lunghezza")
    validi = [i for i, v in enumerate(valori) if _valid(v)]
    if not validi:
        raise ValueError("Nessun valore da disegnare")
    if forma != "linea" and any(valori[i] < 0 for i in validi):
        raise ValueError("Valori negativi supportati solo dal grafico a linea")
    if forma == "orizzontale" and len(valori) > 21:
        raise ValueError("Troppe categorie per le barre orizzontali (max 21, tutte le regioni)")
    if evidenzia is None:
        evidenzia = validi[-1]

    massimo = max(valori[i] for i in validi)
    minimo = min(valori[i] for i in validi)
    lunghezza = 70 if forma == "orizzontale" else 86  # % della scala: lascia posto ai valori
    if forma == "linea":  # scala ristretta: dal minimo al massimo, con un po' di margine
        margine = (massimo - minimo) * 0.25 or abs(massimo) * 0.1 or 1
        basso, alto = minimo - margine, massimo + margine
        scala = lambda v: round((v - basso) / (alto - basso) * 100, 2)  # noqa: E731
    else:
        scala = lambda v: round(v / massimo * lunghezza, 2)  # noqa: E731
    n = len(valori)
    passo = 1 if n <= 13 else math.ceil(n / 12)  # con tante colonne, un'etichetta ogni tanto
    # Oltre 10 colonne i valori non ci stanno tutti: si scrivono solo primo, ultimo ed evidenziato
    mostra = set(range(n)) if n <= 10 else {validi[0], validi[-1], evidenzia}
    if forma in ("linea", "area"):  # sulla linea bastano i punti chiave
        mostra = {validi[0], validi[-1], evidenzia,
                  max(validi, key=lambda i: valori[i]), min(validi, key=lambda i: valori[i])}

    # decimali della forma compatta: quanti ne servono perché valori diversi non sembrino uguali
    # (la popolazione si muove di poche decine di migliaia su 59 milioni: "58,937 mln", non "58,9 mln")
    soglia = 1e9 if abs(massimo) >= 1e9 else 1e6
    ampiezza = (massimo - minimo) / soglia
    dec_compatto = 1 if ampiezza >= 1 else 2 if ampiezza >= 0.1 else 3

    barre = []
    for i, (etichetta, v) in enumerate(zip(etichette, valori)):
        ok = _valid(v)
        barre.append(
            {
                "etichetta": etichetta if (forma == "orizzontale" or i % passo == 0) else "",
                "periodo": etichetta,
                "testo": (testi_valori[i] if testi_valori else _etichetta(v, decimali, unita, forma, dec_compatto)) if ok else "",
                "pct": scala(v) if ok else 0,
                "x": round((i + 0.5) / n * 100, 2),  # centro della colonna: allinea punti ed etichette
                "evidenzia": i == evidenzia,
                "mostra": i in mostra,
                # sulla linea, un punto più basso dei vicini ha la linea che gli scende addosso da sopra:
                # il suo valore va scritto sotto, se no la linea lo attraversa
                "sotto": forma in ("linea", "area") and ok and _avvallamento(valori, i),
            }
        )

    rif = None
    if riferimento is not None:
        rif = {"testo": riferimento_testo, "pct": scala(riferimento)}

    gap_righe, etichetta_px = 36, 30
    if forma == "orizzontale":  # tutte le righe devono stare nello spazio del grafico, senza accavallarsi
        gap_righe = 36 if n <= 6 else (20 if n <= 12 else 8)
        etichetta_px = 30 if n <= 8 else (24 if n <= 14 else 20)
        altezza = min(110 if n <= 3 else 80, int((860 - gap_righe * (n - 1)) / n))
        valore_px = 44 if n <= 4 else (32 if n <= 8 else (26 if n <= 14 else 20))
    elif forma in ("linea", "area"):
        altezza, valore_px = 0, 26
    else:
        altezza = 0
        # La dimensione dei valori dipende da quanto è larga una colonna e da quanto sono lunghi i numeri
        # 104: il margine più largo tra post e reel, così i valori stanno in entrambi i formati
        larghezza_col = (W - 2 * 104 - 12 * (n - 1)) / n
        piu_lungo = max(len(b["testo"]) for b in barre if b["mostra"]) or 1
        valore_px = int(min(36, larghezza_col / (0.62 * piu_lungo)))
        if valore_px < 20:  # non ci stanno tutti: solo primo, ultimo ed evidenziato, più grandi
            for i, b in enumerate(barre):
                b["mostra"] = i in {validi[0], validi[-1], evidenzia}
            valore_px = min(26, int(larghezza_col * 1.6 / (0.62 * piu_lungo)))

    tracciato = " ".join(f"{b['x']},{100 - b['pct']}" for b in barre if b["testo"])  # linea SVG
    if forma == "area" and tracciato:  # chiude la figura sulla linea di base
        xs = [b["x"] for b in barre if b["testo"]]
        tracciato_area = f"{xs[0]},100 {tracciato} {xs[-1]},100"
    else:
        tracciato_area = ""
    dati = dict(occhiello=occhiello, titolo=titolo, nota=nota, fonte=fonte, pagina=pagina, forma=forma,
                barre=barre, riferimento=rif, altezza_barra=altezza, valore_px=valore_px, tracciato=tracciato,
                gap_righe=gap_righe, etichetta_px=etichetta_px,
                tracciato_area=tracciato_area)
    return "grafico.html", dati


PALETTE = ["#2A4D9B", "#111418", "#A9B3C2", "#5A6170", "#D3D8DF"]  # per le serie non evidenziate


def _testo(v: float, decimali: int, unita: str) -> str:
    return f"{fmt_it(v, decimali)}{'' if unita in ('', '%') else ' '}{unita}"


def grafico_linee(etichette: list[str], serie: list[dict], *, occhiello: str, titolo: str, fonte: str,
                  nota: str = "", pagina: str = "", decimali: int = 0, unita: str = "") -> tuple[str, dict]:
    """Più linee a confronto. serie: [{"nome", "valori", "evidenzia"}]; l'evidenziata è in arancio."""
    if not 2 <= len(serie) <= 4:
        raise ValueError("Il grafico a più linee vuole da 2 a 4 serie")
    tutti = [v for s_ in serie for v in s_["valori"] if _valid(v)]
    basso, alto = min(tutti), max(tutti)
    margine = (alto - basso) * 0.2 or abs(alto) * 0.1 or 1
    basso, alto = basso - margine, alto + margine
    n = len(etichette)
    passo = 1 if n <= 13 else math.ceil(n / 12)
    altri = iter(PALETTE)
    linee = []
    for s_ in serie:
        colore = "#F2542D" if s_.get("evidenzia") else next(altri)
        nodi = [{"x": round((i + 0.5) / n * 100, 2), "pct": round((v - basso) / (alto - basso) * 100, 2)}
                for i, v in enumerate(s_["valori"]) if _valid(v)]
        linee.append({"nome": s_["nome"], "colore": colore, "evidenzia": bool(s_.get("evidenzia")),
                      "tracciato": " ".join(f"{q['x']},{100 - q['pct']}" for q in nodi), "ultimo": nodi[-1],
                      "testo": _testo(s_["valori"][-1], decimali, unita)})
    linee.sort(key=lambda l_: l_["evidenzia"])  # l'evidenziata disegnata per ultima, sopra le altre
    dati = dict(occhiello=occhiello, titolo=titolo, nota=nota, fonte=fonte, pagina=pagina, forma="linee",
                linee=linee, etichette=[e if i % passo == 0 else "" for i, e in enumerate(etichette)])
    return "grafico.html", dati


def grafico_ciambella(etichette: list[str], quote: list[float], *, evidenzia: int, centro: str, occhiello: str,
                      titolo: str, fonte: str, nota: str = "", pagina: str = "") -> tuple[str, dict]:
    """Come si divide un totale: quote in percentuale (sommano a circa 100), al massimo 6 parti."""
    if not 2 <= len(quote) <= 6:
        raise ValueError("La ciambella vuole da 2 a 6 parti")
    altri, inizio, spicchi = iter(PALETTE), 0.0, []
    for i, (nome, q) in enumerate(zip(etichette, quote)):
        colore = "#F2542D" if i == evidenzia else next(altri)
        spicchi.append({"nome": nome, "quota": f"{fmt_it(q, 1)}%", "colore": colore, "evidenzia": i == evidenzia,
                        "lunghezza": round(q, 3), "inizio": round(-inizio, 3)})
        inizio += q
    dati = dict(occhiello=occhiello, titolo=titolo, nota=nota, fonte=fonte, pagina=pagina, forma="ciambella",
                spicchi=spicchi, centro=centro)
    return "grafico.html", dati


def copertina(*, occhiello: str, titolo: str, numero: str, didascalia: str, fonte: str,
              unita: str = "") -> tuple[str, dict]:
    """unita: un'unità lunga ("per 100 famiglie") da scrivere sotto il numero grande."""
    return "copertina.html", dict(occhiello=occhiello, titolo=titolo, numero=numero, unita=unita,
                                  didascalia=didascalia, fonte=fonte)


def chiusura(*, punti: list[dict], invito: str, pagina: str, avviso: str = "") -> tuple[str, dict]:
    """punti: [{"numero": "17,71 €", "testo": "...", "evidenzia": True}, ...]"""
    return "chiusura.html", dict(punti=punti, invito=invito, pagina=pagina, avviso=avviso)


# --- Render ------------------------------------------------------------------------------
_CHECK_JS = """([larghezza, altezza]) => {
  const problemi = [];
  for (const el of document.querySelectorAll('.slide *')) {
    const r = el.getBoundingClientRect();
    if (r.width && (r.right > larghezza + .5 || r.bottom > altezza + .5)) problemi.push(el.className || el.tagName);
  }
  const g = document.querySelector('.grafico');
  if (g && g.clientHeight < 360) problemi.push('grafico troppo schiacciato: accorcia titolo o nota');
  // contenuto più alto del suo spazio: le righe finirebbero sopra la nota (nessun bordo superato, ma illeggibile)
  if (g && g.scrollHeight > g.clientHeight + 2) problemi.push('grafico più alto del suo spazio: elementi sovrapposti');
  // etichette su più righe nelle barre: si accavallerebbero con la riga sotto
  for (const e of document.querySelectorAll('.oriz .etichetta')) {
    if (e.scrollWidth > e.clientWidth + 1 && e.textContent.trim().length > 0) e.title = e.textContent;
  }
  return problemi;
}"""


def render(slides: list[tuple[str, dict]], nome: str, out_dir: Path = OUTPUT_DIR,
           salva_html: bool = False, formati: tuple[str, ...] = ("post", "reel"), video: bool = True) -> list[Path]:
    """Trasforma una lista di slide in JPEG, in ogni formato richiesto.

    out_dir è la cartella del giorno (output/AAAA/MM/GG): i file finiscono nelle sue sottocartelle.
    post: 1080×1350 -> post/nome_1.jpg, post/nome_2.jpg, ...
    reel: 1080×1920 -> reel/nome_reel_1.jpg, ...   video: video/nome_video.mp4
    Le slide sono le stesse (stessi dati, stessi testi): cambia solo la tela, quindi ogni
    rigenerazione dalla redazione aggiorna insieme post e reel.

    L'HTML intermedio vive in una cartella temporanea e sparisce a fine render
    (con salva_html=True resta accanto ai JPEG, utile per ritoccare il CSS).
    Se qualcosa esce dai bordi, solleva un errore invece di produrre una slide rotta.
    """
    out_dir = Path(out_dir).resolve()  # la cartella del giorno: i file vanno nelle sue sottocartelle
    for sotto in (POST, REEL, VIDEO):
        (out_dir / sotto).mkdir(parents=True, exist_ok=True)
    common = {
        "fonts": {
            "bricolage": font_path("BricolageGrotesque.ttf").as_uri(),
            "instrument": font_path("InstrumentSans.ttf").as_uri(),
            "mono": font_path("IBMPlexMono-Regular.ttf").as_uri(),
            "mono_medium": font_path("IBMPlexMono-Medium.ttf").as_uri(),
        },
        "c": {k: getattr(theme, k) for k in ("INCHIOSTRO", "NEBBIA", "ARANCIO", "BLU", "GRIGIO_BARRE", "GRIGIO_NOTA")},
    }
    paths = []
    with tempfile.TemporaryDirectory() as temporanea, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for formato in formati:
                f = FORMATI[formato]
                page = browser.new_page(viewport={"width": f["larghezza"], "height": f["altezza"]},
                                        device_scale_factor=1)
                for i, (template, dati) in enumerate(slides, start=1):
                    base = f"{nome}{f['suffisso']}_{i}"
                    html_path = (out_dir if salva_html else Path(temporanea)) / f"{base}.html"
                    html = _env.get_template(template).render(**common, **dati, formato=formato,
                                                              larghezza=f["larghezza"], altezza=f["altezza"])
                    html_path.write_text(html, encoding="utf-8")
                    page.goto(html_path.as_uri())
                    page.evaluate("document.fonts.ready.then(() => true)")
                    problemi = page.evaluate(_CHECK_JS, [f["larghezza"], f["altezza"]])
                    if problemi:
                        raise ValueError(f"Slide {i} ({template}, {formato}) non sta nei bordi: {sorted(set(problemi))}")
                    jpg = out_dir / (REEL if formato == "reel" else POST) / f"{base}.jpg"
                    page.screenshot(path=str(jpg), type="jpeg", quality=95)
                    paths.append(jpg)
                page.close()
            if "reel" in formati and video:
                if mp4 := _video(browser, slides, out_dir / VIDEO / f"{nome}_video.mp4", common, Path(temporanea)):
                    paths.append(mp4)
        finally:
            browser.close()
    return paths


log = logging.getLogger(__name__)
VIDEO_FPS, VIDEO_SECONDI = 30, 12  # intro 1 s · il grafico che cresce 6 s · la notizia 3 s · chiusura 2 s
VIDEO_FORME = ("linea", "area", "colonne", "orizzontale")


def _video(browser, slides: list, destinazione: Path, common: dict, temporanea: Path) -> Path | None:
    """Video verticale (1080×1920, MP4) del grafico che si costruisce periodo dopo periodo, per reel e TikTok.
    Usa gli stessi dati e testi della slide del grafico. None se il grafico non si anima (più linee, ciambella)
    o se manca imageio-ffmpeg."""
    grafico = next((d for t, d in slides if t == "grafico.html"), None)
    numero = next((d for t, d in slides if t == "copertina.html"), None)
    if not grafico or not numero or grafico.get("forma") not in VIDEO_FORME:
        return None
    try:
        import imageio_ffmpeg
    except ImportError:
        log.warning("Video non creato: manca imageio-ffmpeg (pip install imageio-ffmpeg)")
        return None
    in_copertina = grafico.get("copertina")  # con il grafico in copertina i due titoli sono scambiati
    # lo sfondo segue la copertina del post; l'arancione no, perché l'accento arancione sparirebbe
    tema = "chiara" if slides[0][1].get("tema") == "chiara" else "scura"
    html = _env.get_template("video.html").render(
        **common, formato="reel", larghezza=1080, altezza=1920, occhiello=grafico["occhiello"], tema=tema,
        titolo=grafico["titolo"] if in_copertina else numero["titolo"],
        finale=numero["titolo"] if in_copertina else grafico["titolo"], fonte=grafico["fonte"],
        indice="Valori indice" in (grafico.get("nota") or "") + (numero.get("didascalia") or ""),
        dati={"barre": grafico["barre"], "forma": grafico["forma"], "riferimento": grafico.get("riferimento")})
    pagina_html = temporanea / "video.html"
    pagina_html.write_text(html, encoding="utf-8")
    page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
    page.goto(pagina_html.as_uri())
    page.evaluate("document.fonts.ready.then(() => true)")
    # Qualità alta per reggere la ricompressione di Instagram e TikTok: fotogrammi senza perdita (PNG), colori nello
    # standard video (bt709, gamma "tv": senza, le app li leggono slavati), codifica pensata per la grafica piatta
    # (tune animation) a qualità quasi senza perdita, e una traccia audio muta per la massima compatibilità.
    # Si scrive in un file provvisorio e solo a video finito lo si mette al posto di quello vero (sostituzione
    # atomica): due rigenerazioni insieme non possono più mescolare i byte dello stesso file, e chi sta guardando il
    # video vede sempre quello vecchio o quello nuovo, mai un mezzo file.
    provvisorio = destinazione.with_name(f"{destinazione.stem}.{uuid.uuid4().hex[:8]}.tmp.mp4")
    comando = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
               "-f", "image2pipe", "-framerate", str(VIDEO_FPS), "-c:v", "png", "-i", "-",
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
               "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "12", "-profile:v", "high",
               "-level", "4.2", "-maxrate", "25M", "-bufsize", "50M", "-g", str(VIDEO_FPS * 2),
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(provvisorio)]
    ffmpeg = subprocess.Popen(comando, stdin=subprocess.PIPE)
    try:
        try:
            for f in range(VIDEO_FPS * VIDEO_SECONDI):
                page.evaluate(f"frame({f / VIDEO_FPS})")
                ffmpeg.stdin.write(page.screenshot(type="png"))  # senza perdita: niente sbavature prima della codifica
        finally:
            ffmpeg.stdin.close()
            ffmpeg.wait()
            page.close()
        if ffmpeg.returncode != 0:
            return None
        os.replace(provvisorio, destinazione)
        return destinazione
    finally:
        provvisorio.unlink(missing_ok=True)  # se è rimasto (errore a metà), non lascia file in giro
