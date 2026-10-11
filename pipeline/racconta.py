"""Passo 3 · Dai numeri alla storia.

Tre tipi di storia, ognuno con i suoi grafici:
  - serie           una serie nel tempo              → colonne, linea, area
  - confronto       più categorie, ultimo periodo    → barre (classifica), ciambella (parti di un totale)
  - serie_multiple  2-4 categorie nel tempo          → linee

Divisione dei compiti:
  - il CODICE calcola tutti i numeri e li mette in un menù, ognuno con un codice;
  - CLAUDE sceglie dal menù cosa mostrare, quale grafico usare e scrive le parole;
  - ogni testo viene controllato: lunghezza e numeri (solo quelli del menù).
La storia di Claude si accetta tutta o per niente; se non passa i controlli nemmeno al
secondo tentativo, il carosello usa le scelte e le frasi a modello (il paracadute).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from pipeline.claude import chiedi, prompt
from pipeline.disegna import chiusura, copertina, fmt_it, grafico, grafico_ciambella, grafico_linee
from pipeline.periodi import Periodo

log = logging.getLogger(__name__)

INVITO = "Ti aspettavi questo numero? Giralo a chi pensa di conoscere la risposta."
HASHTAG = "#dati #statistiche #italia #istat"
LICENZA = "Dati con licenza CC BY 4.0 (creativecommons.org/licenses/by/4.0)"
TOTALI = {"IT", "ITALIA", "9", "T", "TOTAL", "TOT", "99", "ALL"}

GRAFICI = {
    "colonne": "colonne verticali: confronta pochi periodi tra loro",
    "linea": "linea: andamento lungo e continuo, o differenze piccole rispetto al totale",
    "area": "area: come la linea, ma dà peso alla quantità (popolazioni, volumi, totali)",
    "barre": "barre orizzontali ordinate: una classifica tra categorie",
    "ciambella": "ciambella: come si divide un totale tra poche parti",
    "linee": "più linee con i valori reali: chi è più grande e come cambia (attenzione: con gruppi di "
             "dimensioni diverse le pendenze ingannano, non usarle per confrontare variazioni percentuali)",
    "indice": "più linee in indice (primo anno = 100): tutte partono dallo stesso punto, così si vede chi "
              "cresce o cala di più in proporzione. Il grafico giusto per confrontare variazioni tra gruppi",
    "variazioni": "barre orizzontali con la variazione percentuale di ogni gruppo dal primo all'ultimo anno: "
                  "il modo più diretto per dire chi è cambiato di più",
}


@dataclass
class Storia:
    """Tutto ciò che serve per raccontare e disegnare: menù, elementi, scelte e testi di riserva."""
    tipo: str
    menu: dict
    elementi: dict[str, str]          # codice -> nome: ciò che si può evidenziare (periodi o categorie)
    grafici: tuple[str, ...]          # tipi di grafico ammessi
    scelte: dict                      # scelte a modello
    testi: dict                       # testi a modello
    tempo: str                        # guida sui tempi verbali
    occhiello_grafico: str
    disegna: Callable[[dict, dict, str], tuple] = field(repr=False)  # (scelte, testi, fonte) -> slide
    extra: dict = field(default_factory=dict)


# --- Formattazione ------------------------------------------------------------------------
def _var(da: float, a: float) -> float | None:
    return None if da == 0 else (a - da) / da * 100


def valore(v: float, decimali: int = 0, unita: str = "") -> str:
    return f"{fmt_it(v, decimali)}{'' if unita in ('', '%') else ' '}{unita}"


def compatto(v: float, decimali: int = 0, unita: str = "") -> str:
    """Per i numeri grandi: 1.040.000.000 -> '1,04 mld'."""
    for soglia, nome in ((1e9, "mld"), (1e6, "mln")):
        if abs(v) >= soglia:
            return f"{fmt_it(v / soglia, 2)} {nome}{' ' + unita if unita else ''}"
    return valore(v, decimali, unita)


def variazione(p: float) -> str:
    """+63% · −4,4% · +0,3%: un decimale sotto il 10%, così le piccole variazioni non diventano 0%."""
    return f"{'+' if p > 0 else ''}{fmt_it(p, 1 if abs(p) < 10 else 0)}%"


def _maiuscola(testo: str) -> str:
    return testo[:1].upper() + testo[1:]


UNITA_CORTE = {"", "%", "€", "anni", "mesi", "giorni", "ore", "km", "kg", "t", "°C"}


def _corta(unita: str) -> str:
    """L'unità da scrivere accanto ai numeri dei grafici: solo se breve ("%", "€", "anni", "nati")."""
    return unita if unita in UNITA_CORTE or len(unita) <= 8 else ""


def _nota_unita(unita: str) -> str:
    """Le unità lunghe ("per 100 famiglie") non stanno accanto ai numeri: vanno nell'occhiello del grafico."""
    return "" if _corta(unita) == unita else f" · {unita}"


def separa_unita(numero: str, unita: str) -> tuple[str, str]:
    """'7,20 per 100 famiglie' → ('7,20', 'per 100 famiglie'): il numero grande resta grande, l'unità va sotto."""
    if unita and _corta(unita) != unita and numero.endswith(unita):
        return numero[: -len(unita)].strip(), unita
    return numero, ""


def _voce(desc: str, x: float, dec: int, unita: str) -> dict:
    return {"descrizione": desc, "testo": valore(x, dec, unita), "copertina": compatto(x, dec, unita)}


def _voce_var(desc: str, p: float) -> dict:
    return {"descrizione": desc, "testo": variazione(p), "copertina": variazione(p)}


def _cambio(da: float, a: float, tasso: bool) -> float | None:
    """Di quanto è cambiato un valore: in punti se è già una percentuale, altrimenti in %."""
    return a - da if tasso else _var(da, a)


def _voce_cambio(desc: str, da: float, a: float, dec: int, tasso: bool) -> dict | None:
    """Voce di menù per un cambiamento. Con le percentuali (tassi, quote) una 'variazione percentuale
    di una percentuale' non ha senso: si usa la differenza in punti percentuali."""
    if tasso:
        d = a - da
        return {"descrizione": desc.replace("variazione", "differenza in punti percentuali"),
                "testo": f"{'+' if d > 0 else ''}{fmt_it(d, dec)} punti", "copertina": f"{'+' if d > 0 else ''}{fmt_it(d, dec)} punti"}
    x = _var(da, a)
    return None if x is None else _voce_var(desc, x)


def _voce_diff(desc: str, d: float, dec: int, unita: str) -> dict:
    """Differenza assoluta con il segno: '+0,9 anni'. Per età, tassi e durate si capisce meglio dei %."""
    testo = f"{'+' if d > 0 else ''}{valore(d, dec, unita)}"
    return {"descrizione": desc, "testo": testo, "copertina": testo}


AVVISO_INDICE = ("ATTENZIONE: questi valori sono NUMERI INDICE (un anno base vale 100), non euro né quantità. "
                 "Si confrontano solo le variazioni nel tempo, MAI i livelli tra gruppi diversi: due gruppi che "
                 "valgono entrambi 100 non costano uguale. Non scrivere mai 'vale 100' come fosse un prezzo. ")


AVVISO_RITMO = ("ATTENZIONE: questi valori sono già VARIAZIONI, cioè ritmi di crescita (quanto sale o scende qualcosa "
                "in un anno). Racconta il ritmo e come è cambiato anno per anno guardando i DATI COMPLETI; non "
                "parlare di 'variazioni delle variazioni' né di punti guadagnati dal ritmo. Crescere dello 0,7% l'anno "
                "non vuol dire essere fermi: vuol dire salire lentamente. ")


def e_ritmo(r: dict) -> bool:
    """Valori che sono già variazioni (ritmi di crescita), es. 'variazioni annue dei prezzi'."""
    nome = f"{r.get('fonte_testo', '')} {r.get('soggetto', '')}".lower()
    return r.get("unita") == "%" and any(p in nome for p in ("variazion", "crescita", "tendenzial", "congiuntural"))


def _dati(serie: dict[str, list[tuple[Periodo, float]]], dec: int, unita: str) -> str:
    """L'andamento completo, gruppo per gruppo: a Claude per non giudicare solo da inizio e fine."""
    return "\n".join(f"- {nome}: " + " · ".join(f"{q.breve} {valore(v, dec, unita)}" for q, v in coppie)
                     for nome, coppie in serie.items())


def e_indice(r: dict, df: pd.DataFrame | None = None) -> bool:
    """Numeri indice: lo dice il nome del dataset, oppure tutti i gruppi valgono 100 nello stesso periodo."""
    nome = f"{r.get('fonte_testo', '')} {r.get('soggetto', '')}".lower()
    if "indic" in nome or "base 19" in nome or "base 20" in nome or "=100" in nome.replace(" ", ""):
        return True
    if df is not None and "categoria" in df.columns:
        per_periodo = df.dropna(subset=["obs_value"]).groupby("time_period")["obs_value"]
        return bool(((per_periodo.min() - 100).abs() < 0.05).mul((per_periodo.max() - 100).abs() < 0.05).any())
    return False


TEMPO_OSSERVATO = ("Sono dati osservati: usa il passato per i periodi trascorsi e il presente per l'ultimo "
                   "dato. Non fare previsioni.")


# --- Controlli sui dati ---------------------------------------------------------------------
ANNI_COVID = {2020, 2021, 2022}  # variazioni eccezionali ma vere: non fanno da metro di paragone


def salto_insolito(coppie: list[tuple[Periodo, float]], tasso: bool = False) -> str | None:
    """L'ultimo cambiamento è molto più grande di quelli abituali? Spesso non è una notizia ma un cambio di
    metodo dell'ISTAT (campo di osservazione ampliato, nuova base, nuova classificazione): va verificato."""
    cambi = [(q, _cambio(p, v, tasso)) for (_, p), (q, v) in zip(coppie, coppie[1:])]
    cambi = [(q, c) for q, c in cambi if c is not None]
    if len(cambi) < 4:
        return None
    q_ult, ultimo = cambi[-1]
    abituali = sorted(abs(c) for q, c in cambi[:-1] if q.anno not in ANNI_COVID)
    if len(abituali) < 3:
        return None
    tipico = abituali[len(abituali) // 2]
    soglia = max(3 * tipico, 1.0 if tasso else 8.0)
    if abs(ultimo) <= soglia:
        return None
    unita = " punti" if tasso else "%"
    return (f"Salto insolito {q_ult.quando}: {'+' if ultimo > 0 else ''}{fmt_it(ultimo, 1)}{unita}, contro un "
            f"cambiamento tipico di circa {fmt_it(tipico, 1)}{unita} l'anno. Prima di pubblicare verifica sul sito "
            "ISTAT se è cambiato il metodo o il campo di osservazione (serie non confrontabile).")


# --- Storia 1: una serie nel tempo ---------------------------------------------------------
def _decenni(coppie: list[tuple[Periodo, float]]) -> list[tuple[Periodo, float]]:
    """Per le proiezioni: l'ultimo dato non futuro (oggi), poi un punto ogni dieci anni fino alla fine."""
    passati = [c for c in coppie if c[0].fine() <= date.today()]
    base = passati[-1] if passati else coppie[0]
    scelti = [base] + [c for c in coppie if c[0].anno > base[0].anno and c[0].anno % 10 == 0]
    return scelti if scelti[-1] == coppie[-1] else scelti + [coppie[-1]]


def numero_indice(r: dict) -> bool:
    """Numeri indice a base fissa (es. prezzi, base 2025=100): il livello (105) non dice niente a chi legge,
    contano solo le variazioni. Più stretto di e_indice, che scambia per indici anche gli 'indicatori'."""
    nome = f"{r.get('fonte_testo', '')} {r.get('soggetto', '')}".lower()
    return bool(re.search(r"base\s*(19|20)\d\d|\d{4}\s*=\s*100|numeri indice|indice (armonizzato|nazionale|"
                          r"generale|dei prezzi)|prezzi al consumo|indice della produzione|indice del fatturato", nome))


ASSOLUTE = {"anni", "mesi", "giorni", "ore", "°C"}  # età e durate: '+0,7 anni' si capisce, '+2,1%' no


def storia_serie(r: dict, df: pd.DataFrame, finestra: int) -> Storia:
    coppie = [(Periodo.da_codice(p), v) for p, v in zip(df["time_period"], df["obs_value"]) if v == v]
    futuri = [q for q, _ in coppie if q.fine() > date.today()]
    proiezione = len(futuri) >= 3  # serie con stime per gli anni a venire (es. proiezioni demografiche)
    coppie = _decenni(coppie) if proiezione else coppie[-finestra:]
    if len(coppie) < 3:
        raise ValueError("Servono almeno 3 periodi con dati per raccontare una serie")
    if coppie[-1][0].freq == "M" and len(coppie) < 13 and numero_indice(r) and not r.get("_post_esistente"):
        # per un indice dei prezzi la notizia è la variazione sull'anno prima: senza, meglio un altro dataset
        # (es. la nuova base 2025 parte da gennaio 2026; la serie storica c'è in un altro dataset)
        raise ValueError("indice mensile con meno di 13 mesi: manca il confronto con l'anno prima")
    if coppie[-1][0].freq == "M" and len(coppie) < 13 and "nell'ultimo anno" in r.get("domanda", ""):
        # serie mensile che non arriva a un anno (es. una nuova base): la domanda non può promettere l'anno
        r = {**r, "domanda": r["domanda"].replace("nell'ultimo anno", "negli ultimi mesi")}
    dec, unita, sogg = r.get("decimali", 0), r.get("unita", ""), r["soggetto"]
    (a0, v0), (a1, v1), (ap, vp) = coppie[0], coppie[-1], coppie[-2]
    (amax, vmax), (amin, vmin) = max(coppie, key=lambda c: c[1]), min(coppie, key=lambda c: c[1])
    media = sum(v for _, v in coppie) / len(coppie)

    menu = {
        "ULTIMO": _voce(f"valore {a1.quando} (ultimo dato)", v1, dec, unita),
        "PRIMO": _voce(f"valore {a0.quando} (primo dato del grafico)", v0, dec, unita),
        "MASSIMO": _voce(f"valore più alto del grafico, {amax.quando}", vmax, dec, unita),
        "MINIMO": _voce(f"valore più basso del grafico, {amin.quando}", vmin, dec, unita),
        "MEDIA": _voce(f"media {a0.da} {a1.al}", media, dec, unita),
    }
    tasso = unita == "%" or unita.startswith("per 100 ")  # già percentuali: i cambiamenti si misurano in punti
    ritmo = e_ritmo(r)    # valori che sono già variazioni: niente "variazioni delle variazioni"

    def aggiungi(codice: str, desc: str, da: float, a: float) -> None:
        if not ritmo and (voce := _voce_cambio(desc, da, a, dec, tasso)) is not None:
            menu[codice] = voce

    aggiungi("VAR_PRIMO", f"variazione percentuale {a0.da} {a1.al}", v0, v1)
    if not tasso:
        menu["DIFF_PRIMO"] = _voce_diff(f"differenza assoluta {a0.da} {a1.al}", v1 - v0, dec, unita)
    aggiungi("VAR_PRECEDENTE", f"variazione {ap.da} {a1.al}", vp, v1)
    serie = {q.codice: v for q, v in coppie}
    if a1.freq != "A" and (x0 := serie.get(a1.anno_prima())) is not None:
        prima = Periodo.da_codice(a1.anno_prima())
        aggiungi("VAR_ANNO_PRIMA", f"variazione {prima.da} {a1.al} (stesso periodo, un anno dopo)", x0, v1)
    if amax != a1:
        aggiungi("VAR_DAL_MASSIMO", f"variazione {a1.quando} rispetto al massimo, {amax.quando}", vmax, v1)
    if amin != a1:
        aggiungi("VAR_DAL_MINIMO", f"variazione {a1.quando} rispetto al minimo, {amin.quando}", vmin, v1)
    for q, v in coppie:
        menu[f"PERIODO_{q.codice}"] = _voce(f"valore {q.quando}", v, dec, unita)

    # paracadute: l'angolo più forte, scelto dal codice
    # "il più alto degli ultimi N anni" e non "mai così in alto dal 2016", che fa pensare a un 2016 più alto
    arco = f"degli ultimi {len(coppie)} {({'A': 'anni', 'Q': 'trimestri', 'M': 'mesi'}).get(a1.freq, 'periodi')}"
    if (a1, v1) == (amax, vmax):
        cop, ev, titolo, did = "ULTIMO", a1, f"Il più alto {arco}", f"{sogg} {a1.quando}: il valore più alto {arco}."
    elif (a1, v1) == (amin, vmin):
        cop, ev, titolo, did = "ULTIMO", a1, f"Il più basso {arco}", f"{sogg} {a1.quando}: il valore più basso {arco}."
    elif "VAR_PRIMO" in menu and abs(_cambio(v0, v1, tasso)) >= (1 if tasso else 10):
        cop, ev, titolo = "VAR_PRIMO", a1, f"{menu['VAR_PRIMO']['testo']} {a0.da} {a1.al}"
        did = f"{sogg}: da {menu['PRIMO']['testo']} {a0.quando} a {menu['ULTIMO']['testo']} {a1.quando}."
    else:
        cop, ev, titolo = "MASSIMO", amax, f"Il picco è stato {amax.quando}"
        did = (f"{sogg} {amax.quando}: da allora mai più così in alto." if amax == a0
               else f"{sogg} {amax.quando}, il valore più alto {arco}.")
    nota = f"{_maiuscola(a1.quando)}: {menu['ULTIMO']['testo']}"
    if "VAR_PRECEDENTE" in menu:
        nota += f", {menu['VAR_PRECEDENTE']['testo']} {ap.rispetto}"
    scelte = {"copertina": cop, "evidenzia": ev.codice, "grafico": "colonne",
              "chiusura": ["ULTIMO", "VAR_PRIMO"] if "VAR_PRIMO" in menu else ["ULTIMO"]}
    testi = _testi_base(r, did, titolo, nota + ".", f"{sogg} {a1.quando}.", f"{a0.rispetto}.")
    grafici = ("colonne", "linea") if ritmo else ("colonne", "linea", "area")

    indice = numero_indice(r) and not ritmo
    if indice:
        # numeri indice: in copertina e in chiusura le variazioni, mai il livello; grafico a linea, perché
        # le colonne partono da zero e 100 contro 105 sembrano uguali
        chiave = "VAR_ANNO_PRIMA" if "VAR_ANNO_PRIMA" in menu else "VAR_PRIMO"
        rif = Periodo.da_codice(a1.anno_prima()) if chiave == "VAR_ANNO_PRIMA" else a0
        if chiave in menu:
            var = menu[chiave]["testo"]
            arco_var = "in un anno" if chiave == "VAR_ANNO_PRIMA" else f"{a0.da} {a1.al}"
            scelte.update(copertina=chiave, evidenzia=a1.codice, grafico="linea",
                          chiusura=[chiave, "VAR_PRECEDENTE"] if "VAR_PRECEDENTE" in menu else [chiave])
            testi = _testi_base(r, f"{sogg} {a1.quando}: {var} {rif.rispetto}.", f"{var} {arco_var}",
                                f"{_maiuscola(a1.quando)}: " + (f"{menu['VAR_PRECEDENTE']['testo']} {ap.rispetto}. "
                                                                if "VAR_PRECEDENTE" in menu else "")
                                + "Valori indice, non euro.", f"{rif.rispetto}.", f"{ap.rispetto}.")
        grafici = ("linea",)
    elif unita in ASSOLUTE and "DIFF_PRIMO" in menu:
        # età e durate: il cambiamento si racconta in anni, non in percentuale
        scelte["chiusura"] = ["ULTIMO", "DIFF_PRIMO"]
        diff = _voce_diff("", v1 - vp, dec, unita)["testo"]
        testi["nota_grafico"] = f"{_maiuscola(a1.quando)}: {menu['ULTIMO']['testo']}, {diff} {ap.rispetto}."
        testi["post"] = f"{r['domanda']}\n\n{testi['didascalia_copertina']} {testi['nota_grafico']}"
        if cop == "VAR_PRIMO":
            scelte["copertina"] = "DIFF_PRIMO"
            testi["titolo_grafico"] = f"{menu['DIFF_PRIMO']['testo']} {a0.da} {a1.al}"

    intervallo = f"{a0.breve}–{a1.breve}"
    occhiello = f"{r['occhiello']}, {intervallo}" + (" · stime ISTAT" if proiezione else "")
    tempo = (f"ATTENZIONE: i valori da {futuri[0].nome} in poi sono PROIEZIONI dell'ISTAT, non dati osservati. "
             "Parla al futuro o al condizionale (\"saranno\", \"secondo le stime\") e dillo chiaramente almeno "
             "una volta." if proiezione else TEMPO_OSSERVATO)
    if e_indice(r):
        tempo = AVVISO_INDICE + tempo
    if ritmo:
        tempo = AVVISO_RITMO + tempo
    codici = [q.codice for q, _ in coppie]

    def disegna(sc: dict, tx: dict, fonte: str):
        return grafico([q.breve for q, _ in coppie], [v for _, v in coppie], forma=sc["grafico"],
                       evidenzia=codici.index(sc["evidenzia"]), decimali=dec, unita=_corta(unita), riferimento=media,
                       riferimento_testo=f"media {intervallo}: {menu['MEDIA']['testo']}", occhiello=occhiello + _nota_unita(unita),
                       titolo=tx["titolo_grafico"], nota=tx["nota_grafico"], fonte=fonte, pagina="2/3")

    extra = {"proiezione": proiezione, "periodo": a1.codice, "ultimo_valore": v1,
             "dati": _dati({r["soggetto"]: coppie}, dec, unita)}
    if not proiezione and (avviso := salto_insolito(coppie, tasso)):
        extra["da_controllare"] = [avviso]
    return Storia("serie", menu, {q.codice: q.nome for q, _ in coppie}, grafici,
                  scelte, testi, tempo, occhiello, disegna, extra)


# --- Storia 2: confronto tra categorie nell'ultimo periodo -----------------------------------
def storia_confronto(r: dict, df: pd.DataFrame, nomi: dict[str, str]) -> Storia:
    # il periodo più recente con il maggior numero di categorie presenti
    conteggi = df.dropna(subset=["obs_value"]).groupby("time_period")["categoria"].nunique()
    periodo = Periodo.da_codice(conteggi[conteggi == conteggi.max()].index.max())
    ult = df[(df["time_period"] == periodo.codice) & df["obs_value"].notna()]
    valori = dict(zip(ult["categoria"], ult["obs_value"]))
    totale_cod = next((c for c in valori if c.upper() in TOTALI or "total" in nomi.get(c, "").lower()), None)
    parti = {c: v for c, v in valori.items() if c != totale_cod}
    if len(parti) < 2:
        raise ValueError("Servono almeno 2 categorie per un confronto")
    ordine = sorted(parti, key=parti.get, reverse=True)
    dec, unita, sogg = r.get("decimali", 0), r.get("unita", ""), r["soggetto"]
    nome = lambda c: nomi.get(c, c)  # noqa: E731
    cmax, cmin = ordine[0], ordine[-1]
    # controllo di coerenza: se c'è il totale, le parti devono "contenerlo". Un tasso nazionale sta tra il
    # minimo e il massimo delle aree; un conteggio è la loro somma. Se no manca qualcuno (es. il Mezzogiorno)
    # e la classifica direbbe il falso.
    if totale_cod:
        tot, somma = valori[totale_cod], sum(parti.values())
        margine = max(abs(tot) * 0.005, 0.05)
        dentro = parti[cmin] - margine <= tot <= parti[cmax] + margine
        sommato = tot and abs(somma / tot - 1) < 0.03
        if not (dentro or sommato):
            raise ValueError(f"classifica incompleta: il totale ({fmt_it(tot, dec)}) non è coerente con le "
                             f"{len(parti)} categorie presenti, ne manca qualcuna")
    media = sum(parti.values()) / len(parti)

    menu = {
        "MASSIMO": _voce(f"valore più alto: {nome(cmax)} ({periodo.nome})", parti[cmax], dec, unita),
        "MINIMO": _voce(f"valore più basso: {nome(cmin)} ({periodo.nome})", parti[cmin], dec, unita),
        "MEDIA": _voce(f"media delle {len(parti)} categorie ({periodo.nome})", media, dec, unita),
        "DIVARIO": _voce(f"differenza tra {nome(cmax)} e {nome(cmin)}", parti[cmax] - parti[cmin], dec, unita),
    }
    if parti[cmin] > 0:
        rapporto = parti[cmax] / parti[cmin]
        menu["RAPPORTO"] = {"descrizione": f"{nome(cmax)} rispetto a {nome(cmin)}: quante volte",
                            "testo": f"{fmt_it(rapporto, 1)} volte", "copertina": f"{fmt_it(rapporto, 1)}×"}
    if totale_cod:
        menu["TOTALE"] = _voce(f"valore complessivo: {nome(totale_cod)} ({periodo.nome})", valori[totale_cod], dec, unita)
    for c in ordine:
        menu[f"CAT_{c}"] = _voce(f"{sogg}, solo {nome(c)}, {periodo.nome}", parti[c], dec, unita)
    # le parti sommano al totale? allora si possono esprimere come quote (e disegnare a ciambella)
    somma_ok = totale_cod and valori[totale_cod] > 0 and abs(sum(parti.values()) / valori[totale_cod] - 1) < 0.03
    quote = {c: parti[c] / valori[totale_cod] * 100 for c in ordine} if somma_ok else {}
    for c, q in quote.items():
        menu[f"QUOTA_{c}"] = {"descrizione": f"quota di {nome(c)} sul totale", "testo": f"{fmt_it(q, 1)}%",
                              "copertina": f"{fmt_it(q, 1 if q < 10 else 0)}%"}
    grafici = ("barre", "ciambella") if quote and len(parti) <= 6 else ("barre",)

    scelte = {"copertina": "MASSIMO", "evidenzia": cmax, "grafico": "barre",
              "chiusura": ["MASSIMO", "MINIMO"]}
    testi = _testi_base(r, f"{_maiuscola(sogg)}, {periodo.nome}. Prima in classifica: {nome(cmax)}.",
                        f"In testa: {nome(cmax)}", f"In fondo alla classifica: {nome(cmin)}.",
                        f"{nome(cmax)}, prima in classifica.", f"{nome(cmin)}, ultima in classifica.")
    occhiello = f"{r['occhiello']}, {periodo.nome}"

    def disegna(sc: dict, tx: dict, fonte: str):
        nome_g = lambda c: sc.get("nomi", {}).get(c) or nome(c)  # noqa: E731  (nomi scelti da Claude)
        if sc["grafico"] == "ciambella":
            return grafico_ciambella([nome_g(c) for c in ordine], [quote[c] for c in ordine],
                                     evidenzia=ordine.index(sc["evidenzia"]), centro=menu["TOTALE"]["copertina"],
                                     occhiello=occhiello, titolo=tx["titolo_grafico"], nota=tx["nota_grafico"],
                                     fonte=fonte, pagina="2/3")
        # il totale fa da riferimento solo se è sulla stessa scala delle parti (es. un tasso medio
        # nazionale); se è la loro somma (es. abitanti) si usa la media
        if totale_cod and valori[totale_cod] <= parti[cmax] * 1.2:
            rif, rif_testo = valori[totale_cod], f"{nome_g(totale_cod)}: {menu['TOTALE']['testo']}"
        elif len(parti) >= 3:
            rif, rif_testo = media, f"media: {menu['MEDIA']['testo']}"
        else:  # con due sole barre la media non aggiunge niente
            rif, rif_testo = None, ""
        # con tante righe l'unità ripetuta su ogni barra affolla il grafico: va una volta sola, nell'occhiello
        tante = len(ordine) > 8
        return grafico([nome_g(c) for c in ordine], [parti[c] for c in ordine], forma="orizzontale",
                       evidenzia=ordine.index(sc["evidenzia"]), decimali=dec, unita="" if tante else _corta(unita),
                       riferimento=rif, riferimento_testo=rif_testo,
                       occhiello=occhiello + (f" · {unita}" if tante and unita else _nota_unita(unita)),
                       titolo=tx["titolo_grafico"],
                       nota=tx["nota_grafico"], fonte=fonte, pagina="2/3")

    return Storia("confronto", menu, {c: nome(c) for c in ordine}, grafici, scelte, testi,
                  f"Tutti i valori si riferiscono a {periodo.nome}. " + TEMPO_OSSERVATO, occhiello, disegna,
                  {"proiezione": False, "periodo": periodo.codice, "ultimo_valore": None})


# --- Storia 3: 2-4 categorie nel tempo -----------------------------------------------------------
def storia_multipla(r: dict, df: pd.DataFrame, nomi: dict[str, str], finestra: int) -> Storia:
    tab = df.pivot_table(index="time_period", columns="categoria", values="obs_value").dropna().sort_index()
    tab = tab.iloc[-finestra:]
    if len(tab) < 3 or tab.shape[1] < 2:
        raise ValueError("Servono almeno 2 categorie con 3 periodi in comune")
    periodi = [Periodo.da_codice(p) for p in tab.index]
    a0, a1 = periodi[0], periodi[-1]
    dec, unita, sogg = r.get("decimali", 0), r.get("unita", ""), r["soggetto"]
    nome = lambda c: nomi.get(c, c)  # noqa: E731
    cats = sorted(tab.columns, key=lambda c: tab[c].iloc[-1], reverse=True)

    tasso = unita == "%" or unita.startswith("per 100 ")  # già percentuali: i cambiamenti si misurano in punti
    ritmo = e_ritmo(r)    # valori che sono già variazioni: niente "variazioni delle variazioni"
    menu = {}
    for c in cats:
        menu[f"ULTIMO_{c}"] = _voce(f"{sogg}, solo {nome(c)}, {a1.quando}", tab[c].iloc[-1], dec, unita)
        menu[f"PRIMO_{c}"] = _voce(f"{sogg}, solo {nome(c)}, {a0.quando}", tab[c].iloc[0], dec, unita)
        voce = None if ritmo else _voce_cambio(f"variazione percentuale di {nome(c)} {a0.da} {a1.al}",
                                               tab[c].iloc[0], tab[c].iloc[-1], dec, tasso)
        if voce is not None:
            menu[f"VAR_{c}"] = voce
        if not tasso:
            menu[f"DIFF_{c}"] = _voce_diff(f"differenza assoluta di {nome(c)} {a0.da} {a1.al}",
                                           tab[c].iloc[-1] - tab[c].iloc[0], dec, unita)
    alto, basso = cats[0], cats[-1]
    indice = e_indice(r, df)
    if not indice:  # con i numeri indice le distanze tra gruppi non hanno senso
        menu["DIVARIO_ULTIMO"] = _voce(f"distanza tra {nome(alto)} e {nome(basso)} {a1.quando}",
                                       tab[alto].iloc[-1] - tab[basso].iloc[-1], dec, unita)
        menu["DIVARIO_PRIMO"] = _voce(f"distanza tra {nome(alto)} e {nome(basso)} {a0.quando}",
                                      tab[alto].iloc[0] - tab[basso].iloc[0], dec, unita)
    # quale gruppo è cambiato di più, in proporzione
    cambi = {c: _cambio(tab[c].iloc[0], tab[c].iloc[-1], tasso) for c in cats}
    piu_mosso = max((c for c in cats if cambi[c] is not None), key=lambda c: abs(cambi[c]), default=alto)
    divario_piccolo = abs(tab[alto].iloc[-1] - tab[basso].iloc[-1]) < 0.05 * abs(tab[alto].iloc[-1] or 1)

    if ritmo:  # la storia è il ritmo di oggi, gruppo per gruppo
        scelte = {"copertina": f"ULTIMO_{alto}", "evidenzia": alto, "grafico": "linee",
                  "chiusura": [f"ULTIMO_{alto}", f"ULTIMO_{basso}"]}
    elif indice or divario_piccolo:  # la storia è la variazione più forte, non la distanza
        altro = next(c for c in cats if c != piu_mosso)
        scelte = {"copertina": f"VAR_{piu_mosso}", "evidenzia": piu_mosso, "grafico": "indice",
                  "chiusura": [f"VAR_{piu_mosso}", f"VAR_{altro}"]}
    else:
        scelte = {"copertina": "DIVARIO_ULTIMO", "evidenzia": alto, "grafico": "linee",
                  "chiusura": [f"ULTIMO_{alto}", f"ULTIMO_{basso}"]}
    # paracadute: la copertina è la distanza tra i gruppi e la domanda parla proprio di quella;
    # i nomi stanno solo dove la maiuscola va bene (inizio frase, dopo i due punti)
    testi = _testi_base(r, f"{nome(alto)}: {menu[f'ULTIMO_{alto}']['testo']}. {nome(basso)}: "
                           f"{menu[f'ULTIMO_{basso}']['testo']}. Periodo: {a1.nome}.",
                        f"{nome(alto)} · {nome(basso)}",
                        f"{_maiuscola(a1.quando)}: {menu[f'ULTIMO_{alto}']['testo']} contro "
                        f"{menu[f'ULTIMO_{basso}']['testo']}.", f"{nome(alto)}, {a1.nome}.", f"{nome(basso)}, {a1.nome}.")
    testi["domanda"] = "Quanta distanza c'è tra i due gruppi?" if len(cats) == 2 else "Quanta distanza c'è tra i gruppi?"
    if ritmo:
        testi.update(domanda=r["domanda"],
                     didascalia_copertina=f"{nome(alto)}, {a1.nome}: {menu[f'ULTIMO_{alto}']['testo']} in un anno.",
                     titolo_grafico=f"{nome(alto)} · {nome(basso)}, anno per anno")
    if scelte["copertina"].startswith("VAR_"):
        altro = scelte["chiusura"][1][4:]
        testi.update(
            # con gli indici la domanda suggerita può parlare di livelli ("costa di più?"): meglio una neutra
            domanda=f"Cosa è cambiato di più {a0.da} {a1.al}?" if indice else r["domanda"],
            didascalia_copertina=f"{nome(piu_mosso)}: {menu[f'VAR_{piu_mosso}']['testo']} {a0.da} {a1.al}.",
            titolo_grafico=f"{nome(piu_mosso)}: il cambiamento più forte",
            nota_grafico=f"{a0.da.capitalize()} {a1.al}: {nome(piu_mosso)} {menu[f'VAR_{piu_mosso}']['testo']}, "
                         f"{nome(altro)} {menu[f'VAR_{altro}']['testo']}.",
            punto_1=f"{nome(piu_mosso)}, {a0.da} {a1.al}.", punto_2=f"{nome(altro)}, {a0.da} {a1.al}.",
            post=f"{r['domanda']}\n\n{nome(piu_mosso)}: {menu[f'VAR_{piu_mosso}']['testo']} {a0.da} {a1.al}. "
                 f"{nome(altro)}: {menu[f'VAR_{altro}']['testo']}.")
    occhiello = f"{r['occhiello']}, {a0.breve}–{a1.breve}"

    variazioni = cambi
    stesso_segno = all(v is not None for v in variazioni.values()) and \
        (all(v >= 0 for v in variazioni.values()) or all(v <= 0 for v in variazioni.values()))
    grafici = ("linee", "indice", "variazioni") if stesso_segno else ("linee", "indice")
    if tasso:  # l'indice di un tasso (che può essere vicino a zero) non ha senso
        grafici = tuple(g for g in grafici if g != "indice")
    if ritmo:  # con i ritmi di crescita si mostra l'andamento, non le "variazioni delle variazioni"
        grafici = ("linee",)
    if not all(tab[c].iloc[0] > 0 for c in cats):
        grafici = ("linee",)  # l'indice richiede un primo valore positivo
    if scelte["copertina"].startswith("VAR_"):  # paracadute sulle variazioni: il grafico che le mostra meglio
        scelte["grafico"] = "variazioni" if "variazioni" in grafici else ("indice" if "indice" in grafici else "linee")

    def disegna(sc: dict, tx: dict, fonte: str):
        nome_g = lambda c: sc.get("nomi", {}).get(c) or nome(c)  # noqa: E731  (nomi scelti da Claude)
        if sc["grafico"] == "variazioni":  # barre con la variazione %, dalla più forte
            ordine = sorted(cats, key=lambda c: abs(variazioni[c]), reverse=True)
            return grafico([nome_g(c) for c in ordine], [abs(variazioni[c]) for c in ordine], forma="orizzontale",
                           testi_valori=[menu[f"VAR_{c}"]["testo"] for c in ordine],
                           evidenzia=ordine.index(sc["evidenzia"]),
                           occhiello=f"{occhiello} · {'differenza in punti' if tasso else 'variazione %'}",
                           titolo=tx["titolo_grafico"], nota=tx["nota_grafico"], fonte=fonte, pagina="2/3")
        if sc["grafico"] == "indice":  # tutte le linee partono da 100
            serie = [{"nome": nome_g(c), "valori": (tab[c] / tab[c].iloc[0] * 100).tolist(),
                      "evidenzia": c == sc["evidenzia"]} for c in cats]
            return grafico_linee([q.breve for q in periodi], serie, decimali=1,
                                 occhiello=f"{occhiello} · indice {a0.nome} = 100", titolo=tx["titolo_grafico"],
                                 nota=tx["nota_grafico"], fonte=fonte, pagina="2/3")
        serie = [{"nome": nome_g(c), "valori": tab[c].tolist(), "evidenzia": c == sc["evidenzia"]} for c in cats]
        return grafico_linee([q.breve for q in periodi], serie, decimali=dec, unita=_corta(unita),
                             occhiello=occhiello + _nota_unita(unita),
                             titolo=tx["titolo_grafico"], nota=tx["nota_grafico"], fonte=fonte, pagina="2/3")

    dati = _dati({nome(c): list(zip(periodi, tab[c].tolist())) for c in cats}, dec, unita)
    return Storia("serie_multiple", menu, {c: nome(c) for c in cats}, grafici, scelte, testi,
                  (AVVISO_RITMO if ritmo else "") + (AVVISO_INDICE if indice else "") + TEMPO_OSSERVATO,
                  occhiello, disegna, {"proiezione": False, "periodo": a1.codice, "ultimo_valore": None, "dati": dati})


def _testi_base(r: dict, didascalia: str, titolo: str, nota: str, punto_1: str, punto_2: str) -> dict:
    return {"domanda": r["domanda"], "didascalia_copertina": _maiuscola(didascalia), "titolo_grafico": titolo,
            "nota_grafico": nota, "punto_1": _maiuscola(punto_1), "punto_2": _maiuscola(punto_2),
            "invito": r.get("invito", INVITO), "post": f"{r['domanda']}\n\n{_maiuscola(didascalia)} {nota}"}


# --- Carosello -------------------------------------------------------------------------------------
def costruisci_carosello(r: dict, df: pd.DataFrame, finestra: int, nomi: dict[str, str] | None = None,
                         usa_claude: bool = True, precedente: dict | None = None,
                         varieta: str = "", revisione: bool = True,
                         imposto: dict | None = None) -> tuple[list, str, dict]:
    """Restituisce (slide, didascalia Instagram, rapporto).

    df: colonne time_period, obs_value e, per confronti e serie multiple, categoria.
    nomi: codice di categoria -> nome leggibile.
    """
    tipo = r.get("tipo_storia", "serie")
    nomi = nomi or {}

    def costruisci(nomi_: dict) -> Storia:
        if tipo == "confronto":
            return storia_confronto(r, df, nomi_)
        if tipo == "serie_multiple":
            return storia_multipla(r, df, nomi_, finestra)
        return storia_serie(r, df, finestra)

    st = costruisci(nomi)

    scelte, testi = dict(st.scelte), dict(st.testi)
    if r.get("grafico") in st.grafici:  # il grafico preferito (calendario o redazione), se adatto ai dati
        scelte["grafico"] = r["grafico"]
    origine = {g: "modello" for g in GRUPPI}
    rapporto = {"tipo_storia": st.tipo, "menu": st.menu, **st.extra}
    rapporto["da_controllare"] = list(st.extra.get("da_controllare", []))
    if imposto:  # scelte e testi già decisi (es. corretti a mano in redazione): niente Claude
        scelte.update(imposto.get("scelte") or {})
        testi.update(imposto.get("testi") or {})
        origine = {g: "redazione" for g in GRUPPI}
    elif usa_claude:
        accettati, rapporto["claude"] = scrivi_con_claude(r, st, precedente, varieta, revisione=revisione)
        if avvisi := rapporto["claude"].get("da_controllare"):
            rapporto["da_controllare"] += avvisi  # si aggiungono agli avvisi sui dati, non li sostituiscono
        if not accettati and (buoni := rapporto["claude"].get("nomi_validi")):
            # paracadute con i nomi di Claude invece delle etichette ISTAT ("italiano-a")
            st = costruisci({**nomi, **buoni})
            scelte, testi = dict(st.scelte), dict(st.testi)
            rapporto["menu"] = st.menu
        for gruppo, dati in accettati.items():
            scelte.update(dati["scelte"])
            testi.update(dati["testi"])
            origine[gruppo] = "claude"
    rapporto.update(scelte=scelte, testi=testi, origine=origine, grafici_ammessi=list(st.grafici))

    fonte = f"Fonte: {r.get('fonte_testo', 'ISTAT')}"
    unita = r.get("unita") or ""
    punti = []
    for i, (c, campo) in enumerate(zip(scelte["chiusura"], ("punto_1", "punto_2"))):
        numero, sotto = separa_unita(st.menu[c]["copertina"], unita)
        punti.append({"numero": numero, "unita": sotto, "testo": testi[campo], "evidenzia": i == 0})
    numero, sotto = separa_unita(st.menu[scelte["copertina"]]["copertina"], unita)
    slides = [
        copertina(occhiello=r["occhiello"], titolo=testi["domanda"], numero=numero, unita=sotto,
                  didascalia=testi["didascalia_copertina"], fonte=fonte),
        st.disegna(scelte, testi, fonte),
        chiusura(punti=punti, invito=testi["invito"], pagina="3/3"),
    ]
    testo_ig = f"{testi['post']}\n\n{fonte}. Elaborazione Data%. {LICENZA}.\n\n{HASHTAG}"
    return slides, testo_ig, rapporto


# --- Claude: testi e controlli ------------------------------------------------------------------
GRUPPI = {  # per ogni slide, i testi con la lunghezza massima in caratteri
    "copertina": {"domanda": 70, "didascalia_copertina": 130},
    "grafico": {"titolo_grafico": 60, "nota_grafico": 170},
    "chiusura": {"punto_1": 75, "punto_2": 75, "invito": 90},
    "post": {"post": 700},
}
_NUMERO = re.compile(r"\d[\d.,]*")
MAX_NOME = 28


def pulisci(testo: str) -> str:
    """Rete di sicurezza sullo stile: niente trattini lunghi, spazi in ordine."""
    testo = re.sub(r"\s*[—–]\s*", ", ", testo)
    testo = re.sub(r",\s*([.,;:!?])", r"\1", testo)
    return re.sub(r"\s{2,}", " ", testo).strip()


def _normalizza(token: str) -> str:
    """'1.955' -> '1955', '22,35' -> '22,35', '8.' (fine frase) -> '8'."""
    token = token.rstrip(".,")
    return re.sub(r"\.(?=\d{3}(\D|$))", "", token)


def numeri(testo: str) -> set[str]:
    testo = re.sub(r"\d+\s?°", "", testo)  # gli ordinali ("1° gennaio") non sono dati
    return {_normalizza(t) for t in _NUMERO.findall(testo)}


def _completezza(r: dict) -> str:
    """Se il grafico mostra solo alcuni gruppi, Claude deve saperlo."""
    altri = r.get("altri") or []
    if not altri:
        return "Il grafico mostra tutti i gruppi disponibili di questo tipo."
    elenco = ", ".join(altri[:12]) + (f" e altri {len(altri) - 12}" if len(altri) > 12 else "")
    return (f"ATTENZIONE: il grafico mostra solo una parte dei gruppi. Esistono anche: {elenco}. "
            "Quindi NON scrivere \"tutti\", \"tutte le aree\", \"il più alto\", \"il più basso\", \"guida\", "
            "\"ultimo\" come se fossero tutti: limita i confronti ai gruppi mostrati (\"tra le aree nel grafico\").")


def costruisci_prompt(r: dict, st: Storia, precedente: dict | None = None, varieta: str = "") -> str:
    limiti = "\n".join(f"- {k}: max {n} caratteri" for g in GRUPPI.values() for k, n in g.items())
    testo = prompt(
        "racconta", soggetto=r["soggetto"], occhiello=r["occhiello"], domanda_ricetta=r["domanda"],
        indicatore=r.get("fonte_testo", "ISTAT"), completezza=_completezza(r),
        dati=st.extra.get("dati") or "(vedi il menù)",
        tipo_storia=st.tipo, tempo=st.tempo, limiti=limiti, varieta=varieta,
        menu="\n".join(f'- "{k}": {v["descrizione"]} → {v["testo"]}' for k, v in st.menu.items()),
        elementi="\n".join(f"- {c}: {n}" for c, n in st.elementi.items()),
        grafici="\n".join(f'- "{g}": {GRAFICI[g]}' for g in st.grafici),
    )
    if precedente:
        testo += "\n\n" + prompt("racconta_gia_pubblicata", **{k: precedente.get(k, "") for k in
                                                              ("data", "numero", "copertina", "domanda", "titolo")})
    return testo


def _controlla_testi(proposta: dict, campi: dict, consentiti: set[str]) -> str | None:
    for campo, limite in campi.items():
        testo = proposta.get(campo)
        if not isinstance(testo, str) or not testo.strip():
            return f"{campo} mancante"
        if len(testo) > limite:
            return f"{campo} troppo lungo ({len(testo)}/{limite})"
        if extra := numeri(testo) - consentiti:
            return f"{campo}: numeri non presenti nei fatti {sorted(extra)}"
    return None


def _consentiti(st: Storia) -> set[str]:
    consentiti = set()
    for voce in st.menu.values():
        consentiti |= numeri(voce["testo"]) | numeri(voce["descrizione"])
    for codice, nome in st.elementi.items():
        consentiti |= numeri(codice) | numeri(nome)
    return consentiti | numeri(st.extra.get("dati", ""))


def nomi_validi(proposta: dict, st: Storia) -> dict | None:
    """I nomi dei gruppi proposti da Claude che vanno bene (anche solo alcuni); None se nessuno."""
    nomi, consentiti = proposta.get("nomi_elementi"), _consentiti(st)
    if not isinstance(nomi, dict):
        return None
    buoni = {c: pulisci(n) for c, n in nomi.items() if c in st.elementi and isinstance(n, str) and n.strip()
             and len(n) <= MAX_NOME and not numeri(n) - consentiti}
    return buoni or None


def verifica(proposta: dict, st: Storia, vietata: str | None = None) -> tuple[dict, dict]:
    """Restituisce (gruppi accettati con scelte e testi, gruppi scartati con il motivo)."""
    consentiti = _consentiti(st)

    accettati, scartati = {}, {}
    for gruppo, campi in GRUPPI.items():
        campi, scelte = dict(campi), {}
        if gruppo == "copertina":
            codice = proposta.get("copertina")
            if codice not in st.menu:
                scartati[gruppo] = f"codice di copertina non valido: {codice!r}"
                continue
            if codice == vietata:
                scartati[gruppo] = f"copertina {codice} già usata l'ultima volta con gli stessi dati: scegline un'altra"
                continue
            scelte["copertina"] = codice
        elif gruppo == "grafico":
            elemento, tipo = str(proposta.get("evidenzia")), proposta.get("grafico")
            if elemento not in st.elementi:
                scartati[gruppo] = f"elemento da evidenziare non valido: {elemento!r}"
                continue
            if tipo not in st.grafici:
                scartati[gruppo] = f"grafico non valido: {tipo!r} (ammessi: {', '.join(st.grafici)})"
                continue
            scelte.update(evidenzia=elemento, grafico=tipo)
            if st.tipo != "serie" and (nomi := nomi_validi(proposta, st)):
                scelte["nomi"] = nomi  # facoltativi: altrimenti restano i nomi scelti al passo 2
        elif gruppo == "chiusura":
            codici = proposta.get("chiusura")
            if not (isinstance(codici, list) and 1 <= len(codici) <= 2 and len(set(codici)) == len(codici)
                    and all(c in st.menu for c in codici)):
                scartati[gruppo] = f"codici di chiusura non validi: {codici!r}"
                continue
            if len(codici) == 1:
                campi.pop("punto_2")
            scelte["chiusura"] = codici
        if motivo := _controlla_testi(proposta, campi, consentiti):
            scartati[gruppo] = motivo
            continue
        accettati[gruppo] = {"scelte": scelte, "testi": {k: pulisci(proposta[k]) for k in campi}}
    return accettati, scartati


GRUPPO_DI = {campo: g for g, campi in GRUPPI.items() for campo in campi} | {"copertina": "copertina",
                                                                            "grafico": "grafico"}


def rivedi(r: dict, st: Storia, validi: dict) -> dict:
    """Il caporedattore: rilegge il carosello con i dati completi davanti e segnala gli errori."""
    scelte, testi = dict(st.scelte), dict(st.testi)
    for dati in validi.values():
        scelte.update(dati["scelte"])
        testi.update(dati["testi"])
    return chiedi(prompt(
        "revisione", indicatore=r.get("fonte_testo", "ISTAT"), completezza=_completezza(r),
        dati=st.extra.get("dati") or "(vedi il menù)",
        menu="\n".join(f'- "{k}": {v["descrizione"]} → {v["testo"]}' for k, v in st.menu.items()),
        numero=st.menu[scelte["copertina"]]["copertina"], grafico=scelte["grafico"],
        evidenzia=st.elementi.get(scelte["evidenzia"], scelte["evidenzia"]),
        testi=json.dumps(testi, ensure_ascii=False, indent=1)))


def scrivi_con_claude(r: dict, st: Storia, precedente: dict | None = None, varieta: str = "",
                      tentativi: int = 3, revisione: bool = True) -> tuple[dict, dict]:
    """(tutti i gruppi, rapporto) se la storia di Claude passa i controlli, altrimenti ({}, rapporto).

    Le slide già valide si tengono: ai tentativi successivi Claude corregge solo quelle respinte.
    """
    vietata = precedente["copertina"] if precedente else None
    base = costruisci_prompt(r, st, precedente, varieta)
    testo, rapporto, validi, revisioni, ultimi_problemi = base, {"tentativi": []}, {}, 0, []
    for n in range(1, tentativi + 1):
        try:
            proposta = chiedi(testo)
        except Exception as exc:
            rapporto.setdefault("errori", []).append(str(exc))
            limite = "usage" in str(exc).lower() or "limit" in str(exc).lower()
            if limite or n == tentativi:  # limiti d'uso, o tentativi finiti: si usa la versione a modello
                log.warning("Claude non disponibile, uso la versione a modello: %s", exc)
                return {}, rapporto
            log.warning("Claude non ha risposto (%s): riprovo", exc)  # es. un timeout: capita
            continue
        accettati, scartati = verifica(proposta, st, vietata)
        rapporto["tentativi"].append({"proposta": proposta, "scartati": scartati})
        if st.tipo != "serie" and (nomi := nomi_validi(proposta, st)):
            rapporto["nomi_validi"] = nomi  # utili anche se il resto della storia non passa
        validi.update(accettati)
        mancanti = {g: m for g, m in scartati.items() if g not in validi}
        if not mancanti:
            if not revisione:
                return validi, rapporto
            if revisioni >= 2:  # già rivisto due volte: si pubblica, con i dubbi rimasti nel riepilogo
                if ultimi_problemi:
                    rapporto["da_controllare"] = [f"{e} (corretto ma non riverificato)" for e in ultimi_problemi]
                return validi, rapporto
            revisioni += 1
            try:
                esito = rivedi(r, st, validi)
            except Exception as exc:  # il revisore non risponde: si pubblica, segnalandolo
                log.warning("Revisione non riuscita (%s)", exc)
                rapporto["da_controllare"] = [f"revisione automatica non riuscita: {exc}"]
                return validi, rapporto
            rapporto.setdefault("revisioni", []).append(esito)
            problemi_rev = [p for p in esito.get("problemi") or [] if isinstance(p, dict)]
            if esito.get("ok", True) or not problemi_rev:
                rapporto.pop("da_controllare", None)
                return validi, rapporto
            elenco = ultimi_problemi = [f"{p.get('campo')}: {p.get('problema')}" for p in problemi_rev]
            log.warning("Il revisore segnala:\n%s", "\n".join(f"- {e}" for e in elenco))
            if n == tentativi:  # niente più tentativi: si pubblica, con l'avviso nel riepilogo
                rapporto["da_controllare"] = elenco
                return validi, rapporto
            for p in problemi_rev:  # le slide con problemi si riscrivono
                validi.pop(GRUPPO_DI.get(p.get("campo"), "post"), None)
            testo = (base + "\n\nLA TUA RISPOSTA PRECEDENTE:\n" + json.dumps(proposta, ensure_ascii=False, indent=1)
                     + "\n\n" + prompt("correzione", problemi="\n".join(
                         f"- {p.get('campo')}: {p.get('problema')} (suggerimento: {p.get('suggerimento', '')})"
                         for p in problemi_rev)))
            continue
        problemi = "\n".join(f"- {g}: {m}" for g, m in mancanti.items())
        log.warning("Tentativo %s di Claude, da correggere:\n%s", n, problemi)
        # correzione mirata: Claude rivede la sua risposta invece di riscriverla da capo
        testo = (base + "\n\nLA TUA RISPOSTA PRECEDENTE:\n" + json.dumps(proposta, ensure_ascii=False, indent=1)
                 + "\n\n" + prompt("correzione", problemi=problemi))
    log.warning("Storia di Claude scartata dopo %s tentativi: uso la versione a modello", tentativi)
    return {}, rapporto
