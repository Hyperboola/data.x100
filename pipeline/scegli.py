"""Passo 2 · La serie del giorno: Claude sceglie dataset e codici, il codice controlla.

Il controllo vero arriva dopo, al download: se i codici scelti non hanno dati,
il job prova un altro dataset.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date

from pipeline.categorie import candidati
from pipeline.claude import chiedi, prompt
from pipeline.periodi import Periodo
import pandas as pd

from config import RAW_DIR
from sources.rete import FonteNonDisponibile, NessunDato
from sources.istat import Istat, slug_filtri

FREQ_CODICE = {"annuale": "A", "trimestrale": "Q", "mensile": "M"}
PERIODO = {"annuale": "anno", "trimestrale": "trimestre", "mensile": "mese"}
MAX_CANDIDATI = 12   # dataset proposti a Claude (già ordinati dal codice)
# Parole da dataset tecnico: finiscono in fondo alla lista
TECNICI = ("base ", "pesi", "ponderazion", "deflator", "destagionalizzat", "concatenat", "metadat",
           "contributi alla", "ricostruit", "archivio", "valori a prezzi", "classificazione", "versione")
MEMORIA = RAW_DIR / "istat" / "freschezza.json"  # ultimo periodo noto di ogni dataset, per frequenza
MEMORIA_GIORNI = 30      # "ultimo dato del 2025": può cambiare a ogni nuova pubblicazione ISTAT
MEMORIA_STRUTTURA = 180  # "nessun dato mensile in questo dataset": cambia raramente
MINUTI_SCELTA = 5    # tempo massimo per trovare un dataset buono in una chiamata
MAX_CODICI = 40      # codici mostrati per ogni dimensione
TOTALI = {"IT", "9", "T", "TOTAL", "99", "ALL", "TOT"}
LIMITI = {"occhiello": 40, "soggetto": 60, "domanda": 70}

log = logging.getLogger(__name__)


class NessunaSerie(Exception):
    """Per questo sottotema e questa fascia non si è trovata una serie valida."""


class DatasetEsauriti(NessunaSerie):
    """Tutti i dataset del sottotema sono già stati usati (o esclusi)."""


class SerieNonValida(NessunaSerie):
    """I dataset provati non vanno bene (vecchi, irraggiungibili, codici sbagliati): provarne altri."""

    def __init__(self, messaggio: str, flows: set[str]):
        super().__init__(messaggio)
        self.flows = flows


def scegli(sottotema: str, frequenza: str, cal: dict, esclusi: set[str], istat: Istat | None = None,
           varieta: str = "") -> dict:
    """Una serie nello stesso formato dei preferiti. esclusi: dataset da non riproporre.

    Claude mette in classifica 5 dataset (una sola chiamata); il codice li prova in ordine e
    tiene il primo con serie fresche. Ciò che scopre sulla freschezza lo ricorda per 30 giorni,
    così i dataset fermi non vengono più proposti.
    """
    istat = istat or Istat()
    soglia = cal["freschezza"][frequenza]
    memoria = _memoria()
    scadenza = time.monotonic() + MINUTI_SCELTA * 60
    c = candidati(sottotema, cal, istat)
    c = c[~c["dataset"].isin(esclusi) & ~c["dataset"].map(lambda f: _noto_vecchio(memoria, f, frequenza, soglia))]
    if c.empty:
        raise DatasetEsauriti(f"nessun dataset nuovo e potenzialmente fresco per {sottotema}")
    # prima i dataset "da persone", poi quelli tecnici; a parità, il più aggiornato
    tecnico = c["titolo"].fillna("").str.lower().map(lambda t: any(p in t for p in TECNICI))
    c = c.assign(_tecnico=tecnico).sort_values(["_tecnico", "last_update"], ascending=[True, False],
                                               na_position="last").head(MAX_CANDIDATI)

    if cal.get("scelta_dataset", "claude") == "recente":  # nessuna chiamata a Claude: i primi in ordine
        risposta, classifica = {"motivo": "il più aggiornato tra i dataset non tecnici"}, c["dataset"].head(5).tolist()
    else:
        elenco = "\n".join(f"- {r.dataset}: {r.titolo} (aggiornato {str(r.last_update)[:10]})" for r in c.itertuples())
        tema = cal["giorni"][cal["giorno_di"][sottotema]]["tema"]
        risposta = chiedi(prompt("scegli_dataset", tema=tema, sottotema=sottotema, frequenza=frequenza,
                                 periodo=PERIODO[frequenza], candidati=elenco))
        classifica = [f for f in risposta.get("classifica", []) if f in set(c["dataset"])][:5]
        if not classifica:
            raise NessunaSerie(f"Claude non ha indicato dataset dell'elenco: {risposta.get('classifica')!r}")

    provati = set()
    for flow in classifica:
        provati.add(flow)
        if time.monotonic() > scadenza:
            raise SerieNonValida(f"tempo esaurito dopo {MINUTI_SCELTA} minuti: riprovo con il prossimo giro", provati)
        try:
            codici, meta = istat.struttura(flow)
            serie = istat.serie_disponibili(flow, filtri_ricognizione(codici, frequenza), max_giorni=MEMORIA_GIORNI)
        except NessunDato:  # nessuna serie di questa frequenza (o per l'Italia): non è lento, è vuoto
            _ricorda(memoria, flow, pd.DataFrame({"time_period": []}))  # vuoto per tutte le frequenze
            continue
        except FonteNonDisponibile as exc:  # ISTAT ha un guasto: oggi si salta, ma non è colpa del dataset
            log.info("%s/%s: ISTAT non risponde per %s, lo salto per oggi (%s)", sottotema, frequenza, flow,
                     str(exc)[:80])
            continue
        except Exception as exc:
            if "timed out" in str(exc).lower():
                if _timeout(memoria, flow):  # già successo un altro giorno: davvero troppo pesante
                    log.info("%s/%s: %s di nuovo troppo lento, lo salto per 30 giorni", sottotema, frequenza, flow)
                else:  # forse è solo ISTAT sovraccarico in questo momento: si riprova un altro giorno
                    log.info("%s/%s: %s non ha risposto in tempo, lo salto per oggi", sottotema, frequenza, flow)
            elif isinstance(exc, (KeyError, ValueError)):  # struttura illeggibile: inutile riprovarlo domani
                log.info("%s/%s: %s ha una struttura che non so leggere, lo salto per 30 giorni", sottotema,
                         frequenza, flow)
                _segna_lento(memoria, flow)
            else:
                log.info("%s/%s: %s non raggiungibile (%s)", sottotema, frequenza, flow, str(exc)[:120])
            continue
        _ricorda(memoria, flow, serie)  # vale per tutte le fasce
        fresche = serie_fresche(serie, frequenza, soglia)
        if not fresche.empty:
            break
        log.info("%s/%s: %s senza serie %s recenti (ultimo %s)", sottotema, frequenza, flow, frequenza,
                 serie["time_period"].max() if not serie.empty else "?")
    else:
        raise SerieNonValida(f"nessuno dei {len(classifica)} dataset in classifica ha serie {frequenza} recenti",
                             provati)
    titolo = meta.get("nome") or c.loc[c["dataset"] == flow, "titolo"].iloc[0]
    # il nome del dataset da solo è spesso generico ("Medie annue dal 2010"): gli si premette la categoria
    categoria = c.loc[c["dataset"] == flow, "percorso"].iloc[0].split(" > ")[-1]
    if categoria.lower() not in titolo.lower():
        titolo = f"{categoria}: {titolo[:1].lower()}{titolo[1:]}"
    log.info("%s/%s: scelto %s (%s)", sottotema, frequenza, flow, titolo)

    testo = prompt("scegli_serie", titolo=titolo, dataset=flow, frequenza=frequenza,
                   codice_frequenza=FREQ_CODICE[frequenza], ultimo=Periodo.da_codice(fresche["time_period"].max()).nome,
                   dimensioni=_descrivi(codici, fresche), varieta=varieta)
    for _ in range(2):
        r = chiedi(testo)
        if not (problemi := verifica(r, fresche, frequenza)):
            filtri = {k: [str(x) for x in v] if isinstance(v, list) else str(v) for k, v in r["filtri"].items()}
            variabile = next((k for k, v in filtri.items() if isinstance(v, list)), None)
            tipo = r.get("tipo_storia", "serie") if variabile else "serie"
            altri = _esclusi_dal_grafico(fresche, codici, filtri, variabile) if variabile else []
            return {"id": f"{flow}|{slug_filtri(filtri)}", "sottotema": sottotema, "frequenza": frequenza,
                    "flow": flow, "filtri": filtri, "tipo_storia": tipo, "variabile": variabile,
                    "nomi": {str(k): str(v).strip() for k, v in (r.get("nomi_categorie") or {}).items()},
                    "altri": altri,
                    "occhiello": r["occhiello"], "soggetto": r["soggetto"],
                    "domanda": r["domanda"], "unita": r.get("unita", ""), "decimali": int(r.get("decimali", 0)),
                    "fonte_testo": f"ISTAT, {titolo}", "motivo": risposta.get("motivo", "")}
        testo += "\n\n" + prompt("correzione", problemi="\n".join(f"- {p}" for p in problemi))
    raise SerieNonValida(f"{flow}: codici non validi anche al secondo tentativo ({problemi})", provati)


def _esclusi_dal_grafico(fresche, codici, filtri: dict, variabile: str) -> list[str]:
    """Gruppi che esistono (con gli stessi altri codici) ma non sono stati scelti: per non far scrivere
    a Claude "tutte le aree" o "il più alto" quando nel grafico ne manca qualcuno."""
    righe, v = fresche, variabile.lower()
    for dim, codice in filtri.items():
        if dim != variabile and dim.lower() in righe.columns:
            righe = righe[righe[dim.lower()] == str(codice)]
    mancanti = sorted(set(righe[v]) - set(filtri[variabile])) if v in righe.columns else []
    nomi = dict(codici.loc[codici["dimension"] == variabile, ["code", "name"]].values)
    return [nomi.get(c, c) for c in mancanti]


# --- Memoria della freschezza --------------------------------------------------------------------
def _memoria() -> dict:
    return json.loads(MEMORIA.read_text()) if MEMORIA.exists() else {}


def _ricorda(memoria: dict, flow: str, serie, frequenza: str | None = None) -> None:
    """Salva l'ultimo periodo disponibile del dataset per ogni frequenza (e quali sono state controllate)."""
    ultimi = {}
    for codice in serie["time_period"]:
        try:
            p = Periodo.da_codice(codice)
        except ValueError:
            continue
        ultimi[p.frequenza] = max(ultimi.get(p.frequenza, ""), codice)
    precedente = memoria.get(flow, {}) if not memoria.get(flow, {}).get("lento") else {}
    controllate = sorted(set(precedente.get("controllate", [])) | ({frequenza} if frequenza else
                                                                  {"annuale", "trimestrale", "mensile"}))
    memoria[flow] = {**{k: v for k, v in precedente.items() if k not in ("verificato", "controllate")},
                     **ultimi, "verificato": date.today().isoformat(), "controllate": controllate}
    MEMORIA.parent.mkdir(parents=True, exist_ok=True)
    MEMORIA.write_text(json.dumps(memoria, indent=1))


def _eta_memoria(m: dict) -> int:
    return (date.today() - date.fromisoformat(m.get("verificato", "2000-01-01"))).days


def _noto_vecchio(memoria: dict, flow: str, frequenza: str, soglia: int) -> bool:
    """True se sappiamo già che il dataset non serve per questa frequenza:
    - troppo lento per ISTAT (per 30 giorni);
    - non ha proprio dati di questa frequenza (per 180 giorni: è una caratteristica del dataset);
    - li ha, ma l'ultimo è troppo vecchio (per 30 giorni: può arrivare un aggiornamento)."""
    m = memoria.get(flow)
    if not m:
        return False
    eta = _eta_memoria(m)
    if m.get("lento"):
        return eta <= MEMORIA_GIORNI
    if frequenza not in m.get("controllate", []):
        return False  # questa frequenza non è ancora stata controllata
    if frequenza not in m:
        return eta <= MEMORIA_STRUTTURA
    return eta <= MEMORIA_GIORNI and Periodo.da_codice(m[frequenza]).eta_mesi() > soglia


def _noto_fresco(memoria: dict, flow: str, frequenza: str, soglia: int) -> bool:
    """True se sappiamo (da meno di 30 giorni) che il dataset ha dati freschi di questa frequenza."""
    m = memoria.get(flow) or {}
    return (not m.get("lento") and frequenza in m and _eta_memoria(m) <= MEMORIA_GIORNI
            and Periodo.da_codice(m[frequenza]).eta_mesi() <= soglia)


def _timeout(memoria: dict, flow: str) -> bool:
    """Registra un timeout. True (e dataset segnato lento per 30 giorni) se ne aveva già avuto uno in un altro
    giorno: un timeout isolato di solito è ISTAT sovraccarico, non il dataset."""
    m, oggi = memoria.get(flow, {}), date.today().isoformat()
    if m.get("timeout") and m["timeout"] != oggi:
        _segna_lento(memoria, flow)
        return True
    memoria[flow] = {"verificato": "2000-01-01", **m, "timeout": oggi}  # senza verifica valida: si riprova
    MEMORIA.parent.mkdir(parents=True, exist_ok=True)
    MEMORIA.write_text(json.dumps(memoria, indent=1))
    return False


def _segna_lento(memoria: dict, flow: str) -> None:
    """Un dataset che va in timeout si salta per 30 giorni (come quelli vecchi)."""
    memoria[flow] = {"verificato": date.today().isoformat(), "lento": True}
    MEMORIA.parent.mkdir(parents=True, exist_ok=True)
    MEMORIA.write_text(json.dumps(memoria, indent=1))


def filtri_ricognizione(codici, frequenza: str) -> dict:
    """Per controllare la freschezza basta poco: se c'è un territorio, l'Italia con le sue macro-aree.
    Così anche i dataset per comune rispondono in pochi secondi. Niente filtro sulla frequenza:
    con una sola richiesta si sa se il dataset ha dati freschi annuali, trimestrali e mensili, e la
    risposta (in cache) serve a tutte le fasce. Le serie della frequenza giusta le sceglie serie_fresche."""
    dims = set(codici["dimension"])
    filtri = {}
    territorio = next((d for d in sorted(dims) if any(k in d.upper() for k in ("REF_AREA", "TERR", "AREA"))), None)
    if territorio:
        righe = codici[codici["dimension"] == territorio]
        aree = [c for c, n in zip(righe["code"], righe["name"])
                if c.upper() == "IT" or (len(c) == 3 and c.upper().startswith("IT")) or _ripartizione(n)]
        if aree:
            filtri[territorio] = aree
    return filtri


def serie_fresche(serie, frequenza: str, soglia_mesi: int):
    """Solo le serie della frequenza giusta con l'ultimo dato entro la soglia di freschezza."""
    def ok(codice: str) -> bool:
        try:
            p = Periodo.da_codice(codice)
        except ValueError:
            return False
        return p.frequenza == frequenza and p.eta_mesi() <= soglia_mesi

    return serie[serie["time_period"].map(ok)].reset_index(drop=True)


LIMITI_LISTA = {"confronto": (2, 12), "serie_multiple": (2, 4)}


def verifica(r: dict, fresche, frequenza: str) -> list[str]:
    """Tipo di storia valido, codici esistenti tra le serie fresche, combinazioni presenti, testi nei limiti."""
    problemi = []
    tipo, filtri = r.get("tipo_storia", "serie"), r.get("filtri")
    if tipo not in ("serie", "confronto", "serie_multiple"):
        return [f"tipo_storia non valido: {tipo!r}"]
    if not isinstance(filtri, dict):
        return ["'filtri' deve essere un oggetto {DIMENSIONE: CODICE o [CODICI]}"]
    dims = [c for c in fresche.columns if c != "time_period"]
    scelti = {k.lower(): ([str(x) for x in v] if isinstance(v, list) else [str(v)]) for k, v in filtri.items()}
    liste = [d for d, v in scelti.items() if len(v) > 1 or isinstance(filtri.get(d.upper()), list)]
    if tipo == "serie" and liste:
        problemi.append("per una 'serie' ogni dimensione vuole un solo codice")
    if tipo != "serie":
        lo, hi = LIMITI_LISTA[tipo]
        if len(liste) != 1:
            problemi.append(f"per '{tipo}' serve esattamente UNA dimensione con una lista di codici")
        elif not lo <= len(scelti[liste[0]]) <= hi:
            problemi.append(f"per '{tipo}' la lista deve avere da {lo} a {hi} codici")
    for d in dims:
        if d not in scelti:
            problemi.append(f"{d.upper()}: manca il codice")
        elif mancanti := [c for c in scelti[d] if c not in set(fresche[d])]:
            problemi.append(f"{d.upper()}: codici non elencati {mancanti}")
    if extra := set(scelti) - set(dims):
        problemi.append(f"dimensioni inesistenti: {sorted(k.upper() for k in extra)}")
    if not problemi:  # ogni combinazione deve esistere davvero
        variabile = liste[0] if liste else None
        for codice in (scelti[variabile] if variabile else [None]):
            righe = fresche
            for d in dims:
                righe = righe[righe[d] == (codice if d == variabile else scelti[d][0])]
            if righe.empty:
                problemi.append(f"la combinazione con {variabile.upper() + '=' + codice if codice else 'questi codici'} "
                                "non esiste: scegli codici che compaiono insieme (parti dai totali)")
    if not problemi and liste:  # i gruppi della lista devono avere nomi da persone
        nomi = r.get("nomi_categorie")
        codici_lista = scelti[liste[0]]
        if not isinstance(nomi, dict) or set(codici_lista) - set(map(str, nomi)):
            problemi.append(f"servono 'nomi_categorie' per tutti i codici della lista: {codici_lista}")
        elif lunghi := [c for c in codici_lista if not str(nomi[c]).strip() or len(str(nomi[c])) > 28]:
            problemi.append(f"nomi_categorie vuoti o oltre 28 caratteri: {lunghi}")
    for campo, limite in LIMITI.items():
        if not isinstance(r.get(campo), str) or not r[campo].strip() or len(r[campo]) > limite:
            problemi.append(f"'{campo}' mancante o più lungo di {limite} caratteri")
    if r.get("decimali", 0) not in (0, 1, 2):
        problemi.append("'decimali' deve essere 0, 1 o 2")
    return problemi


def _descrivi(codici, fresche) -> str:
    """Per ogni dimensione, solo i codici che compaiono nelle serie fresche (prima i totali)."""
    blocchi = []
    for (_, dim, nome), g in codici.groupby(["position", "dimension", "dimension_name"]):
        presenti = set(fresche[dim.lower()]) if dim.lower() in fresche.columns else set()
        g = g[g["code"].isin(presenti)]
        if g.empty:
            continue
        g = g.assign(_tot=~(g["code"].isin(TOTALI) | g["name"].str.contains("total", case=False)))
        mostrati = g.sort_values("_tot", kind="stable").head(MAX_CODICI)
        righe = "\n".join(f"  {c}: {n}" for c, n in zip(mostrati["code"], mostrati["name"]))
        altri = len(g) - len(mostrati)
        blocchi.append(f"{dim} ({nome}){f' — altri {altri} codici non mostrati' if altri > 0 else ''}\n{righe}")
    return "\n".join(blocchi)


def _unita_da_nomi(testo: str) -> str:
    """L'unità di misura scritta nei nomi ISTAT: 'per 100 famiglie', '%', 'milioni di euro'..."""
    if m := re.search(r"per\s+(100|1\.?000|10\.?000|100\.?000)\s+([a-zàèéìòù]+)", testo):
        return f"per {m.group(1)} {m.group(2)}"
    if any(p in testo for p in ("%", "percentual", "per cento", "valori percent")):
        return "%"
    if "milioni di euro" in testo:
        return "milioni di €"
    if "migliaia di euro" in testo:
        return "migliaia di €"
    if "euro" in testo:
        return "€"
    if "migliaia" in testo:
        return "migliaia"
    return ""


# --- Senza Claude: dataset e serie scelti con regole ---------------------------------------------
PAROLE_GRUPPI = {"maschi": "uomini", "femmine": "donne"}  # come si dice sui social
PAROLE_TOTALE = ("total", "tutte", "tutti", "complesso", "mondo", "world", "indice generale")
# Dimensioni che descrivono COSA si misura (indicatore, unità, base): il codice scelto lì è già nel soggetto.
# In tutte le altre, se non si sceglie il totale, la serie è solo una parte e il soggetto deve dirlo.
DESCRITTIVE = ("FREQ", "DATA_TYPE", "INDICATOR", "MEASURE", "TIPO_DATO", "UNIT", "BASE", "TIPO_IND")
# Nomi da mostrare in alto nelle slide, sottotema per sottotema
NOMI_SOTTOTEMI = {
    "prezzi": "Prezzi", "stipendi_redditi": "Stipendi e redditi", "risparmio_debiti": "Risparmi e debiti",
    "consumi_famiglie": "Consumi", "poverta_disuguaglianze": "Povertà", "occupazione": "Lavoro",
    "giovani_lavoro": "Giovani e lavoro", "contratti_orari": "Contratti", "imprese": "Imprese",
    "industria": "Industria", "export": "Export", "nascite": "Nascite", "invecchiamento": "Popolazione",
    "famiglie_matrimoni": "Famiglie", "migrazioni": "Migrazioni", "istruzione": "Scuola e università",
    "stranieri": "Stranieri in Italia", "casa_affitti": "Casa", "trasporti_mobilita": "Trasporti",
    "sicurezza_stradale": "Strade", "rifiuti": "Ambiente", "energia": "Energia", "aria_clima": "Ambiente",
    "turismo": "Turismo", "cultura": "Cultura", "sport": "Tempo libero", "tecnologia_internet": "Tecnologia",
    "salute_stili_vita": "Salute", "cibo": "Cibo",
}
NON_CONTEGGI = ("indice", "tasso", "rapporto", "quota", "media", "età", "percentual", "incidenza", "variazion")


def pulisci_nome(nome: str) -> str:
    """'Valle d'Aosta / Vallée d'Aoste' → 'Valle d'Aosta'; 'Provincia Autonoma Bolzano / Bozen' → 'Bolzano'."""
    nome = nome.split(" / ")[0].strip()
    nome = re.sub(r"^Provincia [Aa]utonoma (di )?", "", nome)
    return nome.replace("Trentino Alto Adige", "Trentino-Alto Adige")


def _taglia(testo: str, massimo: int) -> str:
    """Accorcia a parola intera, mai a metà parola."""
    if len(testo) <= massimo:
        return testo
    return testo[:massimo].rsplit(" ", 1)[0].rstrip(" -:,") + "…"


PAROLE_FREQUENZA = {"mensile": ("mensil",), "trimestrale": ("trimestral",),
                    "annuale": ("annual", "annui", "annue", "per anno")}


def _distanza_frequenza(titolo: str, frequenza: str) -> int:
    """0: il titolo dice la frequenza cercata · 1: non dice niente · 2: dice un'altra frequenza."""
    if any(p in titolo for p in PAROLE_FREQUENZA[frequenza]):
        return 0
    altre = (p for f, parole in PAROLE_FREQUENZA.items() if f != frequenza for p in parole)
    return 2 if any(p in titolo for p in altre) else 1


def fascia_esaurita(sottotema: str, frequenza: str, cal: dict, istat: Istat | None = None,
                    minimo: int = 12) -> int:
    """Quanti dataset del sottotema sappiamo già essere senza dati freschi di questa frequenza, se nessuno di
    quelli conosciuti ne ha e se sono abbastanza da fidarsi (almeno `minimo`, o tutti): es. le nascite, che
    ISTAT pubblica solo annuali. 0 se vale la pena cercare."""
    memoria, soglia = _memoria(), cal["freschezza"][frequenza]
    flows = list(candidati(sottotema, cal, istat or Istat())["dataset"])
    if any(_noto_fresco(memoria, f, frequenza, soglia) for f in flows):
        return 0
    vecchi = sum(_noto_vecchio(memoria, f, frequenza, soglia) for f in flows)
    return vecchi if flows and (vecchi >= minimo or vecchi == len(flows)) else 0


def scegli_senza_claude(sottotema: str, frequenza: str, cal: dict, esclusi: set[str],
                        istat: Istat | None = None, serie_usate: set[str] | None = None, **_) -> dict:
    """Come scegli(), ma senza nessuna chiamata a Claude.

    Dataset: il più aggiornato tra quelli non tecnici con serie fresche. Serie: il totale per ogni
    dimensione, l'indicatore più vicino al titolo del dataset; per i sottotemi che in calendario
    chiedono un confronto per territorio, le macro-aree d'Italia. Testi: a modello, da rivedere.
    """
    from pipeline.calendario import preferenze

    istat = istat or Istat()
    soglia, memoria, pref = cal["freschezza"][frequenza], _memoria(), preferenze(cal, sottotema)
    scadenza = time.monotonic() + MINUTI_SCELTA * 60
    c = candidati(sottotema, cal, istat)
    c = c[~c["dataset"].isin(esclusi) & ~c["dataset"].map(lambda f: _noto_vecchio(memoria, f, frequenza, soglia))]
    if c.empty:
        raise DatasetEsauriti(f"nessun dataset nuovo e potenzialmente fresco per {sottotema}")
    titoli = c["titolo"].fillna("").str.lower()
    tecnico = titoli.map(lambda t: any(p in t for p in TECNICI))
    # prima i dataset che nel titolo dicono la frequenza cercata, in fondo quelli che ne dicono un'altra
    # ("Tasso di disoccupazione - dati mensili" non serve quando si cercano dati annuali)
    # prima quelli che la memoria sa già buoni (di solito con l'elenco delle serie già salvato: zero richieste)
    ignoto = c["dataset"].map(lambda f: not _noto_fresco(memoria, f, frequenza, soglia))
    c = c.assign(_ignoto=ignoto, _tecnico=tecnico, _frequenza=titoli.map(lambda t: _distanza_frequenza(t, frequenza)))
    c = c.sort_values(["_ignoto", "_tecnico", "_frequenza", "last_update"], ascending=[True, True, True, False],
                      na_position="last").head(8)
    provati, di_fila, giu = set(), 0, False  # di_fila: timeout consecutivi · giu: ISTAT non risponde
    for n, riga in enumerate(c.itertuples(), start=1):
        flow = riga.dataset
        provati.add(flow)
        if time.monotonic() > scadenza:
            raise SerieNonValida(f"tempo esaurito dopo {MINUTI_SCELTA} minuti: riprovo con il prossimo giro", provati)
        log.info("%s/%s: controllo %s (%s/%s) · %s", sottotema, frequenza, flow, n, len(c), _taglia(str(riga.titolo), 60))
        try:
            codici, meta = istat.struttura(flow)
            filtri_r = filtri_ricognizione(codici, frequenza)
            if giu and not istat.serie_in_cache(flow, filtri_r, MEMORIA_GIORNI):
                continue  # ISTAT è giù e di questo non abbiamo l'elenco salvato: inutile aspettare un altro timeout
            serie = istat.serie_disponibili(flow, filtri_r, max_giorni=MEMORIA_GIORNI)
        except NessunDato:  # nessuna serie di questa frequenza (o per l'Italia): non è lento, è vuoto
            _ricorda(memoria, flow, pd.DataFrame({"time_period": []}))  # vuoto per tutte le frequenze
            continue
        except FonteNonDisponibile as exc:  # ISTAT ha un guasto: oggi si salta, ma non è colpa del dataset
            log.info("%s/%s: ISTAT non risponde per %s, lo salto per oggi (%s)", sottotema, frequenza, flow,
                     str(exc)[:80])
            giu = True
            continue
        except Exception as exc:
            if "timed out" in str(exc).lower():
                if _timeout(memoria, flow):  # già successo un altro giorno: davvero troppo pesante
                    log.info("%s/%s: %s di nuovo troppo lento, lo salto per 30 giorni", sottotema, frequenza, flow)
                else:  # forse è solo ISTAT sovraccarico in questo momento: si riprova un altro giorno
                    log.info("%s/%s: %s non ha risposto in tempo, lo salto per oggi", sottotema, frequenza, flow)
                di_fila += 1
                if di_fila >= 3:  # tre di fila: è ISTAT a essere lento, non i dataset
                    log.info("ISTAT sembra sovraccarico: pausa di 2 minuti prima di continuare")
                    time.sleep(120)
                    di_fila = 0
            elif isinstance(exc, (KeyError, ValueError)):  # struttura illeggibile: inutile riprovarlo domani
                log.info("%s/%s: %s ha una struttura che non so leggere, lo salto per 30 giorni", sottotema,
                         frequenza, flow)
                _segna_lento(memoria, flow)
            else:
                log.info("%s/%s: %s non raggiungibile (%s)", sottotema, frequenza, flow, str(exc)[:120])
            continue
        di_fila = 0
        _ricorda(memoria, flow, serie)  # vale per tutte le fasce
        fresche = serie_fresche(serie, frequenza, soglia)
        if fresche.empty:
            continue
        titolo = meta.get("nome") or riga.titolo
        categoria = riga.percorso.split(" > ")[-1]
        scelta = _alternativa(fresche, codici, frequenza, titolo, pref, flow, serie_usate or set())
        if scelta is None:  # tutte le serie fresche di questo dataset sono già state pubblicate
            continue
        filtri, variabile, nomi_scelti = scelta
        soggetto, unita = _soggetto_unita(codici, filtri, titolo)
        breve = soggetto
        if len(soggetto) > 55 and ": " in soggetto:
            # "Persone di 6 anni e più per spettacoli a cui...: siti archeologici e monumenti": il titolo usa
            # la parte specifica, la spiegazione completa resta nel testo come "(...)"
            generale, breve = soggetto.rsplit(": ", 1)
            soggetto = f"{generale} ({breve})"
        elif len(soggetto) > 40 and " (" in soggetto:  # "inflazione (prezzi al consumo per ...)": titolo "Inflazione"
            breve = soggetto.split(" (", 1)[0]
        parte = [PAROLE_GRUPPI.get(p.lower(), p) for p in nomi_scelti.get("parte", []) if p and p.lower() not in soggetto.lower()]
        testo_parte = ", ".join(x[:1].lower() + x[1:] if x[1:2].islower() else x for x in parte)
        if parte:
            soggetto = f"{soggetto} ({testo_parte})"
        luogo = "Italia" if variabile else nomi_scelti.get("territorio", "Italia")
        # fonte breve: il nome più chiaro tra categoria e dataset, senza ripetizioni, su una o due righe
        if categoria.lower() in titolo.lower() or titolo.lower().startswith(categoria.lower()[:20]):
            fonte = titolo
        else:
            fonte = f"{categoria}: {titolo[:1].lower()}{titolo[1:]}"
        if len(fonte) > 90:
            fonte = min((categoria, titolo), key=len)
        fonte = _taglia(fonte, 90)
        # i conteggi ("arrivi", "nati") hanno come unità la cosa contata: "9,06 mln arrivi"
        if not unita and len(soggetto.split()) <= 2 and not any(p in soggetto.lower() for p in NON_CONTEGGI):
            unita = soggetto
        aree_scelte = filtri.get(variabile, []) if variabile else []
        nome_area = lambda c: dict(zip(codici["code"], codici["name"])).get(c, "")  # noqa: E731
        zona = "area" if any(_ripartizione(nome_area(c)) for c in aree_scelte) else "regione"
        if breve != soggetto:  # titolo corto, con la parte se c'è: "Teatro (donne)"
            Sogg = breve[:1].upper() + breve[1:] + (f" ({testo_parte})" if parte else "")
        else:
            Sogg = soggetto[:1].upper() + soggetto[1:]
        log.info("%s/%s: scelto senza Claude %s (%s)", sottotema, frequenza, flow, titolo)
        return {
            "id": f"{flow}|{slug_filtri(filtri)}", "sottotema": sottotema, "frequenza": frequenza, "flow": flow,
            "filtri": filtri, "tipo_storia": "confronto" if variabile else "serie", "variabile": variabile,
            "grafico": pref.get("grafico"), "nomi": {}, "altri": [],
            "occhiello": f"{NOMI_SOTTOTEMI.get(sottotema, _taglia(categoria, 24))} · {luogo}",
            "soggetto": soggetto, "unita": unita, "decimali": None,
            "domanda": (f"{Sogg}: quale {zona} è in testa?" if variabile
                        else f"{Sogg}: com'è andata nell'ultimo anno?" if frequenza == "mensile"
                        else f"{Sogg}: com'è andata negli ultimi anni?"),
            "fonte_testo": f"ISTAT, {fonte}", "motivo": "scelto con regole (Claude spento)",
        }
    raise SerieNonValida(f"nessuno dei {len(provati)} dataset più recenti ha serie {frequenza} utilizzabili", provati)


def _alternativa(fresche, codici, frequenza: str, titolo: str, pref: dict, flow: str, usate: set[str]):
    """La prima serie del dataset mai pubblicata: quella di sempre, poi un altro indicatore (musei → teatro),
    poi una parte (donne, giovani, un prodotto), infine un'altra misura. Tutto sui dati già letti: zero richieste."""
    def priorita(d: str) -> int:
        alto = d.upper()
        return 0 if alto in ("DATA_TYPE", "INDICATOR", "TIPO_DATO") else 2 if any(k in alto for k in DESCRITTIVE) else 1
    dims = sorted((d for d in fresche.columns if d not in ("time_period", "freq")
                   and not any(k in d.upper() for k in ("REF_AREA", "TERR", "AREA"))), key=priorita)
    singole = [{d: c} for d in dims for c in dict.fromkeys(fresche[d])]
    indicatori = [f for f in singole if priorita(next(iter(f))) == 0]
    gruppi = [f for f in singole if priorita(next(iter(f))) == 1]
    coppie = [{**i, **g} for i in indicatori for g in gruppi]  # "teatro" × "donne", "cinema" × "15-24 anni"
    for forza in [None, *singole, *coppie]:
        scelta = scegli_codici(fresche, codici, frequenza, titolo, pref, forza)
        if scelta and f"{flow}|{slug_filtri(scelta[0])}" not in usate:
            return scelta
    return None


def scegli_codici(fresche, codici, frequenza: str, titolo: str, pref: dict, forza: dict | None = None):
    """(filtri, dimensione variabile o None, nomi utili) per una serie esistente; None se impossibile.
    forza: {dimensione: codice} da usare al posto della scelta abituale (per le altre serie di un dataset)."""
    nomi = {(d.lower(), c): n for d, c, n in codici[["dimension", "code", "name"]].itertuples(index=False)}
    dims = [c for c in fresche.columns if c != "time_period"]
    righe, filtri, utili = fresche, {}, {}
    if "freq" in dims:
        righe = righe[righe["freq"] == FREQ_CODICE[frequenza]]
    territorio = next((d for d in dims if any(k in d.upper() for k in ("REF_AREA", "TERR", "AREA"))), None)
    variabile = territorio if pref.get("storia") == "confronto" and pref.get("per") == "territorio" else None

    parole_titolo = {p for p in titolo.lower().replace("'", " ").split() if len(p) > 3}
    for d in dims:
        if d == variabile:
            continue
        presenti = list(dict.fromkeys(righe[d]))
        if not presenti:
            return None
        # "IT"/"Italia" è il totale solo per il territorio: per la residenza o la cittadinanza vuol dire
        # "solo gli italiani", e sceglierlo escluderebbe gli stranieri senza dirlo
        e_territorio = d == territorio
        totali = [c for c in presenti
                  if (c.upper() in TOTALI and (e_territorio or c.upper() not in ("IT", "ITALIA")))
                  or any(p in nomi.get((d, c), "").lower() for p in PAROLE_TOTALE)
                  or (e_territorio and nomi.get((d, c), "").lower() == "italia")]
        if forza and forza.get(d) in presenti:
            scelto = forza[d]
        elif totali:
            scelto = totali[0]
        else:  # es. l'indicatore: quello il cui nome somiglia di più al titolo del dataset
            somiglianza = lambda c: len(parole_titolo & set(nomi.get((d, c), "").lower().split()))  # noqa: E731
            scelto = max(presenti, key=somiglianza)
        if scelto not in totali and not e_territorio and len(presenti) > 1 and not any(k in d.upper() for k in DESCRITTIVE):
            # una sola voce tra tante (es. un prodotto del paniere, le donne): va detto nel soggetto
            utili.setdefault("parte", []).append(pulisci_soggetto(nomi.get((d, scelto), scelto)))
        righe = righe[righe[d] == scelto]
        filtri[d.upper()] = scelto
        if d == territorio and scelto.upper().startswith("IT") and scelto.upper() not in ("IT", "ITALIA"):
            utili["territorio"] = pulisci_nome(nomi.get((d, scelto), scelto))

    if variabile:
        presenti = list(dict.fromkeys(righe[variabile]))
        italia = [c for c in presenti if c.upper() in ("IT", "ITALIA")]
        macro = [c for c in presenti if len(c) == 3 and c.upper().startswith("IT")]
        regioni = [c for c in presenti if len(c) == 4 and c.upper().startswith("IT")]
        # mai lo stesso territorio due volte: se c'è il Trentino-Alto Adige, via le due province autonome
        nome_di = lambda c: nomi.get((variabile, c), "")  # noqa: E731
        if any("trentino" in nome_di(c).lower() for c in regioni):
            regioni = [c for c in regioni if "provincia autonoma" not in nome_di(c).lower()]
        regioni = [c for c in regioni if not _ripartizione(nome_di(c))]  # "ITFG" Mezzogiorno non è una regione
        # o una suddivisione completa dell'Italia o niente: una classifica parziale direbbe il falso
        # (es. Nord-ovest, Nord-est e Centro senza il Mezzogiorno: il Centro risulterebbe "primo")
        aree = _suddivisione_completa(presenti, nome_di) or (regioni if 20 <= len(regioni) <= 21 else [])
        if len(aree) >= 2:
            filtri[variabile.upper()] = italia[:1] + aree
        else:  # niente dettaglio territoriale: si torna a una serie per l'Italia
            variabile = None
            filtri[territorio.upper()] = (italia or presenti)[0]
    return filtri, (variabile.upper() if variabile else None), utili


# Le suddivisioni dell'Italia per ripartizione usate da ISTAT, dalla più dettagliata. I codici cambiano
# tra un dataset e l'altro (ITD o ITH per il Nord-est, ITFG per il Mezzogiorno...), i nomi no.
SUDDIVISIONI = (
    ("nordovest", "nordest", "centro", "sud", "isole"),
    ("nordovest", "nordest", "centro", "mezzogiorno"),
    ("nord", "centro", "mezzogiorno"),
)
_RIPARTIZIONI = {n for s in SUDDIVISIONI for n in s}


def _chiave_area(nome: str) -> str:
    """'Nord-ovest' → 'nordovest'; 'Isole (Sicilia e Sardegna)' → 'isole'."""
    return re.sub(r"[^a-z]", "", str(nome).split("(")[0].lower())


def _ripartizione(nome: str) -> bool:
    return _chiave_area(nome) in _RIPARTIZIONI


def _suddivisione_completa(codici_presenti, nome_di) -> list[str]:
    """I codici di una suddivisione completa dell'Italia, se tra i presenti ce n'è una."""
    per_nome = {}
    for c in codici_presenti:
        per_nome.setdefault(_chiave_area(nome_di(c)), c)
    for suddivisione in SUDDIVISIONI:
        if all(n in per_nome for n in suddivisione):
            return [per_nome[n] for n in suddivisione]
    return []


# Indicatori che ISTAT esprime in percentuale anche quando il nome non lo dice
PERCENTUALI = re.compile(r"^(tasso|tassi) di (occupazione|disoccupazione|inattivit|attivit|abbandono|"
                         r"povert|deprivazion|risparmio|partecipazione|scolarizzazione|copertura)|^quota d|"
                         r"^incidenza d|^percentuale d|^propensione al risparmio")
_RUMORE = (re.compile(r"\s*\((base\s*)?\d{4}\s*=\s*100\)", re.I),          # "(base 2025=100)"
           re.compile(r"\s*\(base \d{4}\)", re.I),                            # "(base 2025)"
           re.compile(r"\s*[-–]\s*dati (mensili|trimestrali|annuali)\b.*$", re.I),  # "- dati mensili"
           re.compile(r"\s*[-–]\s*(mensili|trimestrali|annuali)\b.*$", re.I),
           re.compile(r"\s*[-–]\s*medie annue\b.*$", re.I))                     # "- medie annue"


def pulisci_soggetto(testo: str) -> str:
    """Via le note tecniche dei nomi ISTAT: 'Indice ... (base 2025=100) - dati mensili' → 'Indice ...'."""
    for r in _RUMORE:
        testo = r.sub("", testo)
    return testo.strip(" -–:,")


def _soggetto_unita(codici, filtri: dict, titolo: str) -> tuple[str, str]:
    """Il soggetto è il nome dell'indicatore scelto (o il titolo); l'unità si deduce dai nomi."""
    nomi = {(d, c): n for d, c, n in codici[["dimension", "code", "name"]].itertuples(index=False)}
    indicatore = next((nomi.get((d, c)) for d, c in filtri.items()
                       if d in ("DATA_TYPE", "INDICATOR", "MEASURE", "TIPO_DATO") and isinstance(c, str)), None)
    soggetto = pulisci_soggetto((indicatore or titolo).strip()) or (indicatore or titolo).strip()
    tutti = (" ".join(str(nomi.get((d, c), "")) for d, c in filtri.items() if isinstance(c, str)) + " " + titolo).lower()
    unita = _unita_da_nomi(tutti)
    basso = soggetto.lower()
    if not unita and PERCENTUALI.search(basso):
        unita = "%"
    elif not unita and basso.startswith(("età media", "eta media", "età mediana", "speranza di vita", "durata media")):
        unita = "anni"
    if len(soggetto) > 1 and soggetto[1].islower():  # "Nati vivi" → "nati vivi", ma "PIL" resta "PIL"
        soggetto = soggetto[:1].lower() + soggetto[1:]
    if "variazion" in tutti and "variazion" not in soggetto.lower():
        # la misura scelta è una variazione (es. MEASURE "variazioni percentuali sull'anno precedente"):
        # i numeri non sono l'indice ma quanto è cambiato. Per i prezzi al consumo quel numero è l'inflazione.
        unita = "%" if "percentual" in tutti or unita == "%" else unita
        if "prezzi al consumo" in soggetto.lower():
            soggetto = f"inflazione ({re.sub(r'^indice dei ', '', soggetto)})"
        else:
            soggetto = f"variazione di: {soggetto}"
    return soggetto, unita
