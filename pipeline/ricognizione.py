"""Ricognizione: legge una volta sola tutti i dataset del calendario e li mette in memoria.

    python -m pipeline.ricognizione                  # tutti i sottotemi (riprende da dove si era fermata)
    python -m pipeline.ricognizione prezzi nascite   # solo alcuni sottotemi
    python -m pipeline.ricognizione --riepilogo      # solo il quadro di cosa c'è, senza chiedere niente a ISTAT
    python -m pipeline.ricognizione --rifai          # rilegge anche i dataset già in memoria

Per ogni dataset basta una richiesta (più la struttura, in cache per 60 giorni) e si sa quali frequenze ha
e quanto è recente l'ultimo dato. Dopo, `pipeline.run` non perde più tempo a cercare: salta subito i dataset
vecchi o senza la fascia richiesta e va dritto a quelli buoni.

ISTAT accetta poche richieste al minuto, quindi la prima volta ci vogliono ore: lanciala la sera e lasciala
andare. Si può interrompere con Ctrl+C in qualunque momento: quello che ha letto resta in memoria.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from pipeline.calendario import carica_calendario
from pipeline.categorie import candidati
from pipeline.run import STORICO
from pipeline.scegli import (MEMORIA_GIORNI, _eta_memoria, _memoria, _noto_fresco, _ricorda, _segna_lento,
                             _taglia, _timeout, filtri_ricognizione)
from sources.istat import Istat, NessunDato
from sources.rete import FonteNonDisponibile

log = logging.getLogger(__name__)
SECONDI_A_DATASET = 30  # stima prudente: struttura + serie, con le pause chieste da ISTAT
PAUSA_IRRAGGIUNGIBILE = 10 * 60  # se ISTAT non si raggiunge per un po' di dataset di fila: si aspetta e si riprova
PAUSE_MASSIME = 6                 # dopo un'ora di tentativi a vuoto ci si ferma (si riparte da lì rilanciando)


def _gia_letto(m: dict | None, aggiornato=None) -> bool:
    """In memoria con una lettura valida (un timeout isolato non conta) e ancora attuale: fatta da meno di 30
    giorni, oppure dopo l'ultimo aggiornamento del dataset secondo il catalogo ISTAT (niente di nuovo da leggere)."""
    if not m or not (m.get("lento") or "controllate" in m):
        return False
    if _eta_memoria(m) <= MEMORIA_GIORNI:
        return True
    return not m.get("lento") and pd.notna(aggiornato) and pd.Timestamp(aggiornato).date().isoformat() <= m["verificato"]


def _elenco(cal: dict, sottotemi: list[str], istat: Istat) -> pd.DataFrame:
    """Tutti i dataset candidati dei sottotemi scelti, una riga per (sottotema, dataset)."""
    righe = []
    for sott in sottotemi:
        try:
            c = candidati(sott, cal, istat)
        except Exception as exc:  # categoria sparita dal catalogo o simili: si segnala e si va avanti
            log.warning("%s: impossibile elencare i dataset (%s)", sott, exc)
            continue
        righe.append(c.assign(sottotema=sott)[["sottotema", "dataset", "titolo", "last_update"]])
    colonne = ["sottotema", "dataset", "titolo", "last_update"]
    return pd.concat(righe, ignore_index=True) if righe else pd.DataFrame(columns=colonne)


def _leggi(istat: Istat, memoria: dict, flow: str) -> str:
    """Legge un dataset e lo mette in memoria. Restituisce una parola per il log."""
    try:
        codici, _ = istat.struttura(flow)
        serie = istat.serie_disponibili(flow, filtri_ricognizione(codici, None))
    except NessunDato:
        _ricorda(memoria, flow, pd.DataFrame({"time_period": []}))
        return "vuoto"
    except FonteNonDisponibile as exc:
        return f"irraggiungibile ({exc})"
    except Exception as exc:
        if "timed out" in str(exc).lower():
            return "lento (30 giorni)" if _timeout(memoria, flow) else "timeout (riprovo un'altra volta)"
        if isinstance(exc, (KeyError, ValueError)):
            _segna_lento(memoria, flow)
            return "illeggibile (30 giorni)"
        return f"errore ({str(exc)[:60]})"
    _ricorda(memoria, flow, serie)
    m = memoria.get(flow, {})
    trovate = [f"{f} {m[f]}" for f in ("annuale", "trimestrale", "mensile") if f in m]
    return ", ".join(trovate) or "nessun periodo leggibile"


def riepilogo(cal: dict, elenco: pd.DataFrame) -> None:
    """Per sottotema: dataset in tutto, già letti, freschi per fascia, freschi e mai usati."""
    memoria = _memoria()
    usati = {v["flow"] for v in json.loads(STORICO.read_text())} if STORICO.exists() else set()
    fasce = cal["slot"]
    print(f"\n{'sottotema':24} {'dataset':>7} {'letti':>6} " + " ".join(f"{f:>10}" for f in fasce)
          + f" {'nuovi e freschi':>16}")
    totali = {"dataset": 0, "letti": 0, **{f: 0 for f in fasce}, "nuovi": 0}
    for sott, g in elenco.groupby("sottotema", sort=False):
        flows = list(dict.fromkeys(g["dataset"]))
        aggiornati = dict(zip(g["dataset"], g["last_update"]))
        letti = sum(_gia_letto(memoria.get(f), aggiornati.get(f)) for f in flows)
        freschi = {f: {d for d in flows if _noto_fresco(memoria, d, f, cal["freschezza"][f])} for f in fasce}
        nuovi = len(set().union(*freschi.values()) - usati)
        print(f"{sott:24} {len(flows):7} {letti:6} " + " ".join(f"{len(freschi[f]):10}" for f in fasce)
              + f" {nuovi:16}" + ("" if letti == len(flows) else "   ← ricognizione incompleta"))
        totali["dataset"] += len(flows)
        totali["letti"] += letti
        totali["nuovi"] += nuovi
        for f in fasce:
            totali[f] += len(freschi[f])
    print(f"{'TOTALE':24} {totali['dataset']:7} {totali['letti']:6} "
          + " ".join(f"{totali[f]:10}" for f in fasce) + f" {totali['nuovi']:16}")
    print("\n'nuovi e freschi' = dataset con dati freschi in almeno una fascia e mai pubblicati: "
          "ognuno è un post possibile.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Legge una volta tutti i dataset del calendario")
    parser.add_argument("sottotemi", nargs="*", help="solo questi sottotemi (di default tutti)")
    parser.add_argument("--riepilogo", action="store_true", help="solo il quadro, senza richieste a ISTAT")
    parser.add_argument("--rifai", action="store_true", help="rilegge anche i dataset già in memoria")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for rumoroso in ("sources", "pipeline.categorie"):
        logging.getLogger(rumoroso).setLevel(logging.WARNING)

    cal, istat = carica_calendario(), Istat()
    tutti = [s for s, codici in cal["sottotemi"].items() if codici]
    if sconosciuti := set(args.sottotemi) - set(tutti):
        parser.error(f"sottotemi sconosciuti: {sorted(sconosciuti)} (esistono: {', '.join(tutti)})")
    elenco = _elenco(cal, args.sottotemi or tutti, istat)
    if args.riepilogo:
        riepilogo(cal, elenco)
        return

    memoria = _memoria()
    # a turno: il 1° dataset di ogni sottotema, poi il 2° di ognuno... Se ISTAT si ferma a metà, tutti i
    # sottotemi (e quindi tutti i giorni della settimana) hanno già qualcosa in memoria
    a_turno = elenco.assign(_n=elenco.groupby("sottotema").cumcount()).sort_values("_n", kind="stable")
    flows = list(dict.fromkeys(a_turno["dataset"]))  # un dataset può stare in più sottotemi: si legge una volta
    titoli = dict(zip(elenco["dataset"], elenco["titolo"].fillna("")))
    aggiornati = dict(zip(elenco["dataset"], elenco["last_update"]))
    da_leggere = [f for f in flows if args.rifai or not _gia_letto(memoria.get(f), aggiornati.get(f))]
    stima = timedelta(seconds=len(da_leggere) * SECONDI_A_DATASET)
    log.info("%s dataset nel calendario, %s già in memoria, %s da leggere (circa %s, fine verso le %s)",
             len(flows), len(flows) - len(da_leggere), len(da_leggere), str(stima).split(".")[0],
             (datetime.now() + stima).strftime("%H:%M"))
    log.info("Puoi interrompere con Ctrl+C quando vuoi: quello che è stato letto resta in memoria.")

    inizio, lenti, persi, pause = time.monotonic(), 0, 0, 0
    try:
        n = 0
        while n < len(da_leggere):
            flow = da_leggere[n]
            esito = _leggi(istat, memoria, flow)
            if esito.startswith("irraggiungibile"):
                persi += 1
                log.warning("[%s/%s] %s → %s", n + 1, len(da_leggere), flow, esito)
                if persi >= 3:  # tre dataset di fila senza connessione: rete giù o ISTAT fermo
                    pause += 1
                    if pause > PAUSE_MASSIME:
                        log.warning("ISTAT non si raggiunge da più di un'ora: mi fermo. Rilancia più tardi, "
                                    "riparto da qui.")
                        break
                    log.warning("ISTAT non si raggiunge: aspetto %s minuti e riprovo (pausa %s di %s)",
                                PAUSA_IRRAGGIUNGIBILE // 60, pause, PAUSE_MASSIME)
                    time.sleep(PAUSA_IRRAGGIUNGIBILE)
                    n = max(n - 3, -1)  # si riprovano i tre dataset appena persi
                    persi = 0
                n += 1
                continue
            persi = pause = 0
            n += 1
            log.info("[%s/%s] %s · %s → %s", n, len(da_leggere), flow, _taglia(titoli.get(flow, ""), 50), esito)
            lenti = lenti + 1 if esito.startswith(("timeout", "lento")) else 0
            if lenti >= 3:  # è ISTAT a essere lento, non i dataset
                log.info("ISTAT sembra sovraccarico: pausa di 2 minuti")
                time.sleep(120)
                lenti = 0
            if n % 25 == 0:
                rimasti = (time.monotonic() - inizio) / n * (len(da_leggere) - n)
                log.info("── %s letti, ne mancano %s (circa %s) ──", n, len(da_leggere) - n,
                         str(timedelta(seconds=int(rimasti))))
    except KeyboardInterrupt:
        log.info("Interrotta: quello che è stato letto resta in memoria. Rilancia per continuare.")
    riepilogo(cal, elenco)


if __name__ == "__main__":
    main()
