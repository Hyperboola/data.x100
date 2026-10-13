"""La redazione: rivedi, scrivi e approva nel browser i post preparati, prima di pubblicarli.

    python -m pipeline.redazione               # su http://localhost:8765, dal primo giorno da fare
    python -m pipeline.redazione 2026-10-12    # parte da un giorno preciso

Per ogni post:
  - correggi le parole (numeri e grafici restano quelli calcolati dal codice) e scegli il grafico;
  - "Salva e rigenera" rifà slide e didascalia con i dati salvati insieme al post: niente Claude, niente ISTAT;
  - "Suggerisci" (facoltativo) chiede a Claude, con il modello leggero del calendario, un testo per un campo;
  - "Cambia serie" sceglie un altro indicatore o un altro dettaglio dello stesso dataset (scarica i dati nuovi);
  - "Approva" segna il post come pronto da pubblicare, ma solo se passa i controlli: unità di misura
    presente quando serve, niente inglese o codici ISTAT nei testi, niente numeri che non sono nei dati.
Ogni correzione resta nel rapporto con il testo prima e dopo. La pagina è visibile solo da questo computer.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import logging
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pandas as pd

from pipeline.calendario import carica_calendario
from pipeline.claude import chiedi, prompt
from pipeline.disegna import OUTPUT_DIR, render
from pipeline.racconta import GRAFICI, GRUPPI, STILI, _completezza, costruisci_carosello, numeri, pulisci
from pipeline.run import STORICO, scarica
from pipeline.scegli import _soggetto_unita, filtri_ricognizione, serie_fresche
from sources.istat import Istat, slug_filtri

PORTA = 8765
ETICHETTE = {"domanda": "Copertina · domanda", "didascalia_copertina": "Copertina · didascalia",
             "titolo_grafico": "Grafico · titolo", "nota_grafico": "Grafico · nota",
             "punto_1": "Chiusura · primo punto", "punto_2": "Chiusura · secondo punto",
             "invito": "Chiusura · invito", "post": "Didascalia di Instagram"}
LIMITI = {campo: n for g in GRUPPI.values() for campo, n in g.items()}
# campi della serie, modificabili anche loro: compaiono nelle slide
ETICHETTE_SERIE = {"occhiello": "Occhiello (in alto nelle slide)", "unita": "Unità di misura (es. %, €, anni, per 100 famiglie)",
                   "fonte_testo": "Fonte (in fondo alle slide)"}
NOMI_STILI = {"numero_scuro": "Numero · sfondo scuro", "grafico_chiaro": "Grafico · sfondo chiaro",
              "numero_arancio": "Numero · sfondo arancione", "numero_chiaro": "Numero · sfondo chiaro",
              "grafico_scuro": "Grafico · sfondo scuro"}
NOMI_GRAFICI = {"colonne": "Colonne", "linea": "Linea", "area": "Area", "barre": "Barre (classifica)",
                "ciambella": "Ciambella (parti di un totale)", "linee": "Più linee", "indice": "Linee in indice (base 100)",
                "variazioni": "Barre delle variazioni"}
# parole inglesi e sigle che non devono finire in un post
INGLESE = (" the ", " of ", " and ", "house", "price", "household", "population", "characteristic", "employment",
           "rate", "total", "births", "deaths")
SIGLE_OK = {"ISTAT", "PIL", "UE", "IPCA", "NIC", "FOI", "IRPEF", "INPS", "ISEE", "IVA", "CC", "BY", "IT"}

log = logging.getLogger(__name__)


# --- File dei post ----------------------------------------------------------------------------------
def giorni_disponibili() -> list[str]:
    return sorted(d.name for d in OUTPUT_DIR.glob("????-??-??") if any(d.glob("*_rapporto.json")))


def post_del_giorno(giorno: str) -> list[Path]:
    return sorted((OUTPUT_DIR / giorno).glob("*_rapporto.json"))


def _leggi(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _scrivi(path: Path, dati: dict) -> None:
    path.write_text(json.dumps(dati, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _file(path: Path, suffisso: str) -> Path:
    return path.with_name(path.name.replace("_rapporto.json", suffisso))


def numeri_sconosciuti(rap: dict, testi: dict) -> list[str]:
    """Numeri scritti a mano che non compaiono nei dati: un avviso, non un blocco."""
    noti = set()
    for voce in rap.get("menu", {}).values():
        noti |= numeri(voce["testo"]) | numeri(voce["descrizione"])
    noti |= numeri(rap.get("dati") or "") | numeri(json.dumps(rap.get("serie", {}), ensure_ascii=False))
    return [f"{campo}: {', '.join(sorted(extra))}" for campo, t in testi.items() if (extra := numeri(t) - noti)]


def problemi_di_pubblicazione(rap: dict) -> list[str]:
    """Cosa impedisce di approvare il post: unità mancanti, inglese o codici nei testi, numeri inventati."""
    s, testi, problemi = rap["serie"], rap["testi"], []
    con_decimali = any("," in v["testo"] for v in rap.get("menu", {}).values())
    if con_decimali and not (s.get("unita") or "").strip():
        problemi.append("Manca l'unità di misura: i valori hanno decimali, quindi non sono un semplice conteggio. "
                        "Indicala (es. %, €, anni, per 100 famiglie).")
    visibili = {"occhiello": s.get("occhiello", ""), "fonte": s.get("fonte_testo", ""), **testi}
    for campo, testo in visibili.items():
        minuscolo = f" {testo.lower()} "
        if inglesi := [p.strip() for p in INGLESE if p in minuscolo]:
            problemi.append(f"{campo}: sembra inglese ({', '.join(inglesi)}). Traducilo.")
        sigle = [t for t in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b|\b\w+_\w+\b", testo) if t not in SIGLE_OK]
        if sigle:
            problemi.append(f"{campo}: contiene codici ({', '.join(sigle)}). Sostituiscili con parole.")
    for avviso in numeri_sconosciuti(rap, testi):
        problemi.append(f"Numeri che non compaiono nei dati, {avviso}.")
    return problemi


# --- Azioni -----------------------------------------------------------------------------------------
def _ricostruisci(path: Path, s: dict, df: pd.DataFrame, nomi: dict, imposto: dict | None) -> dict:
    """Rifà slide, didascalia, dati e rapporto di un post; restituisce il rapporto nuovo."""
    cal = carica_calendario()
    # _post_esistente: le regole per scegliere un dataset nuovo non valgono su un post già fatto
    slides, testo_ig, nuovo = costruisci_carosello({**s, "_post_esistente": True}, df, cal["finestra"][s["frequenza"]],
                                                   nomi=nomi, usa_claude=False, imposto=imposto)
    nome = path.name.removesuffix("_rapporto.json")
    render(slides, nome=nome, out_dir=path.parent)
    didascalia = _file(path, "_didascalia.txt")
    vecchia = didascalia.read_text(encoding="utf-8") if didascalia.exists() else ""
    didascalia.write_text(testo_ig, encoding="utf-8")
    _scrivi(_file(path, "_dati.json"), {"righe": df.to_dict("records"), "nomi": nomi})
    riepilogo = path.parent / "riepilogo.md"
    if riepilogo.exists() and vecchia:  # il riepilogo resta allineato
        riepilogo.write_text(riepilogo.read_text(encoding="utf-8").replace(vecchia, testo_ig), encoding="utf-8")
    return nuovo


def _dati_del_post(path: Path, s: dict) -> tuple[pd.DataFrame, dict]:
    salvati = _file(path, "_dati.json")
    if salvati.exists():  # gli stessi dati del post: i numeri non possono cambiare sotto i testi
        dati = _leggi(salvati)
        df = pd.DataFrame(dati["righe"])
        df["time_period"] = df["time_period"].astype(str)
        return df, dati["nomi"]
    return scarica(s, carica_calendario(), forza=True)  # post creati prima di questa versione


def _aggiorna_storico(path: Path, vecchio_id: str, **campi) -> None:
    if not STORICO.exists():
        return
    voci = json.loads(STORICO.read_text())
    for v in voci:
        if v.get("serie") == vecchio_id and v.get("data") == path.parent.name:
            v.update(campi)
    STORICO.write_text(json.dumps(voci, ensure_ascii=False, indent=2))


def rigenera(path: Path, testi_nuovi: dict, grafico: str | None = None, serie_nuova: dict | None = None,
             stile: str | None = None) -> None:
    """Rifà il post con i testi corretti (e, se indicati, un altro grafico, occhiello, unità o fonte)."""
    vecchio = _leggi(path)
    s = {**vecchio["serie"], **{k: v.strip() for k, v in (serie_nuova or {}).items() if k in ETICHETTE_SERIE}}
    df, nomi = _dati_del_post(path, s)
    testi = {**vecchio["testi"], **{k: v.strip() for k, v in testi_nuovi.items() if k in ETICHETTE}}
    scelte = dict(vecchio["scelte"])
    if grafico and grafico in vecchio.get("grafici_ammessi", [grafico]):
        scelte["grafico"] = grafico
    if stile in STILI:
        scelte["stile"] = stile
    nuovo = _ricostruisci(path, s, df, nomi, {"scelte": scelte, "testi": testi})
    correzioni = [{"campo": k, "prima": vecchio["testi"].get(k, ""), "dopo": testi[k]}
                  for k in ETICHETTE if testi.get(k) != vecchio["testi"].get(k)]
    if scelte["grafico"] != vecchio["scelte"]["grafico"]:
        correzioni.append({"campo": "grafico", "prima": vecchio["scelte"]["grafico"], "dopo": scelte["grafico"]})
    correzioni += [{"campo": k, "prima": vecchio["serie"].get(k, ""), "dopo": s[k]}
                   for k in ETICHETTE_SERIE if s.get(k) != vecchio["serie"].get(k)]
    nuovo.update(serie=s, claude=vecchio.get("claude"), da_controllare=vecchio.get("da_controllare") or [],
                 approvato=None,  # dopo una modifica va riapprovato
                 redazione=[*vecchio.get("redazione", []),
                            {"quando": datetime.now().isoformat(timespec="seconds"), "correzioni": correzioni}])
    _scrivi(path, nuovo)
    _aggiorna_storico(path, s["id"], domanda=testi["domanda"], titolo=testi["titolo_grafico"],
                      grafico=scelte["grafico"], stile=scelte.get("stile"))
    log.info("Rigenerato %s (%s)", path.name, ", ".join(c["campo"] for c in correzioni) or "nessuna modifica")


def opzioni_serie(path: Path) -> list[dict]:
    """Le dimensioni del dataset con i codici disponibili (serie fresche), per i menu a tendina."""
    rap = _leggi(path)
    s, cal, istat = rap["serie"], carica_calendario(), Istat()
    codici, _ = istat.struttura(s["flow"])
    fresche = serie_fresche(istat.serie_disponibili(s["flow"], filtri_ricognizione(codici, s["frequenza"])),
                            s["frequenza"], cal["freschezza"][s["frequenza"]])
    menu = []
    for (_, dim, nome), g in codici.groupby(["position", "dimension", "dimension_name"]):
        if dim in ("FREQ", s.get("variabile")) or dim.lower() not in fresche.columns:
            continue
        presenti = set(fresche[dim.lower()])
        scelte = [(c, n) for c, n in zip(g["code"], g["name"]) if c in presenti]
        if len(scelte) > 1:
            menu.append({"dim": dim, "nome": nome, "codici": scelte[:200], "attuale": s["filtri"].get(dim)})
    return menu


def cambia_serie(path: Path, nuovi: dict) -> None:
    """Stesso dataset, altri codici: scarica i dati nuovi e riparte dai testi a modello."""
    vecchio = _leggi(path)
    s = dict(vecchio["serie"])
    filtri = {**s["filtri"], **{k: v for k, v in nuovi.items() if v}}
    if filtri == s["filtri"]:
        return
    codici, _ = Istat().struttura(s["flow"])
    titolo = s.get("fonte_testo", "").removeprefix("ISTAT, ")
    soggetto, unita = _soggetto_unita(codici, filtri, titolo)
    s.update(filtri=filtri, id=f"{s['flow']}|{slug_filtri(filtri)}", soggetto=soggetto, unita=unita, decimali=None,
             domanda=f"{soggetto[:1].upper()}{soggetto[1:]}: com'è cambiato?")
    cal = carica_calendario()
    df, nomi = scarica(s, cal, forza=True)
    nuovo = _ricostruisci(path, s, df, nomi, None)  # testi a modello: i numeri sono cambiati
    nuovo.update(serie=s, approvato=None, da_controllare=[],
                 redazione=[*vecchio.get("redazione", []), {"quando": datetime.now().isoformat(timespec="seconds"),
                                                            "cambio_serie": {"prima": vecchio["serie"]["filtri"],
                                                                             "dopo": filtri}}])
    _scrivi(path, nuovo)
    sc = nuovo["scelte"]
    _aggiorna_storico(path, vecchio["serie"]["id"], serie=s["id"], filtri=filtri, periodo=df["time_period"].max(),
                      copertina=sc["copertina"], numero=nuovo["menu"][sc["copertina"]]["copertina"],
                      domanda=nuovo["testi"]["domanda"], titolo=nuovo["testi"]["titolo_grafico"])
    log.info("Cambiata la serie di %s: %s", path.name, filtri)


def approva(path: Path) -> None:
    """Segna il post come pronto da pubblicare (nel rapporto e nel riepilogo)."""
    rap = _leggi(path)
    rap["approvato"] = datetime.now().isoformat(timespec="seconds")
    _scrivi(path, rap)
    riepilogo = path.parent / "riepilogo.md"
    if riepilogo.exists():
        testo, segno = riepilogo.read_text(encoding="utf-8"), f"**{rap['testi']['domanda']}**"
        if segno in testo and f"✅ {segno}" not in testo:
            riepilogo.write_text(testo.replace(segno, f"✅ {segno}", 1), encoding="utf-8")
    log.info("Approvato %s", path.name)


def suggerisci(path: Path, campo: str, testi: dict) -> tuple[str, list[str]]:
    """Un testo per un campo, scritto da Claude con il modello leggero. Restituisce (testo, avvisi)."""
    rap = _leggi(path)
    r = rap["serie"]
    risposta = chiedi(prompt(
        "suggerisci", campo=campo, etichetta=ETICHETTE[campo], limite=LIMITI[campo],
        indicatore=r.get("fonte_testo", "ISTAT"), completezza=_completezza(r), dati=rap.get("dati") or "(vedi i fatti)",
        menu="\n".join(f'- {v["descrizione"]} → {v["testo"]}' for v in rap.get("menu", {}).values()),
        testi=json.dumps({k: v for k, v in testi.items() if k != campo}, ensure_ascii=False, indent=1)))
    testo = pulisci(str(risposta.get("testo", "")))
    avvisi = numeri_sconosciuti(rap, {campo: testo})
    if len(testo) > LIMITI[campo]:
        avvisi.append(f"{len(testo)} caratteri su {LIMITI[campo]}: accorcialo")
    return testo, avvisi


# --- Le pagine --------------------------------------------------------------------------------------
STILE = """
body { font-family: -apple-system, system-ui, sans-serif; background: #EEF1F4; color: #111418; margin: 0; padding: 32px; }
h1 { font-size: 28px; margin: 0 0 12px; } h1 span { color: #F2542D; }
nav { margin-bottom: 24px; display: flex; flex-wrap: wrap; gap: 8px; }
nav a { text-decoration: none; color: #111418; background: white; padding: 6px 12px; border-radius: 999px; font-size: 14px; }
nav a.qui { background: #111418; color: white; } nav a .n { color: #5A6170; } nav a.tutti::after { content: " ✅"; }
.post { background: white; border-radius: 16px; padding: 24px; margin-bottom: 32px; }
.post h2 { margin: 0 0 4px; font-size: 20px; } .meta { color: #5A6170; font-size: 14px; margin-bottom: 16px; }
.slide { display: flex; gap: 12px; margin-bottom: 20px; } .slide img { width: 32%; border-radius: 8px; }
.formato { font-size: 13px; color: #5A6170; margin: 4px 0 6px; }
.slide.reel img { width: 24%; }
.avviso { background: #FFF1EC; border-left: 4px solid #F2542D; padding: 10px 14px; margin: 8px 0; border-radius: 6px; }
.ok { background: #EAF6EE; border-left: 4px solid #2E8B57; padding: 10px 14px; margin: 8px 0; border-radius: 6px; }
label { display: flex; align-items: baseline; gap: 10px; font-weight: 600; font-size: 14px; margin: 14px 0 4px; }
label small { font-weight: 400; color: #5A6170; } small.troppo { color: #F2542D; font-weight: 600; }
.sugg { margin-left: auto; font-size: 12px; background: none; color: #2A4D9B; border: 1px solid #2A4D9B; padding: 3px 10px; margin-top: 0; }
.nota { font-size: 13px; color: #5A6170; margin-top: 4px; } .nota.male { color: #F2542D; }
input { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 16px; padding: 10px; border: 1px solid #C9D0D8; border-radius: 8px; }
textarea, select { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 16px; line-height: 1.4; padding: 10px; border: 1px solid #C9D0D8; border-radius: 8px; }
select { width: auto; }
button { margin-top: 18px; background: #111418; color: white; border: 0; padding: 12px 22px; border-radius: 999px; font-size: 15px; cursor: pointer; }
button.approva { background: #2E8B57; margin-left: 8px; } a.serie { margin-left: 12px; font-size: 14px; color: #2A4D9B; }
.stato { display: inline-block; font-size: 13px; padding: 3px 10px; border-radius: 999px; margin-left: 8px; vertical-align: middle; }
.stato.si { background: #EAF6EE; color: #2E8B57; } .stato.no { background: #EEF1F4; color: #5A6170; }
"""
SCRIPT = """
document.querySelectorAll('textarea').forEach(t => {
  const c = document.querySelector('label[for="' + t.id + '"] small');
  const agg = () => { c.textContent = t.value.length + '/' + t.dataset.max; c.className = t.value.length > +t.dataset.max ? 'troppo' : ''; };
  t.addEventListener('input', agg); agg();
});
document.querySelectorAll('button.sugg').forEach(b => b.addEventListener('click', async () => {
  const form = b.closest('form'), campo = b.dataset.campo, nota = form.querySelector('.nota-' + campo);
  b.disabled = true; nota.className = 'nota nota-' + campo; nota.textContent = 'Claude sta scrivendo…';
  const dati = new URLSearchParams(new FormData(form)); dati.set('campo', campo);
  try {
    const r = await (await fetch('/suggerisci', {method: 'POST', body: dati})).json();
    if (r.errore) { nota.textContent = 'Errore: ' + r.errore; nota.className += ' male'; }
    else {
      const t = form.querySelector('textarea[name="' + campo + '"]'); t.value = r.testo; t.dispatchEvent(new Event('input'));
      nota.textContent = r.avvisi.length ? '⚠ ' + r.avvisi.join(' · ') : 'Suggerimento inserito: controllalo, poi "Salva e rigenera".';
      if (r.avvisi.length) nota.className += ' male';
    }
  } catch (e) { nota.textContent = 'Errore: ' + e; nota.className += ' male'; }
  b.disabled = false;
}));
"""


def _pagina(titolo: str, corpo: str) -> str:
    return (f'<!doctype html><html lang="it"><head><meta charset="utf-8"><title>{titolo}</title>'
            f'<style>{STILE}</style></head><body>{corpo}<script>{SCRIPT}</script></body></html>')


def _navigazione(giorno: str) -> str:
    voci = []
    for g in giorni_disponibili():
        posts = post_del_giorno(g)
        fatti = sum(1 for p in posts if _leggi(p).get("approvato"))
        nome = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"][date.fromisoformat(g).weekday()]
        classe = " ".join(c for c in ("qui" if g == giorno else "", "tutti" if fatti == len(posts) else "") if c)
        voci.append(f'<a class="{classe}" href="/?giorno={g}">{nome} {g[8:]}/{g[5:7]} '
                    f'<span class="n">{fatti}/{len(posts)}</span></a>')
    return f'<nav>{"".join(voci)}</nav>'


def filtri_leggibili(s: dict) -> str:
    """'Paese di residenza: Italia · Tipo di esercizio: totale…' per vedere subito cosa misura la serie."""
    try:
        codici, _ = Istat().struttura(s["flow"])  # dalla cache: nessuna richiesta a ISTAT
    except Exception:
        return ""
    nomi = {(d, c): n for d, c, n in codici[["dimension", "code", "name"]].itertuples(index=False)}
    dim = dict(codici[["dimension", "dimension_name"]].drop_duplicates().itertuples(index=False))
    parti = []
    for d, v in s.get("filtri", {}).items():
        if d == "FREQ":
            continue
        valori = v if isinstance(v, list) else [v]
        testo = f"{len(valori)} voci" if len(valori) > 3 else ", ".join(nomi.get((d, c), c) for c in valori)
        parti.append(f"<b>{html.escape(dim.get(d, d))}</b>: {html.escape(testo)}")
    return " · ".join(parti)


def pagina(giorno: str, messaggi: dict[str, str]) -> str:
    blocchi = []
    for path in post_del_giorno(giorno):
        rap = _leggi(path)
        nome = path.name.removesuffix("_rapporto.json")
        ver = int(path.stat().st_mtime)
        slide = "".join(f'<img src="/img/{giorno}/{nome}_{i}.jpg?v={ver}">' for i in (1, 2, 3))
        if (path.parent / f"{nome}_reel_1.jpg").exists():
            reel = "".join(f'<img src="/img/{giorno}/{nome}_reel_{i}.jpg?v={ver}">' for i in (1, 2, 3))
            reel = f'<div class="formato">Reel 9:16</div><div class="slide reel">{reel}</div>'
        else:  # post creato prima dei reel: si generano al primo "Salva e rigenera"
            reel = '<div class="formato">Reel 9:16: premi "Salva e rigenera" per crearli.</div>'
        slide = f'<div class="formato">Post 3:4</div><div class="slide">{slide}</div>{reel}'
        avvisi = "".join(f'<div class="avviso">⚠ {html.escape(a)}</div>' for a in rap.get("da_controllare") or [])
        if rap.get("origine") and set(rap["origine"].values()) == {"modello"}:
            avvisi += '<div class="avviso">✍ Testi a modello: scrivili tu prima di approvare.</div>'
        avvisi += messaggi.get(nome, "")
        campi = "".join(
            f'<label for="{nome}-{c}">{e} <small></small>'
            f'<button type="button" class="sugg" data-campo="{c}">Suggerisci</button></label>'
            f'<textarea id="{nome}-{c}" name="{c}" data-max="{LIMITI[c]}" rows="{6 if c == "post" else 2}">'
            f'{html.escape(rap["testi"].get(c, ""))}</textarea><div class="nota nota-{c}"></div>'
            for c, e in ETICHETTE.items())
        ammessi = rap.get("grafici_ammessi") or [rap["scelte"]["grafico"]]
        grafico = "".join(f'<option value="{g}"{" selected" if g == rap["scelte"]["grafico"] else ""}>'
                          f'{NOMI_GRAFICI.get(g, g)}</option>' for g in ammessi)
        stile = "".join(f'<option value="{k}"{" selected" if k == rap["scelte"].get("stile", "numero_scuro") else ""}>'
                        f'{NOMI_STILI[k]}</option>' for k in STILI)
        campi_serie = "".join(
            f'<label for="{nome}-{c}">{e}</label><input id="{nome}-{c}" name="{c}" '
            f'value="{html.escape(str(rap["serie"].get(c) or ""))}">' for c, e in ETICHETTE_SERIE.items())
        s = rap["serie"]
        stato = (f'<span class="stato si">✅ approvato {rap["approvato"][11:16]}</span>' if rap.get("approvato")
                 else '<span class="stato no">da approvare</span>')
        cambia = f'/serie?{urlencode({"giorno": giorno, "file": path.name})}'
        blocchi.append(
            f'<div class="post" id="{nome}"><h2>{html.escape(rap["testi"]["domanda"])}{stato}</h2>'
            f'<div class="meta">{s["frequenza"]} · {s["sottotema"]} · {html.escape(s.get("fonte_testo", ""))}</div>'
            f'<div class="meta">Cosa misura la serie: {filtri_leggibili(s) or "(struttura non in cache)"}</div>'
            f'{avvisi}{slide}'
            f'<form method="post" action="/salva"><input type="hidden" name="file" value="{path.name}">'
            f'<input type="hidden" name="giorno" value="{giorno}">'
            f'<label>Copertina</label><select name="stile">{stile}</select>'
            f'<label>Grafico</label><select name="grafico">{grafico}</select>{campi_serie}{campi}'
            + (f'<label class="verifica"><input type="checkbox" name="verificato" value="1" style="width:auto"> '
               f'Ho verificato gli avvisi ⚠ sui dati e il post è corretto</label>' if rap.get("da_controllare") else "")
            + f'<button name="azione" value="salva">Salva e rigenera</button>'
            f'<button class="approva" name="azione" value="approva">Approva</button>'
            f'<a class="serie" href="{cambia}">Cambia serie…</a></form></div>')
    corpo = "".join(blocchi) or "<p>Nessun post per questo giorno.</p>"
    return _pagina("Redazione Data%", f'<h1>Redazione Data<span>%</span> · {giorno}</h1>{_navigazione(giorno)}{corpo}')


def pagina_serie(giorno: str, file: str, errore: str = "") -> str:
    path = OUTPUT_DIR / giorno / Path(file).name
    rap = _leggi(path)
    try:
        opzioni = opzioni_serie(path)
    except Exception as exc:
        return _pagina("Cambia serie", f'<div class="avviso">ISTAT non risponde: {html.escape(str(exc))}</div>'
                                       f'<a href="/?giorno={giorno}">← torna alla redazione</a>')
    menu = "".join(
        f'<label>{html.escape(o["nome"])} <small>{o["dim"]}</small></label><select name="{o["dim"]}">'
        + "".join(f'<option value="{html.escape(c)}"{" selected" if c == o["attuale"] else ""}>'
                  f'{html.escape(n)}</option>' for c, n in o["codici"]) + "</select>" for o in opzioni)
    corpo = (f'<h1>Cambia serie</h1><div class="meta">{html.escape(rap["serie"].get("fonte_testo", ""))}</div>'
             f'{f"<div class=avviso>{html.escape(errore)}</div>" if errore else ""}'
             f'<div class="post"><p>Stesso dataset, altri codici. I testi ripartiranno da quelli a modello, '
             f'perché i numeri cambiano.</p><form method="post" action="/cambia">'
             f'<input type="hidden" name="file" value="{path.name}"><input type="hidden" name="giorno" value="{giorno}">'
             f'{menu or "<p>Questo dataset non ha altre serie fresche da scegliere.</p>"}'
             f'<button>Applica e rigenera</button> <a class="serie" href="/?giorno={giorno}">annulla</a></form></div>')
    return _pagina("Cambia serie", corpo)


# --- Il server ------------------------------------------------------------------------------------
def avvia(giorno: str, porta: int = PORTA) -> None:
    messaggi: dict[str, str] = {}

    class Gestore(BaseHTTPRequestHandler):
        def log_message(self, *args):  # niente rumore nel terminale
            pass

        def _invia(self, codice: int, corpo: bytes, tipo: str, extra: dict | None = None) -> None:
            self.send_response(codice)
            self.send_header("Content-Type", tipo)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def _html(self, testo: str) -> None:
            self._invia(200, testo.encode(), "text/html; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path.startswith("/img/"):
                _, _, g, file = url.path.split("/", 3)
                f = OUTPUT_DIR / Path(g).name / Path(unquote(file)).name  # solo file di output
                if f.suffix == ".jpg" and f.exists():
                    return self._invia(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "no-store"})
                return self._invia(404, b"", "text/plain")
            if url.path == "/serie":
                return self._html(pagina_serie(q.get("giorno", giorno), q.get("file", "")))
            self._html(pagina(Path(q.get("giorno", giorno)).name, messaggi))

        def do_POST(self):
            dati = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(),
                                                   keep_blank_values=True).items()}
            g = Path(dati.get("giorno", giorno)).name
            path = OUTPUT_DIR / g / Path(dati.get("file", "")).name
            nome = path.name.removesuffix("_rapporto.json")
            torna = {"Location": f"/?giorno={g}#{nome}"}
            if self.path == "/suggerisci":
                try:
                    testo, avvisi = suggerisci(path, dati["campo"], {k: dati.get(k, "") for k in ETICHETTE})
                    risposta = {"testo": testo, "avvisi": avvisi}
                except Exception as exc:
                    risposta = {"errore": str(exc)}
                return self._invia(200, json.dumps(risposta).encode(), "application/json")
            if self.path == "/cambia":
                try:
                    cambia_serie(path, {k: v for k, v in dati.items() if k not in ("file", "giorno")})
                    messaggi[nome] = '<div class="ok">✓ Serie cambiata: ora scrivi i testi.</div>'
                except Exception as exc:
                    log.exception("Cambio di serie non riuscito")
                    return self._html(pagina_serie(g, path.name, f"Non riuscito: {exc}"))
                return self._invia(303, b"", "text/plain", torna)
            testi = {k: dati.get(k, "") for k in ETICHETTE}
            try:
                serie_nuova = {k: dati.get(k, "") for k in ETICHETTE_SERIE}
                if dati.get("azione") == "approva":
                    rap = _leggi(path)
                    if any(testi[k].strip() != rap["testi"].get(k, "").strip() for k in ETICHETTE) \
                            or any(v.strip() != str(rap["serie"].get(k) or "").strip() for k, v in serie_nuova.items()) \
                            or dati.get("grafico", rap["scelte"]["grafico"]) != rap["scelte"]["grafico"]:
                        messaggi[nome] = '<div class="avviso">Hai modifiche non salvate: premi prima "Salva e rigenera".</div>'
                    elif rap.get("da_controllare") and not dati.get("verificato"):
                        messaggi[nome] = ('<div class="avviso"><b>Ci sono avvisi ⚠ sui dati.</b> Verificali (per esempio '
                                          'sul sito ISTAT), correggi il post se serve, poi spunta "Ho verificato" e approva.</div>')
                    elif problemi := problemi_di_pubblicazione(rap):
                        messaggi[nome] = '<div class="avviso"><b>Non ancora pubblicabile:</b><ul>' + "".join(
                            f"<li>{html.escape(p)}</li>" for p in problemi) + "</ul></div>"
                    else:
                        approva(path)
                        messaggi[nome] = '<div class="ok">✅ Post approvato.</div>'
                else:
                    rigenera(path, testi, dati.get("grafico"), serie_nuova, dati.get("stile"))
                    avvisi = numeri_sconosciuti(_leggi(path), testi)
                    messaggi[nome] = '<div class="ok">✓ Slide rigenerate.</div>' + "".join(
                        f'<div class="avviso">Numeri che non compaiono nei dati: {html.escape(a)}</div>' for a in avvisi)
            except Exception as exc:
                log.exception("Operazione non riuscita")
                messaggi[nome] = f'<div class="avviso">Errore: {html.escape(str(exc))}</div>'
            self._invia(303, b"", "text/plain", torna)

    server = ThreadingHTTPServer(("127.0.0.1", porta), Gestore)
    indirizzo = f"http://localhost:{porta}/?giorno={giorno}"
    print(f"Redazione aperta su {indirizzo} (Ctrl+C per chiudere)")
    webbrowser.open(indirizzo)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRedazione chiusa.")


def primo_giorno() -> str:
    """Il primo giorno, da oggi in poi, con post ancora da approvare (altrimenti oggi)."""
    oggi = date.today().isoformat()
    for g in giorni_disponibili():
        if g >= oggi and any(not _leggi(p).get("approvato") for p in post_del_giorno(g)):
            return g
    return oggi


def main() -> None:
    parser = argparse.ArgumentParser(description="Rivedi, scrivi e approva i post preparati")
    parser.add_argument("giorno", nargs="?", help="AAAA-MM-GG (default: il primo giorno da fare)")
    parser.add_argument("--porta", type=int, default=PORTA)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    avvia(args.giorno or primo_giorno(), args.porta)


if __name__ == "__main__":
    main()
