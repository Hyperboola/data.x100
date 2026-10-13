"""Passo 4 · Le slide: template HTML (templates/) fotografati da Chromium headless.

Un ritocco grafico si fa nel CSS dei template, non qui.
Requisito una tantum: python -m playwright install chromium
"""
from __future__ import annotations

import math
import tempfile
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
def _etichetta(v: float, decimali: int, unita: str, forma: str) -> str:
    """Il valore sopra una colonna o un punto: sopra il milione in forma compatta ("139,6 mln"),
    altrimenti un numero a otto cifre diventa illeggibile nello spazio di una colonna."""
    if forma != "orizzontale" and abs(v) >= 1e6:
        for soglia, nome in ((1e9, "mld"), (1e6, "mln")):
            if abs(v) >= soglia:
                return f"{fmt_it(v / soglia, 1)} {nome}"
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

    barre = []
    for i, (etichetta, v) in enumerate(zip(etichette, valori)):
        ok = _valid(v)
        barre.append(
            {
                "etichetta": etichetta if (forma == "orizzontale" or i % passo == 0) else "",
                "testo": (testi_valori[i] if testi_valori else _etichetta(v, decimali, unita, forma)) if ok else "",
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
           salva_html: bool = False, formati: tuple[str, ...] = ("post", "reel")) -> list[Path]:
    """Trasforma una lista di slide in JPEG, in ogni formato richiesto.

    post: 1080×1440 -> nome_1.jpg, nome_2.jpg, ...
    reel: 1080×1920 -> nome_reel_1.jpg, nome_reel_2.jpg, ...
    Le slide sono le stesse (stessi dati, stessi testi): cambia solo la tela, quindi ogni
    rigenerazione dalla redazione aggiorna insieme post e reel.

    L'HTML intermedio vive in una cartella temporanea e sparisce a fine render
    (con salva_html=True resta accanto ai JPEG, utile per ritoccare il CSS).
    Se qualcosa esce dai bordi, solleva un errore invece di produrre una slide rotta.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
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
                    jpg = out_dir / f"{base}.jpg"
                    page.screenshot(path=str(jpg), type="jpeg", quality=95)
                    paths.append(jpg)
                page.close()
        finally:
            browser.close()
    return paths
