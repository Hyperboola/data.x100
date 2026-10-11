"""La giornata di Data%: calendario → serie → storia → slide.

    python -m pipeline.run                          # i post di oggi
    python -m pipeline.run --settimana              # i post della prossima settimana, da lunedì a domenica
    python -m pipeline.run --settimana --settimane 2  # le prossime due settimane
    python -m pipeline.run --settimana --dal 2026-10-12   # 7 giorni da una data precisa
    python -m pipeline.run --settimana --rifai      # rifà anche i giorni già preparati
    python -m pipeline.run --giorno lunedi          # come se fosse lunedì
    python -m pipeline.run --serie nati_italia      # una serie preferita (prova, non va nello storico)
    python -m pipeline.run --serie X --forza        # anche se il dato non è fresco

Ogni giorno ha un tema; i sottotemi ruotano (tocca a quello usato meno di recente).
Le fasce (annuale, trimestrale, mensile) si alternano. Per ogni post:
  - con usa_claude = false (calendario.toml): dataset più recente, serie scelta con regole,
    grafico da [racconto], testi a modello da scrivere in redazione;
  - con usa_claude = true: Claude sceglie dataset e serie e scrive la storia;
  - nei giorni con preferiti = true (il sabato) si usano le serie di preferiti.toml.
Ogni dataset si usa una volta; una serie già uscita torna solo con un dato nuovo o dopo la pausa.

Tutto ciò che esce viene salvato in storico.json (versionato: è la memoria della pagina).
Risultato in output/<giorno di pubblicazione>/: per ogni post tre JPEG, didascalia, rapporto e
dati, più riepilogo.md. Poi: python -m pipeline.redazione per rivedere, scrivere e approvare.
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import re
import shutil
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

from config import ROOT
from pipeline.calendario import GIORNI, carica_calendario, carica_preferiti
from pipeline.claude import riepilogo_consumi
from pipeline.disegna import OUTPUT_DIR, render
from pipeline.periodi import Periodo
from pipeline.racconta import costruisci_carosello
from pipeline.scegli import (DatasetEsauriti, NessunaSerie, SerieNonValida, fascia_esaurita, pulisci_nome, scegli,
                             scegli_senza_claude)
from sources.istat import Istat
from sources.rete import FonteNonDisponibile

STORICO = ROOT / "storico.json"
# Scadenze della cache, in giorni. I cataloghi si sovrascrivono da soli e non servono qui.
SCADENZE_CACHE = {"dati": 7, "serie": 2, "struttura": 60}
SCADENZA_OUTPUT = 30
TENTATIVI_ESPLORAZIONE = 4   # dataset provati per ogni giro
MINUTI_PER_FASCIA = 10       # oltre, la fascia si salta: meglio un post in meno che un job infinito

log = logging.getLogger(__name__)
QUANDO = date.today()  # giorno di pubblicazione dei post che si stanno preparando (cambia con --settimana)
_claude_finito = False  # limite d'uso raggiunto: per oggi niente più esplorazione
_istat_giu = False      # ISTAT non si raggiunge: per oggi inutile riprovare


class NonPubblicabile(Exception):
    """Oggi questa serie non va pubblicata (dato vecchio, in pausa, sarebbe una ripetizione)."""


# --- Storico -------------------------------------------------------------------------------------
def _storico() -> list[dict]:
    return json.loads(STORICO.read_text()) if STORICO.exists() else []


def _registra(voce: dict) -> None:
    STORICO.parent.mkdir(parents=True, exist_ok=True)
    STORICO.write_text(json.dumps(_storico() + [voce], ensure_ascii=False, indent=2))


def _ultima(chiave: str, valore: str, storico: list[dict]) -> dict | None:
    voci = [v for v in storico if v.get(chiave) == valore]
    return max(voci, key=lambda v: v["data"]) if voci else None


# --- Una serie → un carosello -------------------------------------------------------------------
def scarica(s: dict, cal: dict, forza: bool = False):
    """Dati della serie (o delle serie, per confronti e serie multiple), con i controlli di base.

    Restituisce (df, nomi): df con time_period, obs_value e, se c'è una dimensione variabile,
    la colonna categoria; nomi traduce i codici di categoria in nomi leggibili.
    """
    finestra = cal["finestra"][s["frequenza"]]
    anni = {"annuale": finestra + 2, "trimestrale": finestra // 4 + 2, "mensile": 3}[s["frequenza"]]
    istat = Istat()
    df = istat.dati(s["flow"], s["filtri"], inizio=str(date.today().year - anni))
    variabile = s.get("variabile")
    nomi = {}
    if variabile:
        df = df.rename(columns={variabile.lower(): "categoria"})
        try:
            codici, _ = istat.struttura(s["flow"])
            nomi = dict(codici.loc[codici["dimension"] == variabile, ["code", "name"]].values)
        except Exception:  # senza struttura si usano i codici
            pass
        nomi = {c: pulisci_nome(n) for c, n in nomi.items()}  # niente nomi bilingui o "Provincia Autonoma"
        nomi.update(s.get("nomi") or {})  # i nomi naturali scelti da Claude (o scritti nei preferiti)
    elif df["time_period"].duplicated().any():
        raise NonPubblicabile("i filtri non individuano una sola serie")
    if s.get("decimali") is None:  # serie scelte senza Claude: decimali dedotti dai valori
        valori = df["obs_value"].dropna().abs()
        massimo = 0 if (valori % 1 == 0).all() or valori.abs().max() >= 100 else (1 if valori.abs().max() >= 10 else 2)
        # mai più decimali di quelli che ISTAT pubblica: 9,6 resta 9,6 e non diventa 9,60
        osservati = max(len(f"{v:.6f}".rstrip("0").split(".")[1]) for v in valori)
        s["decimali"] = min(massimo, osservati)
    ultimo = Periodo.da_codice(df["time_period"].max())
    if ultimo.frequenza != s["frequenza"]:
        raise NonPubblicabile(f"dati {ultimo.frequenza}, serviva {s['frequenza']}")
    eta, soglia = ultimo.eta_mesi(), cal["freschezza"][s["frequenza"]]
    if eta > soglia and not forza:
        raise NonPubblicabile(f"ultimo dato {ultimo.nome}, {eta} mesi fa (massimo {soglia})")
    return df, nomi


def varieta(storico: list[dict], chiave: str, n: int = 10) -> str:
    """Promemoria per Claude: cosa è uscito negli ultimi post, per non ripetersi."""
    usati = [v[chiave] for v in storico[-n:] if v.get(chiave)]
    if not usati:
        return ""
    conteggi = ", ".join(f"{k} {usati.count(k)}" for k in dict.fromkeys(usati))
    return (f"Negli ultimi {len(usati)} post: {conteggi}. A parità di efficacia, preferisci una scelta usata "
            "meno di recente.")


def esegui(s: dict, cal: dict, usa_claude: bool = True, forza: bool = False,
           storico: list[dict] | None = None) -> tuple[list[Path], dict]:
    df, nomi = scarica(s, cal, forza)
    precedente = None
    if storico is not None and (ultima := _ultima("serie", s["id"], storico)):
        nuovo = df["time_period"].max() > ultima["periodo"]
        giorni = (QUANDO - date.fromisoformat(ultima["data"])).days
        if not nuovo and giorni < cal["pausa_settimane"] * 7:
            raise NonPubblicabile(f"pubblicata {giorni} giorni fa, nessun dato nuovo")
        precedente = None if nuovo else ultima  # stessi dati: serve una storia diversa

    slides, testo_ig, rapporto = costruisci_carosello(s, df, cal["finestra"][s["frequenza"]], nomi=nomi,
                                                      usa_claude=usa_claude, precedente=precedente,
                                                      varieta=varieta(storico or [], "grafico"),
                                                      revisione=cal.get("revisione", True))
    if precedente and all(o == "modello" for o in rapporto["origine"].values()):
        raise NonPubblicabile("senza i testi di Claude sarebbe uguale all'ultima volta")

    out = OUTPUT_DIR / QUANDO.isoformat()
    nome = _nome_file(s, out)
    paths = render(slides, nome=nome, out_dir=out)
    (out / f"{nome}_didascalia.txt").write_text(testo_ig, encoding="utf-8")
    # i dati esatti del post: la redazione rigenera da questi, senza chiedere niente a ISTAT
    (out / f"{nome}_dati.json").write_text(json.dumps({"righe": df.to_dict("records"), "nomi": nomi},
                                                      ensure_ascii=False, default=str), encoding="utf-8")
    (out / f"{nome}_rapporto.json").write_text(json.dumps({"serie": s, **rapporto}, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    scelte = rapporto["scelte"]
    log.info("%s: storia %s di %s, grafico %s, copertina %s", s["id"], rapporto["tipo_storia"],
             "Claude" if "claude" in rapporto["origine"].values() else "modello", scelte["grafico"],
             scelte["copertina"])
    try:
        url = Istat().url_dati(s["flow"], s["filtri"])  # il puntatore: da qui si riscaricano i dati
    except Exception:
        url = None
    voce = {"data": QUANDO.isoformat(), "serie": s["id"], "flow": s["flow"], "filtri": s["filtri"],
            "url": url, "sottotema": s["sottotema"], "slot": s["frequenza"], "tipo_storia": rapporto["tipo_storia"],
            "grafico": scelte["grafico"], "periodo": df["time_period"].max(),
            "ultimo_valore": rapporto.get("ultimo_valore"), "copertina": scelte["copertina"],
            "numero": rapporto["menu"][scelte["copertina"]]["copertina"],
            "domanda": rapporto["testi"]["domanda"], "titolo": rapporto["testi"]["titolo_grafico"],
            "da_controllare": rapporto.get("da_controllare") or []}
    if voce["da_controllare"]:
        log.warning("⚠ %s da controllare prima di pubblicare: %s", nome, "; ".join(voce["da_controllare"]))
    return paths + [out / f"{nome}_didascalia.txt", out / f"{nome}_rapporto.json"], voce


def _nome_file(s: dict, cartella: Path) -> str:
    """Nome parlante: fascia_sottotema_argomento, es. annuale_nascite_eta-media-delle-madri."""
    argomento = unicodedata.normalize("NFKD", s["soggetto"]).encode("ascii", "ignore").decode().lower()
    parole, argomento = re.sub(r"[^a-z0-9]+", " ", argomento).split(), ""
    for parola in parole:  # taglia a parola intera, entro 40 caratteri
        if len(argomento) + len(parola) + 1 > 40:
            break
        argomento = f"{argomento}-{parola}" if argomento else parola
    nome = f"{s['frequenza']}_{s['sottotema']}_{argomento}"
    n = 2
    while (cartella / f"{nome}_1.jpg").exists():  # due post sullo stesso argomento nello stesso giorno
        nome = f"{s['frequenza']}_{s['sottotema']}_{argomento}_{n}"
        n += 1
    return nome


# --- La giornata -----------------------------------------------------------------------------------
def pulizia() -> None:
    """All'avvio: via la cache scaduta e i caroselli vecchi. Lo storico non si tocca mai."""
    tolti = Istat().pulisci(SCADENZE_CACHE)
    for cartella in OUTPUT_DIR.glob("????-??-??"):
        if (date.today() - date.fromisoformat(cartella.name)).days > SCADENZA_OUTPUT:
            shutil.rmtree(cartella)
            tolti += 1
    if tolti:
        log.info("Pulizia: tolti %s file o cartelle scaduti", tolti)


def giornata(cal: dict, preferiti: list[dict], giorno: str, usa_claude: bool = True) -> list[Path]:
    pulizia()
    g = cal["giorni"][giorno]
    if not g["tema"]:
        log.info("%s: giorno di pausa", giorno)
        return []
    storico = _storico()
    uso = {}
    for v in storico:
        for chiave in (v["serie"], v["sottotema"], f"fascia:{v['slot']}"):
            uso[chiave] = max(uso.get(chiave, ""), v["data"])
    # Le fasce si alternano: prima quella pubblicata meno di recente
    fasce = sorted(cal["slot"], key=lambda f: (uso.get(f"fascia:{f}", ""), cal["slot"].index(f)))
    quanti = cal.get("post_al_giorno", len(fasce))
    log.info("%s · %s · %s post, fasce in ordine: %s", giorno, g["tema"], quanti, ", ".join(fasce))

    creati, pubblicati = [], []
    if g.get("preferiti"):
        for slot in fasce:
            if len(pubblicati) >= quanti:
                break
            serie = sorted((s for s in preferiti if s["frequenza"] == slot), key=lambda s: uso.get(s["id"], ""))
            if not any(_pubblica(s, cal, usa_claude, storico, creati, pubblicati) for s in serie):
                log.info("%s: nessun preferito pubblicabile per la fascia %s", g["tema"], slot)
    else:
        sottotemi = [s for s, cod in g.get("sottotemi", {}).items() if cod]
        ordine = sorted(sottotemi, key=lambda s: (uso.get(s, ""), sottotemi.index(s)))
        log.info("Sottotema del giorno: %s", ordine[0] if ordine else "-")
        # Prima il sottotema a cui tocca, in ogni fascia (la meno recente per prima); solo se non ha
        # niente di pubblicabile si passa al sottotema successivo. Ogni post: sottotema e fascia diversi.
        usate = set()
        for sott in ordine:
            if len(pubblicati) >= quanti:
                break
            rimaste = [f for f in fasce if f not in usate]
            for slot in rimaste:
                # la fascia preferita ha un solo giro (8 dataset): se lì non c'è, si passa subito alla
                # successiva, che riusa quello che si è appena letto. L'ultima fascia ha tutti i giri.
                giri = TENTATIVI_ESPLORAZIONE if slot == rimaste[-1] else 1
                if _esplora(sott, slot, cal, storico, creati, pubblicati, usa_claude, giri=giri):
                    usate.add(slot)
                    break
            else:
                log.info("%s: niente di pubblicabile in nessuna fascia, provo il sottotema successivo", sott)
    if len(pubblicati) < quanti:
        log.warning("%s: pubblicati %s post su %s", g["tema"], len(pubblicati), quanti)
    log.info("Consumo di oggi: %s", riepilogo_consumi())
    if pubblicati:
        creati.append(_riepilogo(giorno, g["tema"], pubblicati))
    return creati


def _pubblica(s: dict, cal: dict, usa_claude: bool, storico: list[dict], creati: list, pubblicati: list) -> bool:
    try:
        paths, voce = esegui(s, cal, usa_claude, storico=storico)
    except NonPubblicabile as exc:
        log.info("%s/%s: salto %s, %s", s["sottotema"], s["frequenza"], s["id"], exc)
        return False
    except Exception as exc:  # fonte giù, filtri sbagliati...: si prova la prossima
        log.warning("%s/%s: %s non riuscita: %s", s["sottotema"], s["frequenza"], s["id"], exc)
        return False
    _registra(voce)
    storico.append(voce)
    creati += paths
    pubblicati.append((voce, paths))
    return True


def _esplora(sott: str, slot: str, cal: dict, storico: list[dict], creati: list, pubblicati: list,
             usa_claude: bool = True, giri: int = TENTATIVI_ESPLORAZIONE) -> bool:
    """Prima i dataset mai usati; solo se il sottotema li ha esauriti, quelli usati fuori dalla pausa."""
    global _claude_finito, _istat_giu
    if _claude_finito or _istat_giu:
        return False
    try:  # se la memoria dice già che qui non c'è niente di questa fascia, inutile cercare
        if n := fascia_esaurita(sott, slot, cal):
            log.info("%s/%s: nessuno dei %s dataset già controllati ha dati %s freschi, salto subito la fascia",
                     sott, slot, n, slot)
            return False
    except Exception as exc:  # catalogo non disponibile: si cerca come sempre
        log.debug("controllo fascia non riuscito: %s", exc)
    pausa = cal["pausa_settimane"] * 7
    mai = {v["flow"] for v in storico}
    recenti = {v["flow"] for v in storico if abs((QUANDO - date.fromisoformat(v["data"])).days) < pausa}
    falliti, inizio = set(), time.monotonic()  # dataset che oggi non vanno: non si riprovano
    for esclusi, giro in ((mai, "dataset mai usati"), (recenti, "dataset già usati, fuori pausa")):
        provati = esclusi | falliti
        for _ in range(giri):
            if time.monotonic() - inizio > MINUTI_PER_FASCIA * 60:
                log.warning("%s/%s: superati %s minuti, salto la fascia", sott, slot, MINUTI_PER_FASCIA)
                return False
            try:
                trova = scegli if usa_claude else scegli_senza_claude
                s = trova(sott, slot, cal, esclusi=provati, varieta=varieta(storico, "tipo_storia"))
            except DatasetEsauriti:
                break  # tutti usati: si passa al secondo giro
            except SerieNonValida as exc:  # quel dataset non va: se ne prova un altro
                log.info("%s/%s: %s", sott, slot, exc)
                provati |= exc.flows
                falliti |= exc.flows
                continue
            except NessunaSerie as exc:  # es. risposta di Claude fuori elenco: si riprova
                log.info("%s/%s: %s", sott, slot, exc)
                continue
            except FonteNonDisponibile as exc:  # ISTAT giù o irraggiungibile: stop per oggi, senza martellare
                _istat_giu = True
                log.warning("ISTAT non raggiungibile (%s): per oggi mi fermo. Riprova tra qualche ora; "
                            "se il sito si apre nel browser, prova con un'altra rete.", str(exc)[:160])
                return False
            except Exception as exc:  # Claude o ISTAT non disponibili: inutile insistere oggi
                if "usage" in str(exc).lower() or "limit" in str(exc).lower():
                    _claude_finito = True
                    log.warning("Claude ha raggiunto il limite d'uso (%s): per oggi stop all'esplorazione", exc)
                else:
                    log.warning("%s/%s: esplorazione non riuscita: %s", sott, slot, exc)
                return False
            log.info("%s/%s: %s", sott, slot, giro)
            provati.add(s["flow"])
            if _pubblica(s, cal, usa_claude, storico, creati, pubblicati):
                return True
            falliti.add(s["flow"])
        else:
            return False  # tentativi finiti senza aver esaurito i dataset nuovi: niente secondo giro
    return False


def _riepilogo(giorno: str, tema: str, pubblicati: list) -> Path:
    """output/AAAA-MM-GG/riepilogo.md: i post del giorno con didascalie, pronti da pubblicare."""
    righe = [f"# {QUANDO.isoformat()} · {giorno} · {tema}\n", f"_{riepilogo_consumi()}_\n"]
    for voce, paths in pubblicati:
        slide = ", ".join(p.name for p in paths if p.suffix == ".jpg" and "_reel_" not in p.name)
        reel = ", ".join(p.name for p in paths if p.suffix == ".jpg" and "_reel_" in p.name)
        didascalia = next(p for p in paths if p.name.endswith("_didascalia.txt")).read_text(encoding="utf-8")
        avvisi = [f"> ⚠ **Da controllare:** {a}" for a in voce.get("da_controllare") or []]
        righe += [f"## {voce['slot']} · {voce['sottotema']}", *avvisi, f"**{voce['domanda']}** ({voce['numero']})",
                  f"Slide: {slide}", f"Reel: {reel}", "", "```", didascalia, "```", ""]
    path = paths[0].parent / "riepilogo.md"
    path.write_text("\n".join(righe), encoding="utf-8")
    return path


def prossimo_lunedi(oggi: date | None = None) -> date:
    """Il lunedì della prossima settimana: da sabato o domenica è dopodomani o domani,
    da lunedì è il lunedì dopo (la settimana in corso si considera già preparata)."""
    oggi = oggi or date.today()
    return oggi + timedelta(days=7 - oggi.weekday())


def gia_preparato(giorno: date) -> bool:
    """Ci sono già post per quel giorno di pubblicazione?"""
    return any((OUTPUT_DIR / giorno.isoformat()).glob("*_rapporto.json"))


def _durata(secondi: float) -> str:
    """3725 -> '1 h 2 min 5 s'."""
    s = int(secondi)
    parti = [(s // 3600, "h"), (s % 3600 // 60, "min"), (s % 60, "s")]
    return " ".join(f"{n} {u}" for n, u in parti if n) or "0 s"


def _timer() -> None:
    """Segna inizio e fine della pipeline; la fine si scrive anche se la interrompi con Ctrl+C."""
    avvio, t0 = datetime.now(), time.monotonic()
    log.info("▶ Inizio pipeline: %s", avvio.strftime("%d/%m/%Y %H:%M:%S"))
    atexit.register(lambda: log.info("■ Fine pipeline: %s · durata %s", datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                                     _durata(time.monotonic() - t0)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera i caroselli di Data%")
    parser.add_argument("--giorno", choices=GIORNI, help="esegui come se fosse questo giorno")
    parser.add_argument("--serie", help="genera una serie di preferiti.toml, senza registrarla")
    parser.add_argument("--forza", action="store_true", help="con --serie: ignora la freschezza")
    parser.add_argument("--senza-claude", action="store_true", help="solo frasi a modello, anche se il calendario "
                        "dice usa_claude = true")
    parser.add_argument("--settimana", action="store_true", help="prepara i post della prossima settimana, da lunedì "
                        "a domenica (o 7 giorni da --dal), ognuno nella cartella del suo giorno di pubblicazione")
    parser.add_argument("--settimane", type=int, default=1, help="con --settimana: quante settimane preparare")
    parser.add_argument("--dal", help="con --settimana: primo giorno, AAAA-MM-GG")
    parser.add_argument("--rifai", action="store_true", help="con --settimana: rifà anche i giorni già preparati "
                        "(di solito si saltano, per non avere post doppi)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _timer()

    global QUANDO
    cal = carica_calendario()
    preferiti = carica_preferiti(cal)
    usa_claude = cal["usa_claude"] and not args.senza_claude
    if args.serie:
        trovate = [s for s in preferiti if s["id"] == args.serie]
        if not trovate:
            raise SystemExit(f"Serie '{args.serie}' non in preferiti.toml: {[s['id'] for s in preferiti]}")
        try:
            creati, _ = esegui(trovate[0], cal, usa_claude, args.forza)
        except NonPubblicabile as exc:
            raise SystemExit(f"Non pubblicabile: {exc}. Usa --forza per generarla comunque.")
    elif args.settimana:
        inizio = date.fromisoformat(args.dal) if args.dal else prossimo_lunedi()
        creati = []
        log.info("ISTAT accetta poche richieste al minuto: la prima volta ci vuole tempo (anche decine di minuti "
                 "a giorno). Puoi interrompere con Ctrl+C e rilanciare: i giorni già pronti si saltano e ciò che "
                 "si è scoperto sui dataset resta in memoria, quindi i giri successivi sono molto più veloci.")
        for i in range(7 * max(1, args.settimane)):
            QUANDO = inizio + timedelta(days=i)
            if not args.rifai and gia_preparato(QUANDO):
                log.info("──── %s ──── già preparato, salto (usa --rifai per rifarlo)", QUANDO.isoformat())
                continue
            log.info("──── %s ────", QUANDO.isoformat())
            t_giorno = time.monotonic()
            creati += giornata(cal, preferiti, GIORNI[QUANDO.weekday()], usa_claude)
            log.info("⏱ %s fatto in %s", QUANDO.isoformat(), _durata(time.monotonic() - t_giorno))
    else:
        creati = giornata(cal, preferiti, args.giorno or GIORNI[date.today().weekday()], usa_claude)
    for path in creati:
        print(f"Creato {path}")


if __name__ == "__main__":
    main()
