"""ISTAT: catalogo dei dataset, albero delle categorie, struttura e dati.

Canale principale: SDMXWS, il servizio che ISTAT indica per l'accesso automatico
(limite dichiarato: 5 richieste al minuto per IP; noi ne facciamo al massimo 4-5).
Riserva: il backend del sito IstatData, usato per l'albero delle categorie e per i dati
quando SDMXWS non risponde. I dati ISTAT sono rilasciati con licenza CC BY 4.0.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from config import RAW_DIR
from sources.rete import Client, ErroreFonte, NessunDato

SDMX = "https://esploradati.istat.it/SDMXWS/rest"
SITO = "https://esploradati.istat.it/databrowserhub/api/core/nodes/1"
USER_AGENT = "aperidata/1.0 (divulgazione statistica; https://instagram.com/aperidata)"
CSV = "application/vnd.sdmx.data+csv;version=1.0.0"
XML = "application/xml"
NS = {"s": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure",
      "c": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"}
LANG = "{http://www.w3.org/XML/1998/namespace}lang"

log = logging.getLogger(__name__)


class Istat:
    def __init__(self, cartella: Path = RAW_DIR / "istat", client: Client | None = None):
        self.cartella = Path(cartella)
        self.client = client or Client(USER_AGENT)

    # --- Cataloghi (cache settimanale; se l'aggiornamento fallisce si usa la copia vecchia) ---
    def dataset(self) -> pd.DataFrame:
        """Tutti i dataset SDMX: id, versione, nome e data dell'ultimo aggiornamento."""
        path = self.cartella / "dataset.parquet"
        return self._catalogo(path, lambda: _leggi_dataflow(
            self.client.richiesta(f"{SDMX}/dataflow/IT1", XML).content), pd.read_parquet,
            lambda df: df.to_parquet(path, index=False))

    def categorie(self) -> pd.DataFrame:
        """Albero delle categorie del sito ISTAT: una riga per ogni (dataset, categoria)."""
        path = self.cartella / "categorie_it.json"  # chiesto in italiano (il vecchio categorie.json era misto)
        testo = self._catalogo(path, lambda: self.client.richiesta(f"{SITO}/catalog", "application/json",
                                                                   intestazioni={"UserLang": "it"}).text,
                               lambda p: p.read_text(encoding="utf-8"),
                               lambda t: path.write_text(t, encoding="utf-8"))
        return _leggi_albero(json.loads(testo))

    def _catalogo(self, path: Path, scarica, leggi, salva, giorni: int = 7):
        if _fresco(path, giorni):
            return leggi(path)
        try:
            contenuto = scarica()
        except ErroreFonte as exc:
            if not path.exists():
                raise
            log.warning("%s non aggiornato (%s): uso la copia del %s", path.name, exc,
                        date.fromtimestamp(path.stat().st_mtime))
            return leggi(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        salva(contenuto)
        return contenuto

    # --- Struttura di un dataset (cache 30 giorni) --------------------------------------------
    def struttura(self, flow: str) -> tuple[pd.DataFrame, dict]:
        """Dimensioni e codici con i nomi leggibili, più nome e ultimo aggiornamento del dataset."""
        base = self.cartella / "struttura" / flow
        codici, meta = base.with_suffix(".parquet"), base.with_suffix(".json")
        if _fresco(codici, 30) and meta.exists():
            return pd.read_parquet(codici), json.loads(meta.read_text())
        # timeout corto e un solo tentativo: un dataset che non risponde in un minuto si salta
        try:
            resp = self.client.richiesta(f"{SDMX}/dataflow/IT1/{flow}/{self._versione(flow)}", XML,
                                         params={"references": "all"}, timeout=60, tentativi=1)
        except ErroreFonte:
            if codici.exists() and meta.exists():  # le dimensioni di un dataset cambiano di rado:
                log.info("%s: struttura non aggiornata, uso la copia salvata", flow)  # meglio la vecchia
                return pd.read_parquet(codici), json.loads(meta.read_text())
            raise
        df, info = _leggi_struttura(resp.content)
        codici.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(codici, index=False)
        meta.write_text(json.dumps(info, ensure_ascii=False))
        return df, info

    def serie_in_cache(self, flow: str, filtri: dict | None = None, max_giorni: int = 1) -> bool:
        """C'è già l'elenco delle serie salvato (es. dalla ricognizione)? Allora non serve chiedere a ISTAT."""
        nome = flow if not filtri else f"{flow}~{slug_filtri(filtri)}"
        return _fresco(self.cartella / "serie" / f"{nome}.parquet", max_giorni)

    def serie_disponibili(self, flow: str, filtri: dict | None = None, max_giorni: int = 1) -> pd.DataFrame:
        """Le serie che esistono davvero, con il loro ultimo periodo (cache 1 giorno).

        Una sola richiesta SDMX (lastNObservations=1): una riga per serie, una colonna per
        dimensione (codici) più time_period = ultimo periodo disponibile. Con i filtri (es. la
        frequenza e il territorio) la domanda è molto più piccola: indispensabile per i dataset
        enormi, come quelli per comune.
        """
        nome = flow if not filtri else f"{flow}~{slug_filtri(filtri)}"
        path = self.cartella / "serie" / f"{nome}.parquet"
        if _fresco(path, max_giorni):  # la pipeline accetta l'elenco della ricognizione; la ricognizione no
            return pd.read_parquet(path)
        url = self.url_dati(flow, filtri) if filtri else f"{SDMX}/data/{flow}"
        resp = self.client.richiesta(url, CSV, params={"lastNObservations": 1}, timeout=90, tentativi=1)
        df = _leggi_csv(resp.text)
        codici, _ = self.struttura(flow)
        dims = [d.lower() for d in codici.drop_duplicates("dimension").sort_values("position")["dimension"]]
        df = df[[d for d in dims if d in df.columns] + ["time_period"]].drop_duplicates()
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        return df

    def pulisci(self, giorni: dict[str, int]) -> int:
        """Cancella i file di cache più vecchi della loro scadenza; restituisce quanti ne ha tolti."""
        tolti = 0
        for cartella, scadenza in giorni.items():
            for f in (self.cartella / cartella).rglob("*"):
                if f.is_file() and not _fresco(f, scadenza):
                    f.unlink()
                    tolti += 1
        return tolti

    def _versione(self, flow: str) -> str:
        try:
            cat = self.dataset()
            return cat.loc[cat["id"] == flow, "version"].iloc[0]
        except (ErroreFonte, IndexError):
            return "1.0"

    # --- Dati (un'istantanea al giorno per ogni serie) -----------------------------------------
    def dati(self, flow: str, filtri: dict, inizio: str | None = None) -> pd.DataFrame:
        """Osservazioni di un dataset filtrato per codici: {"FREQ": "A", "REF_AREA": "IT"}.

        Colonne: una per dimensione (codici, minuscolo), time_period, obs_value.
        """
        path = self.cartella / "dati" / flow / slug_filtri(filtri) / f"{date.today().isoformat()}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        try:
            df = self._dati_sdmx(flow, filtri, inizio)
        except NessunDato:
            raise  # la combinazione non esiste: il sito non avrebbe niente di diverso
        except ErroreFonte as exc:
            log.warning("SDMXWS non disponibile (%s): uso il sito ISTAT", exc)
            df = self._dati_sito(flow, filtri, inizio)
        if df.empty:
            raise ErroreFonte(f"Nessun dato per {flow} con filtri {filtri}")
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        return df

    def url_dati(self, flow: str, filtri: dict) -> str:
        """Indirizzo ufficiale SDMX della serie: basta questo per riscaricarla quando serve."""
        codici, _ = self.struttura(flow)
        ordine = codici.drop_duplicates("dimension").sort_values("position")["dimension"].tolist()
        if sconosciute := set(filtri) - set(ordine):
            raise ValueError(f"{flow}: dimensioni inesistenti {sorted(sconosciute)} (esistono: {ordine})")
        return f"{SDMX}/data/{flow}/" + ".".join("+".join(_lista(filtri.get(d, []))) for d in ordine)

    def _dati_sdmx(self, flow: str, filtri: dict, inizio: str | None) -> pd.DataFrame:
        resp = self.client.richiesta(self.url_dati(flow, filtri), CSV,
                                     params={"startPeriod": inizio} if inizio else None)
        return _pulisci(_leggi_csv(resp.text), filtri, inizio)

    def _dati_sito(self, flow: str, filtri: dict, inizio: str | None) -> pd.DataFrame:
        corpo = [{"id": d, "filterValues": _lista(v), "type": "CodeValues", "period": 0} for d, v in filtri.items()]
        resp = self.client.richiesta(f"{SITO}/datasets/IT1,{flow},{self._versione(flow)}/data", "application/json",
                                     corpo=corpo, intestazioni={"UserLang": "it"})
        return _pulisci(_leggi_jsonstat(resp.json()), filtri, inizio)


# --- Lettori dei formati -----------------------------------------------------------------------
def _nomi(el: ET.Element) -> dict:
    return {n.get(LANG): (n.text or "").strip() for n in el.findall("c:Name", NS)}


def _annotazione(el: ET.Element, ident: str) -> str | None:
    for ann in el.iter(f"{{{NS['c']}}}Annotation"):
        if ann.get("id") == ident:
            return ann.findtext("c:AnnotationTitle", namespaces=NS)
    return None


def _leggi_dataflow(xml: bytes) -> pd.DataFrame:
    righe = [{"id": f.get("id"), "version": f.get("version"), "nome": _nomi(f).get("it"),
              "last_update": _annotazione(f, "LAST_UPDATE")}
             for f in ET.fromstring(xml).iter(f"{{{NS['s']}}}Dataflow")]
    df = pd.DataFrame(righe, columns=["id", "version", "nome", "last_update"])
    df["last_update"] = pd.to_datetime(df["last_update"], utc=True, errors="coerce")
    return df


def _leggi_albero(catalogo: dict) -> pd.DataFrame:
    """Dal catalogo del sito: (dataset, titolo, percorso, antenati) per ogni categoria in cui compare."""
    titoli = {k: v.get("title") for k, v in catalogo.get("datasetMap", {}).items()}
    righe = []

    def visita(categorie, antenati, percorso):
        for c in categorie:
            ant, perc = antenati + [c["id"]], percorso + [c["label"]]
            for ident in c.get("datasetIdentifiers", []):
                righe.append({"dataset": ident.split(",")[1], "titolo": titoli.get(ident),
                              "percorso": " > ".join(perc), "antenati": ant})
            visita(c.get("childrenCategories", []), ant, perc)

    for gruppo in catalogo.get("categoryGroups", []):
        visita(gruppo.get("categories", []), [], [gruppo.get("label", "")])
    return pd.DataFrame(righe, columns=["dataset", "titolo", "percorso", "antenati"])


def _leggi_struttura(xml: bytes) -> tuple[pd.DataFrame, dict]:
    root = ET.fromstring(xml)
    flusso = next(root.iter(f"{{{NS['s']}}}Dataflow"))
    meta = {"nome": _nomi(flusso).get("it"), "last_update": _annotazione(flusso, "LAST_UPDATE")}
    concetti = {c.get("id"): _nomi(c) for c in root.iter(f"{{{NS['s']}}}Concept")}
    liste = {cl.get("id"): [(x.get("id"), _nomi(x)) for x in cl.findall("s:Code", NS)]
             for cl in root.iter(f"{{{NS['s']}}}Codelist")}
    dsd = next(root.iter(f"{{{NS['s']}}}DataStructure"))
    righe = []
    for dim in dsd.findall("s:DataStructureComponents/s:DimensionList/s:Dimension", NS):
        concetto, lista = dim.find("s:ConceptIdentity/Ref", NS), dim.find("s:LocalRepresentation/s:Enumeration/Ref", NS)
        nome_dim = concetti.get(concetto.get("id") if concetto is not None else dim.get("id"), {})
        for codice, nomi in liste.get(lista.get("id") if lista is not None else "", []):
            righe.append({"position": int(dim.get("position")), "dimension": dim.get("id"),
                          "dimension_name": nome_dim.get("it") or dim.get("id"),
                          "code": codice, "name": nomi.get("it") or nomi.get("en") or codice})
    return pd.DataFrame(righe), meta


def _leggi_csv(testo: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(testo), dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _leggi_jsonstat(d: dict) -> pd.DataFrame:
    """JSON-stat 2.0 → una colonna per dimensione (codici), time_period, obs_value."""
    dims, dimensioni = d["id"], d["size"]
    codici = []
    for dim in dims:
        idx = d["dimension"][dim]["category"]["index"]
        codici.append(idx if isinstance(idx, list) else sorted(idx, key=idx.get))
    valori = d["value"]
    righe = []
    for pos, val in (valori.items() if isinstance(valori, dict) else enumerate(valori)):
        if val is None:
            continue
        resto, coord = int(pos), []
        for n in reversed(dimensioni):  # indice "piatto": l'ultima dimensione varia più in fretta
            resto, i = divmod(resto, n)
            coord.append(i)
        riga = {dim.lower(): cod[i] for dim, i, cod in zip(dims, reversed(coord), codici)}
        riga["obs_value"] = val
        righe.append(riga)
    return pd.DataFrame(righe)


def _pulisci(df: pd.DataFrame, filtri: dict, inizio: str | None) -> pd.DataFrame:
    """Stesso formato da entrambe le fonti; riapplica filtri e periodo per sicurezza."""
    if df.empty or "time_period" not in df.columns:
        return pd.DataFrame()
    for dim, v in filtri.items():
        if dim.lower() in df.columns:
            df = df[df[dim.lower()].isin(_lista(v))]
    if inizio:
        df = df[df["time_period"].astype(str) >= str(inizio)]
    tieni = [c for c in df.columns if c in {d.lower() for d in filtri} or c in ("time_period", "obs_value", "obs_status")]
    df = df[tieni].copy()
    df["time_period"] = df["time_period"].astype(str)
    df["obs_value"] = pd.to_numeric(df["obs_value"], errors="coerce")
    return df.sort_values("time_period").reset_index(drop=True)


# --- Utilità -----------------------------------------------------------------------------------
def _lista(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def slug_filtri(filtri: dict) -> str:
    """Nome di cartella per un insieme di filtri; se è troppo lungo per il sistema operativo,
    si tiene l'inizio leggibile più un'impronta univoca."""
    testo = ",".join(f"{k}={'+'.join(_lista(v))}" for k, v in sorted(filtri.items()))
    testo = re.sub(r"[^A-Za-z0-9_.,=+-]", "-", testo) or "_tutto"
    if len(testo) > 120:
        testo = f"{testo[:100]}~{hashlib.sha1(testo.encode()).hexdigest()[:12]}"
    return testo


def _fresco(path: Path, giorni: int) -> bool:
    return path.exists() and (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) < giorni * 86400
